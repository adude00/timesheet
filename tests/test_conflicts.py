"""Slice 2: duplicate punches, illegal punches, replayed submissions, races."""
import threading

from clock import server_clock
from db import Db


def test_second_in_while_open_is_rejected(tmp_path):
    db = Db(tmp_path / "timesheet.sqlite3")
    db.init()
    server_clock.set_now(1_700_000_000)

    first = db.punch_in(token="tok-in-1")
    assert first["ok"]

    second = db.punch_in(token="tok-in-2")
    assert second["ok"] is False
    assert second["error"] == "already_open"
    assert second["interval_id"] == first["interval_id"]

    assert len(db.fetch_all()) == 1


def test_out_without_open_interval_is_rejected(tmp_path):
    db = Db(tmp_path / "timesheet.sqlite3")
    db.init()
    server_clock.set_now(1_700_000_000)

    result = db.punch_out(token="tok-out")
    assert result["ok"] is False
    assert result["error"] == "no_open_interval"
    assert db.fetch_all() == []


def test_replayed_submission_is_idempotent(tmp_path):
    db = Db(tmp_path / "timesheet.sqlite3")
    db.init()
    server_clock.set_now(1_700_000_000)

    first = db.punch_in(token="tok-same")
    assert first["ok"]

    replay = db.punch_in(token="tok-same")
    assert replay["ok"]
    assert replay["duplicate"] is True
    assert replay["interval_id"] == first["interval_id"]

    assert len(db.fetch_all()) == 1


def test_concurrent_in_requests_cannot_create_two_open_intervals(tmp_path):
    db = Db(tmp_path / "timesheet.sqlite3")
    db.init()
    server_clock.set_now(1_700_000_000)

    start = threading.Barrier(2)
    results = {}

    def worker(index):
        start.wait()
        results[index] = db.punch_in(token=f"tok-tab-{index}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successful = [r for r in results.values() if r["ok"]]
    assert len(successful) == 1
    assert len(db.fetch_all()) == 1


def test_concurrent_out_requests_close_only_once(tmp_path):
    db = Db(tmp_path / "timesheet.sqlite3")
    db.init()
    server_clock.set_now(1_700_000_000)
    assert db.punch_in(token="tok-in")["ok"]
    server_clock.set_now(1_700_003_600)

    start = threading.Barrier(2)
    results = {}

    def worker(index):
        start.wait()
        results[index] = db.punch_out(token=f"tok-out-{index}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rows = db.fetch_all()
    assert len(rows) == 1
    assert rows[0][2] == 1_700_003_600
    closed = [r for r in results.values() if r["ok"]]
    assert len(closed) == 1
