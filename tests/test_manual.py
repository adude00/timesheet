"""Slice 3: manual add / edit / delete and the server-side validation rules."""
from db import Db

T0 = 1_700_000_000
H = 3_600
NOW = T0 + 10 * H  # "server now" far enough ahead that the samples are in the past


def _db(tmp_path):
    db = Db(tmp_path / "timesheet.sqlite3")
    db.init()
    return db


def test_add_completed_interval(tmp_path):
    db = _db(tmp_path)
    result = db.add_interval(T0, T0 + H, now=NOW)
    assert result["ok"]
    assert db.fetch_all() == [(1, T0, T0 + H, NOW, NOW)]


def test_add_missing_in_as_open_interval(tmp_path):
    db = _db(tmp_path)
    result = db.add_interval(T0, None, now=NOW)
    assert result["ok"]
    assert db.fetch_open() == (1, T0)


def test_close_open_interval_with_manual_time(tmp_path):
    db = _db(tmp_path)
    assert db.add_interval(T0, None, now=NOW)["ok"]
    result = db.close_interval(1, T0 + 2 * H, now=NOW)
    assert result["ok"]
    assert db.fetch_open() is None
    assert db.fetch_all() == [(1, T0, T0 + 2 * H, NOW, NOW)]


def test_edit_both_endpoints(tmp_path):
    db = _db(tmp_path)
    assert db.add_interval(T0, T0 + H, now=NOW)["ok"]
    result = db.update_interval(1, T0 + 60, T0 + 2 * H, now=NOW)
    assert result["ok"]
    assert db.fetch_all() == [(1, T0 + 60, T0 + 2 * H, NOW, NOW)]


def test_delete_interval(tmp_path):
    db = _db(tmp_path)
    assert db.add_interval(T0, T0 + H, now=NOW)["ok"]
    result = db.delete_interval(1, now=NOW)
    assert result["ok"]
    assert db.fetch_all() == []


def test_end_must_be_strictly_after_start(tmp_path):
    db = _db(tmp_path)
    result = db.add_interval(T0, T0, now=NOW)
    assert result["ok"] is False
    assert result["error"] == "end_not_after_start"
    assert db.fetch_all() == []


def test_future_endpoints_rejected(tmp_path):
    db = _db(tmp_path)
    result = db.add_interval(T0 + H, T0 + 2 * H, now=T0)
    assert result["ok"] is False
    assert result["error"] == "future_timestamp"
    assert db.fetch_all() == []


def test_overlapping_intervals_rejected(tmp_path):
    db = _db(tmp_path)
    assert db.add_interval(T0, T0 + H, now=NOW)["ok"]
    result = db.add_interval(T0 + H // 2, T0 + 2 * H, now=NOW)
    assert result["ok"] is False
    assert result["error"] == "overlap"
    assert result["conflicts"] == [(1, T0, T0 + H)]
    assert len(db.fetch_all()) == 1


def test_adjacent_intervals_allowed(tmp_path):
    db = _db(tmp_path)
    assert db.add_interval(T0, T0 + H, now=NOW)["ok"]
    result = db.add_interval(T0 + H, T0 + 2 * H, now=NOW)
    assert result["ok"]
    assert len(db.fetch_all()) == 2


def test_new_interval_conflicts_with_open_interval(tmp_path):
    db = _db(tmp_path)
    assert db.add_interval(T0, None, now=NOW)["ok"]
    result = db.add_interval(T0 + H, T0 + 2 * H, now=NOW)
    assert result["ok"] is False
    assert result["error"] == "overlap"
    assert result["conflicts"] == [(1, T0, None)]
    assert len(db.fetch_all()) == 1


def test_edit_conflicting_with_other_interval_rejected(tmp_path):
    db = _db(tmp_path)
    assert db.add_interval(T0, T0 + H, now=NOW)["ok"]
    assert db.add_interval(T0 + 2 * H, T0 + 3 * H, now=NOW)["ok"]
    result = db.update_interval(2, T0, T0 + 3 * H, now=NOW)
    assert result["ok"] is False
    assert result["error"] == "overlap"
    assert result["conflicts"] == [(1, T0, T0 + H)]
    assert db.fetch_all() == [
        (1, T0, T0 + H, NOW, NOW),
        (2, T0 + 2 * H, T0 + 3 * H, NOW, NOW),
    ]


def test_missing_start_is_rejected_not_a_crash(tmp_path):
    db = _db(tmp_path)
    result = db.add_interval(None, T0 + H, now=NOW)
    assert result["ok"] is False
    assert result["error"] == "missing_start"
    assert db.fetch_all() == []


def test_close_without_end_is_rejected(tmp_path):
    db = _db(tmp_path)
    assert db.add_interval(T0, None, now=NOW)["ok"]
    result = db.close_interval(1, None, now=NOW)
    assert result["ok"] is False
    assert result["error"] == "missing_end"
    assert db.fetch_all() == [(1, T0, None, NOW, NOW)]
