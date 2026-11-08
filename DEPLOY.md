# Putting the explorer on your website (games.brutalc.com)

This guide assumes shared hosting with cPanel and a **subdomain** (`games.brutalc.com`), linked
from your main site. A subdomain is separate from your main site's `.htaccess`, so it can't
interfere with WordPress or other tools. (The explorer can also run under a path such as
`/games`; see the note at the end of Plan A.)

**Read "Will your host cope?" first**: the
explorer itself is light, but the daily refresh is not, and that is where shared hosting
often fails.

## Before you publish: terms
IGDB's data is free for **non-commercial** use under Twitch's terms, and a public site makes it
redistribution. Read <https://api-docs.igdb.com/#terms-of-service>. The explorer shows an
"IGDB.com" credit and links each game back to IGDB; keep both. No ads or paywall on top of it.

## Will your host cope?
Check each of these in cPanel (or ask your host's support, who can answer them in a minute):

| Need | Why |
|---|---|
| **Setup Python App** exists in cPanel (Python 3.9+; 3.11 or 3.12 is the safe pick) | runs the explorer |
| **Terminal or SSH** access | install packages, run scripts |
| **Cron Jobs** that can run for 30+ minutes | the daily refresh |
| **~2 GB or more of RAM** for your account | rebuilding the database |
| **~10 GB free disk** | the data is 1.5–3 GB of text plus a ~1 GB database |
| Outbound HTTPS to `api.igdb.com` and `id.twitch.tv` | fetching updates |

Typical trouble: shared hosts kill processes that use too much memory or CPU. The explorer
itself is fine, but rebuilding the database from a multi-GB dump may get killed. If that
happens, use Plan B or C at the bottom. You don't have to find out in advance: try it, and
read the log.

## Plan A: everything on the shared host

### 1. Get the code onto the server
In cPanel's Terminal (or SSH). Put it **outside** `public_html`:
```bash
cd ~
git clone https://github.com/michael-ebmeier/VideoGameData igdb-explorer
```

### 2. Create the subdomain and the Python app
First create the subdomain: cPanel → **Domains** → **Create A New Domain** → `games.brutalc.com`.
Leave its document root as the default empty folder; the app does not live there.

Then, in **Setup Python App** (on some Bluehost accounts: **Application Manager**) → **Create
Application**:
- **Python version:** 3.11 or 3.12
- **Application root:** `igdb-explorer`
- **Domain / Application URL:** `games.brutalc.com`, with no path after it
- **Application startup file:** `passenger_wsgi.py`
- **Application Entry point:** `application`

After saving, cPanel shows a command like `source /home/USER/virtualenv/igdb-explorer/3.11/bin/activate`.
Run it in the terminal, then install the packages:
```bash
cd ~/igdb-explorer
pip install -r requirements.txt
```

### 3. Add your Twitch credentials (on the server only)
```bash
python make_env.py        # paste the Client Secret when asked
chmod 600 .env
```
`.env` stays in the app folder, outside the web root, and is never committed to git.

### 4. Load the initial data
The first full download takes hours, so **do it on your own computer** (you already have it)
and upload `data/igdb/` (the `.jsonl` files) to `~/igdb-explorer/data/igdb/` using cPanel's
File Manager or SFTP (zip it first; it's large). Then on the server:
```bash
cd ~/igdb-explorer
DUCKDB_MEMORY_LIMIT=1GB DUCKDB_THREADS=1 python load_duckdb.py
```
Then restart the app: the **Restart** button in Setup Python App, or `touch tmp/restart.txt`
in the app folder. Visit <https://games.brutalc.com>. A new subdomain's HTTPS certificate
(AutoSSL) can take a few minutes to appear. (No `state.json` yet? That's fine: the
first update copes and creates it.)

### 5. Schedule the daily refresh
cPanel → **Cron Jobs** → add a daily job. Pick an off-peak, odd minute (your server's time zone):
```
17 4 * * * cd /home/USER/igdb-explorer && DUCKDB_MEMORY_LIMIT=1GB DUCKDB_THREADS=1 /home/USER/virtualenv/igdb-explorer/3.11/bin/python update.py --monthly-refresh > data/update.log 2>&1
```
Replace `USER` and the Python path with what cPanel showed in step 2. What it does each day:
1. downloads only what IGDB changed in **all** endpoints and merges it in,
2. builds a new database and swaps it in. The running site picks it up by itself; no restart,
3. on the 1st of each month, first re-downloads `games` from scratch to drop records IGDB deleted.

A lock stops two runs overlapping, and if anything fails the old database stays in place.
`data/update.log` holds the last run's timestamped output.

### 6. Check it worked
Next morning:
```bash
tail -20 ~/igdb-explorer/data/update.log
```
You want `done in …s` at the end. Also try a search at games.brutalc.com for something added
recently.

### Updating the code later
```bash
cd ~/igdb-explorer && git pull
touch tmp/restart.txt
```

**Running under a path instead** (`brutalc.com/games`): the app supports it. Enter the path as
the application's base URL when creating it. The catch is that the host adds rules to the main
site's `.htaccess`, the same file WordPress uses, so there is a small chance of a conflict. A
subdomain avoids that, which is why this guide uses one.

## Plan B (recommended if Plan A struggles): a small VPS
A $5/month Linux VPS runs this comfortably. Same steps, but run the app with
`gunicorn passenger_wsgi:application` behind nginx, and point `games.brutalc.com` at it
(a DNS record to the VPS's address). To keep a `/games` path instead, put your domain
on Cloudflare and route `brutalc.com/games*` to the VPS with a Worker. Ask me and I'll write
the exact config.

## Plan C: build elsewhere, serve on the shared host
Run `python update.py` on your own computer (or a VPS), then upload only the new
`data/igdb.duckdb` to the host under a temporary name and rename it into place (the rename is
atomic, and the site picks it up on the next request). The shared host then only serves
pages, which is the easy part. Upload `data/igdb.duckdb.new`, then:
```bash
mv data/igdb.duckdb.new data/igdb.duckdb
```

## Troubleshooting
- **500 error / blank page:** check `stderr.log` in the app folder (File Manager).
- **"No module named duckdb":** you installed into the wrong Python. Re-run the
  `source …/activate` command from step 2 first.
- **"The database is not available yet":** `data/igdb.duckdb` doesn't exist yet (step 4).
- **Cron job produced no `done`:** the host probably killed it. Check `update.log` for the last
  line and see Plan B or C.
- **Covers missing:** the covers load from IGDB in the visitor's browser, so they need the
  `covers` endpoint downloaded and an internet connection on the visitor's side.
