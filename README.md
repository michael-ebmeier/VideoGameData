# VideoGameData

A resumable dump of [IGDB](https://www.igdb.com) (the Internet Game Database) to local
files, plus a loader that puts it into DuckDB for SQL queries.

## Setup

1. Create a Twitch account (two-factor authentication must be on), then register an app at
   <https://dev.twitch.tv/console/apps> (category *Application Integration*, redirect URL
   `https://localhost`, which is never used; keep *Confidential*). Copy the **Client ID** and generate a **Client Secret**.
2. Install dependencies and add your credentials:

   ```bash
   python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env        # then edit .env with your Client ID and Secret
   ```

## Download

```bash
python fetch_igdb.py                              # everything
python fetch_igdb.py --endpoints games genres     # just some endpoints
python fetch_igdb.py --list                       # list the endpoints it knows
```

Output is one file per endpoint, `data/igdb/<endpoint>.jsonl` (one raw record per line,
references left as numeric IDs). The script stays within IGDB's 4 requests/second limit and
saves after every page, so you can stop it (Ctrl+C) and re-run the same command to resume.
When it finishes it compares each file's record count with IGDB's reported total.

## Keeping it up to date

After the first full download, you don't need to download everything again. One command
fetches only what IGDB changed since last time, merges it into your files, and rebuilds the
database:

```bash
python update.py
```

(Stop the explorer first; it holds the database open.) What it does:

- `python fetch_igdb.py --update` asks IGDB for records with an `updated_at` newer than your
  last run and merges them into the existing `.jsonl` files: changed records are replaced, new
  ones added. It also finishes any interrupted download. Small tables without `updated_at` are
  just re-downloaded. When each table was last fetched is kept in `data/igdb/state.json`.
- Each run re-checks the last hour as a safety margin, so the "changed or new" count is never
  exactly zero. That's expected.
- **Deleted records:** IGDB doesn't announce deletions, so `--update` can't see them. It warns
  when a file has more records than IGDB lists. Fix that table with a clean re-download, which
  swaps the file in only when it finishes:
  ```bash
  python fetch_igdb.py --refresh --endpoints games
  ```
  Doing this for `games` every few months is plenty.

## Query

```bash
python load_duckdb.py        # builds data/igdb.duckdb, one table per endpoint
```

```python
import duckdb
con = duckdb.connect("data/igdb.duckdb")
con.sql("""
    select name, to_timestamp(first_release_date) as released, total_rating
    from games
    where total_rating_count > 500
    order by total_rating desc limit 20
""").show()
```

Fields that point at other records (`genres`, `platforms`, `cover`, ...) are IDs; join them
to the matching table (see the header of `load_duckdb.py`). `data/` is gitignored.

## Explore in the browser

```bash
python explorer/server.py        # opens http://127.0.0.1:8000
```

A small local web app over `data/igdb.duckdb` (run `load_duckdb.py` first): a cover grid with
search, genre/platform/year filters and sorting; a page per game (rating, platforms, developers,
screenshots, similar games); and a **Stats** tab with a few charts. Everything runs on your
machine and the server only listens on `127.0.0.1`. Cover art loads from IGDB's image host in your
browser, so covers appear only once the `covers` endpoint has been downloaded and you're online;
otherwise you get title placeholders. Options: `--port 9000`, `--no-open`, `--db PATH`.

Stop the server with Ctrl+C. Rebuild the database (`load_duckdb.py`) only while it is stopped.

## Default scope and "Wild west"

By default every list, count and chart covers only games with an **ESRB age rating** (EC, E, E10+, T
or M), which keeps out unrated shovelware and adult titles and makes the statistics more meaningful.
ESRB's "Rating Pending" and "Adults Only" don't count, and ratings from other boards (PEGI, CERO, …)
don't either. A **Wild west** switch in the header (with a confirmation) shows every game instead; the
choice is remembered in the browser, and the Stats page and Relationships analysis follow it. A game
outside the scope can still be opened by direct link, with a note. The scope is worked out from
IGDB's own age-rating tables (`age_ratings`, `age_rating_organizations`, `age_rating_categories`); the
rule lives in `Store._build_scope` in `explorer/server.py`. If the data has no age ratings, the switch
doesn't appear and everything is shown.

## Relationships (Stats page)

The Stats page ends with a **Relationships** section. A correlation matrix shows how rating, time to
beat, release year, number of ratings and number of platforms move together (Spearman's rank
correlation by default, or Pearson's). Below it you can compare any two measures: pick the X and Y
axes, or one of the preset questions. The chart follows the choice: two measures give a scatter plot
with a trend line, a genre or platform against a measure gives bars (average or median, optionally
as the difference from all games), and two categories give a grid of how many games have both. The
numbers are calculated in the browser (`explorer/static/analysis.js`) from the games table, so this
needs no extra data.

A **Trim extremes** switch (on by default) leaves out the lowest and highest 1% of time to beat and
of number of ratings, so a few extreme or mistaken entries (a game that "takes" 100,000 hours) don't
stretch the axes and flatten the pattern. It applies to the matrix, the scatter plots and the bar
charts alike, the notes say exactly which range is shown and how many games were left out, and
turning it off (or `trim=0` in the page address) shows everything. Rating and release year are not
trimmed, because their ranges are bounded and trimming would drop real games.

## Putting it on a website

The explorer can be built as a **static website**: plain files you can host anywhere, with no
Python app or database on the host and a nightly cron job that refreshes it.

```bash
python build_static.py --out site                     # build it
python -m http.server 8000 --directory site           # try it at http://127.0.0.1:8000
python update.py --static-out ~/games.example.com     # the nightly job: update data + rebuild
```

`DEPLOY.md` walks through hosting it on cPanel shared hosting at `games.brutalc.com`, including
the cron job.

## Terms

IGDB data is free for non-commercial use under Twitch's terms. Check
<https://api-docs.igdb.com/#terms-of-service> before redistributing the data.
