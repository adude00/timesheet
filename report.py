"""Monthly report calculations.

One implementation (``build_report``) feeds the HTML screen, the print view and
the CSV download, so all three show identical numbers.

Rules encoded here:

* every calendar day of the month is listed, including empty ones
* several intervals on one day are listed separately; the day total is the sum
  of the intervals, never "earliest IN .. latest OUT" (that would pay lunch)
* open intervals are shown but never counted in finalized totals
* overnight intervals are split at local midnight and marked as continued, so a
  clipped midnight is not mistaken for a real punch
* intervals crossing a month boundary are clipped: each month counts only the
  part inside it
* durations are summed as exact seconds and formatted once, at the end
"""
from __future__ import annotations

import csv
import io

from timeutil import (
    days_in_month,
    format_clock,
    format_decimal_hours,
    format_hhmm,
    format_weekday,
    tz_name,
    local_day_bounds,
    month_bounds,
    split_local_days,
)

INT64_MAX = 2**63 - 1


def build_report(db, tz, year, month):
    """Return the month's report as plain data (see module docstring)."""
    month_start, month_end = month_bounds(tz, year, month)
    days = {}
    for day in range(1, days_in_month(year, month) + 1):
        days[(year, month, day)] = {
            "date": (year, month, day),
            "weekday": None,
            "intervals": [],
            "seconds": 0,
            "open_intervals": 0,
        }

    open_intervals = 0
    for _row_id, start, end, _created, _updated in db.fetch_all():
        is_open = end is None
        effective_end = INT64_MAX if is_open else end
        if effective_end <= month_start or start >= month_end:
            continue
        clip_start = max(start, month_start)
        clip_end = min(effective_end, month_end)
        if is_open:
            open_intervals += 1
        for (yy, mm, dd), seg_start, seg_end, seconds in split_local_days(
            tz, clip_start, clip_end
        ):
            day = days.get((yy, mm, dd))
            if day is None:
                continue
            day["intervals"].append(
                {
                    "segment_start_utc": seg_start,
                    "segment_end_utc": seg_end,
                    "actual_start_utc": start,
                    "actual_end_utc": end,
                    "open": is_open,
                    "continued_in": seg_start != start,
                    "continued_out": seg_end != effective_end,
                    "seconds": seconds,
                }
            )
            if not is_open:
                day["seconds"] += seconds
            else:
                day["open_intervals"] += 1

    for (yy, mm, dd), day in days.items():
        day_start, _ = local_day_bounds(tz, yy, mm, dd)
        day["weekday"] = format_weekday(tz, day_start)

    total_seconds = sum(day["seconds"] for day in days.values())
    return {
        "year": year,
        "month": month,
        "tz": tz,
        "timezone": tz_name(tz),
        "days": [days[k] for k in sorted(days)],
        "total_seconds": total_seconds,
        "open_intervals": open_intervals,
    }


def _interval_label(iv, tz):
    """Readable IN/OUT labels; clipped boundaries say 'cont.', not a fake time."""
    if iv["continued_in"]:
        in_label = "cont."
    else:
        in_label = format_clock(tz, iv["segment_start_utc"])
    if iv["open"]:
        out_label = "open"
    elif iv["continued_out"]:
        out_label = "cont."
    else:
        out_label = format_clock(tz, iv["segment_end_utc"])
    return in_label, out_label


def csv_report(report):
    """CSV text using exactly the numbers already in ``report``."""
    tz = report["tz"]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["timesheet report", f"{report['year']:04d}-{report['month']:02d}", report["timezone"]])
    writer.writerow(["D", "date", "weekday", "hours_hhmm", "hours_decimal"])
    for day in report["days"]:
        year, month, day_of_month = day["date"]
        writer.writerow(
            [
                "D",
                f"{year:04d}-{month:02d}-{day_of_month:02d}",
                day["weekday"],
                format_hhmm(day["seconds"]),
                format_decimal_hours(day["seconds"]),
            ]
        )
    writer.writerow(
        [
            "I",
            "date",
            "in",
            "out",
            "hours_hhmm",
            "hours_decimal",
            "continued_in",
            "continued_out",
            "open",
        ]
    )
    for day in report["days"]:
        year, month, day_of_month = day["date"]
        for iv in day["intervals"]:
            in_label, out_label = _interval_label(iv, tz)
            writer.writerow(
                [
                    "I",
                    f"{year:04d}-{month:02d}-{day_of_month:02d}",
                    in_label,
                    out_label,
                    format_hhmm(iv["seconds"]),
                    format_decimal_hours(iv["seconds"]),
                    "yes" if iv["continued_in"] else "no",
                    "yes" if iv["continued_out"] else "no",
                    "yes" if iv["open"] else "no",
                ]
            )
    writer.writerow(["MONTH TOTAL", "hours_hhmm", "hours_decimal"])
    writer.writerow(
        [
            "MONTH TOTAL",
            format_hhmm(report["total_seconds"]),
            format_decimal_hours(report["total_seconds"]),
        ]
    )
    return buffer.getvalue()
