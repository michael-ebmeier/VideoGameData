# Putting the explorer on your website (games.brutalc.com)

The website is **static**: plain files (a page, a script, and some data files) that any web
server can host. Nothing runs on the host while people browse: no Python app, no Passenger, no
database. Visitors' browsers do the searching themselves.

A **nightly cron job** on your host keeps it fresh:

1. `fetch_igdb.py --update` downloads what IGDB changed in **all** endpoints and merges it into
   your `.jsonl` files.
2. `build_static.py` turns the tables the explorer uses into a new version of the data files and
   switches the site over to it. Visitors never see a half-updated site, and one that already has
   the page open keeps working (the previous version is kept for a day).

## Before you publish: terms
IGDB's data is free for **non-commercial** use under Twitch's terms, and a public site is
redistribution. Read <https://api-docs.igdb.com/#terms-of-service>. The explorer shows an
"IGDB.com" credit and links each game back to IGDB; keep both. No ads or paywall on top of it.

## What your host needs
- **Cron jobs** that can run for ~30+ minutes (the nightly job is mostly waiting on IGDB).
- **Python 3.9+** with `requests` and `duckdb` in a virtualenv (you already have `~/igdb-venv`).
- **~10 GB free disk.** The `.jsonl` files are 1.5–3 GB; the website itself is under 100 MB.
- **Memory:** in a test with 350,000 synthetic games, the build took ~50 seconds and peaked at
  about 650 MB with `DUCKDB_MEMORY_LIMIT=512MB DUCKDB_THREADS=1`. Real data will differ.
- Outbound HTTPS to `api.igdb.com` and `id.twitch.tv`.

## Setup (once)

### 0. Stop using the Python app
If you registered a Python app for `games.brutalc.com` in cPanel's **Application Manager**, remove
that registration. It isn't needed, and its leftover lines in `~/games.brutalc.com/.htaccess` could
interfere. If that `.htaccess` still has `Passenger…` lines afterwards, delete the file; the build
creates a correct one. (Leave the subdomain itself; the build writes into its folder.)

### 1. Code and packages
In the server terminal:
```bash
cd ~/igdb-explorer && git pull
source ~/igdb-venv/bin/activate
pip install -r requirements.txt
```

### 2. Twitch credentials (server only)
```bash
python make_env.py        # paste the Client Secret at the hidden prompt
chmod 600 .env
```

### 3. Upload your data
From your PC, upload `data\igdb\*.jsonl` to `~/igdb-explorer/data/igdb/` (create the folders). Only the
`.jsonl` files are needed; **don't** upload `igdb.duckdb`. SFTP resumes interrupted transfers, so a
multi-GB upload is fine.

### 4. First build
```bash
cd ~/igdb-explorer && source ~/igdb-venv/bin/activate
DUCKDB_MEMORY_LIMIT=512MB DUCKDB_THREADS=1 python build_static.py --out ~/games.brutalc.com
```
Open <https://games.brutalc.com>. (The first visit downloads the games table, a few MB, and later
visits reuse it from the browser cache.)

### 5. Schedule the nightly job
cPanel → **Cron Jobs** → add a daily job. Use absolute paths and an odd minute (server time zone):
```
17 4 * * * cd /home3/mikeebme/igdb-explorer && DUCKDB_MEMORY_LIMIT=512MB DUCKDB_THREADS=1 /home3/mikeebme/igdb-venv/bin/python update.py --static-out /home3/mikeebme/games.brutalc.com --monthly-refresh > data/update.log 2>&1
```
A lock stops overlapping runs. If any step fails, the version that is live stays live.
`--monthly-refresh` re-downloads `games` from scratch on the 1st of each month to drop records
IGDB deleted (IGDB doesn't announce deletions). `data/update.log` holds the last run.

### 6. Check it worked
The next morning:
```bash
tail -20 ~/igdb-explorer/data/update.log
```
You want `done in …s` at the end. Also search for something added recently. `data/manifest.json`
on the site shows the build time.

### 7. Link to it
Add a link to `games.brutalc.com` from your tools page on brutalc.com.

## Updating the tool itself
When we change the explorer here:
```bash
cd ~/igdb-explorer && git pull
source ~/igdb-venv/bin/activate
python build_static.py --out ~/games.brutalc.com --frontend-only
```
`--frontend-only` refreshes just the page, its scripts, `share.php` and the `.htaccess` rules, so it takes a second. I'll say when a change
also needs the data files rebuilt; then run the same command without `--frontend-only`.

**Link previews (Discord and others) need one full rebuild.** The detail files gain each game's name
and cover, which the preview page reads. Until you rebuild, shared links still work but show a generic
title and no picture. See "Link previews" below.

**The ESRB default scope and the "Wild west" switch need one full rebuild.** They add a column to the
games table and extra files (`facets_all.json`, `stats_all.json`), so after pulling this, run the build
once **without** `--frontend-only`. The build prints how many games fall in the default scope (for
example `default scope: 38,000 of 350,000 games have an ESRB rating`), so you can sanity-check it. Until
you rebuild, the site keeps working but shows everything and has no switch. The nightly job picks it up
on its own either way.

**The Relationships charts need no data rebuild.** They are calculated in the visitor's browser
from the same games table the site already has, so `--frontend-only` is enough to get them.

**Time to beat needs one full rebuild.** The time-to-beat feature adds new columns to the data
files, so after pulling it, run the build once **without** `--frontend-only` (the same command as
step 4). Until then the site keeps working but hides the time-to-beat controls and charts. The
nightly job picks it up on its own either way.

## Clean links and link previews (Discord and others)
The site uses real addresses: `https://games.brutalc.com/game/178026`, `/stats`, `/?genre=3&sort=rating`,
with no `#` in them. Old links with a `#` (`/#/game/178026`) still work: the page converts them to the clean
form as it opens. That clean address is also the one to share: the address bar always shows it, and each
game page has a **Copy share link** button that copies it.

For chat apps that is what makes previews possible. Discord fetches the link, doesn't run JavaScript, and
reads the "Open Graph" tags in the page, so the page for `/game/178026` has to carry that game's tags.
`share.php` does this: it returns the normal app page with the tags for that game swapped in (title and
year, rating, time to beat, genres, platforms, a short summary, and the **cover** as the image; a game with
no cover uses its first screenshot instead). People get the app as usual; Discord gets the preview card.

- **Needs PHP**, which cPanel hosts such as Bluehost run by default. The build writes `share.php` and the
  rewrite rules for `/game/123` and `/stats` into a marked section of the site's `.htaccess`. Anything
  else in that file (for instance cPanel's PHP settings) is left alone.
- **If PHP is off for the subdomain**, build with `--no-php --share-pages 3000`. That skips `share.php`;
  instead the 3,000 most-rated games get a small preview page of their own (`game-pages/`, about 12 MB)
  that passes people on to the app, and every other game opens the plain app with no preview. The share
  button is hidden in this mode.
- **Under a path** (for example `yoursite.com/games/`), build with `--base /games/`. The hosting rewrite
  rules assume the site is at the top of its folder.
- **The front page** has its own preview tags too.
- **Discord caches previews.** After changing anything, an old link may keep its old preview for a while,
  so test with a game you haven't posted before, ideally in a private channel.
- Check what a link returns with `curl -A "Discordbot/2.0" https://games.brutalc.com/game/178026`; look for
  the `og:title`, `og:description` and `og:image` lines in the page's `<head>`.

## Try it on your own computer first
```powershell
python build_static.py --out site
python -m http.server 8000 --directory site
```
then open <http://127.0.0.1:8000>. (Opening `index.html` by double-clicking won't work; it
needs a web server.)

## If the host can't run the build
If the nightly job gets "Killed" or runs out of time, build somewhere else and upload the result:
run `python update.py --static-out site` on your PC (or a small VPS), then upload the contents of
the `site` folder to `~/games.brutalc.com` by SFTP. The finished site is under 100 MB, so this is
a light upload. Upload `data/<new version>/` first and `data/manifest.json` last.

## Troubleshooting
- **"Index of /" listing:** the build hasn't run, or wrote to a different folder. Check that
  `~/games.brutalc.com/index.html` exists.
- **Page loads but says "Something went wrong":** reload. If it persists, check that
  `data/manifest.json` exists on the site and the version folder it names is present.
- **"This browser is too old":** the data is read with a browser feature from 2023 or later.
- **Cron produced no `done` line:** read `data/update.log`. "Killed" means too little memory:
  lower `DUCKDB_MEMORY_LIMIT`, or use the section above.
- **Covers are blank:** they load from IGDB in the visitor's browser, so they need the `covers`
  endpoint in your data and an internet connection on the visitor's side.

## Appendix: running it as a Python app instead
`python explorer/server.py` runs the same explorer locally against a DuckDB database
(`python load_duckdb.py` builds it), and `passenger_wsgi.py` lets hosts that support Python apps
serve it. It's no longer needed for this site but works on a VPS (e.g. `gunicorn
passenger_wsgi:application` behind nginx) if you ever want server-side features.
