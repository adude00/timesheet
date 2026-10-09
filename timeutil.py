"""Local time <-> UTC helpers.

Everything inside the application is a UTC epoch second (integer).  Local time
only exists at the edges: when the user types a date/time into the form, and
when instants are grouped into calendar days/months for the report.

Calendar days are *not* assumed to be 24 hours: daylight-saving transitions
make some local days 23 or 25 hours long, and that is exactly what the tests
pin down.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import calendar

UTC = ZoneInfo("UTC")
SECONDS_PER_DAY = 86_400


def validate_timezone(name):
    """Return a ZoneInfo for a configured IANA timezone or raise ValueError."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("APP_TIMEZONE is not set")
    name = name.strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, ImportError, OSError) as exc:
        raise ValueError(
            f"APP_TIMEZONE={name!r} is not a valid IANA timezone "
            f"(e.g. Europe/Rome, Europe/Lisbon, America/New_York): {exc}"
        )


def tz_name(tz):
    """IANA name of a ZoneInfo (``key`` in 3.12, ``zone_name`` in 3.13+)."""
    for attr in ("zone_name", "key"):
        if hasattr(tz, attr):
            return getattr(tz, attr)
    return str(tz)


def _local_key(tz, ts):
    """(y, m, d, hh, mm) for a UTC instant, in the configured timezone."""
    dt = datetime.fromtimestamp(ts, tz)
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute)


def _day_key(tz, ts):
    dt = datetime.fromtimestamp(ts, tz)
    return dt.year * 10_000 + dt.month * 100 + dt.day


def _first_instant_of_local_day(tz, year, month, day):
    """First UTC instant whose local date is year-month-day.

    Binary search instead of arithmetic so that a local day that starts with a
    daylight-saving jump (no midnight, or a repeated midnight) is handled.
    """
    target = year * 10_000 + month * 100 + day
    guess = int(datetime(year, month, day, 12, 0, tzinfo=tz).timestamp())
    lo, hi = guess - 3 * SECONDS_PER_DAY, guess + 3 * SECONDS_PER_DAY
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if _day_key(tz, mid) >= target:
            hi = mid
        else:
            lo = mid
    return hi


def local_day_bounds(tz, year, month, day):
    """(start, end) UTC instants covering one local calendar day, end exclusive."""
    start = _first_instant_of_local_day(tz, year, month, day)
    next_year, next_month, next_day = _next_calendar_day(year, month, day)
    end = _first_instant_of_local_day(tz, next_year, next_month, next_day)
    return start, end


def _next_calendar_day(year, month, day):
    days = calendar.monthrange(year, month)[1]
    if day < days:
        return year, month, day + 1
    if month < 12:
        return year, month + 1, 1
    return year + 1, 1, 1


def month_bounds(tz, year, month):
    """(start, end) UTC instants covering one local month, end exclusive."""
    start = _first_instant_of_local_day(tz, year, month, 1)
    if month < 12:
        end = _first_instant_of_local_day(tz, year, month + 1, 1)
    else:
        end = _first_instant_of_local_day(tz, year + 1, 1, 1)
    return start, end


def days_in_month(year, month):
    return calendar.monthrange(year, month)[1]


def _offset_label(seconds):
    sign = "-" if seconds < 0 else "+"
    seconds = abs(seconds)
    return f"{sign}{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def _parse_offset(label):
    sign = -1 if label.startswith("-") else 1
    hours, _, minutes = label.lstrip("+-").partition(":")
    return sign * (int(hours) * 3600 + int(minutes) * 60)


def resolve_local(tz, year, month, day, hour, minute, offset=None):
    """Map a typed local wall time to UTC.

    Returns a dict:
      {"ok": True, "utc": int}
      {"ok": False, "error": "nonexistent"}          # skipped by a DST jump
      {"ok": False, "error": "ambiguous", "offsets": [...]}  # repeated by a DST
                                                     # fall-back; the user must
                                                     # pick one
    """
    candidates = []
    for fold in (0, 1):
        aware = datetime(year, month, day, hour, minute, fold=fold, tzinfo=tz)
        ts = int(aware.timestamp())
        back = datetime.fromtimestamp(ts, tz)
        if (back.year, back.month, back.day, back.hour, back.minute) == (
            year,
            month,
            day,
            hour,
            minute,
        ):
            entry = (ts, int(back.utcoffset().total_seconds()))
            if entry not in candidates:
                candidates.append(entry)
    candidates.sort()

    if not candidates:
        return {"ok": False, "error": "nonexistent"}
    if offset is None or offset == "":
        if len(candidates) > 1:
            return {
                "ok": False,
                "error": "ambiguous",
                "offsets": [_offset_label(off) for _, off in candidates],
            }
        return {"ok": True, "utc": candidates[0][0]}
    try:
        wanted = _parse_offset(offset)
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad_offset"}
    for ts, off in candidates:
        if off == wanted:
            return {"ok": True, "utc": ts}
    return {"ok": False, "error": "ambiguous", "offsets": [_offset_label(o) for _, o in candidates]}


def local_to_utc(tz, year, month, day, hour, minute):
    """Convenience wrapper that raises instead of returning an error dict."""
    result = resolve_local(tz, year, month, day, hour, minute)
    if not result["ok"]:
        raise ValueError(result["error"])
    return result["utc"]


def utc_to_local(tz, ts):
    """(y, m, d, hh, mm) for display."""
    return _local_key(tz, int(ts))


def split_local_days(tz, start, end):
    """Split [start, end) into ((y, m, d), seg_start, seg_end, seconds) parts."""
    parts = []
    current = int(start)
    stop = int(end)
    while current < stop:
        year, month, day, _, _ = utc_to_local(tz, current)
        day_start, day_end = local_day_bounds(tz, year, month, day)
        seg_end = min(stop, day_end)
        parts.append(((year, month, day), current, seg_end, seg_end - current))
        current = seg_end
    return parts


def format_hhmm(seconds):
    """Duration as HH:MM; hours are not capped at 24."""
    seconds = int(seconds)
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}"


def format_decimal_hours(seconds):
    return f"{int(seconds) / 3600:.2f}"


def format_clock(tz, ts):
    _, _, _, hour, minute = utc_to_local(tz, ts)
    return f"{hour:02d}:{minute:02d}"


def format_date(tz, ts):
    year, month, day, _, _ = utc_to_local(tz, ts)
    return f"{year:04d}-{month:02d}-{day:02d}"


def format_weekday(tz, ts):
    return datetime.fromtimestamp(int(ts), tz).strftime("%a")
