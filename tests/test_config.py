"""Slice 7: startup configuration must fail loudly, never half-work."""
import os
import stat
import tempfile

import pytest

from app import ConfigError, make_app
from timeutil import validate_timezone

REQUIRED = ("APP_TIMEZONE", "SECRET_KEY", "DATABASE_PATH", "TRUST_PROXY", "COOKIE_SECURE")


@pytest.fixture
def clean_env(monkeypatch):
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)


def test_missing_timezone_is_reported(clean_env):
    with pytest.raises(ConfigError) as raised:
        make_app(secret="x" * 32, database_path=tempfile.mktemp(suffix=".sqlite3"))
    assert "APP_TIMEZONE" in raised.value.args[0]
    assert "Europe/Rome" in raised.value.args[0]


def test_missing_secret_is_reported(clean_env):
    with pytest.raises(ConfigError) as raised:
        make_app(timezone="Europe/Rome", database_path=tempfile.mktemp(suffix=".sqlite3"))
    message = raised.value.args[0]
    assert "SECRET_KEY" in message
    assert "secrets.token_urlsafe" in message


def test_broken_timezone_is_reported(clean_env):
    with pytest.raises(ConfigError) as raised:
        make_app(timezone="Mars/Olympus_Mons", secret="x" * 32,
                 database_path=tempfile.mktemp(suffix=".sqlite3"))
    assert "Mars/Olympus_Mons" in raised.value.args[0]


def test_unusable_database_is_reported(clean_env):
    blocker = tempfile.mkstemp(suffix=".not-a-directory")[1]
    missing = f"{blocker}/deeper.sqlite3"
    with pytest.raises(ConfigError) as raised:
        make_app(timezone="Europe/Rome", secret="x" * 32, database_path=missing)
    assert missing in raised.value.args[0]


def test_debug_mode_is_never_enabled(clean_env):
    app = make_app(timezone="Europe/Rome", secret="x" * 32,
                   database_path=f"{tempfile.mkdtemp()}/ok.sqlite3")
    assert app.config["DEBUG"] is False
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["SESSION_COOKIE_SECURE"] is True  # HTTPS by default
    assert app.config["MAX_CONTENT_LENGTH"] == 16 * 1024
