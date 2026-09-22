"""Regression checks for demo data initialization.

Everything here runs against throwaway in-memory or temporary databases. The
project's own `backend/shiftops.db` is never opened, read, or modified.

Demo initialization is explicit, runs only on an empty database, and has no
repair, reset or fill-in-the-gaps mode. These checks pin that down:

1. An empty database is initialized into the expected demo dataset.
2. Running initialization again refuses and changes nothing.
3. A database holding a manually created worker is refused and untouched.
4. A database with no employees but other scheduling rows is also refused -
   counting employees alone would wrongly call it empty.
5. Edited and deleted demo workers are never restored by a later run.
6. A failure partway through rolls back every insert, leaving the database
   exactly as empty as it was. Shifts are not committed separately from
   workers.
7. An existing schema with no rows is still eligible.

The fixed demo figures (30 workers, 198 shifts) are asserted here, on isolated
fixtures. They are not requirements on anyone's working database, which is
expected to diverge as soon as workers are managed through the application.

Run with:  python verify_seeding.py
Exits non-zero if any check fails.
"""

import sys
import tempfile
from pathlib import Path

import seed as seed_module
from database import create_schema, get_connection
from employees import create_employee
from seed import DatabaseNotEmpty, initialize_demo_data, table_counts
from seed import CANONICAL_UNCOVERED_SHIFTS
from synthetic_data import DEMO_WEEK_STARTS, generate_required_shifts, generate_workers
from eligibility import shift_coverage
from optimizer import generate_draft

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fresh_database():
    connection = get_connection(":memory:")
    create_schema(connection)
    return connection


def expect_refusal(connection, description):
    """Initialization must refuse and leave every row exactly as it was."""
    before = table_counts(connection)
    try:
        initialize_demo_data(connection)
        check(False, f"{description} (it ran instead of refusing)")
        return None
    except DatabaseNotEmpty as error:
        after = table_counts(connection)
        check(before == after, f"{description} (nothing changed: {after})")
        return error


def main():
    shifts = [
        shift
        for week_start in DEMO_WEEK_STARTS
        for shift in generate_required_shifts(week_start)
    ]
    workers = generate_workers(shifts)
    expected_courses = sum(len(worker["courses"]) for worker in workers)
    expected_meetings = sum(
        len(meetings) for worker in workers for _, meetings in worker["courses"]
    )
    expected_leave = sum(len(worker["approved_leave"]) for worker in workers)

    # 1 + 7. An existing but empty schema is eligible, and produces the
    #        expected dataset.
    connection = fresh_database()
    check(
        all(count == 0 for count in table_counts(connection).values()),
        "an existing schema with no rows starts empty",
    )

    counts = initialize_demo_data(connection)
    check(counts["employees"] == 30, f"initialization creates 30 employees ({counts['employees']})")
    check(counts["shifts"] == 198, f"initialization creates 198 shifts ({counts['shifts']})")
    check(
        counts["semester_schedules"] == 30,
        f"initialization creates one semester schedule per worker ({counts['semester_schedules']})",
    )
    check(
        counts["class_blocks"] == expected_meetings
        and counts["approved_leave"] == expected_leave,
        f"every generated class block and leave period is written "
        f"({counts['class_blocks']} blocks vs {expected_meetings} generated meetings)",
    )
    check(counts["shift_preferences"] > 0, f"shift preferences are written ({counts['shift_preferences']})")
    check(counts["approved_leave"] == 30, "every worker has one approved-leave period")
    check(counts["assignments"] > 0, f"valid background assignments are created ({counts['assignments']})")
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM employees WHERE id NOT IN"
            " (SELECT DISTINCT employee_id FROM assignments)"
        ).fetchone()["n"] == 0,
        "every worker has at least one assignment across the two demo weeks",
    )
    for hall, start_datetime in CANONICAL_UNCOVERED_SHIFTS:
        shift_id = connection.execute(
            "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ?",
            (hall, start_datetime),
        ).fetchone()["id"]
        coverage = shift_coverage(connection, shift_id)
        assigned_count = connection.execute(
            "SELECT COUNT(*) AS n FROM assignments WHERE shift_id = ?", (shift_id,)
        ).fetchone()["n"]
        required_staff = connection.execute(
            "SELECT required_staff FROM shifts WHERE id = ?", (shift_id,)
        ).fetchone()["required_staff"]
        check(
            assigned_count < required_staff,
            f"the deliberate scenario remains uncovered: {hall} {start_datetime}",
        )
        check(
            len(coverage["eligible_candidates"]) >= 5,
            f"the scenario has at least five eligible candidates: {hall} {start_datetime}",
        )
    for week_start in DEMO_WEEK_STARTS:
        draft = generate_draft(connection, week_start.strftime("%Y-%m-%d"))
        check(
            draft["status"] == "complete",
            f"remaining positions are fully schedulable for {week_start:%Y-%m-%d}",
        )

    # 2. A second run refuses.
    error = expect_refusal(connection, "a second initialization refuses")
    check(
        error is not None and "empty database" in str(error),
        "the refusal explains that initialization only runs on an empty database",
    )

    # 5. Edits and deletions are not undone by a later run.
    connection.execute(
        "UPDATE employees SET full_name = 'Edited Name' WHERE employee_code = 'SW-001'"
    )
    deleted_id = connection.execute(
        "SELECT id FROM employees WHERE employee_code = 'SW-030'"
    ).fetchone()["id"]
    for table in ("shift_preferences", "approved_leave", "assignments"):
        connection.execute(f"DELETE FROM {table} WHERE employee_id = ?", (deleted_id,))
    connection.execute(
        "DELETE FROM class_blocks WHERE schedule_id IN"
        " (SELECT id FROM semester_schedules WHERE employee_id = ?)",
        (deleted_id,),
    )
    connection.execute(
        "DELETE FROM semester_schedules WHERE employee_id = ?", (deleted_id,)
    )
    # Legacy rows too, for a database that still holds them after migrating.
    connection.execute(
        "DELETE FROM class_meetings WHERE course_id IN (SELECT id FROM courses WHERE employee_id = ?)",
        (deleted_id,),
    )
    connection.execute("DELETE FROM courses WHERE employee_id = ?", (deleted_id,))
    connection.execute("DELETE FROM employees WHERE id = ?", (deleted_id,))
    connection.commit()

    expect_refusal(connection, "initialization still refuses after edits and a deletion")
    check(
        connection.execute(
            "SELECT full_name FROM employees WHERE employee_code = 'SW-001'"
        ).fetchone()["full_name"]
        == "Edited Name",
        "an edited demo worker is not reverted",
    )
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM employees WHERE employee_code = 'SW-030'"
        ).fetchone()["n"]
        == 0,
        "a deleted demo worker is not restored",
    )
    connection.close()

    # 3. A manually managed database is refused and left alone.
    manual = fresh_database()
    # The code is issued by the backend now (D034); on an empty database that
    # is SW-001, so this worker sits exactly where a demo worker would.
    created = create_employee(
        manual, {"full_name": "Manually Added", "student_type": "masters"}
    )
    error = expect_refusal(manual, "a database with a manually created worker is refused")
    check(
        error is not None and "employees" in str(error),
        "the refusal names the table that already holds rows",
    )
    stored = manual.execute(
        "SELECT id, full_name, seed_key FROM employees WHERE employee_code = 'SW-001'"
    ).fetchone()
    check(
        stored["id"] == created["id"] and stored["full_name"] == "Manually Added",
        "the manual worker keeps their id and details",
    )
    check(stored["seed_key"] is None, "the manual worker is not given demo provenance")
    check(
        manual.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 1,
        "no demo workers are added alongside them",
    )
    manual.close()

    # 4. No employees, but other scheduling rows: still refused.
    shifts_only = fresh_database()
    shifts_only.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime) VALUES (?, ?, ?)",
        ("Capella", "2026-10-05 22:00", "2026-10-06 03:00"),
    )
    shifts_only.commit()
    check(
        shifts_only.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 0,
        "the fixture has no employees at all",
    )
    error = expect_refusal(
        shifts_only, "a database with only shifts is refused, not treated as empty"
    )
    check(
        error is not None and "shifts" in str(error),
        "the refusal names shifts as the occupied table",
    )
    shifts_only.close()

    # 6. A failure partway through rolls everything back.
    rollback_db = fresh_database()
    original_insert_worker = seed_module.insert_worker
    state = {"calls": 0}

    def failing_insert_worker(connection, worker, shifts, shift_ids):
        state["calls"] += 1
        if state["calls"] > 5:
            raise RuntimeError("injected failure partway through initialization")
        return original_insert_worker(connection, worker, shifts, shift_ids)

    seed_module.insert_worker = failing_insert_worker
    try:
        initialize_demo_data(rollback_db)
        check(False, "injected failure should have propagated")
    except RuntimeError as error:
        check("injected failure" in str(error), "the injected failure propagates")
    finally:
        seed_module.insert_worker = original_insert_worker

    after_rollback = table_counts(rollback_db)
    check(
        all(count == 0 for count in after_rollback.values()),
        f"a failed initialization leaves every table empty {after_rollback}",
    )
    check(
        after_rollback["shifts"] == 0,
        "shifts are rolled back too - they are not committed separately from workers",
    )

    # The database is still empty, so a clean run afterwards works.
    recovered = initialize_demo_data(rollback_db)
    check(recovered["employees"] == 30, "initialization succeeds after a rolled-back attempt")
    rollback_db.close()

    # Initialized data survives closing and reopening a real file.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "demo.db"
        first = get_connection(path)
        create_schema(first)
        initialize_demo_data(first)
        first.close()

        reopened = get_connection(path)
        check(
            reopened.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 30,
            "initialized demo data survives reopening the database file",
        )
        expect_refusal(reopened, "reopening and re-running still refuses")
        reopened.close()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll demo initialization checks passed (isolated databases only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
