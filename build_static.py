#!/usr/bin/env python3
"""
Build the explorer as a STATIC website: plain files that any web server can host, with no
Python app, database or Passenger on the host. The visitor's browser does the searching.

  python build_static.py --out ~/games.brutalc.com     # the subdomain's folder
  python build_static.py --out site                    # a local folder to try it out

It reads only the IGDB tables the explorer shows (games, covers, genres, platforms, themes,
game_modes, player_perspectives, companies, involved_companies, screenshots, game_time_to_beats) from
data/igdb/*.jsonl and writes into --out:

  index.html, static-api.js, analysis.js   the page (same UI as `python explorer/server.py`)
  .htaccess                      Apache settings (only if none exists yet; see below)
  data/manifest.json             points at the current data version; always re-checked
  data/<version>/games.dat       one compact, gzipped table of every game (search/filter/sort)
  data/<version>/facets.json     genre and platform lists;  stats.json  the Stats page
  data/<version>/d/<n>.dat       game details, 500 games per file, loaded on demand

Publishing is safe to run while people browse: the new version is written to its own folder
and only then does manifest.json switch over. The newest 2 versions are kept, older ones deleted.

Options:
  --frontend-only   just refresh index.html, static-api.js and analysis.js (after a `git pull`), no data rebuild
  --src DIR         where the .jsonl files are (default data/igdb)
  --keep N          how many data versions to keep (default 2)

Optional DUCKDB_MEMORY_LIMIT / DUCKDB_THREADS environment variables limit DuckDB on small hosts.
"""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "explorer"))
from server import TTB10, TTB_SUB, Store  # noqa: E402  (reuse its facets/stats queries so both versions agree)

SHARD = 500                      # games per detail file
HTACCESS_MARK = "# igdb-explorer static site"

# Explicit column types: no schema guessing, one pass over each file, and the same types on
# every DuckDB version. Fields IGDB doesn't send simply come out NULL.
TABLES = {
    "games": {
        "id": "BIGINT", "name": "VARCHAR", "slug": "VARCHAR", "summary": "VARCHAR",
        "storyline": "VARCHAR", "first_release_date": "BIGINT", "total_rating": "DOUBLE",
        "total_rating_count": "BIGINT", "rating": "DOUBLE", "aggregated_rating": "DOUBLE",
        "url": "VARCHAR", "cover": "BIGINT", "genres": "BIGINT[]", "platforms": "BIGINT[]",
        "themes": "BIGINT[]", "game_modes": "BIGINT[]", "player_perspectives": "BIGINT[]",
        "similar_games": "BIGINT[]",
    },
    "covers": {"id": "BIGINT", "game": "BIGINT", "image_id": "VARCHAR"},
    "genres": {"id": "BIGINT", "name": "VARCHAR"},
    "platforms": {"id": "BIGINT", "name": "VARCHAR"},
    "themes": {"id": "BIGINT", "name": "VARCHAR"},
    "game_modes": {"id": "BIGINT", "name": "VARCHAR"},
    "player_perspectives": {"id": "BIGINT", "name": "VARCHAR"},
    "companies": {"id": "BIGINT", "name": "VARCHAR"},
    "involved_companies": {"id": "BIGINT", "game": "BIGINT", "company": "BIGINT",
                           "developer": "BOOLEAN", "publisher": "BOOLEAN"},
    "screenshots": {"id": "BIGINT", "game": "BIGINT", "image_id": "VARCHAR"},
    "game_time_to_beats": {"id": "BIGINT", "game_id": "BIGINT", "hastily": "BIGINT",
                           "normally": "BIGINT", "completely": "BIGINT", "count": "BIGINT"},
}


def log(msg):
    print(msg, flush=True)


def duck_config():
    return {k: v for k, v in (("memory_limit", os.environ.get("DUCKDB_MEMORY_LIMIT")),
                              ("threads", os.environ.get("DUCKDB_THREADS"))) if v}


def load_tables(con, src):
    for name, cols in TABLES.items():
        path = src / f"{name}.jsonl"
        if path.exists() and path.stat().st_size > 0:
            mapping = "{" + ", ".join(f"'{c}': '{t}'" for c, t in cols.items()) + "}"
            con.execute(
                f"CREATE TABLE {name} AS SELECT * FROM read_json(?, format='newline_delimited', "
                f"columns={mapping}, maximum_object_size=33554432)", [str(path)])
            n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            log(f"  {name:<22} {n:>10,} rows")
        else:
            decl = ", ".join(f'"{c}" {t}' for c, t in cols.items())
            con.execute(f"CREATE TABLE {name} ({decl})")
            log(f"  {name:<22} (no {path.name}; continuing without it)")


def write_gz(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str),
                    encoding="utf-8")


def build_details(con, vdir):
    """Per-game detail pages, SHARD games per file, streamed so memory stays small."""
    def names(table):
        return {i: n for i, n in con.execute(f"SELECT id, name FROM {table}").fetchall()}
    lookup = {k: names(k) for k in ("genres", "platforms", "themes", "game_modes", "player_perspectives")}

    def pairs(table, ids):
        return [(i, lookup[table][i]) for i in (ids or []) if i in lookup[table]]

    # Per-game extras are aggregated once into small temporary tables; the games are then read in
    # ID ranges (whole shards at a time). Nothing ever sorts or joins all games at once, so memory
    # stays low even with a 512 MB limit.
    con.execute("CREATE TEMP TABLE ss_agg AS SELECT game, (list(image_id ORDER BY id))[1:12] AS shots "
                "FROM screenshots GROUP BY game")
    con.execute("""CREATE TEMP TABLE co_agg AS
        SELECT ic.game, list(c.name ORDER BY ic.id) FILTER (WHERE ic.developer) AS devs,
               list(c.name ORDER BY ic.id) FILTER (WHERE ic.publisher) AS pubs
        FROM involved_companies ic JOIN companies c ON c.id = ic.company GROUP BY ic.game""")
    con.execute(f"CREATE TEMP TABLE tb_best AS SELECT * FROM {TTB_SUB} t")
    top = con.execute("SELECT coalesce(max(id), 0) FROM games").fetchone()[0]
    chunk = SHARD * 20                                  # ~10,000 games per read

    def read_games():
        for lo in range(0, top + 1, chunk):
            yield from con.execute(f"""
                SELECT g.id, g.summary, g.storyline, g.first_release_date, g.total_rating,
                       g.total_rating_count, g.rating, g.aggregated_rating, g.url,
                       g.genres, g.platforms, g.themes, g.game_modes, g.player_perspectives, g.similar_games,
                       ss.shots, co.devs, co.pubs, tb.hastily, tb.normally, tb.completely, tb.reports
                FROM games g
                LEFT JOIN tb_best tb ON tb.game_id = g.id
                LEFT JOIN ss_agg ss ON ss.game = g.id
                LEFT JOIN co_agg co ON co.game = g.id
                WHERE g.name IS NOT NULL AND g.id >= {lo} AND g.id < {lo + chunk} ORDER BY g.id""").fetchall()
    shard, current, files = None, {}, 0

    def flush():
        nonlocal current, files
        if current:
            write_gz(vdir / "d" / f"{shard}.dat", current)
            files += 1
            current = {}

    for (gid, summary, story, released, rating, votes, urating, crating, url, genres, plats,
         themes, modes, persp, similar, shots, devs, pubs, t_fast, t_norm, t_full, t_n) in read_games():
        if gid // SHARD != shard:
            flush()
            shard = gid // SHARD
        gp, pp = pairs("genres", genres), pairs("platforms", plats)
        detail = {
            "summary": summary, "storyline": story, "release": released, "rating": rating,
            "votes": votes, "user_rating": urating, "critic_rating": crating, "url": url,
            "genres": [n for _, n in gp], "genre_ids": [i for i, _ in gp],
            "platforms": [n for _, n in pp], "platform_ids": [i for i, _ in pp],
            "themes": [n for _, n in pairs("themes", themes)],
            "modes": [n for _, n in pairs("game_modes", modes)],
            "perspectives": [n for _, n in pairs("player_perspectives", persp)],
            "screenshots": shots, "developers": devs, "publishers": pubs,
            "similar_ids": (similar or [])[:12],
            "time_to_beat": ({"hastily": t_fast, "normally": t_norm, "completely": t_full, "count": t_n}
                             if any(v is not None for v in (t_fast, t_norm, t_full)) else None),
        }
        current[str(gid)] = {k: v for k, v in detail.items() if v not in (None, [], "")}
    flush()
    return files


def build_data(src, out, keep):
    tmp_db = HERE / "data" / "static-build.duckdb"
    tmp_db.parent.mkdir(parents=True, exist_ok=True)
    for leftover in (tmp_db, tmp_db.with_name(tmp_db.name + ".wal")):
        leftover.unlink(missing_ok=True)

    log("Reading the tables the explorer needs:")
    con = duckdb.connect(str(tmp_db), config=duck_config())
    version = time.strftime("%Y%m%d-%H%M%S")
    data_dir = out / "data"
    staging = data_dir / f".building-{version}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        load_tables(con, src)
        log("Writing game detail files…")
        files = build_details(con, staging)
        log(f"  {files:,} files")
        con.close()
        store = Store(tmp_db)             # same queries as the Python explorer, so both agree
        log("Writing the games table…")
        cols = store.columns()
        n = len(cols["id"])
        write_gz(staging / "games.dat", cols)
        del cols
        log(f"  {n:,} games")
        write_json(staging / "facets.json", store.facets())
        write_json(staging / "stats.json", store.stats())
        del store

        final = data_dir / version
        os.replace(staging, final)
        write_json(data_dir / "manifest.json.new", {"version": version, "games": n, "format": 1,
                                                    "built": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())})
        os.replace(data_dir / "manifest.json.new", data_dir / "manifest.json")   # the switch-over
        log(f"Published data version {version}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            con.close()
        except Exception:
            pass
        for leftover in (tmp_db, tmp_db.with_name(tmp_db.name + ".wal")):
            leftover.unlink(missing_ok=True)

    old = sorted(p for p in data_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    for p in old[:-keep]:
        shutil.rmtree(p, ignore_errors=True)
        log(f"Removed old version {p.name}")


HTACCESS = f"""{HTACCESS_MARK} (this file is rewritten by build_static.py; delete this line to keep your own edits)
Options -Indexes
<IfModule mod_headers.c>
  Header set X-Content-Type-Options "nosniff"
  Header set Referrer-Policy "no-referrer"
  Header set Content-Security-Policy "default-src 'self'; img-src 'self' https://images.igdb.com data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'self'; frame-ancestors 'self'"
  <FilesMatch "(manifest\\.json|\\.html)$">
    Header set Cache-Control "no-cache"
  </FilesMatch>
</IfModule>
"""


def publish_frontend(out):
    out.mkdir(parents=True, exist_ok=True)
    static = HERE / "explorer" / "static"
    api_js = (static / "static-api.js").read_bytes()
    analysis_js = (static / "analysis.js").read_bytes()
    version = hashlib.sha1(api_js + analysis_js).hexdigest()[:10]
    html = (static / "index.html").read_text(encoding="utf-8")
    html = (html.replace("__BASE__", "./")
            .replace("<!--ANALYSIS_JS-->", f'<script src="analysis.js?v={version}"></script>')
            .replace("<!--STATIC_API-->", f'<script src="static-api.js?v={version}"></script>'))
    (out / "static-api.js").write_bytes(api_js)
    (out / "analysis.js").write_bytes(analysis_js)
    (out / "index.html").write_text(html, encoding="utf-8")
    ht = out / ".htaccess"
    if not ht.exists() or HTACCESS_MARK in ht.read_text(errors="replace"):
        ht.write_text(HTACCESS)
    else:
        log(f"Note: left your existing {ht} alone. It's recommended to add `Options -Indexes` "
            "and no-cache headers for manifest.json and .html (see HTACCESS in build_static.py).")
    log(f"Wrote the page into {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="site", help="folder to write the website into (default: site)")
    ap.add_argument("--src", default=str(HERE / "data" / "igdb"), help="folder with the .jsonl files")
    ap.add_argument("--keep", type=int, default=2, help="data versions to keep (default 2)")
    ap.add_argument("--frontend-only", action="store_true", help="only refresh the page and its scripts")
    args = ap.parse_args()

    out = Path(args.out).expanduser().resolve()
    src = Path(args.src).expanduser().resolve()
    if not args.frontend_only:
        if not (src / "games.jsonl").exists():
            sys.exit(f"{src / 'games.jsonl'} not found. Run fetch_igdb.py first.")
        build_data(src, out, max(1, args.keep))
    publish_frontend(out)
    log(f"\nDone. Open {out / 'index.html'} through a web server (not by double-clicking the file).")


if __name__ == "__main__":
    main()
