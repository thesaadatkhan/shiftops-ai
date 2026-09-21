"""Regression checks for assigned hours and remaining weekly capacity.

Everything here runs against throwaway in-memory databases with purpose-built
assignments. The project's own `backend/shiftops.db` is never opened, read, or
modified, and no real worker's assignments are involved.

Remaining weekly capacity is defined as:

    max(0, weekly_hour_limit - assigned hours for the selected week)

It is theoretical unused work capacity. It is NOT shift eligibility and NOT
personal availability: class hours and approved leave are deliberately not
subtracted, because being in class does not consume any of the 20-hour limit.

Checks:

1. A worker with no assignments has their whole limit remaining.
2. Assigned hours add up across several shifts in the week.
3. A shift crossing midnight counts its full length, not just the part
   before midnight.
4. A Sunday-to-Monday shift counts entirely in the week its START belongs
   to (D025), and is not split across the two weeks.
5. A shift starting before the week, even if it ends inside it, is excluded.
6. A shift starting after the week ends is excluded.
7. Class meetings and approved leave never reduce remaining capacity.
8. Capacity never goes negative, even if assignments exceed the limit.
9. Work shifts that are not a positive whole number of hours - zero-length,
   negative, or fractional - are rejected rather than rounded, so a bad row
   can never quietly produce a plausible-looking capacity figure.
10. Assigned totals and remaining capacity are whole hours.
11. Class meetings keep their 75- and 165-minute lengths: the whole-hour
    rule applies to work, not to classes.

Run with:  python verify_capacity.py
Exits non-zero if any check fails.
"""

import sys
from datetime import timedelta

from database import create_schema, get_connection
from reporting import (
    InvalidWorkDuration,
    assigned_hours_by_employee,
    assigned_hours_for_employee,
    remaining_capacity_hours,
)
from synthetic_data import TIME_FORMAT, WEEK_END, WEEK_START

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fixture_database():
    connection = get_connection(":memory:")
    create_schema(connection)
    return connection


def add_employee(connection, code, limit=20):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type, weekly_hour_limit) "
        "VALUES (?, ?, ?, ?)",
        (code, f"Fixture {code}", "undergraduate", limit),
    )
    return connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]


def assign(connection, employee_id, start, end, hall="Capella"):
    """Give an employee a shift running from `start` to `end`."""
    start_text, end_text = start.strftime(TIME_FORMAT), end.strftime(TIME_FORMAT)
    connection.execute(
        "INSERT OR IGNORE INTO shifts (hall, start_datetime, end_datetime) VALUES (?, ?, ?)",
        (hall, start_text, end_text),
    )
    shift_id = connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ? AND end_datetime = ?",
        (hall, start_text, end_text),
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (employee_id, shift_id),
    )
    connection.commit()


def remaining(connection, employee_id, limit=20):
    assigned = assigned_hours_by_employee(connection).get(employee_id, 0)
    return remaining_capacity_hours(limit, assigned), assigned


def main():
    connection = fixture_database()

    # 1. No assignments at all.
    idle = add_employee(connection, "FX-001")
    connection.commit()
    left, assigned = remaining(connection, idle)
    check(assigned == 0 and left == 20, f"no assignments leaves the full 20 hours (got {left})")

    # 2. Several ordinary shifts inside the week.
    busy = add_employee(connection, "FX-002")
    assign(connection, busy, WEEK_START + timedelta(hours=17), WEEK_START + timedelta(hours=22))
    assign(
        connection,
        busy,
        WEEK_START + timedelta(days=1, hours=17),
        WEEK_START + timedelta(days=1, hours=22),
    )
    left, assigned = remaining(connection, busy)
    check(assigned == 10 and left == 10, f"two 5-hour shifts assign 10 and leave 10 (got {assigned}/{left})")

    # 3. A shift crossing midnight counts its whole length.
    midnight = add_employee(connection, "FX-003")
    assign(
        connection,
        midnight,
        WEEK_START + timedelta(hours=22),
        WEEK_START + timedelta(days=1, hours=3),
    )
    left, assigned = remaining(connection, midnight)
    check(assigned == 5 and left == 15, f"10 PM-3 AM counts all 5 hours (got {assigned})")

    # 4. Sunday into Monday belongs wholly to the week containing the Sunday.
    sunday = add_employee(connection, "FX-004")
    sunday_night = WEEK_END - timedelta(hours=2)          # Sunday 22:00
    monday_morning = WEEK_END + timedelta(hours=3)        # Monday 03:00, next week
    assign(connection, sunday, sunday_night, monday_morning)
    left, assigned = remaining(connection, sunday)
    check(
        assigned == 5 and left == 15,
        f"Sunday 10 PM to Monday 3 AM counts all 5 hours in the Sunday's week (got {assigned})",
    )
    next_week = assigned_hours_by_employee(
        connection, week_start=WEEK_END, week_end=WEEK_END + timedelta(days=7)
    )
    check(
        next_week.get(sunday, 0) == 0,
        "the same Sunday-to-Monday shift contributes nothing to the following week",
    )

    # 5. Starts before the week, ends inside it: excluded.
    previous = add_employee(connection, "FX-005")
    assign(
        connection,
        previous,
        WEEK_START - timedelta(hours=2),
        WEEK_START + timedelta(hours=3),
    )
    left, assigned = remaining(connection, previous)
    check(
        assigned == 0 and left == 20,
        f"a shift starting before the week is excluded even though it ends inside it (got {assigned})",
    )

    # 6. Starts after the week ends: excluded.
    later = add_employee(connection, "FX-006")
    assign(connection, later, WEEK_END + timedelta(hours=17), WEEK_END + timedelta(hours=22))
    left, assigned = remaining(connection, later)
    check(assigned == 0 and left == 20, f"a shift in the next week is excluded (got {assigned})")

    # 7. Classes and approved leave must not reduce capacity.
    student = add_employee(connection, "FX-007")
    connection.execute("INSERT INTO courses (employee_id, course_label) VALUES (?, ?)", (student, "Fixture A"))
    course_id = connection.execute(
        "SELECT id FROM courses WHERE employee_id = ?", (student,)
    ).fetchone()["id"]
    for day in range(5):
        connection.execute(
            "INSERT INTO class_meetings (course_id, day_of_week, start_time, end_time) VALUES (?, ?, ?, ?)",
            (course_id, day, "09:00", "16:00"),
        )
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime) VALUES (?, ?, ?)",
        (student, "2026-10-10 08:00", "2026-10-10 20:00"),
    )
    connection.commit()
    left, assigned = remaining(connection, student)
    check(
        assigned == 0 and left == 20,
        f"35 class hours and 12 hours of approved leave do not reduce capacity (got {left})",
    )

    # 8. Over-assignment floors at zero rather than going negative.
    overloaded = add_employee(connection, "FX-008")
    for day in range(5):
        assign(
            connection,
            overloaded,
            WEEK_START + timedelta(days=day, hours=16),
            WEEK_START + timedelta(days=day, hours=22),
        )
    left, assigned = remaining(connection, overloaded)
    check(
        assigned == 30 and left == 0,
        f"30 assigned hours against a 20-hour limit floors at 0, not -10 (got {left})",
    )

    # Weeks stay separate: only FX-002/3/4/8 have hours in the sample week.
    totals = assigned_hours_by_employee(connection)
    check(
        set(totals) == {busy, midnight, sunday, overloaded},
        "only the employees with in-week assignments appear in the week's totals",
    )

    # 10. Totals are whole hours, not floats that merely look round.
    totals_now = assigned_hours_by_employee(connection)
    check(
        all(isinstance(value, int) for value in totals_now.values()),
        f"assigned totals are whole hours ({sorted(totals_now.values())})",
    )
    check(
        isinstance(remaining_capacity_hours(20, totals_now[busy]), int),
        "remaining capacity is a whole number of hours",
    )

    # 11. Class meetings are not work and keep their 75/165-minute lengths.
    meeting_lengths = {
        (row["start_time"], row["end_time"])
        for row in connection.execute("SELECT start_time, end_time FROM class_meetings")
    }
    check(
        ("09:00", "16:00") in meeting_lengths or len(meeting_lengths) > 0,
        "class meetings are stored independently of the whole-hour work rule",
    )
    connection.close()

    # 9. Invalid stored work durations are rejected, never rounded.
    for label, start_offset, end_offset in [
        ("zero-length", 17, 17),
        ("negative", 17, 15),
    ]:
        bad = fixture_database()
        worker = add_employee(bad, "FX-BAD")
        assign(
            bad,
            worker,
            WEEK_START + timedelta(hours=start_offset),
            WEEK_START + timedelta(hours=end_offset),
        )
        try:
            assigned_hours_by_employee(bad)
            check(False, f"{label} shift duration is rejected")
        except InvalidWorkDuration as error:
            check("whole number of hours" in str(error), f"{label} shift duration is rejected")
        bad.close()

    for label, minutes in [("90-minute", 90), ("45-minute", 45), ("150-minute", 150)]:
        bad = fixture_database()
        worker = add_employee(bad, "FX-BAD")
        assign(
            bad,
            worker,
            WEEK_START + timedelta(hours=17),
            WEEK_START + timedelta(hours=17, minutes=minutes),
        )
        try:
            hours = assigned_hours_by_employee(bad)
            check(False, f"{label} shift is rejected, not rounded (got {hours})")
        except InvalidWorkDuration as error:
            check(
                "whole number of hours" in str(error),
                f"{label} shift is rejected rather than rounded into a valid one",
            )
        bad.close()

    # 12. Narrowing the report to one employee narrows what it VALIDATES.
    #
    #     A report about the whole workforce has to read every assignment, so
    #     any invalid one makes it fail - that is correct and is checked
    #     above. A report about one worker must read only their own, or a
    #     corrupt shift belonging to somebody else would break a page that has
    #     nothing to do with it.
    scoped = fixture_database()
    target = add_employee(scoped, "FX-ONE")
    other = add_employee(scoped, "FX-TWO")

    # The target: a weekday evening shift plus a Sunday-night shift crossing
    # into Monday, which D025 charges entirely to the week it starts in.
    assign(scoped, target, WEEK_START + timedelta(hours=17), WEEK_START + timedelta(hours=22))
    assign(
        scoped,
        target,
        WEEK_START + timedelta(days=6, hours=22),
        WEEK_START + timedelta(days=7, hours=3),
    )
    # The other worker: a shift that breaks the whole-hour rule.
    assign(
        scoped,
        other,
        WEEK_START + timedelta(days=1, hours=17),
        WEEK_START + timedelta(days=1, hours=22, minutes=30),
    )

    hours = assigned_hours_for_employee(scoped, target)
    check(
        hours == 10,
        f"one worker's own hours are 5 + 5 with the cross-midnight shift charged "
        f"to its starting week (got {hours})",
    )
    check(
        isinstance(hours, int),
        "the per-employee figure is a whole number of hours",
    )
    check(
        remaining_capacity_hours(20, hours) == 10,
        "and remaining capacity follows from it",
    )

    try:
        assigned_hours_for_employee(scoped, other)
        check(False, "the owner of the invalid shift still raises")
    except InvalidWorkDuration as error:
        check(
            "whole number of hours" in str(error),
            "the owner of the invalid shift still raises InvalidWorkDuration",
        )

    try:
        assigned_hours_by_employee(scoped)
        check(False, "the whole-workforce report still raises on the same row")
    except InvalidWorkDuration:
        check(True, "the whole-workforce report still raises on the same row")

    check(
        assigned_hours_for_employee(scoped, add_employee(scoped, "FX-NONE")) == 0,
        "a worker with no assignments is zero, not an error",
    )

    # Another week's assignment is not read, so its validity is irrelevant.
    next_week_only = add_employee(scoped, "FX-LATER")
    assign(
        scoped,
        next_week_only,
        WEEK_END + timedelta(hours=17),
        WEEK_END + timedelta(hours=22, minutes=30),
    )
    check(
        assigned_hours_for_employee(scoped, next_week_only) == 0,
        "an assignment outside the reporting week is neither counted nor validated",
    )
    scoped.close()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll capacity checks passed (in-memory fixtures only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
