# VideoGameData

A resumable dump of [IGDB](https://www.igdb.com) (the Internet Game Database) to local
files, plus a loader that puts it into DuckDB for SQL queries.

## Setup

1. Create a Twitch account (two-factor authentication must be on), then register an app at
   <https://dev.twitch.tv/console/apps> (category *Application Integration*, redirect URL
   `http://localhost`). Copy the **Client ID** and generate a **Client Secret**.
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

## Terms

IGDB data is free for non-commercial use under Twitch's terms. Check
<https://api-docs.igdb.com/#terms-of-service> before redistributing the data.
