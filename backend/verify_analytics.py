"""Regression checks for Phase 8's read-only analytics module (`analytics.py`).

Runs entirely against throwaway in-memory databases with purpose-built
workers, shifts, schedules and assignments. The project's own
`backend/shiftops.db` is never opened, read, or modified.

Checks:

 1. An unprepared (empty) week reports all-zero coverage metrics and a
    defined 100% coverage percentage, without preparing anything.
 2. A single fully-staffed shift reports exact required/scheduled hours and
    100% coverage.
 3. A partially staffed shift reports uncovered positions, an unfilled
    shift count, and a coverage percentage between 0 and 100.
 4. A multi-staff shift (required_staff > 1) accounts for every position,
    not just whether the shift as a whole is "covered".
 5. Excess assignments (more assigned than required) are reported
    separately and never inflate filled_positions/coverage above 100%.
 6. A cross-midnight, start-week shift charges its hours entirely to the
    week containing its start (D025), not split or misattributed.
 7. An inactive worker's historical assignment still counts fully towards
    coverage and workforce assigned-hours figures.
 8. Workforce counts distinguish total, active, timetable-ready and
    active-and-timetable-ready populations, using the same accepted
    (confirmed, non-provisional) readiness rule as eligibility.py.
 9. Theoretical active capacity sums only active workers' weekly limits;
    an inactive worker's limit does not count towards it, but their
    recorded assigned hours still count towards the all-workers total.
10. Per-worker utilization/remaining-capacity rows are arithmetically
    consistent with reporting.remaining_capacity_hours.
11. workforce_scenario() computes the documented formulas for arbitrary
    worker counts and weekly-hour assumptions, including the zero-hours
    and zero-required-hours edge cases, rejects negative inputs, rejects a
    fractional hypothetical worker count while still accepting fractional
    weekly hours per worker, and accepts a whole number given as a float.
12. An invalid or non-Monday week_start raises weeks.InvalidWeekStart from
    every analytics entry point.
13. Reading analytics performs no writes: shifts/assignments/employees
    tables are unchanged before and after the call, and no shift is
    inserted for a week that was never prepared.

Run with:  python verify_analytics.py
Exits non-zero if any check fails.
"""

import sys
from datetime import datetime

from analytics import (
    week_analytics,
    week_coverage_metrics,
    workforce_capacity_metrics,
    workforce_scenario,
)
from database import create_schema, get_connection
from weeks import InvalidWeekStart

DATETIME_FORMAT = "%Y-%m-%d %H:%M"

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fixture_database():
    connection = get_connection(":memory:")
    create_schema(connection)
    return connection


def add_employee(connection, code, limit=20, active=True):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active) VALUES (?, ?, 'undergraduate', ?, ?)",
        (code, f"Fixture {code}", limit, 1 if active else 0),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]


def add_ready_schedule(connection, employee_id, start_date="2026-08-24", end_date="2026-12-11"):
    connection.execute(
        "INSERT INTO semester_schedules"
        " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
        " VALUES (?, ?, ?, '2026-01-01 00:00', 0)",
        (employee_id, start_date, end_date),
    )
    connection.commit()


def add_provisional_schedule(connection, employee_id, start_date="2026-08-24", end_date="2026-12-11"):
    connection.execute(
        "INSERT INTO semester_schedules"
        " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
        " VALUES (?, ?, ?, '2026-01-01 00:00', 1)",
        (employee_id, start_date, end_date),
    )
    connection.commit()


def add_shift(connection, start, end, hall="Helix", required_staff=1):
    start_text, end_text = start.strftime(DATETIME_FORMAT), end.strftime(DATETIME_FORMAT)
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES (?, ?, ?, ?)",
        (hall, start_text, end_text, required_staff),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ? AND end_datetime = ?",
        (hall, start_text, end_text),
    ).fetchone()["id"]


def add_assignment(connection, employee_id, shift_id):
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (employee_id, shift_id),
    )
    connection.commit()


WEEK = "2026-10-05"

# --------------------------------------------------------------- checks 1-6

connection = fixture_database()
coverage = week_coverage_metrics(connection, WEEK)
check(coverage["shift_count"] == 0, "an unprepared week reports zero stored shifts")
check(coverage["required_positions"] == 0, "an unprepared week reports zero required positions")
check(coverage["coverage_percentage"] == 100.0, "zero required hours defines coverage percentage as exactly 100.0")
check(
    connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"] == 0,
    "reading analytics for an unprepared week never inserts a shift",
)

connection = fixture_database()
worker = add_employee(connection, "SW-001")
shift = add_shift(connection, datetime(2026, 10, 6, 9, 0), datetime(2026, 10, 6, 13, 0), required_staff=1)
add_assignment(connection, worker, shift)
coverage = week_coverage_metrics(connection, WEEK)
check(coverage["required_positions"] == 1, "a single required-staff-1 shift reports one required position")
check(coverage["filled_positions"] == 1, "a fully staffed shift reports one filled position")
check(coverage["required_coverage_hours"] == 4, "required_coverage_hours = duration x required_staff (4 x 1)")
check(coverage["scheduled_coverage_hours"] == 4, "scheduled_coverage_hours = duration x min(assigned, required) (4 x 1)")
check(coverage["coverage_percentage"] == 100.0, "a fully staffed single shift reports 100% coverage")
check(coverage["uncovered_positions"] == 0, "a fully staffed shift has zero uncovered positions")
check(coverage["unfilled_shift_count"] == 0, "a fully staffed shift is not counted as unfilled")

connection = fixture_database()
shift = add_shift(connection, datetime(2026, 10, 6, 9, 0), datetime(2026, 10, 6, 13, 0), required_staff=1)
coverage = week_coverage_metrics(connection, WEEK)
check(coverage["uncovered_positions"] == 1, "an unstaffed required-staff-1 shift reports one uncovered position")
check(coverage["unfilled_shift_count"] == 1, "an unstaffed shift is counted as unfilled")
check(coverage["scheduled_coverage_hours"] == 0, "an unstaffed shift contributes zero scheduled hours")
check(coverage["coverage_percentage"] == 0.0, "a wholly unstaffed week reports 0% coverage")

connection = fixture_database()
w1 = add_employee(connection, "SW-001")
w2 = add_employee(connection, "SW-002")
shift = add_shift(connection, datetime(2026, 10, 6, 9, 0), datetime(2026, 10, 6, 13, 0), required_staff=2)
add_assignment(connection, w1, shift)
coverage = week_coverage_metrics(connection, WEEK)
check(coverage["required_positions"] == 2, "a required-staff-2 shift reports two required positions")
check(coverage["filled_positions"] == 1, "a required-staff-2 shift with one worker reports one filled position")
check(coverage["uncovered_positions"] == 1, "a required-staff-2 shift with one worker reports one uncovered position")
add_assignment(connection, w2, shift)
coverage = week_coverage_metrics(connection, WEEK)
check(coverage["filled_positions"] == 2, "a required-staff-2 shift with two workers reports two filled positions")
check(coverage["uncovered_positions"] == 0, "a fully staffed required-staff-2 shift has zero uncovered positions")

connection = fixture_database()
w1 = add_employee(connection, "SW-001")
w2 = add_employee(connection, "SW-002")
w3 = add_employee(connection, "SW-003")
shift = add_shift(connection, datetime(2026, 10, 6, 9, 0), datetime(2026, 10, 6, 13, 0), required_staff=1)
add_assignment(connection, w1, shift)
add_assignment(connection, w2, shift)
add_assignment(connection, w3, shift)
coverage = week_coverage_metrics(connection, WEEK)
check(coverage["filled_positions"] == 1, "filled_positions is capped at required_staff even with 3 assignments on a required-staff-1 shift")
check(coverage["excess_assignments"] == 2, "the two extra assignments are reported as excess_assignments, not lost")
check(coverage["coverage_percentage"] == 100.0, "excess assignments never push coverage percentage above 100%")
check(coverage["recorded_assignment_hours"] == 12, "recorded_assignment_hours is uncapped (4 hours x 3 assignments)")

connection = fixture_database()
worker = add_employee(connection, "SW-001")
# Sunday night into Monday morning of the FOLLOWING week - belongs entirely
# to the week containing its Sunday start (D025), not to WEEK below.
cross_midnight_shift = add_shift(
    connection, datetime(2026, 10, 11, 22, 0), datetime(2026, 10, 12, 3, 0), required_staff=1
)
add_assignment(connection, worker, cross_midnight_shift)
coverage_week_a = week_coverage_metrics(connection, WEEK)  # 2026-10-05, contains the Sunday
coverage_week_b = week_coverage_metrics(connection, "2026-10-12")  # the following Monday
check(coverage_week_a["shift_count"] == 1, "a Sunday-night-into-Monday shift is counted in the week containing its Sunday start")
check(coverage_week_a["required_coverage_hours"] == 5, "the cross-midnight shift's full 5 hours are charged to the start week")
check(coverage_week_b["shift_count"] == 0, "the cross-midnight shift contributes nothing to the following week")

# ------------------------------------------------------------- checks 7-10

connection = fixture_database()
active_ready = add_employee(connection, "SW-001", limit=20, active=True)
add_ready_schedule(connection, active_ready)
inactive_with_history = add_employee(connection, "SW-002", limit=15, active=False)
add_ready_schedule(connection, inactive_with_history)
active_unconfirmed = add_employee(connection, "SW-003", limit=10, active=True)
active_provisional = add_employee(connection, "SW-004", limit=10, active=True)
add_provisional_schedule(connection, active_provisional)

shift = add_shift(connection, datetime(2026, 10, 6, 9, 0), datetime(2026, 10, 6, 13, 0), required_staff=1)
add_assignment(connection, inactive_with_history, shift)

coverage = week_coverage_metrics(connection, WEEK)
check(
    coverage["filled_positions"] == 1 and coverage["scheduled_coverage_hours"] == 4,
    "an inactive worker's historical assignment still counts fully towards coverage hours",
)

workforce = workforce_capacity_metrics(connection, WEEK)
check(workforce["total_workers"] == 4, "total_workers counts every stored employee regardless of status")
check(workforce["active_workers"] == 3, "active_workers excludes the deactivated worker")
check(
    workforce["timetable_ready_workers"] == 2,
    "timetable_ready_workers counts only accepted (confirmed, non-provisional) coverage of the whole week - "
    "both the active-ready and inactive-but-ready workers count; the unconfirmed and provisional workers do not",
)
check(
    workforce["active_and_timetable_ready_workers"] == 1,
    "active_and_timetable_ready_workers is the intersection - the ready worker who is also inactive does not count here",
)
check(
    workforce["active_theoretical_capacity_hours"] == 20 + 10 + 10,
    "active theoretical capacity sums only active workers' weekly limits (excludes the inactive worker's 15)",
)
check(
    workforce["recorded_assigned_hours_all_workers"] == 4,
    "recorded_assigned_hours_all_workers includes the inactive worker's historical 4 assigned hours",
)
check(
    workforce["active_assigned_hours"] == 0,
    "active_assigned_hours excludes the inactive worker's hours (they are not an active worker)",
)
check(
    workforce["theoretical_remaining_active_capacity_hours"] == 40,
    "theoretical_remaining_active_capacity_hours = max(0, active capacity - active assigned hours) = 40 - 0",
)

row = next(row for row in workforce["workers"] if row["employee_code"] == "SW-002")
check(row["is_active"] is False, "the inactive worker's row still reports their current active status honestly")
check(row["scheduling_ready"] is True, "the inactive worker's row still reports their historical readiness")
check(row["assigned_hours"] == 4, "the inactive worker's row still reports their recorded assigned hours")
check(row["remaining_capacity_hours"] == 11, "remaining_capacity_hours = max(0, 15 - 4) = 11, matching reporting.py's own formula")
check(row["utilization_percentage"] == round(100 * 4 / 15, 1), "utilization_percentage matches assigned/limit exactly")

# ----------------------------------------------------------------- check 11

scenario = workforce_scenario(required_coverage_hours=100, hypothetical_worker_count=5, weekly_hours_per_worker=20)
check(scenario["hypothetical_theoretical_capacity_hours"] == 100, "hypothetical capacity = worker_count x weekly_hours (5 x 20)")
check(scenario["capacity_gap_hours"] == 0, "exact match reports a zero capacity gap")
check(scenario["theoretical_minimum_workers"] == 5, "ceil(100 / 20) = 5 minimum workers")
check(scenario["capacity_sufficient"] is True, "capacity_sufficient is True when hypothetical capacity meets required hours exactly")

scenario = workforce_scenario(required_coverage_hours=101, hypothetical_worker_count=5, weekly_hours_per_worker=20)
check(scenario["theoretical_minimum_workers"] == 6, "ceil(101 / 20) = 6, not truncated down to 5")
check(scenario["capacity_gap_hours"] == -1, "a one-hour shortfall reports a negative capacity gap of -1")
check(scenario["capacity_sufficient"] is False, "capacity_sufficient is False for any shortfall, however small")

scenario = workforce_scenario(required_coverage_hours=0, hypothetical_worker_count=0, weekly_hours_per_worker=0)
check(scenario["theoretical_minimum_workers"] == 0, "zero required hours needs zero minimum workers, even with zero hypothetical workers")
check(scenario["capacity_sufficient"] is True, "zero required hours is trivially satisfied")

scenario = workforce_scenario(required_coverage_hours=10, hypothetical_worker_count=3, weekly_hours_per_worker=0)
check(
    scenario["theoretical_minimum_workers"] is None,
    "zero weekly hours per worker can never accumulate positive required hours - minimum workers is undefined (None), not 0",
)
check(scenario["capacity_sufficient"] is False, "zero-hour hypothetical workers cannot satisfy a positive requirement")

try:
    workforce_scenario(required_coverage_hours=10, hypothetical_worker_count=-1, weekly_hours_per_worker=20)
    check(False, "a negative hypothetical worker count is rejected")
except ValueError:
    check(True, "a negative hypothetical worker count is rejected")

try:
    workforce_scenario(required_coverage_hours=10, hypothetical_worker_count=5, weekly_hours_per_worker=-1)
    check(False, "negative weekly hours per worker is rejected")
except ValueError:
    check(True, "negative weekly hours per worker is rejected")

scenario = workforce_scenario(required_coverage_hours=100, hypothetical_worker_count=5, weekly_hours_per_worker=7.5)
check(
    scenario["hypothetical_theoretical_capacity_hours"] == 37.5,
    "weekly_hours_per_worker may legitimately be fractional (5 x 7.5 = 37.5)",
)

try:
    workforce_scenario(required_coverage_hours=100, hypothetical_worker_count=2.5, weekly_hours_per_worker=20)
    check(False, "a fractional hypothetical worker count is rejected, not silently truncated")
except ValueError:
    check(True, "a fractional hypothetical worker count is rejected, not silently truncated")

try:
    workforce_scenario(required_coverage_hours=100, hypothetical_worker_count=True, weekly_hours_per_worker=20)
    check(False, "a bool hypothetical worker count is rejected (bool is technically an int subclass in Python)")
except ValueError:
    check(True, "a bool hypothetical worker count is rejected (bool is technically an int subclass in Python)")

# A whole number stored as a float (5.0) is still a whole number - the rule
# is "no fractional part", not "must literally be an int".
scenario = workforce_scenario(required_coverage_hours=100, hypothetical_worker_count=5.0, weekly_hours_per_worker=20)
check(
    scenario["theoretical_minimum_workers"] == 5,
    "a float that represents a whole number (5.0) is accepted, not rejected merely for being a float",
)

# ----------------------------------------------------------------- check 12

connection = fixture_database()
for bad in ("not-a-date", "2026-10-06", "2026-13-01", ""):
    try:
        week_coverage_metrics(connection, bad)
        check(False, f"week_coverage_metrics rejects invalid week_start {bad!r}")
    except InvalidWeekStart:
        check(True, f"week_coverage_metrics rejects invalid week_start {bad!r}")
    try:
        workforce_capacity_metrics(connection, bad)
        check(False, f"workforce_capacity_metrics rejects invalid week_start {bad!r}")
    except InvalidWeekStart:
        check(True, f"workforce_capacity_metrics rejects invalid week_start {bad!r}")
    try:
        week_analytics(connection, bad)
        check(False, f"week_analytics rejects invalid week_start {bad!r}")
    except InvalidWeekStart:
        check(True, f"week_analytics rejects invalid week_start {bad!r}")

# ----------------------------------------------------------------- check 13

connection = fixture_database()
worker = add_employee(connection, "SW-001")
shift = add_shift(connection, datetime(2026, 10, 6, 9, 0), datetime(2026, 10, 6, 13, 0), required_staff=1)
add_assignment(connection, worker, shift)
before = {
    "employees": connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"],
    "shifts": connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"],
    "assignments": connection.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"],
}
week_analytics(connection, WEEK)
week_analytics(connection, "2026-10-12")  # a different, unprepared week too
after = {
    "employees": connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"],
    "shifts": connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"],
    "assignments": connection.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"],
}
check(before == after, "reading analytics for two different weeks writes nothing to employees, shifts, or assignments")


if failures:
    print(f"\n{len(failures)} check(s) failed:")
    for description in failures:
        print(f"  - {description}")
    sys.exit(1)

print("\nAll analytics checks passed (throwaway in-memory databases only).")
