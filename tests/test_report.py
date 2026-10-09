"""Slice 5: monthly report calculations (shared by screen, print and CSV)."""
from db import Db
from report import build_report, csv_report
from timeutil import validate_timezone, local_to_utc, format_hhmm

ROME = validate_timezone("Europe/Rome")
JUNE = (2024, 6)


def _rows(db, intervals):
    for start, end in intervals:
        assert db.add_interval(start, end, now=end + 1)["ok"]


def test_empty_month_has_zero_totals_and_every_day(tmp_path):
    db = Db(tmp_path / "timesheet.sqlite3")
    db.init()
    report = build_report(db, ROME, 2024, 2)
    assert len(report["days"]) == 29  # leap-year February
    assert report["total_seconds"] == 0
    assert report["days"][0]["date"] == (2024, 2, 1)
    assert report["days"][-1]["date"] == (2024, 2, 29)
    assert all(d["seconds"] == 0 for d in report["days"])


def test_lunch_break_is_not_paid(tmp_path):
    db = Db(tmp_path / "timesheet.sqlite3")
    db.init()
    morning = local_to_utc(ROME, 2024, 6, 10, 9, 0)
    lunch_out = local_to_utc(ROME, 2024, 6, 10, 12, 0)
    back_in = local_to_utc(ROME, 2024, 6, 10, 13, 0)
    evening = local_to_utc(ROME, 2024, 6, 10, 17, 0)
    _rows(db, [(morning, lunch_out), (back_in, evening)])

    report = build_report(db, ROME, *JUNE)
    day = next(d for d in report["days"] if d["date"] == (2024, 6, 10))
    assert len(day["intervals"]) == 2
    assert day["seconds"] == (lunch_out - morning) + (evening - back_in)  # 11h, not 8h
    assert day["seconds"] == 7 * 3_600
    assert report["total_seconds"] == 7 * 3_600


def test_overnight_interval_is_split_and_marked(tmp_path):
    db = Db(tmp_path / "timesheet.sqlite3")
    db.init()
    start = local_to_utc(ROME, 2024, 6, 30, 22, 0)
    end = local_to_utc(ROME, 2024, 7, 2, 4, 0)
    assert db.add_interval(start, end, now=end + 1)["ok"]

    june = build_report(db, ROME, *JUNE)
    july = build_report(db, ROME, 2024, 7)

    june_day = next(d for d in june["days"] if d["date"] == (2024, 6, 30))
    assert june_day["seconds"] == 2 * 3_600
    assert june_day["intervals"][0]["continued_out"] is True
    assert june["total_seconds"] == 2 * 3_600

    july_first = next(d for d in july["days"] if d["date"] == (2024, 7, 1))
    july_second = next(d for d in july["days"] if d["date"] == (2024, 7, 2))
    assert july_first["seconds"] == 24 * 3_600
    assert july_second["seconds"] == 4 * 3_600
    assert july_first["intervals"][0]["continued_in"] is True
    assert july_second["intervals"][0]["continued_in"] is True
    assert july["total_seconds"] == 28 * 3_600


def test_monthly_total_can_exceed_24_hours(tmp_path):
    db = Db(tmp_path / "timesheet.sqlite3")
    db.init()
    start = local_to_utc(ROME, 2024, 6, 1, 0, 0)
    end = local_to_utc(ROME, 2024, 6, 3, 6, 30)
    assert db.add_interval(start, end, now=end + 1)["ok"]
    report = build_report(db, ROME, *JUNE)
    assert report["total_seconds"] == 54 * 3_600 + 30 * 60
    assert format_hhmm(report["total_seconds"]) == "54:30"


def test_open_interval_is_visible_but_not_counted(tmp_path):
    db = Db(tmp_path / "timesheet.sqlite3")
    db.init()
    start = local_to_utc(ROME, 2024, 6, 20, 8, 0)
    assert db.add_interval(start, None, now=start)["ok"]
    report = build_report(db, ROME, *JUNE)
    day = next(d for d in report["days"] if d["date"] == (2024, 6, 20))
    assert len(day["intervals"]) == 1
    assert day["intervals"][0]["open"] is True
    assert day["seconds"] == 0
    assert report["total_seconds"] == 0
    assert report["open_intervals"] == 1


def test_csv_totals_match_screen_totals(tmp_path):
    db = Db(tmp_path / "timesheet.sqlite3")
    db.init()
    morning = local_to_utc(ROME, 2024, 6, 10, 9, 0)
    lunch_out = local_to_utc(ROME, 2024, 6, 10, 12, 0)
    back_in = local_to_utc(ROME, 2024, 6, 10, 13, 0)
    evening = local_to_utc(ROME, 2024, 6, 10, 17, 0)
    _rows(db, [(morning, lunch_out), (back_in, evening)])

    report = build_report(db, ROME, *JUNE)
    text = csv_report(report)
    assert f"MONTH TOTAL,{format_hhmm(report['total_seconds'])}" in text
    assert f"2024-06-10,Mon,{format_hhmm(report['days'][9]['seconds'])}" in text
