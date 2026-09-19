"""Regression checks for seeding behaviour.

Everything here runs against a throwaway in-memory database. The project's
own `backend/shiftops.db` is never opened, read, or modified.

The checks exist because routine seeding must be safe to run at any time:

1. Fresh seeding creates the expected dataset.
2. Repeated routine seeding creates no duplicates.
3. Edited worker details, and added class meetings, leave and preferences,
   survive routine reseeding.
4. Existing assignments and unrelated workers are left alone.
5. Repair mode does restore the generator's values, while still preserving
   assignments and workers the generator does not produce.

Run with:  python verify_seeding.py
Exits non-zero if any check fails.
"""

import sys

from database import create_schema, get_connection
from seed import seed, table_counts
from synthetic_data import generate_required_shifts, generate_workers

EDITED_NAME = "Edited Name (kept by routine seeding)"
EXTRA_LEAVE = ("2026-10-08 09:00", "2026-10-08 15:00")
EXTRA_MEETING = (4, "07:30", "08:45")
UNRELATED_CODE = "SW-999"

failures = []


def check(condition, description):
    if condition:
        print(f"PASS  {description}")
    else:
        print(f"FAIL  {description}")
        failures.append(description)


def fresh_database():
    connection = get_connection(":memory:")
    create_schema(connection)
    return connection


def employee_id(connection, employee_code):
    return connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (employee_code,)
    ).fetchone()["id"]


def main():
    shifts = generate_required_shifts()
    workers = generate_workers(shifts)
    expected_courses = sum(len(worker["courses"]) for worker in workers)
    expected_meetings = sum(
        len(meetings) for worker in workers for _, meetings in worker["courses"]
    )
    expected_leave = sum(len(worker["approved_leave"]) for worker in workers)

    connection = fresh_database()

    # 1. Fresh seeding creates the expected dataset.
    print(seed(connection))
    counts = table_counts(connection)
    check(counts["employees"] == 30, "fresh seed creates 30 employees")
    check(counts["shifts"] == 99, "fresh seed creates 99 shifts")
    check(
        counts["courses"] == expected_courses
        and counts["class_meetings"] == expected_meetings
        and counts["approved_leave"] == expected_leave,
        "fresh seed writes every generated course, meeting and leave period",
    )
    check(counts["assignments"] == 0, "fresh seed creates no assignments")
    first_counts = counts

    # 2. Repeated routine seeding creates no duplicates.
    print(seed(connection))
    check(
        table_counts(connection) == first_counts,
        "second routine seed changes no row counts",
    )

    # 3. Edits and additions survive routine reseeding. This is the scenario
    #    from review: approved leave added for Maria must not be deleted.
    maria_id = employee_id(connection, "SW-001")
    connection.execute(
        "UPDATE employees SET full_name = ? WHERE id = ?", (EDITED_NAME, maria_id)
    )
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime) "
        "VALUES (?, ?, ?)",
        (maria_id, *EXTRA_LEAVE),
    )
    maria_course_id = connection.execute(
        "SELECT id FROM courses WHERE employee_id = ? ORDER BY id LIMIT 1", (maria_id,)
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO class_meetings (course_id, day_of_week, start_time, end_time) "
        "VALUES (?, ?, ?, ?)",
        (maria_course_id, *EXTRA_MEETING),
    )
    spare_shift_id = connection.execute(
        """
        SELECT id FROM shifts
        WHERE id NOT IN (SELECT shift_id FROM shift_preferences WHERE employee_id = ?)
        ORDER BY id LIMIT 1
        """,
        (maria_id,),
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO shift_preferences (employee_id, shift_id, preference) "
        "VALUES (?, ?, ?)",
        (maria_id, spare_shift_id, "preferred"),
    )

    # 4. An unrelated worker and an assignment that seeding must not disturb.
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type) "
        "VALUES (?, ?, ?)",
        (UNRELATED_CODE, "Unrelated Worker", "masters"),
    )
    unrelated_id = employee_id(connection, UNRELATED_CODE)
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (maria_id, spare_shift_id),
    )
    connection.commit()

    print(seed(connection))

    stored_name = connection.execute(
        "SELECT full_name FROM employees WHERE id = ?", (maria_id,)
    ).fetchone()["full_name"]
    check(stored_name == EDITED_NAME, "edited worker name survives routine seeding")

    kept_leave = connection.execute(
        "SELECT COUNT(*) AS n FROM approved_leave "
        "WHERE employee_id = ? AND start_datetime = ?",
        (maria_id, EXTRA_LEAVE[0]),
    ).fetchone()["n"]
    check(kept_leave == 1, "added approved leave survives routine seeding")

    kept_meeting = connection.execute(
        "SELECT COUNT(*) AS n FROM class_meetings "
        "WHERE course_id = ? AND start_time = ?",
        (maria_course_id, EXTRA_MEETING[1]),
    ).fetchone()["n"]
    check(kept_meeting == 1, "added class meeting survives routine seeding")

    kept_preference = connection.execute(
        "SELECT COUNT(*) AS n FROM shift_preferences "
        "WHERE employee_id = ? AND shift_id = ?",
        (maria_id, spare_shift_id),
    ).fetchone()["n"]
    check(kept_preference == 1, "added shift preference survives routine seeding")

    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM assignments WHERE employee_id = ? AND shift_id = ?",
            (maria_id, spare_shift_id),
        ).fetchone()["n"]
        == 1,
        "existing assignment survives routine seeding",
    )
    check(
        connection.execute(
            "SELECT full_name FROM employees WHERE id = ?", (unrelated_id,)
        ).fetchone()["full_name"]
        == "Unrelated Worker",
        "unrelated worker survives routine seeding",
    )

    # 5. Repair mode restores generator values, but still protects
    #    assignments and workers the generator does not produce.
    print(seed(connection, repair=True))

    repaired_name = connection.execute(
        "SELECT full_name FROM employees WHERE id = ?", (maria_id,)
    ).fetchone()["full_name"]
    check(repaired_name == "Maria Alvarez", "repair restores the generated name")
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM approved_leave "
            "WHERE employee_id = ? AND start_datetime = ?",
            (maria_id, EXTRA_LEAVE[0]),
        ).fetchone()["n"]
        == 0,
        "repair removes leave the generator does not produce",
    )
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM class_meetings "
            "WHERE course_id = ? AND start_time = ?",
            (maria_course_id, EXTRA_MEETING[1]),
        ).fetchone()["n"]
        == 0,
        "repair removes class meetings the generator does not produce",
    )
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM assignments WHERE employee_id = ? AND shift_id = ?",
            (maria_id, spare_shift_id),
        ).fetchone()["n"]
        == 1,
        "repair preserves existing assignments",
    )
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM employees WHERE employee_code = ?",
            (UNRELATED_CODE,),
        ).fetchone()["n"]
        == 1,
        "repair preserves workers the generator does not produce",
    )

    repaired_counts = table_counts(connection)
    check(
        repaired_counts["courses"] == first_counts["courses"]
        and repaired_counts["class_meetings"] == first_counts["class_meetings"]
        and repaired_counts["approved_leave"] == first_counts["approved_leave"]
        and repaired_counts["shift_preferences"] == first_counts["shift_preferences"],
        "repair returns the seed workers' rows to the fresh-seed totals",
    )

    connection.close()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll seeding checks passed (in-memory database only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
