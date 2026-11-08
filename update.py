#!/usr/bin/env python3
"""
One command to bring your IGDB data up to date:

  python update.py                          # update the files, then rebuild data/igdb.duckdb
  python update.py --static-out DIR         # update the files, then rebuild the STATIC website in DIR

It runs `fetch_igdb.py --update` (downloads only what changed since last time and
merges it into data/igdb/*.jsonl), then either `load_duckdb.py` (new database swapped in
at the end, so a running `explorer/server.py` picks it up) or, with --static-out,
`build_static.py` (new data version published into DIR; visitors switch over seamlessly).

Safe to run from cron every day:
  * a lock file stops two runs from overlapping (a run older than 12 hours is treated as dead)
  * every line of output is timestamped
  * exit status is non-zero if anything failed, so cron/monitoring can tell

Options (anything else is passed to fetch_igdb.py, e.g. --endpoints games):
  --static-out DIR    publish the static website into DIR instead of building the database
  --monthly-refresh   on the 1st of the month, first re-download `games` from scratch.
                      IGDB never announces deleted records, so this is what clears them.
"""
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK = HERE / "data" / ".update.lock"
STALE_AFTER = 12 * 3600


def say(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def run(script, *args):
    say(f"running: python {script} {' '.join(args)}".rstrip())
    proc = subprocess.Popen([sys.executable, "-u", str(HERE / script), *args], cwd=HERE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    progress = 0
    for line in proc.stdout:
        line = line.rstrip()
        # progress lines ("  games: 1,500/300,000") would flood a cron log; keep every 20th
        if line.startswith("  ") and "/" in line and line.count(":") == 1:
            progress += 1
            if progress % 20:
                continue
        say(line)
    proc.wait()
    return proc.returncode


def url_args(args):
    """The --api-url/--token-url options (used for testing) with their values."""
    out = []
    for i, a in enumerate(args):
        if a in ("--api-url", "--token-url") and i + 1 < len(args):
            out += [a, args[i + 1]]
    return out


def take_lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - LOCK.stat().st_mtime > STALE_AFTER:
            say("removing a stale lock from an earlier run")
            LOCK.unlink()
    except FileNotFoundError:
        pass
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        say("another update is already running (data/.update.lock); exiting")
        sys.exit(0)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)


def main():
    args = sys.argv[1:]
    monthly = "--monthly-refresh" in args
    args = [a for a in args if a != "--monthly-refresh"]
    static_out = None
    if "--static-out" in args:
        i = args.index("--static-out")
        if i + 1 >= len(args):
            sys.exit("--static-out needs a folder, e.g. --static-out ~/games.brutalc.com")
        static_out = os.path.expanduser(args[i + 1])
        del args[i:i + 2]

    take_lock()
    try:
        started = time.time()
        if monthly and datetime.date.today().day == 1:
            say("first of the month: refreshing games from scratch to clear deleted records")
            if run("fetch_igdb.py", "--refresh", "--endpoints", "games", *url_args(args)) != 0:
                sys.exit("Monthly refresh of games failed; stopping before the update.")
        if run("fetch_igdb.py", "--update", *args) != 0:
            sys.exit("The update did not finish, so the database was not rebuilt. "
                     "Run it again to continue where it stopped.")
        if static_out:
            if run("build_static.py", "--out", static_out) != 0:
                sys.exit("Building the website failed. The version that is live is unchanged.")
        elif run("load_duckdb.py") != 0:
            sys.exit("Rebuilding the database failed. The previous database (if any) is unchanged.")
        say(f"done in {int(time.time() - started)}s")
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
