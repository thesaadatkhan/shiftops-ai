"""Regression checks for the schema migration.

Everything here runs against throwaway in-memory databases. The project's
own `backend/shiftops.db` is never opened, read, or modified.

`CREATE TABLE IF NOT EXISTS` does nothing once a table exists, so a column
added later never reaches a database created before it. These checks prove
the migration closes that gap without losing data:

1. On a database built from the OLD schema (no `is_active`), the migration
   adds the column and every existing row survives unchanged.
2. Workers that existed before the migration come out active.
3. Running the migration again changes nothing and reports nothing applied.
4. On a fresh database the column is present from the start and the
   migration reports nothing to do.
5. On an OLD-schema database that already holds related records - courses,
   class meetings, approved leave, shifts, preferences and assignments - the
   migration preserves their contents and their relationships, not merely
   their row counts, and a second run changes nothing.
6. An explicitly deactivated worker stays deactivated across repeat runs -
   the migration must not reset statuses back to active.

Run with:  python verify_migration.py
Exits non-zero if any check fails.
"""

import sys

from database import (
    SCHEMA_STATEMENTS,
    create_schema,
    get_connection,
    migrate_schema,
    table_columns,
)

# The employees table exactly as it was before is_active existed.
OLD_EMPLOYEES_TABLE = """
CREATE TABLE employees (
    id INTEGER PRIMARY KEY,
    employee_code TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    student_type TEXT NOT NULL
        CHECK (student_type IN ('undergraduate', 'masters')),
    weekly_hour_limit INTEGER NOT NULL DEFAULT 20
)
"""

OLD_ROWS = [
    ("SW-001", "Maria Alvarez", "undergraduate", 20),
    ("SW-002", "Jordan Kim", "masters", 20),
    ("SW-003", "Priya Natarajan", "undergraduate", 20),
]

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def old_schema_database(with_related_tables=False):
    """A database shaped like one created before is_active existed.

    Only the `employees` table differed, so the other tables are created from
    the current schema; `employees` uses the pre-migration definition above.
    """
    connection = get_connection(":memory:")
    connection.execute(OLD_EMPLOYEES_TABLE)

    if with_related_tables:
        for statement in SCHEMA_STATEMENTS:
            # Skip the current employees table; this fixture keeps the old one.
            if "CREATE TABLE IF NOT EXISTS employees" not in statement:
                connection.execute(statement)

    connection.executemany(
        """
        INSERT INTO employees (employee_code, full_name, student_type, weekly_hour_limit)
        VALUES (?, ?, ?, ?)
        """,
        OLD_ROWS,
    )
    connection.commit()
    return connection


def populate_related_records(connection):
    """Give the old-schema fixture a full set of related records."""
    employee_id = connection.execute(
        "SELECT id FROM employees WHERE employee_code = 'SW-001'"
    ).fetchone()["id"]
    other_id = connection.execute(
        "SELECT id FROM employees WHERE employee_code = 'SW-002'"
    ).fetchone()["id"]

    connection.execute(
        "INSERT INTO courses (employee_id, course_label) VALUES (?, ?)",
        (employee_id, "Writing A"),
    )
    course_id = connection.execute(
        "SELECT id FROM courses WHERE course_label = 'Writing A'"
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO class_meetings (course_id, day_of_week, start_time, end_time) "
        "VALUES (?, ?, ?, ?)",
        (course_id, 0, "09:00", "10:15"),
    )
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime) "
        "VALUES (?, ?, ?)",
        (employee_id, "2026-10-10 08:00", "2026-10-10 14:00"),
    )
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime) VALUES (?, ?, ?)",
        ("Capella", "2026-10-05 22:00", "2026-10-06 03:00"),
    )
    shift_id = connection.execute("SELECT id FROM shifts").fetchone()["id"]
    connection.execute(
        "INSERT INTO shift_preferences (employee_id, shift_id, preference) VALUES (?, ?, ?)",
        (employee_id, shift_id, "preferred"),
    )
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (other_id, shift_id),
    )
    connection.commit()


def related_snapshot(connection):
    """Contents and relationships of the related records, resolved to
    natural keys so the comparison does not depend on row ids."""
    return {
        "courses": [
            tuple(row)
            for row in connection.execute(
                "SELECT e.employee_code, c.course_label FROM courses c "
                "JOIN employees e ON e.id = c.employee_id ORDER BY 1, 2"
            )
        ],
        "class_meetings": [
            tuple(row)
            for row in connection.execute(
                "SELECT e.employee_code, c.course_label, m.day_of_week, m.start_time, m.end_time "
                "FROM class_meetings m JOIN courses c ON c.id = m.course_id "
                "JOIN employees e ON e.id = c.employee_id ORDER BY 1, 2, 3"
            )
        ],
        "approved_leave": [
            tuple(row)
            for row in connection.execute(
                "SELECT e.employee_code, l.start_datetime, l.end_datetime FROM approved_leave l "
                "JOIN employees e ON e.id = l.employee_id ORDER BY 1, 2"
            )
        ],
        "shifts": [
            tuple(row)
            for row in connection.execute(
                "SELECT hall, start_datetime, end_datetime, required_staff FROM shifts ORDER BY 1, 2"
            )
        ],
        "shift_preferences": [
            tuple(row)
            for row in connection.execute(
                "SELECT e.employee_code, s.hall, s.start_datetime, p.preference "
                "FROM shift_preferences p JOIN employees e ON e.id = p.employee_id "
                "JOIN shifts s ON s.id = p.shift_id ORDER BY 1, 2"
            )
        ],
        "assignments": [
            tuple(row)
            for row in connection.execute(
                "SELECT e.employee_code, s.hall, s.start_datetime FROM assignments a "
                "JOIN employees e ON e.id = a.employee_id "
                "JOIN shifts s ON s.id = a.shift_id ORDER BY 1, 2"
            )
        ],
    }


def employee_rows(connection):
    return [
        (row["employee_code"], row["full_name"], row["student_type"], row["weekly_hour_limit"])
        for row in connection.execute(
            "SELECT employee_code, full_name, student_type, weekly_hour_limit "
            "FROM employees ORDER BY employee_code"
        )
    ]


def main():
    # 1-3. Migrating an old-schema database.
    connection = old_schema_database()
    before = employee_rows(connection)
    check("is_active" not in table_columns(connection, "employees"), "old schema starts without is_active")

    applied = migrate_schema(connection)
    check(applied == ["employees.is_active"], f"migration reports what it applied ({applied})")
    check("is_active" in table_columns(connection, "employees"), "migration adds the is_active column")
    check(employee_rows(connection) == before, "every pre-existing employee row survives unchanged")
    check(len(employee_rows(connection)) == len(OLD_ROWS), f"row count preserved ({len(OLD_ROWS)})")

    statuses = [row["is_active"] for row in connection.execute("SELECT is_active FROM employees")]
    check(all(status == 1 for status in statuses), "pre-existing workers default to active")

    applied_again = migrate_schema(connection)
    check(applied_again == [], "re-running the migration applies nothing")
    check(employee_rows(connection) == before, "re-running the migration preserves every row")

    # 6. A deliberate deactivation must survive later migration runs.
    connection.execute("UPDATE employees SET is_active = 0 WHERE employee_code = 'SW-002'")
    connection.commit()
    migrate_schema(connection)
    migrate_schema(connection)
    still_inactive = connection.execute(
        "SELECT is_active FROM employees WHERE employee_code = 'SW-002'"
    ).fetchone()["is_active"]
    check(still_inactive == 0, "a deactivated worker stays deactivated across repeat migrations")
    others_active = [
        row["is_active"]
        for row in connection.execute(
            "SELECT is_active FROM employees WHERE employee_code <> 'SW-002'"
        )
    ]
    check(all(status == 1 for status in others_active), "deactivating one worker leaves the others active")
    connection.close()

    # 4. A fresh database already has the column.
    fresh = get_connection(":memory:")
    fresh_applied = create_schema(fresh)
    check("is_active" in table_columns(fresh, "employees"), "fresh database has is_active from the start")
    check(fresh_applied == [], "fresh database needs no migration")
    check(
        create_schema(fresh) == [],
        "re-running create_schema on a fresh database applies nothing",
    )

    fresh.close()

    # 5. The real test: an OLD-schema database that already holds related
    #    records. This is the case the migration actually has to survive.
    legacy = old_schema_database(with_related_tables=True)
    populate_related_records(legacy)

    check(
        "is_active" not in table_columns(legacy, "employees"),
        "populated legacy fixture genuinely lacks is_active before migrating",
    )
    before_related = related_snapshot(legacy)
    before_employees = employee_rows(legacy)
    check(
        all(len(rows) > 0 for rows in before_related.values()),
        f"legacy fixture holds related records in every table "
        f"({ {table: len(rows) for table, rows in before_related.items()} })",
    )

    legacy_applied = migrate_schema(legacy)
    check(legacy_applied == ["employees.is_active"], "migration runs on the populated legacy fixture")
    check(
        "is_active" in table_columns(legacy, "employees"),
        "populated legacy fixture gains is_active",
    )
    check(employee_rows(legacy) == before_employees, "legacy employee rows survive unchanged")
    check(
        related_snapshot(legacy) == before_related,
        "related record contents and relationships survive the migration exactly",
    )
    check(
        all(row["is_active"] == 1 for row in legacy.execute("SELECT is_active FROM employees")),
        "every migrated legacy worker is active",
    )

    # A second run on the populated legacy database must change nothing.
    second_run = migrate_schema(legacy)
    check(second_run == [], "second run on the legacy fixture applies nothing")
    check(
        related_snapshot(legacy) == before_related and employee_rows(legacy) == before_employees,
        "second run leaves every employee and related record untouched",
    )

    # The assignment still resolves to the right worker and shift, proving
    # the foreign keys were not disturbed.
    assignment = legacy.execute(
        "SELECT e.employee_code, s.hall, s.start_datetime, s.end_datetime "
        "FROM assignments a JOIN employees e ON e.id = a.employee_id "
        "JOIN shifts s ON s.id = a.shift_id"
    ).fetchone()
    check(
        tuple(assignment) == ("SW-002", "Capella", "2026-10-05 22:00", "2026-10-06 03:00"),
        f"assignment still joins to the correct worker and shift after migrating ({tuple(assignment)})",
    )
    legacy.close()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll migration checks passed (in-memory databases only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
