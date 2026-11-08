"""WSGI entry point for hosts that run Python apps through Passenger (e.g. cPanel's
"Setup Python App"). Other WSGI servers can use it too: `gunicorn passenger_wsgi:application`.
Set IGDB_DB in the environment to use a database somewhere other than data/igdb.duckdb."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "explorer"))

from server import App, DEFAULT_DB  # noqa: E402

application = App(os.environ.get("IGDB_DB", DEFAULT_DB))
