"""Slice 1: IN then OUT creates exactly one completed interval."""
import sqlite3

from clock import server_clock
from db import Db


def test_in_then_out_creates_exactly_one_completed_interval(tmp_path):
    db = Db(tmp_path / "timesheet.sqlite3")
    db.init()

    server_clock.set_now(1_700_000_000)  # 2023-11-14 22:13:20 UTC
    in_result = db.punch_in(token="tok-in")
    assert in_result["ok"]
    assert in_result["interval_id"] is not None

    server_clock.set_now(1_700_003_600)  # exactly one hour later
    out_result = db.punch_out(token="tok-out")
    assert out_result["ok"]

    rows = db.fetch_all()
    assert len(rows) == 1
    (row_id, start, end, created, updated) = rows[0]
    assert start == 1_700_000_000
    assert end == 1_700_003_600
    assert row_id == in_result["interval_id"]
