#!/usr/bin/env python3
"""
Dump IGDB (https://api-docs.igdb.com) to local JSON Lines files.

For every IGDB endpoint (games, companies, platforms, genres, covers, ...)
this writes one file, data/igdb/<endpoint>.jsonl, with one raw record per
line (all fields, references left as numeric IDs, exactly as IGDB returns them).

How it works:
  * Authenticates with Twitch (IGDB is owned by Twitch) using a client ID and
    secret, via the "client credentials" flow. Tokens are refreshed on 401.
  * Pages through each endpoint by ID (`where id > last; sort id asc;
    limit 500`) instead of using offsets, so paging is stable and resumable.
  * Stays under IGDB's limit of 4 requests/second and backs off on 429s.
  * Appends each page to disk immediately. Stop it any time (Ctrl+C) and run
    the same command again: it picks up after the last saved ID.
  * Asks IGDB for each endpoint's record count first, and reports at the end
    whether the number of records on disk matches.

Setup:
  1. Create a free Twitch account and enable two-factor authentication.
  2. At https://dev.twitch.tv/console/apps register an application
     (category: Application Integration, redirect URL: https://localhost, client type: Confidential).
  3. Copy its Client ID and generate a Client Secret.
  4. Put them in a file called .env next to this script (see .env.example),
     or export TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET.

Usage:
  python fetch_igdb.py                       # first download (resumable)
  python fetch_igdb.py --endpoints games genres platforms
  python fetch_igdb.py --list                # show known endpoints and exit

Keeping it up to date later (much faster than downloading everything again):
  python fetch_igdb.py --update              # fetch only records changed since last time
  python fetch_igdb.py --refresh games       # clean full re-download of one endpoint
  (or just run `python update.py`, which also rebuilds the DuckDB database)

  --update asks IGDB for records with updated_at newer than the last run and merges
  them into the existing .jsonl file (changed records are replaced, new ones added).
  Endpoints without an updated_at field are simply re-downloaded in full. IGDB does
  not report deletions, so --update warns when a file has more records than IGDB
  lists; run --refresh on that endpoint to clear them. When each endpoint was last
  fetched is remembered in data/igdb/state.json.

IGDB's data is free for non-commercial use under Twitch's terms; check
https://api-docs.igdb.com/#terms-of-service before redistributing it.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

API_URL = "https://api.igdb.com/v4"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"

PAGE_SIZE = 500          # IGDB's maximum
MIN_INTERVAL = 0.26      # seconds between requests (limit is 4/second)
OUT_DIR = Path("data/igdb")

# Known IGDB v4 endpoints. IGDB adds and retires endpoints from time to time;
# any that no longer exist are reported and skipped, not treated as fatal.
# `games` goes first because it is the one you most likely want.
ENDPOINTS = [
    "games",
    "game_time_to_beats",
    "external_games",
    "external_game_sources",
    "release_dates",
    "release_date_regions",
    "release_date_statuses",
    "game_statuses",
    "game_types",
    "game_modes",
    "game_engines",
    "game_engine_logos",
    "game_localizations",
    "game_versions",
    "game_version_features",
    "game_version_feature_values",
    "game_videos",
    "game_release_formats",
    "genres",
    "themes",
    "keywords",
    "player_perspectives",
    "multiplayer_modes",
    "collections",
    "collection_memberships",
    "collection_membership_types",
    "collection_relations",
    "collection_relation_types",
    "collection_types",
    "franchises",
    "companies",
    "company_logos",
    "company_websites",
    "involved_companies",
    "platforms",
    "platform_families",
    "platform_logos",
    "platform_types",
    "platform_versions",
    "platform_version_companies",
    "platform_version_release_dates",
    "platform_websites",
    "characters",
    "character_mug_shots",
    "character_genders",
    "character_species",
    "age_ratings",
    "age_rating_categories",
    "age_rating_content_descriptions",
    "age_rating_content_description_types",
    "age_rating_organizations",
    "alternative_names",
    "artworks",
    "covers",
    "screenshots",
    "websites",
    "website_types",
    "languages",
    "language_supports",
    "language_support_types",
    "regions",
    "network_types",
    "events",
    "event_logos",
    "event_networks",
    "popularity_types",
    "popularity_primitives",
]


def log(msg):
    print(msg, flush=True)


def load_dotenv(path=Path(".env")):
    """Minimal .env reader (KEY=value lines). Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class EndpointUnavailable(Exception):
    """IGDB says this endpoint does not exist or cannot be queried."""


class IGDB:
    def __init__(self, client_id, client_secret, api_url=API_URL, token_url=TOKEN_URL):
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_url = api_url.rstrip("/")
        self.token_url = token_url
        self.session = requests.Session()
        self.token = None
        self._last_request = 0.0

    def authenticate(self):
        r = self.session.post(
            self.token_url,
            params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        if r.status_code != 200:
            sys.exit(f"Twitch authentication failed ({r.status_code}): {r.text[:200]}\n"
                     "Check TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET.")
        self.token = r.json()["access_token"]

    def _throttle(self):
        wait = MIN_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def query(self, endpoint, body, retries=6):
        """POST an Apicalypse query to an endpoint and return the parsed JSON."""
        if self.token is None:
            self.authenticate()
        url = f"{self.api_url}/{endpoint}"
        for attempt in range(retries):
            self._throttle()
            try:
                r = self.session.post(
                    url,
                    data=body,
                    headers={
                        "Client-ID": self.client_id,
                        "Authorization": f"Bearer {self.token}",
                        "Accept": "application/json",
                    },
                    timeout=60,
                )
            except requests.RequestException as e:
                wait = 5 * (attempt + 1)
                log(f"  network error ({e.__class__.__name__}), retrying in {wait}s")
                time.sleep(wait)
                continue

            if r.status_code == 200:
                return r.json()
            if r.status_code == 401:
                log("  token rejected, requesting a new one")
                self.authenticate()
                continue
            if r.status_code == 429:
                wait = 2 * (attempt + 1)
                log(f"  rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            if r.status_code in (500, 502, 503, 504):
                wait = 5 * (attempt + 1)
                log(f"  server error {r.status_code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            if r.status_code in (400, 403, 404):
                raise EndpointUnavailable(f"{r.status_code}: {r.text[:200]}")
            r.raise_for_status()
        raise RuntimeError(f"{endpoint}: query failed after {retries} attempts")

    def count(self, endpoint):
        try:
            return int(self.query(f"{endpoint}/count", "").get("count"))
        except (EndpointUnavailable, TypeError, ValueError):
            return None


OVERLAP = 3600           # seconds of safety margin before the last run (clock skew, in-flight edits)
LEGACY_OVERLAP = 86400   # margin when there is no state.json entry (data from an older download)


def dumps(row):
    return json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


class State:
    """Remembers, per endpoint, when its last (or current) full fetch began."""

    def __init__(self, path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, endpoint):
        return self.data.get(endpoint)

    def set(self, endpoint, started, complete):
        self.data[endpoint] = {"started": started, "complete": complete}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        os.replace(tmp, self.path)


def prepare_file(path):
    """Return (last saved ID, record count) in `path`, repairing a half-written last line.

    A crash can leave a partial line at the end of the file. Anything after the
    final newline is cut off, so the file only ever contains whole records.
    """
    if not path.exists():
        return 0, 0
    with open(path, "rb+") as f:
        data = f.read()
        end = data.rfind(b"\n") + 1
        if end != len(data):
            f.truncate(end)
            data = data[:end]
    lines = data.splitlines()
    if not lines:
        return 0, 0
    return json.loads(lines[-1])["id"], len(lines)


def max_updated_at(path):
    best = 0
    with open(path, "rb") as f:
        for line in f:
            if line.strip():
                best = max(best, json.loads(line).get("updated_at") or 0)
    return best


def download(client, endpoint, path):
    """Fetch every record with an ID after the last one in `path`, appending as it goes.

    Resumable. Returns (saved, expected, finished), or None if the endpoint is unavailable.
    """
    last_id, saved = prepare_file(path)
    expected = client.count(endpoint)
    if expected is None:
        log(f"[{endpoint}] not available, skipping")
        return None
    if saved:
        log(f"[{endpoint}] resuming after id {last_id} ({saved:,} of {expected:,} saved)")
    else:
        log(f"[{endpoint}] {expected:,} records")

    finished = True
    with open(path, "ab") as out:
        while True:
            body = f"fields *; where id > {last_id}; sort id asc; limit {PAGE_SIZE};"
            try:
                rows = client.query(endpoint, body)
            except EndpointUnavailable as e:
                log(f"[{endpoint}] query rejected ({e}), keeping what was saved")
                finished = False
                break
            if not rows:
                break
            for row in rows:
                out.write(dumps(row))
            out.flush()
            os.fsync(out.fileno())
            saved += len(rows)
            last_id = rows[-1]["id"]
            log(f"  {endpoint}: {saved:,}/{expected:,}")
            if len(rows) < PAGE_SIZE:
                break
    return saved, expected, finished


def full_endpoint(client, state, endpoint):
    """Default mode: download (or resume downloading) one endpoint in full."""
    path = OUT_DIR / f"{endpoint}.jsonl"
    if state.get(endpoint) is None and not path.exists():
        state.set(endpoint, int(time.time()), False)
    result = download(client, endpoint, path)
    if result is None:
        return None
    saved, expected, finished = result
    st = state.get(endpoint)
    if st and not st["complete"] and finished:
        state.set(endpoint, st["started"], True)
    return {"total": saved, "expected": expected}


def refresh_endpoint(client, state, endpoint):
    """Clean full re-download into a temp file, swapped in only when complete."""
    path = OUT_DIR / f"{endpoint}.jsonl"
    tmp = path.with_name(path.name + ".new")
    tmp.unlink(missing_ok=True)
    started = int(time.time())
    result = download(client, endpoint, tmp)
    if result is None:
        tmp.unlink(missing_ok=True)
        return None
    saved, expected, finished = result
    if not finished:
        tmp.unlink(missing_ok=True)
        log(f"[{endpoint}] refresh did not finish; kept the existing file")
        return None
    os.replace(tmp, path)
    state.set(endpoint, started, True)
    return {"total": saved, "expected": expected, "note": "refreshed in full"}


def fetch_changes(client, endpoint, since):
    """Every record whose updated_at is after `since`, keyed by ID."""
    changes, last_id = {}, 0
    while True:
        body = (f"fields *; where updated_at > {since} & id > {last_id}; "
                f"sort id asc; limit {PAGE_SIZE};")
        rows = client.query(endpoint, body)
        if not rows:
            break
        for row in rows:
            changes[row["id"]] = row
        last_id = rows[-1]["id"]
        log(f"  {endpoint}: {len(changes):,} changed so far")
        if len(rows) < PAGE_SIZE:
            break
    return changes


def merge(path, changes):
    """Rewrite `path` with `changes` applied: replaced by ID, new IDs inserted in order.

    Written to a temp file and swapped in at the end, so a crash cannot corrupt the data.
    Returns the new record count.
    """
    pending = sorted(changes)
    i = total = 0
    tmp = path.with_name(path.name + ".merge")
    with open(path, "rb") as src, open(tmp, "wb") as out:
        for line in src:
            if not line.strip():
                continue
            rid = json.loads(line)["id"]
            while i < len(pending) and pending[i] < rid:
                out.write(dumps(changes[pending[i]]))
                i += 1
                total += 1
            if i < len(pending) and pending[i] == rid:
                out.write(dumps(changes[rid]))
                i += 1
            else:
                out.write(line if line.endswith(b"\n") else line + b"\n")
            total += 1
        for rid in pending[i:]:
            out.write(dumps(changes[rid]))
            total += 1
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
    return total


def update_endpoint(client, state, endpoint):
    """--update: fill in any missing tail, then merge in records changed since last time."""
    path = OUT_DIR / f"{endpoint}.jsonl"
    if not path.exists() or path.stat().st_size == 0:
        return full_endpoint(client, state, endpoint)

    last_id, _ = prepare_file(path)
    with open(path, "rb") as f:
        last_record = json.loads(f.read().splitlines()[-1])
    if "updated_at" not in last_record:
        log(f"[{endpoint}] has no updated_at, re-downloading in full")
        return refresh_endpoint(client, state, endpoint)

    st = state.get(endpoint)
    started = int(time.time())
    result = download(client, endpoint, path)       # new records at the end, and a resume if interrupted
    if result is None:
        return None
    saved, expected, finished = result
    if not finished:
        return {"total": saved, "expected": expected, "note": "incomplete, run --update again"}

    since = (st["started"] - OVERLAP) if st else (max_updated_at(path) - LEGACY_OVERLAP)
    try:
        changes = fetch_changes(client, endpoint, since)
    except EndpointUnavailable:
        log(f"[{endpoint}] can't filter by updated_at, re-downloading in full")
        return refresh_endpoint(client, state, endpoint)
    total = merge(path, changes) if changes else saved
    state.set(endpoint, started, True)
    return {"total": total, "expected": expected, "changed": len(changes)}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoints", nargs="+", metavar="NAME",
                    help="only handle these endpoints (default: all known)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true",
                      help="fetch only records changed since the last run and merge them in")
    mode.add_argument("--refresh", action="store_true",
                      help="re-download the chosen endpoints from scratch (clears deleted records)")
    ap.add_argument("--list", action="store_true", help="list known endpoints and exit")
    ap.add_argument("--api-url", default=API_URL, help=argparse.SUPPRESS)
    ap.add_argument("--token-url", default=TOKEN_URL, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.list:
        print("\n".join(ENDPOINTS))
        return

    load_dotenv()
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Missing credentials. Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET "
                 "(see .env.example and the setup steps in this script's header).")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state = State(OUT_DIR / "state.json")
    client = IGDB(client_id, client_secret, args.api_url, args.token_url)
    client.authenticate()

    handler = refresh_endpoint if args.refresh else update_endpoint if args.update else full_endpoint
    summary = []
    try:
        for endpoint in args.endpoints or ENDPOINTS:
            summary.append((endpoint, handler(client, state, endpoint)))
    except KeyboardInterrupt:
        log("\nInterrupted. Run the same command again to resume.")
        sys.exit(130)

    log("\nSummary")
    warnings = []
    for endpoint, r in summary:
        if r is None:
            log(f"  {endpoint:<40} skipped (unavailable or unfinished)")
            continue
        extra = []
        if "changed" in r:
            extra.append(f"{r['changed']:,} changed or new")
        if r.get("note"):
            extra.append(r["note"])
        log(f"  {endpoint:<40} {r['total']:>10,}  {'; '.join(extra)}")
        diff = r["total"] - r["expected"]
        if diff > 0:
            warnings.append(f"{endpoint}: {diff:,} more records than IGDB lists (probably deleted "
                            f"upstream). Fix with: python fetch_igdb.py --refresh --endpoints {endpoint}")
        elif diff < 0:
            warnings.append(f"{endpoint}: {-diff:,} fewer records than IGDB lists. Records can be "
                            "added while a long run is going; run --update again to pick them up.")
    if warnings:
        log("\nCheck these:")
        for w in warnings:
            log(f"  - {w}")


if __name__ == "__main__":
    main()
