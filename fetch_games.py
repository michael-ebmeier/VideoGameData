#!/usr/bin/env python3
"""
Export video games from Wikidata to a CSV file.

How it works (and why it isn't one giant query):
  1. One cheap query fetches the Wikidata ID of every item that is an
     instance of "video game" (Q7889).
  2. The IDs are split into small batches. Each batch is one SPARQL query that
     returns title, release date, platforms, genres, developers, publishers and
     external IDs (Steam, IGDB, Giant Bomb, ...) for just those games.
  3. Every batch is saved to data/parts/ as soon as it finishes, so you can stop
     and re-run the script at any time and it resumes where it left off.
  4. All parts are combined into data/games.csv.

Wikidata's public endpoint kills queries after ~60 seconds, which is why the
work is batched. Wikimedia also requires a User-Agent with contact info, so
you must pass --contact (or set the WDQS_CONTACT environment variable).

Usage:
  python fetch_games.py --contact you@example.com
  python fetch_games.py --contact you@example.com --limit 300      # quick test
"""

import argparse
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ENDPOINT = "https://query.wikidata.org/sparql"

# Properties whose values are items with English labels (joined with " | ").
LABEL_PROPS = {
    "platforms": "P400",
    "genres": "P136",
    "developers": "P178",
    "publishers": "P123",
}

# External ID properties. Edit this dict to add or remove sources.
# If a column comes out completely empty, double-check its property ID on
# https://www.wikidata.org/wiki/Property:Pxxxx
EXTERNAL_IDS = {
    "steam_app_id": "P1733",
    "igdb_id": "P5794",
    "giantbomb_id": "P5247",
    "mobygames_id": "P1933",
    "pcgamingwiki_id": "P6337",
    "gog_id": "P2725",
    "metacritic_id": "P1712",
    "howlongtobeat_id": "P2816",
}

DATA_DIR = Path("data")


def log(msg):
    print(msg, flush=True)


def run_query(session, query, retries=5):
    """POST a SPARQL query, retrying on rate limits and server errors."""
    for attempt in range(retries):
        try:
            r = session.post(
                ENDPOINT,
                data={"query": query},
                headers={"Accept": "application/sparql-results+json"},
                timeout=120,
            )
        except requests.RequestException as e:
            wait = 5 * (attempt + 1)
            log(f"  network error ({e.__class__.__name__}), retrying in {wait}s")
            time.sleep(wait)
            continue

        if r.status_code == 200:
            return r.json()["results"]["bindings"]
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 30)) + 1
            log(f"  rate limited, waiting {wait}s")
            time.sleep(wait)
            continue
        if r.status_code in (500, 502, 503, 504):
            wait = 5 * (attempt + 1)
            log(f"  server error {r.status_code} (often a timeout), retrying in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code == 403:
            sys.exit("403 Forbidden: Wikimedia rejected the User-Agent. "
                     "Pass a real contact via --contact.")
        r.raise_for_status()
    raise RuntimeError("query failed after retries")


def fetch_ids(session, include_subclasses):
    path = "wdt:P31/wdt:P279*" if include_subclasses else "wdt:P31"
    query = f"SELECT ?item WHERE {{ ?item {path} wd:Q7889 . }}"
    rows = run_query(session, query)
    ids = sorted({r["item"]["value"].rsplit("/", 1)[-1] for r in rows},
                 key=lambda q: int(q[1:]))
    return ids


def build_query(ids):
    values = " ".join(f"wd:{i}" for i in ids)
    values_clause = f"VALUES ?item {{ {values} }}"

    blocks = [
        '?item rdfs:label ?title . FILTER(LANG(?title) = "en")'
        .join(["OPTIONAL { ", " }"]),
        "OPTIONAL { SELECT ?item (MIN(?d) AS ?release_date) WHERE { "
        f"{values_clause} ?item wdt:P577 ?d . }} GROUP BY ?item }}",
    ]
    for name, prop in LABEL_PROPS.items():
        blocks.append(
            f"OPTIONAL {{ SELECT ?item "
            f'(GROUP_CONCAT(DISTINCT ?l; separator=" | ") AS ?{name}) WHERE {{ '
            f"{values_clause} ?item wdt:{prop} ?v . ?v rdfs:label ?l . "
            f'FILTER(LANG(?l) = "en") }} GROUP BY ?item }}'
        )
    for name, prop in EXTERNAL_IDS.items():
        blocks.append(
            f"OPTIONAL {{ SELECT ?item "
            f'(GROUP_CONCAT(DISTINCT STR(?x); separator=" | ") AS ?{name}) WHERE {{ '
            f"{values_clause} ?item wdt:{prop} ?x . }} GROUP BY ?item }}"
        )

    cols = ["?item", "?title", "?release_date"]
    cols += [f"?{n}" for n in LABEL_PROPS] + [f"?{n}" for n in EXTERNAL_IDS]
    return f"SELECT {' '.join(cols)} WHERE {{ {values_clause} {' '.join(blocks)} }}"


def fetch_batch(session, ids):
    """Fetch one batch; if it keeps failing, split it in half and try again."""
    try:
        return run_query(session, build_query(ids))
    except RuntimeError:
        if len(ids) == 1:
            log(f"  giving up on {ids[0]}")
            return []
        mid = len(ids) // 2
        log(f"  splitting batch of {len(ids)} into {mid} + {len(ids) - mid}")
        return fetch_batch(session, ids[:mid]) + fetch_batch(session, ids[mid:])


def bindings_to_df(bindings):
    records = []
    for b in bindings:
        rec = {k: v["value"] for k, v in b.items()}
        rec["qid"] = rec.pop("item").rsplit("/", 1)[-1]
        records.append(rec)
    return pd.DataFrame(records)


def combine(parts_dir, out_path):
    frames = [pd.read_csv(p, dtype=str, keep_default_na=False)
              for p in sorted(parts_dir.glob("part_*.csv"))]
    if not frames:
        sys.exit("No parts found to combine.")
    df = pd.concat(frames, ignore_index=True).drop_duplicates("qid")

    for col in ["title", "release_date", *LABEL_PROPS, *EXTERNAL_IDS]:
        if col not in df.columns:
            df[col] = ""
    df["release_year"] = df["release_date"].str.extract(r"^(-?\d{4})", expand=False).fillna("")
    df["release_date"] = df["release_date"].str[:10]
    df["wikidata_url"] = "https://www.wikidata.org/wiki/" + df["qid"]

    order = ["qid", "title", "release_date", "release_year",
             *LABEL_PROPS, *EXTERNAL_IDS, "wikidata_url"]
    df = df[order].sort_values("qid", key=lambda s: s.str[1:].astype(int))
    df.to_csv(out_path, index=False)

    log(f"\nWrote {len(df):,} games to {out_path}")
    log("Fill rate per column:")
    for col in order[1:-1]:
        pct = (df[col] != "").mean() * 100
        log(f"  {col:<18} {pct:5.1f}%")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--contact", help="email or URL identifying you (required by Wikimedia)")
    ap.add_argument("--batch-size", type=int, default=100,
                    help="games per query (default 100; lower it if you see timeouts)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds to pause between queries (default 1.0)")
    ap.add_argument("--limit", type=int, help="only fetch the first N games (for testing)")
    ap.add_argument("--subclasses", action="store_true",
                    help="also include subclasses of video game (DLC, expansions, ...); slower")
    ap.add_argument("--refresh-ids", action="store_true",
                    help="re-download the ID list instead of using the cached one")
    ap.add_argument("--combine-only", action="store_true",
                    help="skip downloading; just rebuild games.csv from saved parts")
    args = ap.parse_args()

    sample = args.limit is not None
    parts_dir = DATA_DIR / ("parts_sample" if sample else "parts")
    out_path = DATA_DIR / ("games_sample.csv" if sample else "games.csv")
    parts_dir.mkdir(parents=True, exist_ok=True)

    if args.combine_only:
        combine(parts_dir, out_path)
        return

    contact = args.contact or os.environ.get("WDQS_CONTACT")
    if not contact:
        sys.exit("Please pass --contact you@example.com (or set WDQS_CONTACT). "
                 "Wikimedia requires contact info in the User-Agent.")
    session = requests.Session()
    session.headers["User-Agent"] = f"wikidata-games-export/1.0 ({contact})"

    ids_file = DATA_DIR / ("ids_subclasses.txt" if args.subclasses else "ids.txt")
    if ids_file.exists() and not args.refresh_ids:
        ids = ids_file.read_text().split()
        log(f"Loaded {len(ids):,} cached IDs from {ids_file}")
    else:
        log("Fetching the list of video game IDs...")
        ids = fetch_ids(session, args.subclasses)
        ids_file.write_text("\n".join(ids))
        log(f"Found {len(ids):,} games.")
    if sample:
        ids = ids[: args.limit]

    batches = [ids[i:i + args.batch_size] for i in range(0, len(ids), args.batch_size)]
    for n, batch in enumerate(batches, start=1):
        part = parts_dir / f"part_{n:05d}.csv"
        if part.exists():
            continue
        log(f"Batch {n}/{len(batches)} ({len(batch)} games)")
        df = bindings_to_df(fetch_batch(session, batch))
        if not df.empty:
            df.to_csv(part, index=False)
        time.sleep(args.delay)

    combine(parts_dir, out_path)


if __name__ == "__main__":
    main()
