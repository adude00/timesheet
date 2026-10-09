"""Slice 4: timezone, DST, calendar-day and month math (pure, no Flask)."""
import pytest

from timeutil import (
    days_in_month,
    format_hhmm,
    local_day_bounds,
    local_to_utc,
    month_bounds,
    resolve_local,
    split_local_days,
    utc_to_local,
    validate_timezone,
)

ROME = validate_timezone("Europe/Rome")

JUNE_NOON = 1_717_236_000  # 2024-06-01 12:00 Europe/Rome (CEST, UTC+2)


def test_validate_timezone_rejects_garbage():
    with pytest.raises(ValueError):
        validate_timezone("Mars/Phobos")


def test_local_to_utc_roundtrip():
    ts = local_to_utc(ROME, 2024, 6, 1, 12, 0)
    assert ts == JUNE_NOON
    assert utc_to_local(ROME, ts) == (2024, 6, 1, 12, 0)


def test_nonexistent_dst_time_is_rejected():
    # Europe/Rome springs forward 2024-03-31 at 02:00 -> 03:00.
    result = resolve_local(ROME, 2024, 3, 31, 2, 30)
    assert result["ok"] is False
    assert result["error"] == "nonexistent"


def test_ambiguous_dst_time_needs_an_explicit_offset():
    # Europe/Rome falls back 2024-10-27 at 03:00 -> 02:00, so 02:30 happens twice.
    result = resolve_local(ROME, 2024, 10, 27, 2, 30)
    assert result["ok"] is False
    assert result["error"] == "ambiguous"
    assert result["offsets"] == ["+02:00", "+01:00"]

    first = resolve_local(ROME, 2024, 10, 27, 2, 30, offset="+02:00")
    assert first["ok"]
    second = resolve_local(ROME, 2024, 10, 27, 2, 30, offset="+01:00")
    assert second["ok"]
    assert second["utc"] == first["utc"] + 3_600
    assert utc_to_local(ROME, first["utc"]) == (2024, 10, 27, 2, 30)
    assert utc_to_local(ROME, second["utc"]) == (2024, 10, 27, 2, 30)


def test_local_days_are_not_assumed_to_be_24_hours():
    spring_start, spring_end = local_day_bounds(ROME, 2024, 3, 31)
    assert spring_end - spring_start == 23 * 3_600
    fall_start, fall_end = local_day_bounds(ROME, 2024, 10, 27)
    assert fall_end - fall_start == 25 * 3_600


def test_overnight_interval_is_split_across_local_days():
    start = local_to_utc(ROME, 2024, 6, 1, 22, 0)
    end = local_to_utc(ROME, 2024, 6, 2, 6, 0)
    parts = split_local_days(ROME, start, end)
    assert [(p[0], p[1], p[2]) for p in parts] == [
        ((2024, 6, 1), start, local_day_bounds(ROME, 2024, 6, 1)[1]),
        ((2024, 6, 2), local_day_bounds(ROME, 2024, 6, 2)[0], end),
    ]
    assert sum(p[3] for p in parts) == end - start


def test_month_boundary_interval_is_split():
    start = local_to_utc(ROME, 2024, 1, 31, 12, 0)
    end = local_to_utc(ROME, 2024, 2, 2, 6, 0)
    jan_start, jan_end = month_bounds(ROME, 2024, 1)
    feb_start, feb_end = month_bounds(ROME, 2024, 2)
    assert start < jan_end < end
    assert jan_end == feb_start
    assert end - start == 42 * 3_600
    assert jan_end - start == 12 * 3_600
    assert end - feb_start == 30 * 3_600


def test_days_in_month_handles_leap_february():
    assert days_in_month(2024, 2) == 29
    assert days_in_month(2025, 2) == 28
    assert days_in_month(2024, 12) == 31


def test_durations_format_as_hhmm_with_hours_over_24():
    assert format_hhmm(0) == "0:00"
    assert format_hhmm(45 * 60) == "0:45"
    assert format_hhmm(90 * 60) == "1:30"
    assert format_hhmm(26 * 3_600 + 30 * 60) == "26:30"
    # Sum exact seconds first, never round each interval.
    parts = [50 * 60, 50 * 60]
    assert format_hhmm(sum(parts)) == "1:40"
