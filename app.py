"""The Flask application: routes, CSRF protection, PRG redirects, rendering.

Nothing here decides *whether* a change is legal - that lives in ``db.py``
inside the write transaction.  This module only turns the result into a page.
"""
from __future__ import annotations

import hmac
import os
import re
import secrets

from flask import Flask, abort, jsonify, make_response, redirect, render_template, request, session

import sqlite3

from clock import server_clock
from db import Db
from report import build_report, csv_report
from report import _interval_label
from timeutil import (
    days_in_month,
    format_clock,
    format_date,
    format_decimal_hours,
    format_hhmm,
    format_weekday,
    local_day_bounds,
    month_bounds,
    resolve_local,
    split_local_days,
    tz_name,
    utc_to_local,
    validate_timezone,
)

DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
MONTH_RE = re.compile(r"(\d{4})-(\d{1,2})")


class ConfigError(Exception):
    """Required configuration is missing or invalid; the app must not start."""


def _as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def make_app(
    *,
    timezone=None,
    secret=None,
    database_path=None,
    trust_proxy=None,
    cookie_secure=None,
):
    """Build the Flask app.  Missing values are read from the environment.

    Raises ConfigError with an actionable message instead of starting a
    half-configured (or debug-mode) server.
    """
    timezone = (timezone or os.environ.get("APP_TIMEZONE") or "").strip()
    if not timezone:
        raise ConfigError(
            "APP_TIMEZONE is not set. Pick the IANA timezone your hours belong to, "
            "e.g. APP_TIMEZONE=Europe/Rome (see .env.example)."
        )
    try:
        tz = validate_timezone(timezone)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    secret = secret or os.environ.get("SECRET_KEY") or os.environ.get("CSRF_SECRET")
    if not secret:
        raise ConfigError(
            "SECRET_KEY is not set. Generate one for this install, for example with "
            "python3 -c 'import secrets; print(secrets.token_urlsafe(32))', and put it "
            "in .env (never commit it)."
        )

    database_path = database_path or os.environ.get("DATABASE_PATH", "/data/timesheet.sqlite3")
    trust_proxy = _as_bool(trust_proxy if trust_proxy is not None else os.environ.get("TRUST_PROXY"), False)
    cookie_secure = _as_bool(cookie_secure if cookie_secure is not None else os.environ.get("COOKIE_SECURE"), True)

    app = Flask(__name__)
    app.secret_key = secret
    # Only trust X-Forwarded-* when the documented NGINX arrangement is declared.
    app.trust_proxy = trust_proxy
    app.config.update(
        DEBUG=False,
        SESSION_COOKIE_NAME="timesheet_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cookie_secure,
        MAX_CONTENT_LENGTH=16 * 1024,
    )

    try:
        db = Db(database_path)
        db.init()
    except (OSError, sqlite3.Error) as exc:  # noqa: F401 - sqlite3 imported below
        raise ConfigError(
            f"cannot use the database at {database_path}: {exc}. Check that the "
            "/data volume is mounted and writable by the runtime user."
        ) from exc

    def _ensure_tokens():
        """One CSRF token per session, a fresh idempotency token per render.

        The CSRF token stays put so replaying a rendered form (double tap,
        browser retry) still passes the check; the ``_op`` token changes on
        every render, so only *that* form is recognised as a replay - a
        deliberate new action gets a new token and is processed normally.
        """
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        session["op"] = secrets.token_urlsafe(24)

    def _csrf_ok():
        expected = session.get("csrf", "")
        submitted = request.form.get("_t", "")
        return bool(expected) and hmac.compare_digest(expected, submitted)

    def _csrf_fail():
        abort(
            400,
            description=(
                "This form was not submitted from the page you just loaded, so it was "
                "rejected (CSRF protection). Reload the page and try again."
            ),
        )

    def _echo_values():
        return {key: value for key, value in request.form.items() if not key.startswith("_")}

    def _set_form_errors(errors):
        session["echo"] = {"fields": _echo_values(), "errors": errors}
        session.pop("flash", None)  # never show an old result next to new field errors

    def _clear_form_errors():
        session.pop("echo", None)

    def _flash(kind, text):
        session["flash"] = {"kind": kind, "text": text}
        session.pop("echo", None)  # a fresh result clears stale field errors

    def _db_error_text(result):
        """Server-side rejection reasons, phrased for the user."""
        now = server_clock.now()
        error = result["error"]
        if error == "end_not_after_start":
            return "The end must be strictly after the start (an interval cannot be empty or backwards)."
        if error == "missing_start":
            return "Enter a start date (YYYY-MM-DD) and a start time (HH:MM)."
        if error == "missing_end":
            return "Enter the end date and end time you want to close the interval at."
        if error == "future_timestamp":
            return f"That is in the future; the server clock says {format_date(tz, now)} {format_clock(tz, now)}."
        if error == "overlap":
            parts = []
            for row_id, start, end in result.get("conflicts", []):
                if end is None:
                    parts.append(
                        f"#{row_id}, which you left open since {format_date(tz, start)} "
                        f"{format_clock(tz, start)} (an open interval runs on until you close it)"
                    )
                else:
                    parts.append(
                        f"#{row_id} {format_date(tz, start)} {format_clock(tz, start)} - "
                        f"{format_clock(tz, end)}"
                    )
            return (
                "Overlaps an existing interval: " + "; ".join(parts)
                + ". Adjacent intervals are fine; close or correct the listed one first."
            )
        if error == "already_open":
            start = result.get("start_utc")
            return (
                f"An interval is already open since {format_date(tz, start)} "
                f"{format_clock(tz, start)}. Close or correct it first."
            )
        if error == "not_found":
            return "That interval no longer exists - reload the page and pick again."
        if error == "not_open":
            return "That interval is already closed - reload the page and pick again."
        if error == "confirm_required":
            return "Deletion needs confirmation: type DELETE in the confirmation field."
        if error == "token_reused":
            return "That form was replayed for a different action. Reload the page."
        return f"Rejected: {error}"

    def _flash_db(result, ok_text):
        if result["ok"]:
            _flash("success", ok_text(result))
        else:
            _flash("error", _db_error_text(result))

    def _endpoint(field_date, field_time, field_offset):
        """Parse one local endpoint; return (utc_seconds_or_None, {field: error})."""
        errors = {}
        raw_date = request.form.get(field_date, "")
        raw_time = request.form.get(field_time, "")
        parsed_date = DATE_RE.fullmatch(raw_date.strip()) if raw_date.strip() else None
        parsed_time = TIME_RE.fullmatch(raw_time.strip()) if raw_time.strip() else None
        if raw_date.strip() and not parsed_date:
            errors[field_date] = "Use YYYY-MM-DD (the end date matters for overnight work)."
        if raw_time.strip() and not parsed_time:
            errors[field_time] = "Use HH:MM on a 24-hour clock."
        if parsed_date and parsed_time:
            year, month, day = (int(g) for g in parsed_date.groups())
            hour, minute = (int(g) for g in parsed_time.groups())
            valid_day = 1 <= month <= 12 and 1 <= day <= days_in_month(year, month)
            valid_time = hour <= 23 and minute <= 59
            if not valid_day:
                errors[field_date] = "That date does not exist."
                return None, errors
            if not valid_time:
                errors[field_time] = "That time does not exist."
                return None, errors
            resolved = resolve_local(
                tz, year, month, day, hour, minute, offset=request.form.get(field_offset) or None
            )
            if resolved["ok"]:
                return resolved["utc"], errors
            if resolved["error"] == "nonexistent":
                errors[field_time] = (
                    "That local time does not exist in "
                    f"{tz_name(tz)} on that date (clocks jumped forward). "
                    "Pick a time on the correct side of the jump."
                )
            elif resolved["error"] == "ambiguous":
                errors[field_time] = (
                    "That local time happens twice in "
                    f"{tz_name(tz)} (clocks went back). Choose one offset: "
                    + ", ".join(resolved["offsets"])
                    + f" - put it in the '{field_offset}' field."
                )
            else:
                errors[field_offset] = (
                    "The offset field must be one of the offsets listed for this time."
                )
        return None, errors

    def _interval_labels(limit=10):
        rows = db.fetch_all()
        labels = []
        for row_id, start, end, _created, _updated in reversed(rows[-limit:]):
            labels.append(
                {
                    "id": row_id,
                    "date": format_date(tz, start),
                    "in": format_clock(tz, start),
                    "out": "open" if end is None else format_clock(tz, end),
                    "open": end is None,
                    # Raw local values so the entry page can pre-fill the edit form.
                    "start_date": format_date(tz, start),
                    "start_time": format_clock(tz, start),
                    "end_date": "" if end is None else format_date(tz, end),
                    "end_time": "" if end is None else format_clock(tz, end),
                }
            )
        return labels

    def _home_view():
        now = server_clock.now()
        year, month, day, _, _ = utc_to_local(tz, now)
        day_start, day_end = local_day_bounds(tz, year, month, day)
        completed_today = 0
        open_start = None
        for _row_id, start, end, _created, _updated in db.fetch_all():
            if end is None:
                open_start = start
            elif start < day_end and end > day_start:
                completed_today += min(end, day_end) - max(start, day_start)
        if open_start is not None:
            status = f"At work since {format_date(tz, open_start)} {format_clock(tz, open_start)}"
            status_kind = "open"
            live = format_hhmm(max(0, now - open_start))
        else:
            status = "Not at work"
            status_kind = "closed"
            live = None
        return {
            "timezone": tz_name(tz),
            "status": status,
            "status_kind": status_kind,
            "can_in": open_start is None,
            "can_out": open_start is not None,
            "today_hhmm": format_hhmm(completed_today),
            "today_decimal": format_decimal_hours(completed_today),
            "live_hhmm": live,
            "recent": _interval_labels(8),
            "month_hint": _month_hint(),
        }

    def _report_view(report_data):
        days = []
        for day in report_data["days"]:
            year, month, day_of_month = day["date"]
            rows = []
            for iv in day["intervals"]:
                in_label, out_label = _interval_label(iv, report_data["tz"])
                rows.append(
                    {
                        "in": in_label,
                        "out": out_label,
                        "hhmm": format_hhmm(iv["seconds"]),
                        "open": iv["open"],
                        "continued": iv["continued_in"] or iv["continued_out"],
                    }
                )
            days.append(
                {
                    "date": f"{year:04d}-{month:02d}-{day_of_month:02d}",
                    "weekday": day["weekday"],
                    "rows": rows,
                    "total": format_hhmm(day["seconds"]),
                    "open": day["open_intervals"],
                }
            )
        return {
            "month": f"{report_data['year']:04d}-{report_data['month']:02d}",
            "timezone": report_data["timezone"],
            "days": days,
            "total": format_hhmm(report_data["total_seconds"]),
            "total_decimal": format_decimal_hours(report_data["total_seconds"]),
            "open_intervals": report_data["open_intervals"],
        }

    def _month_from_args():
        raw = request.args.get("month", "")
        if not raw:
            now = server_clock.now()
            year, month, _day, _hh, _mm = utc_to_local(tz, now)
            return year, month, None
        match = MONTH_RE.fullmatch(raw.strip())
        if not match:
            return None, None, f"month={raw!r} is not YYYY-MM"
        year, month = (int(g) for g in match.groups())
        if not 1 <= month <= 12:
            return None, None, "month must be 01..12"
        return year, month, None

    # -------------------------------------------------------------------- pages
    @app.route("/")
    def home():
        _ensure_tokens()
        return render_template(
            "index.html",
            view=_home_view(),
            csrf=session["csrf"],
            op=session["op"],
            flash=session.get("flash"),
        )

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/punch", methods=["POST"])
    def punch():
        _csrf_ok() or _csrf_fail()
        action = request.form.get("action", "")
        token = request.form.get("_op", "")
        if action == "in":
            result = db.punch_in(token=token)
            text = lambda result: (  # noqa: E731
                f"Saved: IN recorded at {format_clock(tz, result['start_utc'])} "
                f"({tz_name(tz)})."
            )
        elif action == "out":
            result = db.punch_out(token=token)
            text = lambda result: (  # noqa: E731
                f"Saved: OUT recorded at {format_clock(tz, result['end_utc'])}; "
                f"that interval is {format_hhmm(result['end_utc'] - result['start_utc'])} long."
            )
        else:
            result = {"ok": False, "error": "unknown_action"}
            text = lambda result: ""  # noqa: E731
        _flash_db(result, text)
        return redirect("/", code=303)

    @app.route("/times")
    def times():
        _ensure_tokens()
        return render_template(
            "times.html",
            csrf=session["csrf"],
            op=session["op"],
            intervals=_interval_labels(10),
            echo=session.get("echo", {"fields": {}, "errors": {}}),
            flash=session.get("flash"),
            timezone=tz_name(tz),
            today=format_date(tz, server_clock.now()),
            first_id=_first_id(),
        )

    def _manual_common():
        """Shared preamble for the manual endpoints.  Returns (errors, token)."""
        _csrf_ok() or _csrf_fail()
        return request.form.get("_op", "")

    @app.route("/times/add", methods=["POST"])
    def times_add():
        token = _manual_common()
        errors = {}
        start, errs = _endpoint("start_date", "start_time", "start_offset")
        errors.update(errs)
        end = None
        if request.form.get("end_date") or request.form.get("end_time"):
            end, errs = _endpoint("end_date", "end_time", "end_offset")
            errors.update(errs)
        if errors:
            _set_form_errors(errors)
            return redirect("/times", code=303)
        if start is None:
            errors["start_date"] = "Enter the start date (YYYY-MM-DD) and the start time (HH:MM)."
        if errors:
            _set_form_errors(errors)
            return redirect("/times", code=303)
        result = db.add_interval(start, end, token=token)
        _flash_db(
            result,
            lambda result: (
                f"Saved: interval #{result['interval_id']} recorded "
                f"({format_date(tz, result['start_utc'])} "
                f"{format_clock(tz, result['start_utc'])} - "
                f"{'open' if result['end_utc'] is None else format_clock(tz, result['end_utc'])})."
            ),
        )
        return redirect("/times", code=303)

    @app.route("/times/close", methods=["POST"])
    def times_close():
        token = _manual_common()
        errors = {}
        interval_id = _interval_id(errors)
        end, errs = _endpoint("end_date", "end_time", "end_offset")
        errors.update(errs)
        if errors:
            _set_form_errors(errors)
            return redirect("/times", code=303)
        if end is None:
            errors["end_time"] = "Enter the end time (HH:MM) to close the interval at."
        if errors:
            _set_form_errors(errors)
            return redirect("/times", code=303)
        result = db.close_interval(interval_id, end, token=token)
        _flash_db(
            result,
            lambda result: (
                f"Saved: interval #{result['interval_id']} closed at "
                f"{format_clock(tz, result['end_utc'])}."
            ),
        )
        return redirect("/times", code=303)

    @app.route("/times/edit", methods=["POST"])
    def times_edit():
        token = _manual_common()
        errors = {}
        interval_id = _interval_id(errors)
        row = None
        for _row_id, start, end, _c, _u in db.fetch_all():
            if _row_id == interval_id:
                row = (start, end)
        if row is None:
            errors["interval_id"] = "That interval does not exist."
            _set_form_errors(errors)
            return redirect("/times", code=303)
        start, errs = _endpoint("start_date", "start_time", "start_offset")
        errors.update(errs)
        end = None
        if request.form.get("end_date") or request.form.get("end_time"):
            end, errs = _endpoint("end_date", "end_time", "end_offset")
            errors.update(errs)
        elif row[1] is not None:
            end = row[1]
        if not errors and start is None and end is None:
            errors["start_date"] = "Nothing to change: fill in a new start or a new end."
        if start is None:
            start = row[0]
        if errors:
            _set_form_errors(errors)
            return redirect("/times", code=303)
        result = db.update_interval(interval_id, start, end, token=token)
        _flash_db(
            result,
            lambda result: (
                f"Saved: interval #{result['interval_id']} corrected."
            ),
        )
        return redirect("/times", code=303)

    @app.route("/times/delete", methods=["POST"])
    def times_delete():
        token = _manual_common()
        errors = {}
        interval_id = _interval_id(errors)
        if request.form.get("confirm", "").strip().upper() != "DELETE":
            errors["confirm"] = (
                "Deletion is permanent. Type DELETE in the confirmation field to confirm."
            )
        if errors:
            _set_form_errors(errors)
            return redirect("/times", code=303)
        result = db.delete_interval(interval_id, token=token)
        _flash_db(
            result,
            lambda result: f"Saved: interval #{result['interval_id']} deleted.",
        )
        return redirect("/times", code=303)

    def _interval_id(errors):
        raw = request.form.get("interval_id", "").strip()
        try:
            return int(raw)
        except ValueError:
            errors["interval_id"] = "Pick one of the listed interval numbers."
            return -1

    @app.route("/times/entry/<int:interval_id>")
    def times_entry(interval_id):
        """One entry, clicked from an interval list: shows it and offers Edit.

        The edit form is pre-filled with the currently recorded values, so the
        user only has to change the fields that are actually wrong.
        """
        _ensure_tokens()
        row = None
        for _row_id, start, end, _c, _u in db.fetch_all():
            if _row_id == interval_id:
                row = (start, end)
                break
        if row is None:
            _flash("error", "That interval no longer exists - reload the page and pick again.")
            return redirect("/times", code=303)
        start, end = row
        entry = {
            "id": interval_id,
            "open": end is None,
            "start_date": format_date(tz, start),
            "start_time": format_clock(tz, start),
            "end_date": "" if end is None else format_date(tz, end),
            "end_time": "" if end is None else format_clock(tz, end),
        }
        return render_template(
            "entry.html",
            entry=entry,
            csrf=session["csrf"],
            op=session["op"],
            flash=session.get("flash"),
            timezone=tz_name(tz),
        )

    @app.route("/report")
    def report_page():
        year, month, problem = _month_from_args()
        if problem:
            return render_template(
                "report.html",
                view=None,
                problem=problem,
                month_hint=_month_hint(),
            )
        view = _report_view(build_report(db, tz, year, month))
        return render_template("report.html", view=view, problem=None, month_hint=_month_hint())

    @app.route("/report/<month_arg>.csv")
    def report_csv(month_arg):
        year, month, problem = _month_from_args_from(month_arg)
        if problem:
            abort(400, description=problem)
        report_data = build_report(db, tz, year, month)
        body = csv_report(report_data)
        response = make_response(body)
        response.mimetype = "text/csv"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="timesheet-{year:04d}-{month:02d}.csv"'
        )
        return response

    def _first_id():
        rows = db.fetch_all()
        return str(rows[0][0]) if rows else ""

    def _month_hint():
        now = server_clock.now()
        year, month, _day, _hh, _mm = utc_to_local(tz, now)
        return f"{year:04d}-{month:02d}"

    def _month_from_args_from(raw):
        match = MONTH_RE.fullmatch((raw or "").strip())
        if not match:
            return None, None, "month must be YYYY-MM"
        year, month = (int(g) for g in match.groups())
        if not 1 <= month <= 12:
            return None, None, "month must be 01..12"
        return year, month, None

    return app
