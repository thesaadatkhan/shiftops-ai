"""Regression checks for deactivating and reactivating employees.

Everything here runs against throwaway in-memory databases. The project's own
`backend/shiftops.db` is never opened, read, or modified.

Every check passes an explicit `reference_time`, so results never depend on
when the suite happens to run. The application itself defaults that argument
to the current local time, following D025's single local simulation clock.

Checks:

1. A worker with no assignments deactivates and reactivates.
2. Deactivating changes only `is_active` - id, code, name, student type,
   weekly limit and seed provenance are untouched.
3. Courses, class meetings, approved leave, shift preferences and assignment
   history all survive both actions.
4. A shift that already finished does not block deactivation.
5. A shift still in progress DOES block it - not just shifts starting later.
6. A future shift blocks it.
7. A blocked deactivation changes nothing and explains which shifts are
   unresolved, without deleting, cancelling or reassigning them.
8. Repeating an action is harmless and idempotent.
9. An unknown employee code reports not-found for both actions.
10. Reactivation restores the same row rather than recreating it, and works
    while the worker holds unfinished assignments.
11. Status survives closing and reopening the database file.

Run with:  python verify_employee_status.py
Exits non-zero if any check fails.
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

from database import create_schema, get_connection
from employees import (
    DeactivationBlocked,
    EmployeeNotFound,
    create_employee,
    find_by_code,
    set_active,
)

# Mid-week inside the sample week, chosen so that shifts can be placed
# clearly before, around and after it.
NOW = datetime(2026, 10, 7, 12, 0)

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fixture():
    connection = get_connection(":memory:")
    create_schema(connection)
    return connection


def add_worker(connection, code="SW-001", name="Fixture Worker"):
    """Create a worker through the real path and confirm the code issued.

    Employee codes are allocated by the backend now (D034), so `code` is what
    this fixture EXPECTS rather than what it supplies. On a fresh fixture the
    sequence starts at SW-001, so the expectation is deterministic - and if
    allocation ever stopped behaving that way, these checks would say so
    instead of silently testing a different worker.
    """
    created = create_employee(
        connection, {"full_name": name, "student_type": "undergraduate"}
    )
    if created["employee_code"] != code:
        raise AssertionError(
            f"fixture expected {code} to be issued, got {created['employee_code']}"
        )
    return created


def assign(connection, employee_id, start, end, hall="Capella"):
    connection.execute(
        "INSERT OR IGNORE INTO shifts (hall, start_datetime, end_datetime) VALUES (?, ?, ?)",
        (hall, start, end),
    )
    shift_id = connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ? AND end_datetime = ?",
        (hall, start, end),
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (employee_id, shift_id),
    )
    connection.commit()
    return shift_id


def add_related_records(connection, employee_id):
    connection.execute(
        "INSERT INTO courses (employee_id, course_label) VALUES (?, ?)",
        (employee_id, "Writing A"),
    )
    course_id = connection.execute("SELECT id FROM courses").fetchone()["id"]
    connection.execute(
        "INSERT INTO class_meetings (course_id, day_of_week, start_time, end_time) VALUES (?, ?, ?, ?)",
        (course_id, 0, "09:00", "10:15"),
    )
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime) VALUES (?, ?, ?)",
        (employee_id, "2026-10-10 08:00", "2026-10-10 14:00"),
    )
    connection.execute(
        "INSERT OR IGNORE INTO shifts (hall, start_datetime, end_datetime) VALUES (?, ?, ?)",
        ("Vega", "2026-10-05 17:00", "2026-10-05 22:00"),
    )
    pref_shift = connection.execute(
        "SELECT id FROM shifts WHERE hall = 'Vega'"
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO shift_preferences (employee_id, shift_id, preference) VALUES (?, ?, ?)",
        (employee_id, pref_shift, "preferred"),
    )
    connection.commit()


def related_counts(connection, employee_id):
    return tuple(
        connection.execute(
            "SELECT (SELECT COUNT(*) FROM courses WHERE employee_id = ?) AS c,"
            " (SELECT COUNT(*) FROM class_meetings m JOIN courses c2 ON c2.id = m.course_id"
            "   WHERE c2.employee_id = ?) AS m,"
            " (SELECT COUNT(*) FROM approved_leave WHERE employee_id = ?) AS l,"
            " (SELECT COUNT(*) FROM shift_preferences WHERE employee_id = ?) AS p,"
            " (SELECT COUNT(*) FROM assignments WHERE employee_id = ?) AS a",
            (employee_id,) * 5,
        ).fetchone()
    )


def main():
    # 1-3. Plain deactivate/reactivate preserves everything but the flag.
    connection = fixture()
    worker = add_worker(connection)
    add_related_records(connection, worker["id"])
    # A finished shift, so assignment history is part of what must survive.
    assign(connection, worker["id"], "2026-10-06 17:00", "2026-10-06 22:00")
    before_related = related_counts(connection, worker["id"])
    check(
        before_related == (1, 1, 1, 1, 1),
        f"the fixture really holds one of each related record {before_related}",
    )

    deactivated = set_active(connection, "SW-001", active=False, reference_time=NOW)
    check(deactivated["is_active"] == 0, "the worker is deactivated")
    check(deactivated["id"] == worker["id"], "deactivating keeps the internal id")
    check(
        (deactivated["employee_code"], deactivated["full_name"], deactivated["student_type"])
        == (worker["employee_code"], worker["full_name"], worker["student_type"]),
        "code, name and student type are untouched",
    )
    check(
        deactivated["weekly_hour_limit"] == worker["weekly_hour_limit"]
        and deactivated["seed_key"] == worker["seed_key"],
        "weekly limit and seed provenance are untouched",
    )
    check(
        related_counts(connection, worker["id"]) == before_related,
        f"related records survive deactivation {before_related}",
    )

    reactivated = set_active(connection, "SW-001", active=True, reference_time=NOW)
    check(reactivated["is_active"] == 1, "the worker is reactivated")
    check(reactivated["id"] == worker["id"], "reactivation restores the same row, not a new one")
    check(
        related_counts(connection, worker["id"]) == before_related,
        "related records survive reactivation",
    )

    # 8. Repeating either action is harmless.
    again = set_active(connection, "SW-001", active=True, reference_time=NOW)
    check(again["is_active"] == 1 and again["id"] == worker["id"], "reactivating twice is idempotent")
    set_active(connection, "SW-001", active=False, reference_time=NOW)
    twice = set_active(connection, "SW-001", active=False, reference_time=NOW)
    check(twice["is_active"] == 0 and twice["id"] == worker["id"], "deactivating twice is idempotent")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 1,
        "repeated actions never create extra rows",
    )
    connection.close()

    # 4. A finished shift does not block.
    past = fixture()
    past_worker = add_worker(past)
    assign(past, past_worker["id"], "2026-10-06 17:00", "2026-10-06 22:00")
    result = set_active(past, "SW-001", active=False, reference_time=NOW)
    check(result["is_active"] == 0, "a shift that already finished does not block deactivation")
    check(
        past.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"] == 1,
        "the historical assignment is still attached",
    )
    past.close()

    # 5. A shift in progress blocks - the important case.
    running = fixture()
    running_worker = add_worker(running)
    assign(running, running_worker["id"], "2026-10-07 09:00", "2026-10-07 15:00")
    try:
        set_active(running, "SW-001", active=False, reference_time=NOW)
        check(False, "a shift in progress should block deactivation")
    except DeactivationBlocked as error:
        check("09:00" in str(error), f"a shift in progress blocks deactivation -> {error}")
    check(
        find_by_code(running, "SW-001")["is_active"] == 1,
        "a blocked deactivation leaves the worker active",
    )
    check(
        running.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"] == 1,
        "a blocked deactivation does not delete, cancel or reassign the shift",
    )
    running.close()

    # 6-7. A future shift blocks, and the message lists it.
    future = fixture()
    future_worker = add_worker(future)
    assign(future, future_worker["id"], "2026-10-09 17:00", "2026-10-09 22:00", hall="Sirius")
    assign(future, future_worker["id"], "2026-10-06 17:00", "2026-10-06 22:00", hall="Helix")
    try:
        set_active(future, "SW-001", active=False, reference_time=NOW)
        check(False, "a future shift should block deactivation")
    except DeactivationBlocked as error:
        message = str(error)
        check("Sirius" in message, "the message names the unresolved future shift")
        check("Helix" not in message, "the finished shift is not listed as unresolved")
        check("1 unfinished" in message, f"only the unfinished shift is counted -> {message}")
    check(
        future.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"] == 2,
        "both assignments remain after a blocked deactivation",
    )

    # 10. Reactivation works even while unfinished assignments exist.
    future.execute("UPDATE employees SET is_active = 0")
    future.commit()
    restored = set_active(future, "SW-001", active=True, reference_time=NOW)
    check(restored["is_active"] == 1, "reactivation is not blocked by unfinished assignments")
    check(restored["id"] == future_worker["id"], "reactivation keeps the same row")
    future.close()

    # 9. Unknown employee.
    missing = fixture()
    for active in (False, True):
        try:
            set_active(missing, "SW-NOPE", active=active, reference_time=NOW)
            check(False, f"unknown employee should report not-found (active={active})")
        except EmployeeNotFound as error:
            check("SW-NOPE" in str(error), f"unknown employee reports not-found (active={active})")
    missing.close()

    # 11. Status persists across reopening the file.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "status.db"
        first = get_connection(path)
        create_schema(first)
        add_worker(first)
        set_active(first, "SW-001", active=False, reference_time=NOW)
        first.close()

        reopened = get_connection(path)
        stored = find_by_code(reopened, "SW-001")
        check(stored["is_active"] == 0, "deactivated status survives reopening the database")
        set_active(reopened, "SW-001", active=True, reference_time=NOW)
        reopened.close()

        again_open = get_connection(path)
        check(
            find_by_code(again_open, "SW-001")["is_active"] == 1,
            "reactivated status survives reopening the database",
        )
        again_open.close()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll employee status checks passed (isolated databases only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
