"""Regression checks for creating and editing employees.

Everything here runs against throwaway in-memory or temporary databases. The
project's own `backend/shiftops.db` is never opened, read, or modified.

Editing covers the name and student type only. An employee code is fixed once
the worker exists (D042), so there is no rename to test and no duplicate-code
check on the update path - an edit that cannot change the code cannot collide
with another worker.

Checks:

1. A worker can be created in an empty database, with no seeding.
2. Creation does not invent classes, preferences, leave or assignments.
3. New workers start active with the 20-hour weekly limit.
4. Editing keeps the internal employee id and the employee code exactly as
   they were, so every related record stays attached.
5. Duplicate employee codes are rejected when creating, including codes
   differing only by letter case.
6. Blank, whitespace-only, missing, non-text and unsupported student-type
   values are rejected with clear messages.
7. An edit submitting any other code is rejected: a different code, a
   case-only change, and another worker's code. A rejected edit changes
   nothing, and re-saving under the unchanged code still works.
8. Editing an employee code that does not exist reports not-found, whether or
   not the submitted code matches the one in the URL.
9. Saved changes survive closing and reopening the database file.
10. Demo initialization refuses on a database that already holds records, so
    an edited or deleted worker can never be restored by re-running it.
11. Employee codes typed into the Add form must be URL-safe, because the code
    appears in the path of PUT /api/employees/{employee_code}. A code
    containing "/" or similar would be accepted but then impossible to
    address. An edit refuses to change the code at all, so it has nothing to
    validate.
12. A manually managed database is refused outright by demo initialization,
    leaving the manual worker and their (absent) related records untouched.
13. An edited demo worker keeps their edit, their internal id and their
    employee code.

Run with:  python verify_employee_writes.py
Exits non-zero if any check fails.
"""

import sys
import tempfile
from pathlib import Path

from database import create_schema, get_connection
from employees import (
    EMPLOYEE_CODE_PATTERN,
    DuplicateEmployeeCode,
    EmployeeNotFound,
    EmployeeValidationError,
    create_employee,
    find_by_code,
    update_employee,
)
from seed import DatabaseNotEmpty, initialize_demo_data

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def expect_error(error_type, action, description):
    try:
        action()
        check(False, f"{description} (no error raised)")
        return None
    except error_type as error:
        check(True, f"{description} -> {error}")
        return error
    except Exception as error:  # noqa: BLE001 - surfacing the wrong type is the point
        check(False, f"{description} (raised {type(error).__name__}: {error})")
        return None


def empty_database():
    connection = get_connection(":memory:")
    create_schema(connection)
    return connection


def check_issued_codes_are_addressable():
    """10. Every issued code can still be addressed by the edit route.

    D036 required codes to be URL-safe because the code sits in the path of
    PUT /api/employees/{employee_code}. Automatic allocation (D034) satisfies
    that by construction - `SW-` plus digits contains nothing that needs
    escaping - and there is no longer any way to submit a code on create. So
    this checks the property still holds rather than checking a rejection
    that can no longer be triggered.
    """
    connection = empty_database()
    issued = [
        create_employee(connection, {"full_name": f"Worker {n}", "student_type": "masters"})[
            "employee_code"
        ]
        for n in range(1, 4)
    ]
    check(
        all(EMPLOYEE_CODE_PATTERN.match(code) for code in issued),
        f"every issued code is URL-safe by construction {issued}",
    )

    # Addressable in practice: the edit route finds each of them by code.
    for code in issued:
        edited = update_employee(
            connection,
            code,
            {"employee_code": code, "full_name": f"Edited {code}", "student_type": "undergraduate"},
        )
        check(
            edited["employee_code"] == code and edited["full_name"] == f"Edited {code}",
            f"issued code {code} can be addressed and edited",
        )

    expect_error(
        EmployeeValidationError,
        lambda: update_employee(
            connection,
            issued[0],
            {"employee_code": "SW/061", "full_name": "X", "student_type": "masters"},
        ),
        "an edit submitting a different code is rejected, URL-unsafe or not",
    )
    check(
        find_by_code(connection, issued[0])["employee_code"] == issued[0],
        "a rejected edit leaves the stored code unchanged",
    )
    connection.close()


def check_demo_initialization_is_refused():
    """11. Demo initialization never touches a manually managed database.

    Collision handling used to matter because seeding ran repeatedly and had
    to decide what to do about a manual worker holding a demo code. Demo
    initialization now only runs on an empty database, so the question is
    answered earlier and more simply: it refuses outright.
    """
    connection = empty_database()
    # Allocation issues SW-001 on an empty database, which is exactly the code
    # the first demo worker would use - so this still sets up the collision
    # that initialization must refuse rather than resolve.
    manual = create_employee(
        connection, {"full_name": "Manually Added", "student_type": "masters"}
    )
    check(manual["employee_code"] == "SW-001", "the manual worker is issued SW-001")

    try:
        initialize_demo_data(connection)
        check(False, "demo initialization should refuse a non-empty database")
    except DatabaseNotEmpty as error:
        check("employees" in str(error), f"initialization refuses and says why -> {error}")

    stored = find_by_code(connection, "SW-001")
    check(stored["id"] == manual["id"], "the manual worker keeps their internal id")
    check(stored["full_name"] == "Manually Added", "the manual worker is not overwritten")
    check(stored["seed_key"] is None, "the manual worker is not given demo provenance")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 1,
        "no demo workers are added alongside them",
    )
    attached = connection.execute(
        "SELECT (SELECT COUNT(*) FROM courses WHERE employee_id = ?) AS courses,"
        " (SELECT COUNT(*) FROM approved_leave WHERE employee_id = ?) AS leave_rows,"
        " (SELECT COUNT(*) FROM shift_preferences WHERE employee_id = ?) AS prefs",
        (manual["id"], manual["id"], manual["id"]),
    ).fetchone()
    check(tuple(attached) == (0, 0, 0), "no demo records are attached to them")
    connection.close()


def check_edits_survive_initialization_attempts():
    """12. Editing a demo worker cannot be undone by re-running initialization."""
    connection = empty_database()
    initialize_demo_data(connection)
    original_id = find_by_code(connection, "SW-001")["id"]

    update_employee(
        connection,
        "SW-001",
        {"employee_code": "SW-001", "full_name": "Edited Demo Worker", "student_type": "masters"},
    )

    try:
        initialize_demo_data(connection)
        check(False, "initialization should refuse after an edit")
    except DatabaseNotEmpty:
        check(True, "initialization refuses on an initialized database")

    edited = find_by_code(connection, "SW-001")
    check(edited is not None and edited["id"] == original_id, "the edited worker keeps their id")
    check(edited["full_name"] == "Edited Demo Worker", "the edit is not reverted")
    check(edited["employee_code"] == "SW-001", "the employee code is unchanged by the edit")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 30,
        "no extra worker appears",
    )
    connection.close()


def main():
    # 1-3. Creating into a completely empty database.
    connection = empty_database()
    check(
        connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 0,
        "fixture database starts with no employees",
    )

    created = create_employee(
        connection, {"full_name": "New Worker", "student_type": "masters"}
    )
    check(created["employee_code"] == "SW-001", "worker created in an empty database is issued SW-001")
    check(created["is_active"] == 1, "new worker starts active")
    check(created["weekly_hour_limit"] == 20, "new worker keeps the 20-hour weekly limit")
    check(created["seed_key"] is None, "manually created worker has no seed origin")

    related_counts = {
        table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in ("courses", "class_meetings", "approved_leave", "shift_preferences", "assignments")
    }
    check(
        all(count == 0 for count in related_counts.values()),
        f"creating a worker generates no classes, preferences, leave or assignments {related_counts}",
    )

    # Surrounding whitespace is trimmed rather than stored.
    trimmed = create_employee(
        connection, {"full_name": "  Spaced Name  ", "student_type": "undergraduate"}
    )
    check(
        trimmed["employee_code"] == "SW-002" and trimmed["full_name"] == "Spaced Name",
        "surrounding whitespace is trimmed on save, and the next code is issued",
    )

    # 5. Duplicate codes cannot arise on create any more: the caller does not
    #    choose the code, and the sequence never repeats a number. What is
    #    checked instead is that supplying one is refused outright rather than
    #    silently ignored.
    expect_error(
        EmployeeValidationError,
        lambda: create_employee(
            connection,
            {"employee_code": "SW-500", "full_name": "Chooser", "student_type": "masters"},
        ),
        "a client-supplied employee code is rejected on create",
    )
    expect_error(
        EmployeeValidationError,
        lambda: create_employee(
            connection,
            {"employee_code": "SW-001", "full_name": "Clash", "student_type": "masters"},
        ),
        "supplying a code that already exists is rejected as a supplied code, not as a duplicate",
    )

    # 6. Invalid input.
    invalid_cases = [
        ({"full_name": "   ", "student_type": "masters"}, "whitespace-only name"),
        ({"full_name": "X", "student_type": "professor"}, "unsupported student type"),
        ({"full_name": "X"}, "missing student type"),
        ({"student_type": "masters"}, "missing name"),
        ({"full_name": 42, "student_type": "masters"}, "non-text name"),
        ({"full_name": "X", "student_type": 7}, "non-text student type"),
    ]
    for payload, label in invalid_cases:
        expect_error(
            EmployeeValidationError,
            lambda payload=payload: create_employee(connection, payload),
            f"{label} is rejected",
        )

    check(
        connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 2,
        "no invalid or refused attempt created a row",
    )
    check(
        find_by_code(connection, "SW-500") is None and find_by_code(connection, "SW-003") is None,
        "a refused create neither honoured the supplied code nor consumed the next one",
    )

    # 4. Editing keeps identity and related records intact.
    original_id = find_by_code(connection, "SW-001")["id"]
    connection.execute("INSERT INTO courses (employee_id, course_label) VALUES (?, ?)", (original_id, "Fixture A"))
    course_id = connection.execute("SELECT id FROM courses").fetchone()["id"]
    connection.execute(
        "INSERT INTO class_meetings (course_id, day_of_week, start_time, end_time) VALUES (?, ?, ?, ?)",
        (course_id, 0, "09:00", "10:15"),
    )
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime) VALUES (?, ?, ?)",
        (original_id, "2026-10-10 08:00", "2026-10-10 14:00"),
    )
    connection.execute("INSERT INTO shifts (hall, start_datetime, end_datetime) VALUES (?, ?, ?)",
                       ("Capella", "2026-10-05 22:00", "2026-10-06 03:00"))
    shift_id = connection.execute("SELECT id FROM shifts").fetchone()["id"]
    connection.execute("INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)", (original_id, shift_id))
    connection.commit()

    edited = update_employee(
        connection,
        "SW-001",
        {"employee_code": "SW-001", "full_name": "Edited Worker", "student_type": "undergraduate"},
    )
    check(edited["id"] == original_id, "editing keeps the internal employee id stable")
    check(edited["employee_code"] == "SW-001", "the employee code is preserved exactly")
    check(edited["full_name"] == "Edited Worker", "name is updated")
    check(edited["student_type"] == "undergraduate", "student type is updated")
    check(edited["is_active"] == 1 and edited["weekly_hour_limit"] == 20,
          "editing leaves active status and the weekly limit alone")
    check(find_by_code(connection, "SW-001") is not None, "the worker is still addressable by their code")

    still_linked = connection.execute(
        "SELECT (SELECT COUNT(*) FROM courses WHERE employee_id = ?) AS courses,"
        " (SELECT COUNT(*) FROM class_meetings m JOIN courses c ON c.id = m.course_id"
        "   WHERE c.employee_id = ?) AS meetings,"
        " (SELECT COUNT(*) FROM approved_leave WHERE employee_id = ?) AS leave_rows,"
        " (SELECT COUNT(*) FROM assignments WHERE employee_id = ?) AS assignments",
        (original_id,) * 4,
    ).fetchone()
    check(
        tuple(still_linked) == (1, 1, 1, 1),
        f"every related record still points at the same worker after editing {tuple(still_linked)}",
    )

    # 4b. The employee code is immutable once the worker exists (D042).
    before_rename = dict(find_by_code(connection, "SW-001"))
    for attempted, label in [
        ("SW-999", "a different employee code"),
        ("sw-001", "a case-only change"),
        ("SW-002", "another worker's employee code"),
    ]:
        expect_error(
            EmployeeValidationError,
            lambda attempted=attempted: update_employee(
                connection,
                "SW-001",
                {"employee_code": attempted, "full_name": "Renamed", "student_type": "masters"},
            ),
            f"editing to {label} is rejected",
        )

    after_rename = dict(find_by_code(connection, "SW-001"))
    check(after_rename == before_rename, "a rejected rename changes nothing at all")
    check(
        find_by_code(connection, "SW-999") is None and find_by_code(connection, "sw-001") is None,
        "no row appears under a rejected code",
    )
    check(
        tuple(connection.execute(
            "SELECT (SELECT COUNT(*) FROM courses WHERE employee_id = ?) AS courses,"
            " (SELECT COUNT(*) FROM assignments WHERE employee_id = ?) AS assignments",
            (original_id, original_id),
        ).fetchone()) == (1, 1),
        "a rejected rename leaves related records attached",
    )

    message = ""
    try:
        update_employee(
            connection,
            "SW-001",
            {"employee_code": "SW-999", "full_name": "Renamed", "student_type": "masters"},
        )
    except EmployeeValidationError as error:
        message = str(error)
    check(
        "cannot be changed" in message and "SW-001" in message and "SW-999" in message,
        f"the refusal explains the rule and names both codes -> {message}",
    )

    # Re-saving a worker under their own unchanged code must still work.
    same = update_employee(
        connection,
        "SW-001",
        {"employee_code": "SW-001", "full_name": "Edited Worker", "student_type": "undergraduate"},
    )
    check(same["id"] == original_id, "saving a worker under their own unchanged code is allowed")

    # 7. Editing a worker that does not exist reports not-found, NOT an
    #    immutability error - the URL target is what is missing.
    expect_error(
        EmployeeNotFound,
        lambda: update_employee(
            connection,
            "SW-DOES-NOT-EXIST",
            {"employee_code": "SW-041", "full_name": "Ghost", "student_type": "masters"},
        ),
        "editing an unknown employee reports not-found even when the code differs",
    )
    expect_error(
        EmployeeNotFound,
        lambda: update_employee(
            connection,
            "SW-DOES-NOT-EXIST",
            {"employee_code": "SW-DOES-NOT-EXIST", "full_name": "Ghost", "student_type": "masters"},
        ),
        "editing an unknown employee reports not-found when the code matches too",
    )
    connection.close()

    # 8. Changes survive closing and reopening a real database file.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "persistence.db"
        first = get_connection(path)
        create_schema(first)
        made = create_employee(first, {"full_name": "Persisted", "student_type": "masters"})
        code = made["employee_code"]
        update_employee(first, code, {"employee_code": code, "full_name": "Persisted Twice", "student_type": "undergraduate"})
        first.close()

        reopened = get_connection(path)
        stored = find_by_code(reopened, code)
        check(
            stored is not None and stored["full_name"] == "Persisted Twice"
            and stored["student_type"] == "undergraduate",
            "created and edited details survive reopening the database file",
        )
        check(
            create_employee(reopened, {"full_name": "Second", "student_type": "masters"})[
                "employee_code"
            ] == "SW-002",
            "allocation also resumes correctly after reopening",
        )
        reopened.close()

    check_issued_codes_are_addressable()
    check_demo_initialization_is_refused()
    check_edits_survive_initialization_attempts()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll employee write checks passed (isolated databases only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
