#!/usr/bin/env python3
"""
One command to bring your local IGDB data up to date:

  python update.py

It runs `fetch_igdb.py --update` (downloads only what changed since last time and
merges it into data/igdb/*.jsonl), then `load_duckdb.py` to rebuild data/igdb.duckdb.
Any extra arguments go to fetch_igdb.py, e.g. `python update.py --endpoints games`.

Stop the explorer (explorer/server.py) first: it holds the database open, and the
rebuild replaces that file.
"""
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent


def run(script, *args):
    print(f"\n$ python {script} {' '.join(args)}".rstrip(), flush=True)
    return subprocess.run([sys.executable, str(here / script), *args], cwd=here).returncode


if run("fetch_igdb.py", "--update", *sys.argv[1:]) != 0:
    sys.exit("The update did not finish, so the database was not rebuilt. "
             "Run `python update.py` again to continue.")
if run("load_duckdb.py") != 0:
    sys.exit("Rebuilding the database failed. If the explorer is running, stop it and try again.")
print("\nDone. Start the explorer with: python explorer/server.py")
