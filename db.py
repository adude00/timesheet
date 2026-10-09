"""SQLite storage for the timesheet.

All timestamps are UTC epoch seconds (integers).  Conversion to local time
happens only at input/display and at calendar-day/month reporting boundaries.

Integrity rules are enforced by the database itself, not only by the UI:

* CHECK(end_utc > start_utc) rejects empty or backwards intervals
* a partial unique index (CREATE UNIQUE INDEX one_open ON intervals (1)
  WHERE end_utc IS NULL) makes a second open interval impossible, even when
  two browser tabs race
* every write runs inside one serialized BEGIN/COMMIT transaction, so overlap
  checks cannot interleave with another writer
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from clock import server_clock

SCHEMA_VERSION = 1
BUSY_TIMEOUT_S = 10.0

SQL_INIT = (
    """
    CREATE TABLE meta (
      schema_version INTEGER PRIMARY KEY
    );
    """,
    """
    CREATE TABLE intervals (
      id          INTEGER PRIMARY KEY,
      start_utc   INTEGER NOT NULL,
      end_utc     INTEGER,
      created_utc INTEGER NOT NULL,
      updated_utc INTEGER NOT NULL,
      CHECK( end_utc IS NULL OR end_utc > start_utc )
    );
    """,
    # Partial unique index: at most one row may have end_utc IS NULL.
    "CREATE UNIQUE INDEX one_open ON intervals (1) WHERE end_utc IS NULL;",
    """
    CREATE TABLE ops (
      token       TEXT PRIMARY KEY,
      kind        TEXT NOT NULL,
      interval_id INTEGER,
      result      TEXT NOT NULL,
      created_utc INTEGER NOT NULL
    );
    """,
)

# How many times a write transaction is retried when another process holds the
# SQLite write lock (busy timeout applies between attempts as well).
WRITE_ATTEMPTS = 5
WRITE_RETRY_SLEEP_S = 0.05

# Raised when storage is unusable; the UI shows a short version, the log the long one.
StorageError = type("StorageError", (Exception,), {})

# Migration SQL keyed by the schema version it upgrades from.
SQL_MIGRATIONS: dict[int, tuple[str, ...]] = {}


class Db:
    """One SQLite file holding the single user's work intervals."""

    def __init__(self, path):
        self.path = os.fspath(path)
        # Serializes writers inside this process; SQLite's busy timeout covers
        # writers in other processes (extra gunicorn workers).
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def _ensure_dir(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Touch the file here: sqlite3.connect is lazy, so a path that cannot be
        # opened must fail at startup rather than on the first punch.
        with open(self.path, "ab"):
            pass

    def _now(self, now=None):
        """Server clock, unless a fixed instant is supplied (tests)."""
        return int(now) if now is not None else server_clock.now()

    def read_conn(self):
        return sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S)

    # -------------------------------------------------------------- lifecycle
    def init(self):
        """Create the file and schema when missing. Idempotent.

        An existing populated database is never recreated.
        """
        self._ensure_dir()
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S)
        try:
            version = self._read_schema_version(conn)
            if version is None:
                self._create(conn)
            elif version != SCHEMA_VERSION:
                self._migrate(conn, version)
        finally:
            conn.close()

    def _read_schema_version(self, conn):
        try:
            row = conn.execute("SELECT schema_version FROM meta LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None

    def _create(self, conn):
        conn.execute("BEGIN")
        try:
            for stmt in SQL_INIT:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO meta (schema_version) VALUES (?)", (SCHEMA_VERSION,)
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _migrate(self, conn, version):
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"stored schema version {version} is newer than this application "
                f"supports (version {SCHEMA_VERSION})"
            )
        conn.execute("BEGIN")
        try:
            for from_version in range(version, SCHEMA_VERSION):
                for stmt in SQL_MIGRATIONS[from_version]:
                    conn.execute(stmt)
                conn.execute("DELETE FROM meta WHERE schema_version = ?", (from_version,))
                conn.execute(
                    "INSERT INTO meta (schema_version) VALUES (?)", (from_version + 1,)
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------- reads
    def fetch_all(self):
        conn = self.read_conn()
        try:
            return conn.execute(
                "SELECT id, start_utc, end_utc, created_utc, updated_utc "
                "FROM intervals ORDER BY start_utc, id"
            ).fetchall()
        finally:
            conn.close()

    def fetch_open(self):
        conn = self.read_conn()
        try:
            return conn.execute(
                "SELECT id, start_utc FROM intervals WHERE end_utc IS NULL"
            ).fetchone()
        finally:
            conn.close()

    # ------------------------------------------------------------------ writes
    def write(self, kind, token, action):
        """Run action(conn) inside one atomic, serialized write transaction.

        The whole BEGIN/COMMIT block is serialized: in-process writers wait on
        ``self._lock``, other processes wait on SQLite's busy timeout.  Overlap
        checks therefore run against exactly the rows this transaction will
        replace.
        """
        with self._lock:
            last_error = None
            for _ in range(WRITE_ATTEMPTS):
                conn = sqlite3.connect(
                    self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None
                )
                try:
                    conn.execute("BEGIN")
                    result = action(conn)
                    conn.execute("COMMIT")
                    return result
                except sqlite3.OperationalError as exc:
                    if "database is locked" not in str(exc):
                        raise
                    last_error = exc
                    time.sleep(WRITE_RETRY_SLEEP_S)
                finally:
                    conn.close()
            raise StorageError(
                f"timed out waiting for the database write lock at {self.path}: "
                f"{last_error}"
            )

    def _replay(self, conn, token, kind):
        """Idempotency guard: a replayed form submission returns the old result."""
        if not token:
            return None
        row = conn.execute(
            "SELECT kind, result FROM ops WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return None
        if row[0] != kind:
            return {"ok": False, "error": "token_reused", "duplicate": True}
        result = json.loads(row[1])
        result["duplicate"] = True
        return result

    def _record(self, conn, token, kind, interval_id, result, now):
        """Remember a processed token so replaying that form does nothing new.

        Tokens older than a day are dropped: no bookmarked form can be that old
        and the table must not grow without bound.
        """
        if token:
            conn.execute(
                "INSERT INTO ops (token, kind, interval_id, result, created_utc) "
                "VALUES (?, ?, ?, ?, ?)",
                (token, kind, interval_id, json.dumps(result), now),
            )
            conn.execute(
                "DELETE FROM ops WHERE created_utc < ?", (now - 86_400,)
            )

    # ------------------------------------------------------------------- checks
    # Sentinel for "an open interval continues indefinitely" in overlap tests.
    OPEN_END = 2**63 - 1

    def _validate(self, conn, start_utc, end_utc, now_ts, exclude_id=None):
        """Return an error dict, or None when the change is acceptable."""
        if start_utc is None:
            return {"ok": False, "error": "missing_start"}
        if end_utc is not None and end_utc <= start_utc:
            return {"ok": False, "error": "end_not_after_start"}
        if start_utc > now_ts or (end_utc is not None and end_utc > now_ts):
            return {"ok": False, "error": "future_timestamp"}
        if exclude_id is None:
            sql = (
                "SELECT id, start_utc, end_utc FROM intervals"
                " WHERE start_utc < ? AND (end_utc IS NULL OR end_utc > ?)"
            )
            args = (end_utc if end_utc is not None else self.OPEN_END, start_utc)
        else:
            sql = (
                "SELECT id, start_utc, end_utc FROM intervals"
                " WHERE id != ? AND start_utc < ? AND (end_utc IS NULL OR end_utc > ?)"
            )
            args = (exclude_id, end_utc if end_utc is not None else self.OPEN_END, start_utc)
        conflicts = conn.execute(sql, args).fetchall()
        if conflicts:
            return {"ok": False, "error": "overlap", "conflicts": conflicts}
        return None

    def _fetch_interval(self, conn, interval_id):
        return conn.execute(
            "SELECT start_utc, end_utc FROM intervals WHERE id = ?", (interval_id,)
        ).fetchone()

    def _insert(self, conn, start_utc, end_utc, now_ts):
        cur = conn.execute(
            "INSERT INTO intervals (start_utc, end_utc, created_utc, updated_utc)"
            " VALUES (?, ?, ?, ?)",
            (start_utc, end_utc, now_ts, now_ts),
        )
        return cur.lastrowid

    def _set_endpoints(self, conn, interval_id, start_utc, end_utc, now_ts):
        conn.execute(
            "UPDATE intervals SET start_utc = ?, end_utc = ?, updated_utc = ?"
            " WHERE id = ?",
            (start_utc, end_utc, now_ts, interval_id),
        )

    # ------------------------------------------------------------- manual edits
    def add_interval(self, start_utc, end_utc=None, now=None, token=None):
        def action(conn):
            replay = self._replay(conn, token, "add")
            if replay:
                return replay
            now_ts = self._now(now)
            error = self._validate(conn, start_utc, end_utc, now_ts)
            if error:
                return error
            if end_utc is None:
                open_row = conn.execute(
                    "SELECT id, start_utc FROM intervals WHERE end_utc IS NULL"
                ).fetchone()
                if open_row:
                    return {
                        "ok": False,
                        "error": "already_open",
                        "interval_id": open_row[0],
                        "start_utc": open_row[1],
                    }
            interval_id = self._insert(conn, start_utc, end_utc, now_ts)
            result = {
                "ok": True,
                "interval_id": interval_id,
                "start_utc": start_utc,
                "end_utc": end_utc,
            }
            self._record(conn, token, "add", interval_id, result, now_ts)
            return result

        return self.write("add", token, action)

    def close_interval(self, interval_id, end_utc, now=None, token=None):
        def action(conn):
            replay = self._replay(conn, token, "close")
            if replay:
                return replay
            now_ts = self._now(now)
            row = self._fetch_interval(conn, interval_id)
            if row is None:
                return {"ok": False, "error": "not_found", "interval_id": interval_id}
            start_utc, current_end = row
            if current_end is not None:
                return {"ok": False, "error": "not_open", "interval_id": interval_id}
            if end_utc is None:
                return {"ok": False, "error": "missing_end", "interval_id": interval_id}
            error = self._validate(conn, start_utc, end_utc, now_ts, exclude_id=interval_id)
            if error:
                return error
            self._set_endpoints(conn, interval_id, start_utc, end_utc, now_ts)
            result = {
                "ok": True,
                "interval_id": interval_id,
                "start_utc": start_utc,
                "end_utc": end_utc,
            }
            self._record(conn, token, "close", interval_id, result, now_ts)
            return result

        return self.write("close", token, action)

    def update_interval(self, interval_id, start_utc, end_utc, now=None, token=None):
        def action(conn):
            replay = self._replay(conn, token, "edit")
            if replay:
                return replay
            now_ts = self._now(now)
            row = self._fetch_interval(conn, interval_id)
            if row is None:
                return {"ok": False, "error": "not_found", "interval_id": interval_id}
            error = self._validate(conn, start_utc, end_utc, now_ts, exclude_id=interval_id)
            if error:
                return error
            if end_utc is None:
                open_row = conn.execute(
                    "SELECT id, start_utc FROM intervals WHERE end_utc IS NULL"
                ).fetchone()
                if open_row and open_row[0] != interval_id:
                    return {
                        "ok": False,
                        "error": "already_open",
                        "interval_id": open_row[0],
                        "start_utc": open_row[1],
                    }
            self._set_endpoints(conn, interval_id, start_utc, end_utc, now_ts)
            result = {
                "ok": True,
                "interval_id": interval_id,
                "start_utc": start_utc,
                "end_utc": end_utc,
            }
            self._record(conn, token, "edit", interval_id, result, now_ts)
            return result

        return self.write("edit", token, action)

    def delete_interval(self, interval_id, now=None, token=None):
        def action(conn):
            replay = self._replay(conn, token, "delete")
            if replay:
                return replay
            now_ts = self._now(now)
            cur = conn.execute("DELETE FROM intervals WHERE id = ?", (interval_id,))
            if cur.rowcount == 0:
                return {"ok": False, "error": "not_found", "interval_id": interval_id}
            result = {"ok": True, "interval_id": interval_id}
            self._record(conn, token, "delete", interval_id, result, now_ts)
            return result

        return self.write("delete", token, action)

    # ------------------------------------------------------------------ punches
    def punch_in(self, token=None, now=None):
        def action(conn):
            replay = self._replay(conn, token, "in")
            if replay:
                return replay
            ts = self._now(now)
            open_row = conn.execute(
                "SELECT id, start_utc FROM intervals WHERE end_utc IS NULL"
            ).fetchone()
            if open_row:
                result = {
                    "ok": False,
                    "error": "already_open",
                    "interval_id": open_row[0],
                    "start_utc": open_row[1],
                }
                return result
            cur = conn.execute(
                "INSERT INTO intervals (start_utc, end_utc, created_utc, updated_utc) "
                "VALUES (?, NULL, ?, ?)",
                (ts, ts, ts),
            )
            result = {"ok": True, "interval_id": cur.lastrowid, "start_utc": ts}
            self._record(conn, token, "in", result["interval_id"], result, ts)
            return result

        return self.write("in", token, action)

    def punch_out(self, token=None, now=None):
        def action(conn):
            replay = self._replay(conn, token, "out")
            if replay:
                return replay
            ts = self._now(now)
            open_row = conn.execute(
                "SELECT id, start_utc FROM intervals WHERE end_utc IS NULL"
            ).fetchone()
            if open_row is None:
                return {"ok": False, "error": "no_open_interval"}
            interval_id, start = open_row
            if ts <= start:
                return {"ok": False, "error": "end_not_after_start", "start_utc": start}
            conn.execute(
                "UPDATE intervals SET end_utc = ?, updated_utc = ? WHERE id = ?",
                (ts, ts, interval_id),
            )
            result = {
                "ok": True,
                "interval_id": interval_id,
                "start_utc": start,
                "end_utc": ts,
            }
            self._record(conn, token, "out", interval_id, result, ts)
            return result

        return self.write("out", token, action)
