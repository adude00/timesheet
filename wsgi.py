"""Gunicorn entry point: ``wsgi:application``.

Configuration comes from the environment (see .env.example).  A missing secret
or timezone aborts startup with an actionable message instead of starting a
half-configured server.
"""
import sqlite3
import sys

from app import ConfigError, make_app


def _load():
    try:
        return make_app()
    except ConfigError as exc:
        sys.stderr.write(f"timesheet: refusing to start: {exc}\n")
        raise SystemExit(1)
    except (OSError, sqlite3.Error) as exc:
        sys.stderr.write(f"timesheet: cannot open the database: {exc}\n")
        raise SystemExit(1)


application = _load()
