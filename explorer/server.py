#!/usr/bin/env python3
"""
Local web explorer for the IGDB dump.

Reads data/igdb.duckdb (build it with `python load_duckdb.py`) and serves a small
browser UI: a cover grid with search and filters, a page per game, and a stats
page with a few charts. Everything runs on your machine; the only outside
requests are your browser loading cover art from images.igdb.com.

It is a standard WSGI app (see `App` below and passenger_wsgi.py), so it can also run behind
a web server under a path such as /games. See DEPLOY.md.

Usage (from the repo folder):
  python explorer/server.py                # opens http://127.0.0.1:8000
  python explorer/server.py --port 9000 --no-open
  python explorer/server.py --db path/to/igdb.duckdb

The database is opened read-only, so it is safe to run alongside other readers
but not while load_duckdb.py is rebuilding it.
"""

import argparse
import json
import math
import os
import re
import sys
import threading
import time
import webbrowser
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import duckdb

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE.parent / "data" / "igdb.duckdb"
PAGE_SIZE = 48

# Whitelisted sort orders: the UI sends a key, never raw SQL.
SORTS = {
    "popular": "g.total_rating_count DESC NULLS LAST, g.id",
    "rating": "g.total_rating DESC NULLS LAST, g.id",
    "newest": "g.first_release_date DESC NULLS LAST, g.id",
    "oldest": "g.first_release_date ASC NULLS LAST, g.id",
    "name": "g.name ASC NULLS LAST, g.id",
    "shortest": "{ttb} ASC NULLS LAST, g.id",
    "longest": "{ttb} DESC NULLS LAST, g.id",
}

# Time to beat (IGDB's game_time_to_beats; seconds). Figures built on fewer reports than this are
# too noisy to sort or chart by.
MIN_REPORTS = 3
MIN_GENRE_GAMES, MIN_YEAR_GAMES = 30, 20
# One row per game (the one with the most reports), joined as `tb`.
TTB_SUB = ('(SELECT game_id, normally, hastily, completely, "count" AS reports FROM ('
           'SELECT *, row_number() OVER (PARTITION BY game_id ORDER BY "count" DESC NULLS LAST, id) AS rn '
           'FROM game_time_to_beats) WHERE rn = 1)')
TTB10 = "CAST(round(tb.normally / 360.0) AS INTEGER)"     # normal playthrough in tenths of an hour

# The default "scope" is games that carry an ESRB age rating (E, E10+, T or M). Everything else (unrated
# games, shovelware, adult titles, ESRB 'Adults Only' and 'Rating Pending') is the opt-in "wild west".
# The ids of the in-scope games live in an in-memory table: the database file itself is read-only, and
# unlike a TEMP table an ordinary memory table is visible from every request's cursor.
RATED = "memory.main.rated_games"


def clean(v):
    """Make a value JSON-safe (no NaN/inf, no odd types)."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def text(v):
    """Text field or None. A column that is empty everywhere is typed JSON by DuckDB and reads 'null'."""
    return None if v in (None, "", "null") else v


def as_int_list(v):
    return [int(x) for x in v] if isinstance(v, (list, tuple)) else []


class Store:
    def __init__(self, path):
        # Optional limits for small servers, e.g. DUCKDB_MEMORY_LIMIT=512MB DUCKDB_THREADS=1
        config = {k: v for k, v in (("memory_limit", os.environ.get("DUCKDB_MEMORY_LIMIT")),
                                    ("threads", os.environ.get("DUCKDB_THREADS"))) if v}
        # An in-memory connection with the file ATTACHed read-only (rather than connecting to the
        # file directly): DuckDB reuses an already-open database for the same path, which would keep
        # serving the OLD file after a rebuild swaps in a new one.
        self.con = duckdb.connect(":memory:", config=config)
        self.con.execute("ATTACH '" + str(path).replace("'", "''") + "' AS igdb (READ_ONLY)")
        self.con.execute("USE igdb")
        self.tables = {r[0] for r in self.con.execute("SHOW TABLES").fetchall()}
        if "games" not in self.tables:
            sys.exit(f"{path} has no 'games' table. Did the download include it?")
        self.game_cols = {r[0] for r in self.con.execute("DESCRIBE games").fetchall()}
        self.has_ttb, self.ttb_rows = False, 0
        if "game_time_to_beats" in self.tables:
            cols = {r[0] for r in self.con.execute("DESCRIBE game_time_to_beats").fetchall()}
            self.has_ttb = {"game_id", "normally", "count"} <= cols
            if self.has_ttb:
                self.ttb_rows = self.con.execute("SELECT count(*) FROM game_time_to_beats").fetchone()[0]
        self._cache = {}
        self._lock = threading.Lock()
        self.scope_ok, self.rated_n, self.all_n, self.scope_note = False, 0, 0, ""
        try:
            self._build_scope()
        except Exception as e:      # a surprise in the ratings tables must never take the explorer down
            self.scope_ok = False
            self.scope_note = f"could not read the age ratings ({str(e)[:120]}); every game is shown"
        if self.scope_note:
            print("Note:", self.scope_note, file=sys.stderr)

    def _build_scope(self):
        """The default scope: games with an ESRB age rating of EC, E, E10+, T or M.

        Read straight from IGDB's age-rating tables: `age_rating_organizations` says which id is ESRB,
        `age_rating_categories` lists that organization's ratings (RP and AO are left out: Rating Pending
        isn't a rating, and Adults Only is exactly what the default scope is meant to keep out), and each
        game's `age_ratings` points at `age_ratings` rows that carry a `rating_category`."""
        self.all_n = self.con.execute("SELECT count(*) FROM games WHERE name IS NOT NULL").fetchone()[0]
        needed = ("age_ratings", "age_rating_organizations", "age_rating_categories")
        if "age_ratings" not in self.game_cols or any(t not in self.tables for t in needed):
            self.scope_note = "no age-rating data found; every game is shown"
            return
        esrb = [r[0] for r in self.con.execute(
            "SELECT id FROM age_rating_organizations WHERE upper(trim(name)) = 'ESRB'").fetchall()]
        if not esrb:
            self.scope_note = "ESRB is not among the age-rating organizations; every game is shown"
            return
        org = int(esrb[0])
        cats = [int(r[0]) for r in self.con.execute(
            "SELECT id FROM age_rating_categories WHERE organization = ? AND rating IN ('EC', 'E', 'E10+', 'T', 'M')",
            [org]).fetchall()]
        if not cats:
            self.scope_note = "no ESRB rating categories found; every game is shown"
            return
        self.con.execute(f"DROP TABLE IF EXISTS {RATED}")
        self.con.execute(
            f"CREATE TABLE {RATED} AS SELECT DISTINCT g.id FROM "
            f"(SELECT id, unnest(age_ratings) AS arid FROM games WHERE age_ratings IS NOT NULL AND name IS NOT NULL) g "
            f"JOIN age_ratings ar ON ar.id = g.arid "
            f"WHERE ar.organization = {org} AND ar.rating_category IN ({','.join(map(str, cats))})")
        self.rated_n = self.con.execute(f"SELECT count(*) FROM {RATED}").fetchone()[0]
        self.scope_ok = self.rated_n > 0
        if not self.scope_ok:
            self.scope_note = "no games have an ESRB rating in this data; every game is shown"

    def scope_of(self, value):
        """'rated' (the default) or 'all'. Without usable ratings everything is 'all'."""
        return "all" if (value == "all" or not self.scope_ok) else "rated"

    def scope_sql(self, scope):
        return f"g.id IN (SELECT id FROM {RATED})" if scope == "rated" and self.scope_ok else ""

    def scope_info(self, scope):
        return {"available": self.scope_ok, "scope": scope, "rated": self.rated_n, "all": self.all_n}

    # -- helpers -----------------------------------------------------------
    def rows(self, sql, params=()):
        cur = self.con.cursor()  # one cursor per call keeps threads independent
        cur.execute("USE igdb")   # the default catalog is per connection
        res = cur.execute(sql, list(params))
        names = [d[0] for d in res.description]
        return [dict(zip(names, r)) for r in res.fetchall()]

    def col(self, name, cast="DOUBLE"):
        """A games column, or NULL if this dump doesn't have it."""
        return f"g.{name}" if name in self.game_cols else f"NULL::{cast}"

    def cover_join(self):
        if "covers" in self.tables:
            return "LEFT JOIN covers cv ON cv.id = g.cover", "cv.image_id"
        return "", "NULL"

    def ttb_join(self):
        return f"LEFT JOIN {TTB_SUB} tb ON tb.game_id = g.id" if self.has_ttb else ""

    def ttb10(self):
        return TTB10 if self.has_ttb else "NULL::INTEGER"

    def reports(self):
        return "tb.reports" if self.has_ttb else "NULL::BIGINT"

    def pairs(self, table, ids):
        """[(id, name), ...] for the ids that have a name, in the original order."""
        ids = as_int_list(ids)
        if not ids or table not in self.tables:
            return []
        marks = ",".join("?" * len(ids))
        found = {r["id"]: r["name"] for r in
                 self.rows(f"SELECT id, name FROM {table} WHERE id IN ({marks})", ids)}
        return [(i, found[i]) for i in ids if i in found]

    def names(self, table, ids):
        return [name for _, name in self.pairs(table, ids)]

    YEAR = "CAST(date_part('year', make_timestamp(CAST(CAST(g.first_release_date AS BIGINT) * 1000000 AS BIGINT))) AS INTEGER)"

    def game_card(self, r):
        return {
            "id": r["id"], "name": r["name"], "year": clean(r["year"]),
            "rating": clean(r["rating"]), "votes": clean(r["votes"]), "cover": r["cover"],
            "ttb": None if r["ttb10"] is None else r["ttb10"] / 10,
        }

    def card_select(self):
        join, cover = self.cover_join()
        sql = (f"SELECT g.id, g.name, {self.YEAR} AS year, {self.col('total_rating')} AS rating, "
               f"{self.col('total_rating_count', 'BIGINT')} AS votes, {cover} AS cover, "
               f"{self.ttb10()} AS ttb10 FROM games g {join} {self.ttb_join()}")
        return sql

    # -- endpoints ---------------------------------------------------------
    def search(self, q):
        where, params = ["g.name IS NOT NULL"], []
        text = (q.get("q") or [""])[0].strip()
        if text:
            esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("g.name ILIKE ? ESCAPE '\\'")
            params.append(f"%{esc}%")
        for key, column in (("genre", "genres"), ("platform", "platforms")):
            val = (q.get(key) or [""])[0]
            if val.isdigit() and column in self.game_cols:
                where.append(f"list_contains(g.{column}, ?)")
                params.append(int(val))
        for key, op in (("from", ">="), ("to", "<=")):
            val = (q.get(key) or [""])[0]
            if re.fullmatch(r"-?\d{1,4}", val or ""):
                where.append(f"{self.YEAR} {op} ?")
                params.append(int(val))

        length = (q.get("length") or [""])[0]
        m = re.fullmatch(r"(\d{1,3})-(\d{0,3})", length)       # hours, e.g. "10-20" or "40-"
        if m:
            where.append(f"{self.ttb10()} >= ?")
            params.append(int(m.group(1)) * 10)
            if m.group(2):
                where.append(f"{self.ttb10()} < ?")
                params.append(int(m.group(2)) * 10)

        scope = self.scope_of((q.get("scope") or [""])[0])
        if self.scope_sql(scope):
            where.append(self.scope_sql(scope))

        sort = (q.get("sort") or ["popular"])[0]
        if sort not in SORTS:
            sort = "popular"
        if sort in ("shortest", "longest"):
            where.append(f"{self.ttb10()} IS NOT NULL AND {self.reports()} >= {MIN_REPORTS}")
        if sort == "newest":
            where.append("g.first_release_date <= ?")
            params.append(int(time.time()))
        min_votes = (q.get("min_votes") or [""])[0]
        if min_votes.isdigit():
            minimum = int(min_votes)
        else:
            minimum = 10 if sort == "rating" else 0
        if minimum and "total_rating_count" in self.game_cols:
            where.append("g.total_rating_count >= ?")
            params.append(minimum)

        raw_page = (q.get("page") or ["1"])[0]
        page = max(1, int(raw_page)) if raw_page.isdigit() else 1
        clause = " AND ".join(where)
        total = self.rows(f"SELECT count(*) AS n FROM games g {self.ttb_join()} WHERE {clause}", params)[0]["n"]
        order = SORTS[sort].format(ttb=self.ttb10())
        rows = self.rows(
            f"{self.card_select()} WHERE {clause} ORDER BY {order} LIMIT {PAGE_SIZE} OFFSET ?",
            params + [(page - 1) * PAGE_SIZE])
        return {"total": total, "page": page, "page_size": PAGE_SIZE,
                "games": [self.game_card(r) for r in rows]}

    def game(self, game_id, scope="rated"):
        scope = self.scope_of(scope)
        found = self.rows("SELECT * FROM games WHERE id = ?", [game_id])
        if not found:
            return None
        g = found[0]
        out = {
            "id": g["id"], "name": g.get("name"), "slug": g.get("slug"),
            "summary": text(g.get("summary")), "storyline": text(g.get("storyline")),
            "release": clean(g.get("first_release_date")),
            "rating": clean(g.get("total_rating")), "votes": clean(g.get("total_rating_count")),
            "user_rating": clean(g.get("rating")), "critic_rating": clean(g.get("aggregated_rating")),
            "url": text(g.get("url")),
            "genres": self.names("genres", g.get("genres")),
            "genre_ids": [i for i, _ in self.pairs("genres", g.get("genres"))],
            "platforms": self.names("platforms", g.get("platforms")),
            "platform_ids": [i for i, _ in self.pairs("platforms", g.get("platforms"))],
            "themes": self.names("themes", g.get("themes")),
            "modes": self.names("game_modes", g.get("game_modes")),
            "perspectives": self.names("player_perspectives", g.get("player_perspectives")),
            "cover": None, "screenshots": [], "developers": [], "publishers": [], "similar": [],
            "time_to_beat": None,
            "rated": (bool(self.rows(f"SELECT 1 AS x FROM {RATED} WHERE id = ?", [game_id])) if self.scope_ok else None),
        }
        if self.has_ttb:
            r = self.rows(f"SELECT hastily, normally, completely, reports FROM {TTB_SUB} tb WHERE game_id = ?", [game_id])
            if r and any(r[0][k] is not None for k in ("hastily", "normally", "completely")):
                out["time_to_beat"] = {"hastily": r[0]["hastily"], "normally": r[0]["normally"],
                                       "completely": r[0]["completely"], "count": r[0]["reports"]}
        if "covers" in self.tables and g.get("cover") is not None:
            r = self.rows("SELECT image_id FROM covers WHERE id = ?", [g["cover"]])
            out["cover"] = r[0]["image_id"] if r else None
        if "screenshots" in self.tables:
            out["screenshots"] = [r["image_id"] for r in self.rows(
                "SELECT image_id FROM screenshots WHERE game = ? ORDER BY id LIMIT 12", [game_id])]
        if "involved_companies" in self.tables and "companies" in self.tables:
            for r in self.rows(
                    "SELECT c.name, ic.developer, ic.publisher FROM involved_companies ic "
                    "JOIN companies c ON c.id = ic.company WHERE ic.game = ? ORDER BY ic.id", [game_id]):
                if r["developer"]:
                    out["developers"].append(r["name"])
                if r["publisher"]:
                    out["publishers"].append(r["name"])
        similar = as_int_list(g.get("similar_games"))[:12]
        if similar:
            marks = ",".join("?" * len(similar))
            sc = self.scope_sql(scope)
            cards = {r["id"]: r for r in self.rows(
                f"{self.card_select()} WHERE g.id IN ({marks})" + (f" AND {sc}" if sc else ""), similar)}
            out["similar"] = [self.game_card(cards[i]) for i in similar if i in cards]
        return out

    def columns(self):
        """Every named game as parallel columns: what the browser searches and analyses.
        build_static.py writes this as games.dat; the Python explorer serves it as /api/columns."""
        return self._cached("columns", self._columns)

    def _columns(self):
        keys = ["id", "name", "year", "rel", "rating", "votes", "cover", "genres", "platforms", "ttb", "ttbn"]
        if self.scope_ok:
            keys.append("rated")
        cols = {k: [] for k in keys}
        join, cover = self.cover_join()
        sql = (f"SELECT g.id, g.name, {self.YEAR} AS year, "
               f"CAST(floor({self.col('first_release_date', 'BIGINT')} / 86400.0) AS INTEGER) AS rel, "
               f"CAST(round({self.col('total_rating')} * 100) AS INTEGER) AS rating, "
               f"{self.col('total_rating_count', 'BIGINT')} AS votes, {cover} AS cover, "
               f"{self.col('genres', 'BIGINT[]')} AS genres, {self.col('platforms', 'BIGINT[]')} AS platforms, "
               f"{self.ttb10()} AS ttb, {self.reports()} AS ttbn"
               + (f", CASE WHEN g.id IN (SELECT id FROM {RATED}) THEN 1 ELSE 0 END AS rated" if self.scope_ok else "")
               + f" FROM games g {join} {self.ttb_join()} WHERE g.name IS NOT NULL ORDER BY g.id")
        cur = self.con.cursor()
        cur.execute("USE igdb")
        cur.execute(sql)
        while True:
            rows = cur.fetchmany(50000)
            if not rows:
                break
            for row in rows:
                for key, value in zip(keys, row):
                    cols[key].append((list(value) if value else []) if key in ("genres", "platforms") else value)
        return cols

    def facets(self, scope="rated"):
        scope = self.scope_of(scope)
        return self._cached(f"facets:{scope}", lambda: self._facets(scope))

    def _facets(self, scope):
        sc = self.scope_sql(scope)
        out = {"genres": [], "platforms": []}
        for key, table, column in (("genres", "genres", "genres"), ("platforms", "platforms", "platforms")):
            if table in self.tables and column in self.game_cols:
                out[key] = self.rows(
                    f"SELECT t.id, t.name, count(*) AS n FROM {table} t "
                    f"JOIN (SELECT unnest({column}) AS ref FROM games g" + (f" WHERE {sc}" if sc else "") + ") x ON x.ref = t.id "
                    f"GROUP BY t.id, t.name ORDER BY n DESC, t.id")
        out["platforms"] = [p for p in out["platforms"] if p["n"] >= 50]
        out["genres"].sort(key=lambda r: r["name"] or "")
        out["has_time_to_beat"] = self.ttb_rows > 0
        out["scope"] = self.scope_info(scope)
        return out

    def stats(self, scope="rated"):
        scope = self.scope_of(scope)
        return self._cached(f"stats:{scope}", lambda: self._stats(scope))

    def _stats(self, scope):
        sc = self.scope_sql(scope)
        wh = f" WHERE {sc}" if sc else ""          # for queries with no other condition
        an = f" AND {sc}" if sc else ""            # for queries that already have one
        s = {"scope": self.scope_info(scope)}
        total = self.rows(
            f"SELECT count(*) AS games, count({self.col('total_rating')}) AS rated, "
            f"count(g.cover) AS covered, "
            f"min({self.YEAR}) FILTER (WHERE g.first_release_date <= ?) AS first_year, "
            f"max({self.YEAR}) FILTER (WHERE g.first_release_date <= ?) AS last_year "
            f"FROM games g{wh}", [int(time.time())] * 2)[0]
        s["totals"] = {k: clean(v) for k, v in total.items()}
        this_year = time.gmtime().tm_year
        s["totals"]["timed"] = 0
        s["ttb_buckets"], s["ttb_by_genre"], s["ttb_by_year"] = [], [], []
        if self.has_ttb:
            self._ttb_stats(s, this_year, an)
        s["per_year"] = self.rows(
            f"SELECT {self.YEAR} AS label, count(*) AS value FROM games g "
            f"WHERE g.first_release_date IS NOT NULL{an} GROUP BY 1 HAVING label BETWEEN 1970 AND {this_year} ORDER BY 1")
        if "total_rating" in self.game_cols:
            s["ratings"] = self.rows(
                "SELECT least(floor(g.total_rating / 10), 9)::INTEGER * 10 AS label, count(*) AS value "
                f"FROM games g WHERE g.total_rating IS NOT NULL AND g.total_rating_count >= 5{an} GROUP BY 1 ORDER BY 1")
        else:
            s["ratings"] = []
        for key in ("genres", "platforms"):
            facets = self._facets(scope)[key] if self.tables >= {key} else []
            top = sorted(facets, key=lambda r: -r["n"])[:15]
            s[f"top_{key}"] = [{"label": r["name"], "value": r["n"]} for r in top]
        return s

    def _ttb_stats(self, s, this_year, an=""):
        """Time-to-beat charts: only games whose normal-playthrough figure has MIN_REPORTS+ reports."""
        base = (f"FROM games g {self.ttb_join()} "
                f"WHERE tb.normally IS NOT NULL AND tb.reports >= {MIN_REPORTS}{an}")
        s["totals"]["timed"] = self.rows(f"SELECT count(*) AS n {base}")[0]["n"]
        if not s["totals"]["timed"]:
            return
        edges = [2, 5, 10, 20, 40, 80]
        labels = ["Under 2 h", "2–5 h", "5–10 h", "10–20 h", "20–40 h", "40–80 h", "80+ h"]
        case = " ".join(f"WHEN tb.normally < {e * 3600} THEN {i}" for i, e in enumerate(edges))
        counts = {r["b"]: r["n"] for r in self.rows(
            f"SELECT CASE {case} ELSE {len(edges)} END AS b, count(*) AS n {base} GROUP BY b")}
        s["ttb_buckets"] = [{"label": lab, "value": counts.get(i, 0)} for i, lab in enumerate(labels)]
        if "genres" in self.tables and "genres" in self.game_cols:
            s["ttb_by_genre"] = self.rows(
                f"SELECT ge.name AS label, round(median(x.h), 1) AS value, count(*) AS n FROM ("
                f"SELECT unnest(g.genres) AS gid, tb.normally / 3600.0 AS h {base}) x "
                f"JOIN genres ge ON ge.id = x.gid GROUP BY ge.name HAVING count(*) >= {MIN_GENRE_GAMES} "
                f"ORDER BY value DESC, label LIMIT 15")
        s["ttb_by_year"] = self.rows(
            f"SELECT {self.YEAR} AS label, round(median(tb.normally / 3600.0), 1) AS value, count(*) AS n "
            f"{base} AND g.first_release_date IS NOT NULL GROUP BY 1 "
            f"HAVING count(*) >= {MIN_YEAR_GAMES} AND label BETWEEN 1985 AND {this_year} ORDER BY 1")

    def _cached(self, key, fn):
        with self._lock:
            if key not in self._cache:
                self._cache[key] = fn()
            return self._cache[key]


SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy",
     "default-src 'self'; img-src 'self' https://images.igdb.com data:; "
     "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
     "connect-src 'self'; base-uri 'self'; frame-ancestors 'self'"),
]


class App:
    """WSGI application. Re-opens the database by itself when the file is replaced,
    so a daily rebuild (load_duckdb.py swaps the file in atomically) needs no restart."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.index = HERE / "static" / "index.html"
        self._store, self._sig = None, None
        self._lock = threading.Lock()

    def store(self):
        st = os.stat(self.db_path)
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        with self._lock:
            if sig != self._sig:
                self._store, self._sig = Store(self.db_path), sig
            return self._store

    @staticmethod
    def reply(start_response, code, body, ctype, extra=()):
        data = body if isinstance(body, bytes) else body.encode()
        reasons = {200: "OK", 204: "No Content", 301: "Moved Permanently", 404: "Not Found",
                   500: "Internal Server Error", 503: "Service Unavailable"}
        start_response(f"{code} {reasons.get(code, 'OK')}", [
            ("Content-Type", ctype), ("Content-Length", str(len(data))), *SECURITY_HEADERS, *extra])
        return [data]

    def json(self, start_response, obj, code=200, cache=300):
        cc = f"public, max-age={cache}" if code == 200 and cache else "no-store"
        return self.reply(start_response, code, json.dumps(obj, default=str),
                          "application/json; charset=utf-8", [("Cache-Control", cc)])

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO") or ""
        script = (environ.get("SCRIPT_NAME") or "").rstrip("/")
        q = parse_qs(environ.get("QUERY_STRING", ""))
        try:
            if path in ("", "/", "/index.html"):
                if path == "" and script:      # /games  ->  /games/  (relative URLs need the slash)
                    return self.reply(start_response, 301, b"", "text/plain", [("Location", script + "/")])
                html = (self.index.read_text(encoding="utf-8").replace("__BASE__", script + "/")
                        .replace("<!--ANALYSIS_JS-->", '<script src="analysis.js"></script>'))
                return self.reply(start_response, 200, html, "text/html; charset=utf-8",
                                  [("Cache-Control", "no-cache")])
            if path == "/favicon.ico":
                return self.reply(start_response, 204, b"", "image/x-icon")
            if path == "/analysis.js":
                return self.reply(start_response, 200, (HERE / "static" / "analysis.js").read_bytes(),
                                  "text/javascript; charset=utf-8", [("Cache-Control", "no-cache")])
            if not path.startswith("/api/"):
                return self.json(start_response, {"error": "not found"}, 404)
            try:
                store = self.store()
            except (FileNotFoundError, duckdb.Error):
                return self.json(start_response, {"error": "The database is not available yet. "
                                                  "Try again in a minute."}, 503)
            if path == "/api/games":
                return self.json(start_response, store.search(q))
            if path == "/api/columns":
                return self.json(start_response, store.columns(), cache=3600)
            scope = (q.get("scope") or [""])[0]
            if path == "/api/facets":
                return self.json(start_response, store.facets(scope), cache=3600)
            if path == "/api/stats":
                return self.json(start_response, store.stats(scope), cache=3600)
            m = re.fullmatch(r"/api/game/(\d+)", path)
            if m:
                game = store.game(int(m.group(1)), scope)
                return self.json(start_response, game) if game else self.json(start_response, {"error": "not found"}, 404)
            return self.json(start_response, {"error": "not found"}, 404)
        except Exception as e:  # keep serving; the UI shows the message
            print(f"error handling {path}: {e!r}", file=sys.stderr)
            return self.json(start_response, {"error": str(e)}, 500)


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB), help="path to igdb.duckdb")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="leave as 127.0.0.1 to keep it private")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    args = ap.parse_args()

    if not Path(args.db).exists():
        sys.exit(f"{args.db} not found. Run `python load_duckdb.py` first (after fetch_igdb.py).")
    app = App(args.db)
    app.store()   # fail fast if the file is not a usable database

    class Handler(WSGIRequestHandler):
        def log_message(self, fmt, *a):
            if args.verbose:
                super().log_message(fmt, *a)

    server = make_server(args.host, args.port, app, server_class=ThreadedServer, handler_class=Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Explorer running at {url}  (Ctrl+C to stop)")
    if not args.no_open:
        threading.Timer(0.5, webbrowser.open, args=[url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
