"""Regression checks for Phase 6 increment 1: deterministic shift eligibility
and the read-only Coverage query (`eligibility.py`).

Runs entirely against throwaway in-memory databases with purpose-built
workers, schedules, shifts, leave and assignments. The project's own
`backend/shiftops.db` is never opened, read or modified.

Checks:

 1. An eligible worker with a confirmed, populated timetable.
 2. A deliberately confirmed "no classes" semester is valid coverage.
 3. Missing timetable information is not treated as available.
 4. An unconfirmed timetable does not count as coverage.
 5. A confirmed timetable covering only part of a cross-midnight shift's
    dates leaves the other date's part uncovered.
 6. Semester boundary dates are inclusive on both ends.
 7. A recurring block outside its own semester's dates causes no conflict.
 8. Class overlap is reported; a class touching the shift's exact boundary
    is not (D025's touching-endpoint rule).
 9. Approved-leave overlap and endpoint touching, the same rule.
10. Existing-assignment overlap and endpoint touching, the same rule.
11. An inactive worker is ineligible.
12. Preferred, neutral and low are facts only; low remains eligible when
    every hard rule passes.
13. Weekly hours exactly reaching the limit is still eligible.
14. Weekly hours exceeding the limit is not.
15. The start-week rule: a Sunday-night-to-Monday shift's hours are charged
    only to the week containing its Sunday start, never split.
16. Several simultaneous ineligibility reasons are all returned, in a fixed
    deterministic order.
17. An arbitrary, manually created worker (not a generated one) is handled
    exactly like any other.
18. `shift_coverage` raises for an unknown shift id and for a nonexistent
    excluded worker.
19. The excluded worker is removed from `eligible_candidates` but still
    appears in `results`.
20. `shift_coverage` is read-only: it makes no database changes.
21. A schedule with `confirmed_at` set but `dates_provisional = 1` (the shape
    the Phase 5C migration can produce) does NOT establish coverage.
22. Its uncovered date is reported through `missing_timetable_dates`.
23. After the real `confirm_schedule()` operation accepts the dates - clearing
    `dates_provisional` in the same write that (re)confirms - the same
    otherwise-clear worker becomes eligible.
24. A normal confirmed, non-provisional "no classes" semester remains
    eligible (re-affirms check 2 under the corrected rule).

Run with:  python verify_eligibility.py
Exits non-zero if any check fails.
"""

import sys
from datetime import datetime, timedelta

from database import create_schema, get_connection
from eligibility import (
    DATETIME_FORMAT,
    ShiftNotFound,
    UnknownExcludedWorker,
    evaluate_shift_eligibility,
    shift_coverage,
)
from timetables import confirm_schedule

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fixture_database():
    connection = get_connection(":memory:")
    create_schema(connection)
    return connection


def add_employee(connection, code, limit=20, active=True, student_type="undergraduate"):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active) VALUES (?, ?, ?, ?, ?)",
        (code, f"Fixture {code}", student_type, limit, 1 if active else 0),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]


def add_shift(connection, start, end, hall="Helix"):
    start_text, end_text = start.strftime(DATETIME_FORMAT), end.strftime(DATETIME_FORMAT)
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime) VALUES (?, ?, ?)",
        (hall, start_text, end_text),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ? AND end_datetime = ?",
        (hall, start_text, end_text),
    ).fetchone()["id"]


def add_schedule(connection, employee_id, start_date, end_date, confirmed=False):
    confirmed_at = "2026-01-01 00:00" if confirmed else None
    connection.execute(
        "INSERT INTO semester_schedules"
        " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
        " VALUES (?, ?, ?, ?, 0)",
        (employee_id, start_date, end_date, confirmed_at),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM semester_schedules WHERE employee_id = ?"
        " AND start_date = ? AND end_date = ?",
        (employee_id, start_date, end_date),
    ).fetchone()["id"]


def add_provisional_confirmed_schedule(connection, employee_id, start_date, end_date, confirmed_at):
    """The exact shape the Phase 5C migration can produce: `confirmed_at` set
    (because the stored classes matched the generated demo timetable) while
    `dates_provisional = 1` (because the legacy schema recorded no semester
    dates and the migration had to assume them). Inserted directly rather
    than through `add_schedule`, which always writes `dates_provisional = 0`
    - that helper models a supervisor's own entry, not a migrated row.
    """
    connection.execute(
        "INSERT INTO semester_schedules"
        " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
        " VALUES (?, ?, ?, ?, 1)",
        (employee_id, start_date, end_date, confirmed_at),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM semester_schedules WHERE employee_id = ?"
        " AND start_date = ? AND end_date = ?",
        (employee_id, start_date, end_date),
    ).fetchone()["id"]


def add_block(connection, schedule_id, day_of_week, start_time, end_time):
    connection.execute(
        "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time)"
        " VALUES (?, ?, ?, ?)",
        (schedule_id, day_of_week, start_time, end_time),
    )
    connection.commit()


def add_leave(connection, employee_id, start, end):
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime)"
        " VALUES (?, ?, ?)",
        (employee_id, start.strftime(DATETIME_FORMAT), end.strftime(DATETIME_FORMAT)),
    )
    connection.commit()


def add_assignment(connection, employee_id, shift_id):
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (employee_id, shift_id),
    )
    connection.commit()


def set_preference(connection, employee_id, shift_id, preference):
    connection.execute(
        "INSERT INTO shift_preferences (employee_id, shift_id, preference)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT (employee_id, shift_id) DO UPDATE SET preference = excluded.preference",
        (employee_id, shift_id, preference),
    )
    connection.commit()


def evaluate(connection, shift_id, employee_id):
    shift = connection.execute(
        "SELECT id, hall, start_datetime, end_datetime FROM shifts WHERE id = ?",
        (shift_id,),
    ).fetchone()
    employee = connection.execute(
        "SELECT id, employee_code, full_name, is_active, weekly_hour_limit"
        " FROM employees WHERE id = ?",
        (employee_id,),
    ).fetchone()
    return evaluate_shift_eligibility(connection, shift, employee)


def database_snapshot(connection):
    return tuple(connection.iterdump())


def main():
    # ---------------------------------------------------------- 1, 12, 17
    # An arbitrary manually created worker (not a generated demo one), with
    # a confirmed populated timetable that does not touch the candidate
    # shift, is eligible - and remains eligible under every preference.
    connection = fixture_database()
    worker = add_employee(connection, "SW-901")
    shift = add_shift(
        connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 14, 0)
    )  # Monday
    schedule = add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    add_block(connection, schedule, 2, "09:00", "10:15")  # Wednesday, no overlap

    result = evaluate(connection, shift, worker)
    check(result["eligible"], f"1/17: manually created worker with confirmed timetable is eligible ({result['reason_codes']})")
    check(result["shift_duration_hours"] == 6, "shift duration is computed as 6 whole hours")
    check(result["preference"] == "neutral", "12: an unset preference reads as neutral")

    set_preference(connection, worker, shift, "low")
    result = evaluate(connection, shift, worker)
    check(
        result["eligible"] and result["preference"] == "low",
        "12: a low preference is a fact, not a hard restriction - still eligible",
    )

    set_preference(connection, worker, shift, "preferred")
    result = evaluate(connection, shift, worker)
    check(result["preference"] == "preferred", "12: a preferred preference reads back correctly")

    # -------------------------------------------------------------------- 2
    # Deliberately confirmed no classes: a confirmed schedule with zero
    # blocks still counts as coverage.
    connection = fixture_database()
    worker = add_employee(connection, "SW-902")
    shift = add_shift(connection, datetime(2026, 10, 6, 8, 0), datetime(2026, 10, 6, 14, 0))
    add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    result = evaluate(connection, shift, worker)
    check(
        result["eligible"],
        "2: a confirmed empty semester ('no classes') counts as coverage",
    )

    # -------------------------------------------------------------------- 3
    # Missing timetable information entirely.
    connection = fixture_database()
    worker = add_employee(connection, "SW-903")
    shift = add_shift(connection, datetime(2026, 10, 6, 8, 0), datetime(2026, 10, 6, 14, 0))
    result = evaluate(connection, shift, worker)
    check(
        "timetable_not_confirmed" in result["reason_codes"] and not result["eligible"],
        "3: no semester schedule at all is not treated as available",
    )

    # -------------------------------------------------------------------- 4
    # An unconfirmed (but present) timetable does not count as coverage.
    connection = fixture_database()
    worker = add_employee(connection, "SW-904")
    shift = add_shift(connection, datetime(2026, 10, 6, 8, 0), datetime(2026, 10, 6, 14, 0))
    add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=False)
    result = evaluate(connection, shift, worker)
    check(
        "timetable_not_confirmed" in result["reason_codes"],
        "4: an unconfirmed timetable does not count as coverage",
    )

    # -------------------------------------------------------------------- 5
    # Cross-midnight shift, confirmed coverage for only one of its two dates.
    connection = fixture_database()
    worker = add_employee(connection, "SW-905")
    # Saturday 22:00 to Sunday 03:00 - touches both 2026-10-10 and 2026-10-11.
    shift = add_shift(connection, datetime(2026, 10, 10, 22, 0), datetime(2026, 10, 11, 3, 0))
    add_schedule(connection, worker, "2026-08-24", "2026-10-10", confirmed=True)
    result = evaluate(connection, shift, worker)
    check(
        "timetable_not_confirmed" in result["reason_codes"]
        and result["conflicts"]["missing_timetable_dates"] == ["2026-10-11"],
        f"5: partial cross-midnight coverage reports exactly the uncovered date ({result['conflicts']['missing_timetable_dates']})",
    )

    # -------------------------------------------------------------------- 6
    # Semester boundary dates are inclusive.
    connection = fixture_database()
    worker = add_employee(connection, "SW-906")
    shift = add_shift(connection, datetime(2026, 12, 11, 8, 0), datetime(2026, 12, 11, 12, 0))
    add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    result = evaluate(connection, shift, worker)
    check(result["eligible"], "6: a shift on the semester's inclusive end date is covered")

    connection2 = fixture_database()
    worker2 = add_employee(connection2, "SW-907")
    shift2 = add_shift(connection2, datetime(2026, 12, 12, 8, 0), datetime(2026, 12, 12, 12, 0))
    add_schedule(connection2, worker2, "2026-08-24", "2026-12-11", confirmed=True)
    result2 = evaluate(connection2, shift2, worker2)
    check(
        "timetable_not_confirmed" in result2["reason_codes"],
        "6: the day immediately after the semester ends is not covered",
    )

    # -------------------------------------------------------------------- 7
    # A recurring block outside its own semester's dates causes no conflict.
    connection = fixture_database()
    worker = add_employee(connection, "SW-908")
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 12, 0))  # Monday
    schedule = add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    # A Monday block, but in a semester that has already ended by the shift's date.
    other_schedule = add_schedule(connection, worker, "2026-01-01", "2026-01-31")
    add_block(connection, other_schedule, 0, "09:00", "10:00")
    result = evaluate(connection, shift, worker)
    check(
        result["eligible"] and not result["conflicts"]["class_blocks"],
        "7: a block outside its own semester's dates does not conflict",
    )

    # -------------------------------------------------------------------- 8
    # Class overlap, and touching endpoints do not conflict.
    connection = fixture_database()
    worker = add_employee(connection, "SW-909")
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 12, 0))  # Monday
    schedule = add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    add_block(connection, schedule, 0, "09:00", "10:00")  # overlaps 08:00-12:00
    result = evaluate(connection, shift, worker)
    check(
        "class_conflict" in result["reason_codes"] and len(result["conflicts"]["class_blocks"]) == 1,
        "8: a class inside the shift's hours is a conflict",
    )

    connection = fixture_database()
    worker = add_employee(connection, "SW-910")
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 12, 0))
    schedule = add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    add_block(connection, schedule, 0, "12:00", "13:00")  # starts exactly when shift ends
    result = evaluate(connection, shift, worker)
    check(
        "class_conflict" not in result["reason_codes"],
        "8: a class starting exactly when the shift ends does not conflict (touching endpoint)",
    )

    # -------------------------------------------------------------------- 9
    # Approved-leave overlap and touching endpoint.
    connection = fixture_database()
    worker = add_employee(connection, "SW-911")
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 12, 0))
    add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    add_leave(connection, worker, datetime(2026, 10, 5, 10, 0), datetime(2026, 10, 5, 11, 0))
    result = evaluate(connection, shift, worker)
    check("leave_conflict" in result["reason_codes"], "9: leave inside the shift's hours is a conflict")

    connection = fixture_database()
    worker = add_employee(connection, "SW-912")
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 12, 0))
    add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    add_leave(connection, worker, datetime(2026, 10, 5, 12, 0), datetime(2026, 10, 5, 14, 0))
    result = evaluate(connection, shift, worker)
    check(
        "leave_conflict" not in result["reason_codes"],
        "9: leave starting exactly when the shift ends does not conflict",
    )

    # ------------------------------------------------------------------- 10
    # Existing-assignment overlap and touching endpoint.
    connection = fixture_database()
    worker = add_employee(connection, "SW-913")
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 12, 0))
    other_shift = add_shift(connection, datetime(2026, 10, 5, 10, 0), datetime(2026, 10, 5, 14, 0))
    add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    add_assignment(connection, worker, other_shift)
    result = evaluate(connection, shift, worker)
    check(
        "assignment_conflict" in result["reason_codes"],
        "10: an overlapping existing assignment is a conflict",
    )

    connection = fixture_database()
    worker = add_employee(connection, "SW-914")
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 12, 0))
    other_shift = add_shift(connection, datetime(2026, 10, 5, 12, 0), datetime(2026, 10, 5, 16, 0))
    add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    add_assignment(connection, worker, other_shift)
    result = evaluate(connection, shift, worker)
    check(
        "assignment_conflict" not in result["reason_codes"],
        "10: an assignment starting exactly when the shift ends does not conflict",
    )

    # A worker already holding this exact shift does not conflict with themselves.
    connection = fixture_database()
    worker = add_employee(connection, "SW-915")
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 12, 0))
    add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    add_assignment(connection, worker, shift)
    result = evaluate(connection, shift, worker)
    check(
        "assignment_conflict" not in result["reason_codes"],
        "10: a worker's own current assignment to this exact shift is not self-conflicting",
    )

    # ------------------------------------------------------------------- 11
    connection = fixture_database()
    worker = add_employee(connection, "SW-916", active=False)
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 12, 0))
    add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    result = evaluate(connection, shift, worker)
    check("worker_inactive" in result["reason_codes"], "11: an inactive worker is ineligible")

    # ------------------------------------------------------------- 13, 14
    connection = fixture_database()
    worker = add_employee(connection, "SW-917", limit=20)
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 14, 0))  # 6h
    other_shift = add_shift(connection, datetime(2026, 10, 6, 8, 0), datetime(2026, 10, 6, 22, 0))  # 14h
    add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    add_assignment(connection, worker, other_shift)
    result = evaluate(connection, shift, worker)
    check(
        result["projected_weekly_hours"] == 20 and result["eligible"],
        f"13: weekly hours exactly reaching the limit is still eligible ({result['projected_weekly_hours']})",
    )

    connection = fixture_database()
    worker = add_employee(connection, "SW-918", limit=20)
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 15, 0))  # 7h
    other_shift = add_shift(connection, datetime(2026, 10, 6, 8, 0), datetime(2026, 10, 6, 22, 0))  # 14h
    add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    add_assignment(connection, worker, other_shift)
    result = evaluate(connection, shift, worker)
    check(
        "weekly_hour_limit_exceeded" in result["reason_codes"] and result["projected_weekly_hours"] == 21,
        f"14: weekly hours exceeding the limit is ineligible ({result['projected_weekly_hours']})",
    )

    # ------------------------------------------------------------------- 15
    # Sunday-night-to-Monday shift: charged wholly to the week containing the
    # Sunday start. An assignment the following week must not count against it.
    connection = fixture_database()
    worker = add_employee(connection, "SW-919", limit=20)
    # Sunday 2026-10-11 22:00 to Monday 2026-10-12 03:00 (5h), start-week is
    # the week of 2026-10-05.
    shift = add_shift(connection, datetime(2026, 10, 11, 22, 0), datetime(2026, 10, 12, 3, 0))
    # An assignment on Monday 2026-10-12 (the NEXT week) must not be counted.
    next_week_shift = add_shift(connection, datetime(2026, 10, 12, 8, 0), datetime(2026, 10, 12, 22, 0))  # 14h
    add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    add_assignment(connection, worker, next_week_shift)
    result = evaluate(connection, shift, worker)
    check(
        result["assigned_hours_this_week"] == 0 and result["eligible"],
        f"15: a cross-midnight shift's hours are charged only to its start week ({result['assigned_hours_this_week']})",
    )

    # ------------------------------------------------------------------- 16
    # Several simultaneous ineligibility reasons, in the fixed rule order.
    connection = fixture_database()
    worker = add_employee(connection, "SW-920", active=False, limit=1)
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 12, 0))
    schedule = add_schedule(connection, worker, "2026-08-24", "2026-12-11")  # unconfirmed
    add_block(connection, schedule, 0, "09:00", "10:00")  # overlaps
    add_leave(connection, worker, datetime(2026, 10, 5, 9, 0), datetime(2026, 10, 5, 10, 0))
    other_shift = add_shift(connection, datetime(2026, 10, 5, 9, 0), datetime(2026, 10, 5, 11, 0))
    add_assignment(connection, worker, other_shift)
    result = evaluate(connection, shift, worker)
    check(
        result["reason_codes"]
        == [
            "worker_inactive",
            "timetable_not_confirmed",
            "class_conflict",
            "leave_conflict",
            "assignment_conflict",
            "weekly_hour_limit_exceeded",
        ],
        f"16: all six simultaneous reasons are returned in deterministic order ({result['reason_codes']})",
    )

    # --------------------------------------------------------------- 18, 19
    connection = fixture_database()
    worker_a = add_employee(connection, "SW-921")
    worker_b = add_employee(connection, "SW-922")
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 12, 0))
    add_schedule(connection, worker_a, "2026-08-24", "2026-12-11", confirmed=True)
    add_schedule(connection, worker_b, "2026-08-24", "2026-12-11", confirmed=True)

    try:
        shift_coverage(connection, shift + 999)
        check(False, "18: an unknown shift id raises")
    except ShiftNotFound:
        check(True, "18: an unknown shift id raises ShiftNotFound")

    try:
        shift_coverage(connection, shift, exclude_employee_code="SW-999")
        check(False, "18: a nonexistent excluded worker raises")
    except UnknownExcludedWorker:
        check(True, "18: a nonexistent excluded worker raises UnknownExcludedWorker")

    coverage = shift_coverage(connection, shift, exclude_employee_code="SW-921")
    codes = {row["employee_code"] for row in coverage["eligible_candidates"]}
    result_codes = {row["employee_code"] for row in coverage["results"]}
    check(
        "SW-921" not in codes and "SW-922" in codes,
        f"19: the excluded worker is removed from eligible_candidates ({codes})",
    )
    check(
        "SW-921" in result_codes and "SW-922" in result_codes,
        f"19: the excluded worker still appears in results ({result_codes})",
    )

    # ------------------------------------------------------------------- 20
    before = database_snapshot(connection)
    shift_coverage(connection, shift)
    after = database_snapshot(connection)
    check(before == after, "20: shift_coverage makes no database changes")

    # --------------------------------------------------------------- 21, 22
    # A migration-shaped schedule: confirmed_at set, dates_provisional = 1.
    connection = fixture_database()
    worker_code = "SW-923"
    worker = add_employee(connection, worker_code)
    shift = add_shift(connection, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 12, 0))  # Monday
    schedule = add_provisional_confirmed_schedule(
        connection, worker, "2026-08-24", "2026-12-11", confirmed_at="2026-08-24 00:00"
    )
    result = evaluate(connection, shift, worker)
    check(
        "timetable_not_confirmed" in result["reason_codes"] and not result["eligible"],
        f"21: confirmed_at set but dates_provisional=1 does not establish coverage ({result['reason_codes']})",
    )
    check(
        result["conflicts"]["missing_timetable_dates"] == ["2026-10-05"],
        f"22: the uncovered date is reported through missing_timetable_dates ({result['conflicts']['missing_timetable_dates']})",
    )
    check(
        "confirmed and accepted" in result["reasons"][0]
        and "provisional" in result["reasons"][0],
        f"22b: the readable reason explains provisional/unaccepted dates, not just 'unconfirmed' ({result['reasons'][0]!r})",
    )

    # ------------------------------------------------------------------- 23
    # The real confirm_schedule() operation accepts the dates (submitting
    # them back unchanged, with the stored - empty - class snapshot and the
    # required no-classes acknowledgement), clearing dates_provisional in the
    # same write. The same otherwise-clear worker then becomes eligible.
    confirm_schedule(
        connection,
        worker_code,
        schedule,
        {
            "start_date": "2026-08-24",
            "end_date": "2026-12-11",
            "class_blocks": [],
            "acknowledge_no_classes": True,
        },
    )
    result = evaluate(connection, shift, worker)
    check(
        result["eligible"],
        f"23: accepting the dates via confirm_schedule() clears dates_provisional and establishes coverage ({result['reason_codes']})",
    )

    # ------------------------------------------------------------------- 24
    # A normal confirmed, non-provisional "no classes" semester (add_schedule
    # always writes dates_provisional=0) remains eligible under the corrected
    # rule - re-affirms check 2.
    connection = fixture_database()
    worker = add_employee(connection, "SW-924")
    shift = add_shift(connection, datetime(2026, 10, 6, 8, 0), datetime(2026, 10, 6, 14, 0))
    add_schedule(connection, worker, "2026-08-24", "2026-12-11", confirmed=True)
    result = evaluate(connection, shift, worker)
    check(
        result["eligible"],
        "24: a normal confirmed, non-provisional 'no classes' semester remains eligible",
    )

    if failures:
        print(f"\n{len(failures)} check(s) failed:")
        for description in failures:
            print(f" - {description}")
        sys.exit(1)
    print(f"\nAll checks passed.")


if __name__ == "__main__":
    main()
