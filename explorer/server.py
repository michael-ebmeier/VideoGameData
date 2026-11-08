#!/usr/bin/env python3
"""
Local web explorer for the IGDB dump.

Reads data/igdb.duckdb (build it with `python load_duckdb.py`) and serves a small
browser UI: a cover grid with search and filters, a page per game, and a stats
page with a few charts. Everything runs on your machine; the only outside
requests are your browser loading cover art from images.igdb.com.

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
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
        self.con = duckdb.connect(str(path), read_only=True)
        self.tables = {r[0] for r in self.con.execute("SHOW TABLES").fetchall()}
        if "games" not in self.tables:
            sys.exit(f"{path} has no 'games' table. Did the download include it?")
        self.game_cols = {r[0] for r in self.con.execute("DESCRIBE games").fetchall()}
        self._cache = {}
        self._lock = threading.Lock()

    # -- helpers -----------------------------------------------------------
    def rows(self, sql, params=()):
        cur = self.con.cursor()  # one cursor per call keeps threads independent
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

    def names(self, table, ids):
        ids = as_int_list(ids)
        if not ids or table not in self.tables:
            return []
        marks = ",".join("?" * len(ids))
        found = {r["id"]: r["name"] for r in
                 self.rows(f"SELECT id, name FROM {table} WHERE id IN ({marks})", ids)}
        return [found[i] for i in ids if i in found]

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
            "genre_ids": as_int_list(g.get("genres")),
            "platforms": self.names("platforms", g.get("platforms")),
            "platform_ids": as_int_list(g.get("platforms")),
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
                    f"GROUP BY t.id, t.name ORDER BY n DESC")
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


def make_handler(store):
    index = HERE / "static" / "index.html"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if "--verbose" in sys.argv:
                super().log_message(fmt, *args)

        def send_body(self, code, body, ctype):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, obj, code=200):
            self.send_body(code, json.dumps(obj, default=str), "application/json; charset=utf-8")

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    return self.send_body(200, index.read_bytes(), "text/html; charset=utf-8")
                if url.path == "/favicon.ico":
                    return self.send_body(204, b"", "image/x-icon")
                if url.path == "/api/games":
                    return self.send_json(store.search(q))
                if url.path == "/api/facets":
                    return self.send_json(store.facets())
                if url.path == "/api/stats":
                    return self.send_json(store.stats())
                m = re.fullmatch(r"/api/game/(\d+)", url.path)
                if m:
                    game = store.game(int(m.group(1)))
                    return self.send_json(game) if game else self.send_json({"error": "not found"}, 404)
                self.send_json({"error": "not found"}, 404)
            except Exception as e:  # keep the server alive; show the cause in the UI
                print(f"error handling {self.path}: {e!r}", file=sys.stderr)
                self.send_json({"error": str(e)}, 500)

    return Handler


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
    store = Store(args.db)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store))
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
