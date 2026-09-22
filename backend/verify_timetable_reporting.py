"""Regression checks for class summaries and timetable readiness.

**Read this before running it.** `main.py` calls `ensure_schema()` at import
time against whatever `database.DATABASE_PATH` points at, so this module
redirects that path to a throwaway file *before* importing `main`, and
refuses to run if `main` has already been imported. The project's own
`backend/shiftops.db` is never created, opened or migrated here.

These checks call `main.list_employees()` - the real endpoint function - and
read the payload it returns. They deliberately do NOT re-implement its SQL or
assert arithmetic constants worked out by hand, because a test that
duplicates the logic it is checking agrees with the code by construction and
proves nothing.

*Limitation:* the endpoint is called as a Python function. Starlette's
TestClient needs `httpx2`, which this project does not depend on. So these
exercise the endpoint's data handling and payload shape. They are NOT HTTP
transport tests: routing, serialization over the wire, status codes and CORS
are not covered here.

Checks:

1. A semester starting midweek excludes that week's earlier weekday classes.
2. A semester ending midweek excludes that week's later weekday classes.
3. Classes exactly on the inclusive semester boundaries are counted.
4. A semester wholly outside the reporting week contributes nothing.
5. Class hours stay fractional.
6. Timetable readiness for the displayed week: missing, outside_period,
   unconfirmed, partial, confirmed.
7. Confirmed-with-no-classes stays distinguishable from missing.
8. Active status is independent of timetable readiness.

Run with:  python verify_timetable_reporting.py
Exits non-zero if any check fails.
"""

import sys
import tempfile
from pathlib import Path

if "main" in sys.modules:  # pragma: no cover - defensive
    raise SystemExit(
        "main was imported before the database path was redirected; refusing to run."
    )

import database  # noqa: E402  - imported early on purpose, see the docstring

_TEMPORARY = tempfile.TemporaryDirectory()
_SCRATCH = Path(_TEMPORARY.name)
database.DATABASE_PATH = _SCRATCH / "bootstrap.db"

import main  # noqa: E402

failures = []
_counter = {"n": 0}


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fresh_database():
    """Point the application at a brand-new throwaway file and return it."""
    _counter["n"] += 1
    database.DATABASE_PATH = _SCRATCH / f"case-{_counter['n']}.db"
    connection = database.get_connection()
    database.create_schema(connection)
    return connection


def add_worker(connection, code, name="Worker", active=1):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active, seed_key)"
        " VALUES (?, ?, 'undergraduate', 20, ?, NULL)",
        (code, name, active),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]


def add_schedule(connection, employee_id, start, end, confirmed=None):
    connection.execute(
        "INSERT INTO semester_schedules (employee_id, start_date, end_date, confirmed_at)"
        " VALUES (?, ?, ?, ?)",
        (employee_id, start, end, confirmed),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM semester_schedules WHERE employee_id = ?"
        " AND start_date = ? AND end_date = ?",
        (employee_id, start, end),
    ).fetchone()["id"]


def add_block(connection, schedule_id, day, start, end):
    connection.execute(
        "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time)"
        " VALUES (?, ?, ?, ?)",
        (schedule_id, day, start, end),
    )
    connection.commit()


def reporting_payload():
    """Run behavior fixtures against their explicit historical test week."""
    return main.list_employees(week_start="2026-10-05")


def listed(code):
    """One worker's row from the real endpoint."""
    payload = reporting_payload()
    for employee in payload["employees"]:
        if employee["employee_code"] == code:
            return employee
    raise AssertionError(f"{code} not returned by the endpoint")


def main_checks():
    week = main.list_employees()
    check(
        (week["week_start"], week["week_end"]) == ("2026-09-21", "2026-09-27"),
        f"the default reporting week is Mon 21 - Sun 27 September 2026 "
        f"({week['week_start']}..{week['week_end']})",
    )

    # 1. Semester starts on the Wednesday of the reporting week. The Monday
    #    and Tuesday classes have already passed before it begins.
    connection = fresh_database()
    worker = add_worker(connection, "SW-001", "Midweek Start")
    schedule = add_schedule(connection, worker, "2026-10-07", "2026-12-11", "2026-10-07 00:00")
    for day in range(7):  # one class every weekday of the week
        add_block(connection, schedule, day, "09:00", "10:00")
    connection.close()

    row = listed("SW-001")
    check(
        row["class_block_count"] == 5,
        f"a semester starting Wednesday counts only Wed-Sun classes "
        f"({row['class_block_count']}, expected 5)",
    )
    check(row["weekly_class_hours"] == 5.0,
          f"and only those hours ({row['weekly_class_hours']})")

    # 2. Semester ends on the Wednesday of the reporting week.
    connection = fresh_database()
    worker = add_worker(connection, "SW-001", "Midweek End")
    schedule = add_schedule(connection, worker, "2026-08-24", "2026-10-07", "2026-08-24 00:00")
    for day in range(7):
        add_block(connection, schedule, day, "09:00", "10:00")
    connection.close()

    row = listed("SW-001")
    check(
        row["class_block_count"] == 3,
        f"a semester ending Wednesday counts only Mon-Wed classes "
        f"({row['class_block_count']}, expected 3)",
    )

    # 3. Classes exactly on the inclusive boundaries are counted.
    connection = fresh_database()
    worker = add_worker(connection, "SW-001", "Boundaries")
    # A semester that is exactly the reporting week.
    schedule = add_schedule(connection, worker, "2026-10-05", "2026-10-11", "2026-10-05 00:00")
    add_block(connection, schedule, 0, "09:00", "10:00")  # Monday = start date
    add_block(connection, schedule, 6, "09:00", "10:00")  # Sunday = end date
    connection.close()

    row = listed("SW-001")
    check(row["class_block_count"] == 2,
          f"classes on the exact first and last days are counted ({row['class_block_count']})")
    check(row["timetable_status"] == "confirmed",
          f"a confirmed semester covering exactly the week is confirmed ({row['timetable_status']})")

    # 4. A semester wholly outside the week contributes nothing.
    connection = fresh_database()
    worker = add_worker(connection, "SW-001", "Spring Only")
    schedule = add_schedule(connection, worker, "2026-01-12", "2026-05-01", "2026-01-12 00:00")
    for day in range(7):
        add_block(connection, schedule, day, "09:00", "10:00")
    connection.close()

    row = listed("SW-001")
    check(row["class_block_count"] == 0,
          f"an expired semester contributes no classes ({row['class_block_count']})")
    check(row["weekly_class_hours"] == 0,
          "and no class hours")
    check(
        row["timetable_status"] == "outside_period",
        f"an expired confirmed semester is NOT reported as confirmed for this week "
        f"({row['timetable_status']})",
    )

    # A future semester reads the same way.
    connection = fresh_database()
    worker = add_worker(connection, "SW-001", "Next Year")
    schedule = add_schedule(connection, worker, "2027-01-11", "2027-05-01", "2027-01-11 00:00")
    add_block(connection, schedule, 0, "09:00", "10:00")
    connection.close()
    check(listed("SW-001")["timetable_status"] == "outside_period",
          "a future confirmed semester is not reported as confirmed for this week")

    # 5. Fractional class hours survive.
    connection = fresh_database()
    worker = add_worker(connection, "SW-001", "Fractional")
    schedule = add_schedule(connection, worker, "2026-08-24", "2026-12-11", "2026-08-24 00:00")
    add_block(connection, schedule, 0, "09:00", "10:15")   # 75 minutes
    add_block(connection, schedule, 2, "13:00", "15:45")   # 165 minutes
    connection.close()

    row = listed("SW-001")
    check(row["weekly_class_hours"] == 4.0,
          f"75 + 165 minutes is 4.0 hours ({row['weekly_class_hours']})")
    connection = fresh_database()
    worker = add_worker(connection, "SW-001", "One Class")
    schedule = add_schedule(connection, worker, "2026-08-24", "2026-12-11", "2026-08-24 00:00")
    add_block(connection, schedule, 0, "09:00", "10:15")
    connection.close()
    check(listed("SW-001")["weekly_class_hours"] == 1.25,
          f"a single 75-minute class is 1.25 hours, not rounded to a whole number "
          f"({listed('SW-001')['weekly_class_hours']})")

    # 6-8. Readiness states, all in one database so they are comparable.
    connection = fresh_database()
    missing = add_worker(connection, "SW-001", "No Timetable")
    unconfirmed = add_worker(connection, "SW-002", "Entered Only")
    partial = add_worker(connection, "SW-003", "Half Week")
    confirmed = add_worker(connection, "SW-004", "Full Week")
    no_classes = add_worker(connection, "SW-005", "Confirmed None", active=0)
    expired = add_worker(connection, "SW-006", "Expired")

    add_schedule(connection, unconfirmed, "2026-08-24", "2026-12-11", None)
    add_schedule(connection, partial, "2026-10-05", "2026-10-08", "2026-10-05 00:00")
    add_schedule(connection, confirmed, "2026-08-24", "2026-12-11", "2026-08-24 00:00")
    add_schedule(connection, no_classes, "2026-08-24", "2026-12-11", "2026-08-24 00:00")
    add_schedule(connection, expired, "2026-01-12", "2026-05-01", "2026-01-12 00:00")
    connection.close()

    states = {row["employee_code"]: row["timetable_status"] for row in reporting_payload()["employees"]}
    check(states["SW-001"] == "missing", f"no schedule at all reads missing ({states['SW-001']})")
    check(states["SW-002"] == "unconfirmed",
          f"a schedule nobody confirmed reads unconfirmed ({states['SW-002']})")
    check(states["SW-003"] == "partial",
          f"a confirmed schedule covering only part of the week reads partial ({states['SW-003']})")
    check(states["SW-004"] == "confirmed",
          f"a confirmed schedule covering the whole week reads confirmed ({states['SW-004']})")
    check(states["SW-006"] == "outside_period",
          f"a confirmed schedule for another period reads outside_period ({states['SW-006']})")

    # 7. Confirmed-with-no-classes must stay distinguishable from missing.
    rows = {row["employee_code"]: row for row in reporting_payload()["employees"]}
    check(
        rows["SW-005"]["class_block_count"] == 0 and rows["SW-001"]["class_block_count"] == 0,
        "both the confirmed-no-classes and the missing worker have zero blocks",
    )
    check(
        rows["SW-005"]["timetable_status"] != rows["SW-001"]["timetable_status"],
        "yet they report different timetable statuses",
    )
    check(rows["SW-005"]["timetable_status"] == "confirmed",
          "confirmed with no classes reads confirmed, a deliberate 'no classes'")

    # 8. Active status is independent of readiness.
    check(rows["SW-005"]["is_active"] is False and rows["SW-005"]["timetable_status"] == "confirmed",
          "an inactive worker can still have a confirmed timetable")
    check(rows["SW-001"]["is_active"] is True and rows["SW-001"]["timetable_status"] == "missing",
          "an active worker can still have no timetable")

    # Two schedules together covering the week count as confirmed.
    connection = fresh_database()
    split = add_worker(connection, "SW-001", "Two Halves")
    add_schedule(connection, split, "2026-09-01", "2026-10-07", "2026-09-01 00:00")
    add_schedule(connection, split, "2026-10-08", "2026-12-11", "2026-10-08 00:00")
    connection.close()
    check(listed("SW-001")["timetable_status"] == "confirmed",
          "two confirmed semesters that together cover the week read confirmed")

    # An unconfirmed schedule filling the gap does not make the week confirmed.
    connection = fresh_database()
    mixed = add_worker(connection, "SW-001", "Mixed")
    add_schedule(connection, mixed, "2026-09-01", "2026-10-07", "2026-09-01 00:00")
    add_schedule(connection, mixed, "2026-10-08", "2026-12-11", None)
    connection.close()
    check(listed("SW-001")["timetable_status"] == "partial",
          "an unconfirmed schedule does not complete the week's confirmation")

    # The payload keeps the fields the frontend reads.
    connection = fresh_database()
    add_worker(connection, "SW-001", "Shape")
    connection.close()
    row = listed("SW-001")
    for field in ("class_block_count", "weekly_class_hours", "timetable_status",
                  "approved_leave_count", "assigned_hours", "remaining_capacity_hours"):
        check(field in row, f"the payload carries {field}")
    check("course_count" not in row and "class_meeting_count" not in row,
          f"the obsolete course-count fields are gone {sorted(row)}")


def run():
    try:
        main_checks()
    finally:
        _TEMPORARY.cleanup()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll timetable reporting checks passed (throwaway databases only).")
    return 0


if __name__ == "__main__":
    sys.exit(run())
