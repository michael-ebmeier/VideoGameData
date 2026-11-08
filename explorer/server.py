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
}


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
        self._cache = {}
        self._lock = threading.Lock()

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
        }

    def card_select(self):
        join, cover = self.cover_join()
        sql = (f"SELECT g.id, g.name, {self.YEAR} AS year, {self.col('total_rating')} AS rating, "
               f"{self.col('total_rating_count', 'BIGINT')} AS votes, {cover} AS cover "
               f"FROM games g {join}")
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

        sort = (q.get("sort") or ["popular"])[0]
        if sort not in SORTS:
            sort = "popular"
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
        total = self.rows(f"SELECT count(*) AS n FROM games g WHERE {clause}", params)[0]["n"]
        rows = self.rows(
            f"{self.card_select()} WHERE {clause} ORDER BY {SORTS[sort]} LIMIT {PAGE_SIZE} OFFSET ?",
            params + [(page - 1) * PAGE_SIZE])
        return {"total": total, "page": page, "page_size": PAGE_SIZE,
                "games": [self.game_card(r) for r in rows]}

    def game(self, game_id):
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
        }
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
            cards = {r["id"]: r for r in self.rows(
                f"{self.card_select()} WHERE g.id IN ({marks})", similar)}
            out["similar"] = [self.game_card(cards[i]) for i in similar if i in cards]
        return out

    def facets(self):
        return self._cached("facets", self._facets)

    def _facets(self):
        out = {"genres": [], "platforms": []}
        for key, table, column in (("genres", "genres", "genres"), ("platforms", "platforms", "platforms")):
            if table in self.tables and column in self.game_cols:
                out[key] = self.rows(
                    f"SELECT t.id, t.name, count(*) AS n FROM {table} t "
                    f"JOIN (SELECT unnest({column}) AS ref FROM games) x ON x.ref = t.id "
                    f"GROUP BY t.id, t.name ORDER BY n DESC, t.id")
        out["platforms"] = [p for p in out["platforms"] if p["n"] >= 50]
        out["genres"].sort(key=lambda r: r["name"] or "")
        return out

    def stats(self):
        return self._cached("stats", self._stats)

    def _stats(self):
        s = {}
        total = self.rows(
            f"SELECT count(*) AS games, count({self.col('total_rating')}) AS rated, "
            f"count(g.cover) AS covered, "
            f"min({self.YEAR}) FILTER (WHERE g.first_release_date <= ?) AS first_year, "
            f"max({self.YEAR}) FILTER (WHERE g.first_release_date <= ?) AS last_year "
            "FROM games g", [int(time.time())] * 2)[0]
        s["totals"] = {k: clean(v) for k, v in total.items()}
        this_year = time.gmtime().tm_year
        s["per_year"] = self.rows(
            f"SELECT {self.YEAR} AS label, count(*) AS value FROM games g "
            f"WHERE g.first_release_date IS NOT NULL GROUP BY 1 HAVING label BETWEEN 1970 AND {this_year} ORDER BY 1")
        if "total_rating" in self.game_cols:
            s["ratings"] = self.rows(
                "SELECT least(floor(g.total_rating / 10), 9)::INTEGER * 10 AS label, count(*) AS value "
                "FROM games g WHERE g.total_rating IS NOT NULL AND g.total_rating_count >= 5 GROUP BY 1 ORDER BY 1")
        else:
            s["ratings"] = []
        for key in ("genres", "platforms"):
            facets = self._facets()[key] if self.tables >= {key} else []
            top = sorted(facets, key=lambda r: -r["n"])[:15]
            s[f"top_{key}"] = [{"label": r["name"], "value": r["n"]} for r in top]
        return s

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
                html = self.index.read_text(encoding="utf-8").replace("__BASE__", script + "/")
                return self.reply(start_response, 200, html, "text/html; charset=utf-8",
                                  [("Cache-Control", "no-cache")])
            if path == "/favicon.ico":
                return self.reply(start_response, 204, b"", "image/x-icon")
            if not path.startswith("/api/"):
                return self.json(start_response, {"error": "not found"}, 404)
            try:
                store = self.store()
            except (FileNotFoundError, duckdb.Error):
                return self.json(start_response, {"error": "The database is not available yet. "
                                                  "Try again in a minute."}, 503)
            if path == "/api/games":
                return self.json(start_response, store.search(q))
            if path == "/api/facets":
                return self.json(start_response, store.facets(), cache=3600)
            if path == "/api/stats":
                return self.json(start_response, store.stats(), cache=3600)
            m = re.fullmatch(r"/api/game/(\d+)", path)
            if m:
                game = store.game(int(m.group(1)))
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
