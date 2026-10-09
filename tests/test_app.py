"""Slice 6: the Flask application (CSRF, PRG, server-side validation, restarts)."""
import re

from app import make_app
from clock import server_clock
from timeutil import validate_timezone
from db import Db

ROME = "Europe/Rome"
SECRET = "t" * 48  # test-only value; a real one is generated per install


def _make(tmp_path):
    app = make_app(
        timezone=ROME,
        secret=SECRET,
        database_path=tmp_path / "timesheet.sqlite3",
    )
    return app, app.test_client()


def _post(tmp_path, path, path_form_fields):
    """Submit a form exactly as the browser does (CSRF + idempotency token)."""
    _app, client = _make(tmp_path)
    page = client.get("/")
    csrf = re.search(r'name="_t" value="([^"]+)"', page.text)
    op = re.search(r'name="_op" value="([^"]+)"', page.text)
    assert csrf and op
    data = {"_t": csrf.group(1), "_op": op.group(1), **path_form_fields}
    return client, client.post(path, data=data, follow_redirects=True)


def test_health_endpoint_is_minimal(tmp_path):
    _app, client = _make(tmp_path)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_home_page_shows_status_timezone_and_today(tmp_path):
    server_clock.set_now(1_717_236_000)  # 2024-06-01 12:00 Europe/Rome
    _app, client = _make(tmp_path)
    page = client.get("/")
    assert "Not at work" in page.text
    assert "Europe/Rome" in page.text
    assert "Add / correct times" in page.text


def test_in_then_out_creates_one_interval_over_http(tmp_path):
    server_clock.set_now(1_717_236_000)
    _app, client = _make(tmp_path)

    client, after_in = _post(tmp_path, "/punch", {"action": "in"})
    assert after_in.status_code == 200
    assert "Saved: IN recorded" in after_in.text
    assert "At work since" in after_in.text

    server_clock.set_now(1_717_243_200)  # two hours later
    _app2, client2 = _make(tmp_path)
    _c, after_out = _post(tmp_path, "/punch", {"action": "out"})
    assert "Saved: OUT recorded" in after_out.text
    assert "Not at work" in after_out.text

    db = Db(tmp_path / "timesheet.sqlite3")
    rows = db.fetch_all()
    assert len(rows) == 1
    assert rows[0][1] == 1_717_236_000 and rows[0][2] == 1_717_243_200


def test_csrf_token_is_required(tmp_path):
    _app, client = _make(tmp_path)
    response = client.post("/punch", data={"action": "in"}, follow_redirects=False)
    assert response.status_code == 400
    db = Db(tmp_path / "timesheet.sqlite3")
    assert db.fetch_all() == []


def test_double_submission_is_idempotent(tmp_path):
    server_clock.set_now(1_717_236_000)
    _app, client = _make(tmp_path)
    page = client.get("/")
    csrf = re.search(r'name="_t" value="([^"]+)"', page.text).group(1)
    op = re.search(r'name="_op" value="([^"]+)"', page.text).group(1)
    data = {"_t": csrf, "_op": op, "action": "in"}

    first = client.post("/punch", data=data, follow_redirects=True)
    second = client.post("/punch", data=data, follow_redirects=True)
    assert "Saved: IN recorded" in first.text
    assert "Saved: IN recorded" in second.text
    db = Db(tmp_path / "timesheet.sqlite3")
    assert len(db.fetch_all()) == 1


def test_in_while_open_reports_error_not_success(tmp_path):
    server_clock.set_now(1_717_236_000)
    _app, client = _make(tmp_path)
    _c, first = _post(tmp_path, "/punch", {"action": "in"})
    assert "Saved: IN recorded" in first.text

    _app2, client2 = _make(tmp_path)
    _c, second = _post(tmp_path, "/punch", {"action": "in"})
    assert "already open" in second.text
    assert "Saved: IN recorded" not in second.text
    db = Db(tmp_path / "timesheet.sqlite3")
    assert len(db.fetch_all()) == 1


def test_manual_add_is_validated_server_side(tmp_path):
    server_clock.set_now(1_717_236_000)  # 2024-06-01 12:00 Rome
    _app, client = _make(tmp_path)
    _c, response = _post(
        tmp_path,
        "/times/add",
        {"start_date": "2024-06-01", "start_time": "13:00", "end_date": "2024-06-01",
         "end_time": "17:00"},
    )
    assert "in the future" in response.text
    db = Db(tmp_path / "timesheet.sqlite3")
    assert db.fetch_all() == []


def test_manual_add_of_a_past_interval(tmp_path):
    server_clock.set_now(1_717_236_000)
    _app, client = _make(tmp_path)
    _c, response = _post(
        tmp_path,
        "/times/add",
        {"start_date": "2024-05-30", "start_time": "09:00", "end_date": "2024-05-30",
         "end_time": "12:00"},
    )
    assert "Saved:" in response.text
    db = Db(tmp_path / "timesheet.sqlite3")
    assert len(db.fetch_all()) == 1


def test_report_page_and_csv_agree(tmp_path):
    server_clock.set_now(1_719_741_600)  # 2024-06-30 12:00 Rome
    _app, client = _make(tmp_path)
    _c, _response = _post(
        tmp_path,
        "/times/add",
        {"start_date": "2024-06-10", "start_time": "09:00", "end_date": "2024-06-10",
         "end_time": "12:00"},
    )
    _app2, client2 = _make(tmp_path)
    page = client2.get("/report?month=2024-06")
    csv_text = client2.get("/report/2024-06.csv")
    assert "3:00" in page.text
    assert "MONTH TOTAL,3:00" in csv_text.get_data(as_text=True)


def test_data_survives_application_restart(tmp_path):
    server_clock.set_now(1_717_236_000)
    _app, client = _make(tmp_path)
    _c, _response = _post(
        tmp_path,
        "/times/add",
        {"start_date": "2024-05-30", "start_time": "09:00", "end_date": "2024-05-30",
         "end_time": "12:00"},
    )
    _app2, client2 = _make(tmp_path)
    page = client2.get("/report?month=2024-05")
    assert "3:00" in page.text


def test_manual_add_without_start_shows_form_error(tmp_path):
    client, response = _post(tmp_path, "/times/add", {})
    assert response.status_code == 200
    assert "Enter the start date" in response.text


def test_manual_close_without_end_shows_form_error(tmp_path):
    """A close form with no end time is a form error, not a 500 and not a silent no-op."""
    server_clock.set_now(1_717_236_000)  # 2024-06-01 12:00 Europe/Rome
    app, client = _make(tmp_path)
    page = client.get("/")
    csrf = re.search(r'name="_t" value="([^"]+)"', page.text).group(1)
    op = re.search(r'name="_op" value="([^"]+)"', page.text).group(1)
    added = client.post(
        "/times/add",
        data={"_t": csrf, "_op": op, "start_date": "2024-06-01", "start_time": "09:00"},
        follow_redirects=True,
    )
    assert "Saved: interval #1 recorded" in added.text
    page = client.get("/times")
    op = re.search(r'name="_op" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/times/close",
        data={"_t": csrf, "_op": op, "interval_id": "1"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Enter the end time" in response.text
