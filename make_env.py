"""Create the .env file that fetch_igdb.py reads, prompting for the secret.

Run from the repo folder:  python make_env.py
The Client ID is not secret. The Client Secret is typed hidden and written only
to .env, which is gitignored. Get a secret at https://dev.twitch.tv/console/apps
(Manage -> New Secret; Twitch shows it once).
"""
from getpass import getpass
from pathlib import Path

CLIENT_ID = "xq3lbm20pbz61b0uv7vtr00156kqaf"

secret = getpass("Paste your Twitch Client Secret (input is hidden): ").strip()
if not secret:
    raise SystemExit("No secret entered, nothing written.")

Path(".env").write_text(
    f"TWITCH_CLIENT_ID={CLIENT_ID}\nTWITCH_CLIENT_SECRET={secret}\n",
    encoding="utf-8",
)
print("Wrote .env in", Path(".env").resolve())
