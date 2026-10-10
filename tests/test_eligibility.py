"""Exact hard-eligibility rules: variable durations, 20-hour cap, half-open intervals, timetables."""

from datetime import datetime

import pytest

import eligibility
from eligibility import EligibilityError


def split(conn, shift_id, outgoing="W01"):
    result = eligibility.check_candidates(conn, shift_id, outgoing)
    eligible = {c["worker_id"]: c for c in result["eligible"]}
    excluded = {x["worker_id"]: x["reasons"] for x in result["excluded"]}
    return eligible, excluded


def test_thirteen_hours_plus_seven_hour_shift_is_allowed(tiny_conn):
    eligible, _ = split(tiny_conn, "SPECIAL-7H")
    assert eligible["W02"]["assigned_hours_now"] == 13
    assert eligible["W02"]["hours_after_covering"] == 20  # exactly at the cap is allowed


def test_eighteen_hours_cannot_take_any_full_shift_of_three_or_more_hours(tiny_conn):
    _, excluded = split(tiny_conn, "SPECIAL-7H")
    assert any("18h assigned + 7h shift = 25h > 20h cap" in r for r in excluded["W03"])


def test_no_partial_coverage_even_when_some_hours_remain(tiny_conn):
    # W07 has 16h, so 4h remain; the 5h overnight is rejected rather than shortened to fit.
    _, excluded = split(tiny_conn, "OVN")
    assert any("16h assigned + 5h shift = 21h > 20h cap" in r for r in excluded["W07"])


def test_stored_limit_above_twenty_does_not_raise_cap(tiny_conn):
    assert eligibility.effective_cap_minutes(25) == 20 * 60
    _, excluded = split(tiny_conn, "SPECIAL-7H")  # W07: 16h + 7h
    assert any("> 20h cap" in r for r in excluded["W07"])


def test_touching_class_is_not_an_overlap_but_one_minute_is(tiny_conn):
    eligible, excluded = split(tiny_conn, "SPECIAL-7H")  # 11:00-18:00
    assert "W04" in eligible  # class ends exactly 11:00
    assert any("class overlap CLASS:2" in r for r in excluded["W05"])  # class starts 17:59


def test_overnight_needs_timetable_for_both_dates(tiny_conn):
    eligible, excluded = split(tiny_conn, "OVN")  # Oct 14 23:00 - Oct 15 04:00
    assert excluded["W06"] == ["class timetable not confirmed for 2026-10-15"]
    eligible_day, _ = split(tiny_conn, "SPECIAL-7H")  # Oct 14 only
    assert "W06" in eligible_day


def test_all_applicable_reasons_returned_and_ids_ordered(tiny_conn):
    eligible, excluded = split(tiny_conn, "SPECIAL-7H")
    assert excluded["W01"] == ["outgoing worker (called out)"]
    assert "inactive worker" in excluded["W08"]
    assert list(eligible) == sorted(eligible)


def test_week_boundary_charges_by_start(tiny_conn):
    monday = datetime(2026, 10, 12)
    next_monday = datetime(2026, 10, 19)
    # W01: 7h special + 5h Wed overnight + 5h Sunday overnight (starts Sunday) = 17h this week.
    assert eligibility.weekly_minutes(tiny_conn, "W01", monday) == 17 * 60
    assert eligibility.weekly_minutes(tiny_conn, "W01", next_monday) == 0
    # The Monday 04:00 early morning belongs to the next week.
    assert eligibility.weekly_minutes(tiny_conn, "W03", next_monday) == 4 * 60


def test_dates_touched_and_midnight_end():
    assert eligibility.dates_touched(datetime(2026, 10, 14, 18), datetime(2026, 10, 15, 0)) == [datetime(2026, 10, 14).date()]
    assert len(eligibility.dates_touched(datetime(2026, 10, 14, 23), datetime(2026, 10, 15, 4))) == 2


def test_invalid_durations_are_rejected_not_rounded():
    with pytest.raises(ValueError):
        eligibility.minutes_between(datetime(2026, 10, 14, 10), datetime(2026, 10, 14, 10))
    with pytest.raises(ValueError):
        eligibility.minutes_between(datetime(2026, 10, 14, 10, 0, 0), datetime(2026, 10, 14, 10, 0, 30))


def test_outgoing_worker_must_hold_the_shift(tiny_conn):
    with pytest.raises(EligibilityError, match="not assigned"):
        eligibility.check_candidates(tiny_conn, "SPECIAL-7H", "W02")
    with pytest.raises(EligibilityError, match="unknown shift"):
        eligibility.check_candidates(tiny_conn, "NOPE", "W01")


# ----- the real seed scenarios -----

def test_scenario_pools_from_seed(conn):
    s1, ex1 = split(conn, "AND-1012-EVE", "W01")
    assert list(s1) == ["W05", "W09", "W14", "W21"]
    assert s1["W21"]["hours_after_covering"] == 20  # 15h + 5h is exactly the cap
    assert any("18h assigned + 5h shift = 23h" in r for r in ex1["W08"])  # 18 + 5 rejected
    assert ex1["W27"] == ["class timetable not confirmed for 2026-10-12"]

    s2, ex2 = split(conn, "HEL-1013-MOR", "W06")
    assert list(s2) == ["W14", "W21", "W29"]  # W29's early-morning shift ends exactly at 08:00
    assert any("overlaps own assignment VEG-1013-MOR" in r for r in ex2["W09"])

    s3, _ = split(conn, "CAP-1016-AFT", "W12")
    assert s3 == {}
