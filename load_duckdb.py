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

    DB.unlink(missing_ok=True)
    con = duckdb.connect(str(DB))
    for f in files:
        con.execute(
            f'CREATE TABLE "{f.stem}" AS SELECT * FROM read_json_auto(?, '
            "format='newline_delimited', sample_size=-1, maximum_object_size=33554432)",
            [str(f)],
        )
        n = con.execute(f'SELECT count(*) FROM "{f.stem}"').fetchone()[0]
        print(f"{f.stem:<40} {n:>10,} rows")
    con.close()
    print(f"\nWrote {DB}")


if __name__ == "__main__":
    main()
