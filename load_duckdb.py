#!/usr/bin/env python3
"""
Load the IGDB dump (data/igdb/*.jsonl) into a DuckDB database so you can query
it with SQL. Each endpoint becomes one table named after it.

Usage:
  python load_duckdb.py                  # writes data/igdb.duckdb
  python -c "import duckdb; print(duckdb.connect('data/igdb.duckdb').sql(
      'select name, first_release_date from games limit 5'))"

Fields that reference other records (genres, platforms, cover, ...) hold
numeric IDs; join them to the matching table, e.g.
  select g.name, ge.name as genre
  from games g, unnest(g.genres) as t(genre_id)
  join genres ge on ge.id = t.genre_id;
Timestamps such as first_release_date are Unix seconds: use to_timestamp(...).
"""

import os
import sys
from pathlib import Path

import duckdb

SRC = Path("data/igdb")
DB = Path("data/igdb.duckdb")


def main():
    files = sorted(SRC.glob("*.jsonl"))
    files = [f for f in files if f.stat().st_size > 0]
    if not files:
        sys.exit(f"No .jsonl files in {SRC}. Run fetch_igdb.py first.")

    # Build into a temporary file and swap it in at the end, so a running explorer keeps
    # serving the old database until the new one is complete (and a failed build leaves it alone).
    new = DB.with_name(DB.name + ".new")
    for leftover in (new, new.with_name(new.name + ".wal")):
        leftover.unlink(missing_ok=True)
    # Optional limits for small servers, e.g. DUCKDB_MEMORY_LIMIT=1GB DUCKDB_THREADS=1
    config = {k: v for k, v in (("memory_limit", os.environ.get("DUCKDB_MEMORY_LIMIT")),
                                ("threads", os.environ.get("DUCKDB_THREADS"))) if v}
    con = duckdb.connect(str(new), config=config)
    for f in files:
        con.execute(
            f'CREATE TABLE "{f.stem}" AS SELECT * FROM read_json_auto(?, '
            "format='newline_delimited', sample_size=-1, maximum_object_size=33554432)",
            [str(f)],
        )
        n = con.execute(f'SELECT count(*) FROM "{f.stem}"').fetchone()[0]
        print(f"{f.stem:<40} {n:>10,} rows")
    con.close()
    try:
        os.replace(new, DB)
    except PermissionError:
        sys.exit(f"Built {new} but could not replace {DB}: is the explorer running? "
                 "Stop it and move the file into place, or re-run this script.")
    print(f"\nWrote {DB}")


if __name__ == "__main__":
    main()
