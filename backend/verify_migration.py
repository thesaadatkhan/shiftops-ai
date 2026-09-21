"""Regression checks for the schema migration.

Everything here runs against throwaway in-memory or temporary-file databases.
The project's own `backend/shiftops.db` is never opened, read, or modified.

`CREATE TABLE IF NOT EXISTS` does nothing once a table exists, so a column
added later never reaches a database created before it. These checks prove
the migration closes that gap without losing data:

1. On a database built from the OLD schema (no `is_active`, no `seed_key`),
   the migration adds both columns and every existing row survives
   unchanged. `seed_key` is back-filled from `employee_code`, which is what
   seeding previously matched on.
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
7. Two connections starting `migrate_schema()` together on a database that
   predates `semester_schedules.dates_provisional` do not collide: exactly
   one applies the column, the other rechecks after the lock and applies
   nothing, both succeed, and the backfill and unrelated rows are correct.
   Two full concurrent `create_schema()` startups succeed the same way.

Run with:  python verify_migration.py
Exits non-zero if any check fails.
"""

import sys
import tempfile
import threading
from pathlib import Path

from database import (
    MIGRATION_NOTE_PREFIX,
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


# --------------------------------------------------------------------------
# 7. Two connections migrating together must not collide on ALTER TABLE.
#
#    Codex reproduced the race: both connections ran PRAGMA table_info,
#    both saw dates_provisional absent (the check happened before either
#    held the write lock), and both issued ALTER TABLE. The second one hit
#    "duplicate column name". The fix is lock ordering: BEGIN IMMEDIATE
#    first, decide what to add only once the lock is held.
# --------------------------------------------------------------------------

# semester_schedules exactly as it was before dates_provisional existed.
OLD_SEMESTER_SCHEDULES_TABLE = """
CREATE TABLE semester_schedules (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE (employee_id, start_date, end_date)
)
"""


def pre_provisional_database(path):
    """A file-backed database shaped like one before dates_provisional
    existed, with is_active/seed_key already present so the race under test
    is isolated to the one column this checks.

    A real file, not `:memory:`: two separate connections need to see the
    same database, and each `:memory:` connection is a private database of
    its own.
    """
    connection = get_connection(path)
    for statement in SCHEMA_STATEMENTS:
        if "CREATE TABLE IF NOT EXISTS semester_schedules" in statement:
            connection.execute(OLD_SEMESTER_SCHEDULES_TABLE)
        else:
            connection.execute(statement)
    connection.commit()
    return connection


def populate_provisional_fixture(connection):
    """One migrated worker (provisional) and one manually-entered worker
    (not provisional), so the backfill has something real to get right."""
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type) "
        "VALUES ('SW-001', 'Migrated Worker', 'undergraduate')"
    )
    migrated_id = connection.execute(
        "SELECT id FROM employees WHERE employee_code = 'SW-001'"
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO semester_schedules (employee_id, start_date, end_date, confirmed_at) "
        "VALUES (?, '2026-08-24', '2026-12-11', NULL)",
        (migrated_id,),
    )
    migrated_schedule_id = connection.execute(
        "SELECT id FROM semester_schedules WHERE employee_id = ?", (migrated_id,)
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time, source_note) "
        "VALUES (?, 0, '09:00', '10:15', ?)",
        (migrated_schedule_id, f"{MIGRATION_NOTE_PREFIX} legacy course data, timetable unconfirmed"),
    )

    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type) "
        "VALUES ('SW-002', 'Direct Entry Worker', 'masters')"
    )
    direct_id = connection.execute(
        "SELECT id FROM employees WHERE employee_code = 'SW-002'"
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO semester_schedules (employee_id, start_date, end_date, confirmed_at) "
        "VALUES (?, '2026-08-24', '2026-12-11', NULL)",
        (direct_id,),
    )
    direct_schedule_id = connection.execute(
        "SELECT id FROM semester_schedules WHERE employee_id = ?", (direct_id,)
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time, source_note) "
        "VALUES (?, 1, '11:00', '12:15', NULL)",
        (direct_schedule_id,),
    )
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime) VALUES "
        "('Vega', '2026-10-05 17:00', '2026-10-05 22:00')"
    )
    connection.commit()
    return migrated_id, direct_id


def snapshot_all(connection, tables):
    return {
        table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
        for table in tables
    }


def check_concurrent_column_migration():
    tables = [
        "employees",
        "semester_schedules",
        "class_blocks",
        "shifts",
        "shift_preferences",
        "approved_leave",
        "assignments",
    ]

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "concurrent-migration.db"
        builder = pre_provisional_database(path)
        migrated_id, direct_id = populate_provisional_fixture(builder)
        check(
            "dates_provisional" not in table_columns(builder, "semester_schedules"),
            "the fixture genuinely lacks dates_provisional before migrating",
        )
        before = snapshot_all(builder, tables)
        builder.close()

        # Each thread opens its OWN connection, inside its OWN thread -
        # sqlite3 connections cannot cross threads. Both read the column
        # list first and only then wait on the barrier, so both are
        # provably holding a pre-migration view before either takes the
        # write lock. The barrier is released BEFORE either calls
        # migrate_schema - putting it after one has the lock would
        # deadlock the other against it.
        both_ready = threading.Barrier(2)
        results = {}
        errors = []
        saw_column = {}

        def racer(name):
            own = get_connection(path)
            own.execute("PRAGMA busy_timeout = 10000")
            try:
                saw_column[name] = "dates_provisional" in table_columns(
                    own, "semester_schedules"
                )
                both_ready.wait(timeout=10)
                results[name] = migrate_schema(own)
            except Exception as error:  # noqa: BLE001 - recorded, not swallowed
                errors.append(f"{name}: {type(error).__name__}: {error}")
            finally:
                own.close()

        threads = [threading.Thread(target=racer, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        check(errors == [], f"both concurrent migrations succeeded ({errors})")
        check(
            saw_column.get("a") is False and saw_column.get("b") is False,
            f"both callers genuinely held a pre-migration view {saw_column}",
        )
        check(
            sorted(results.get("a", []) + results.get("b", [])).count(
                "semester_schedules.dates_provisional"
            )
            == 1,
            "exactly one caller applies the column; the other rechecks "
            f"after the lock and finds nothing to do ({results})",
        )

        after = get_connection(path)
        column_count = sum(
            1
            for row in after.execute("PRAGMA table_info(semester_schedules)")
            if row["name"] == "dates_provisional"
        )
        check(column_count == 1, f"the column exists exactly once ({column_count})")

        rows = {
            row["employee_id"]: row["dates_provisional"]
            for row in after.execute(
                "SELECT employee_id, dates_provisional FROM semester_schedules"
            )
        }
        check(
            rows.get(migrated_id) == 1,
            f"the migrated schedule backfills as provisional ({rows.get(migrated_id)})",
        )
        check(
            rows.get(direct_id) == 0,
            f"the directly-entered schedule stays non-provisional ({rows.get(direct_id)})",
        )

        unchanged = snapshot_all(after, tables)
        for table in tables:
            if table == "semester_schedules":
                continue  # dates_provisional is the expected, checked change.
            check(
                unchanged[table] == before[table],
                f"{table} is unchanged by the concurrent migration",
            )

        check(
            migrate_schema(after) == [],
            "a later call is a no-op once both concurrent callers are done",
        )
        after.close()

    # A second fixture: two full create_schema() calls racing at once, the
    # real startup path.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "concurrent-startup.db"
        builder = pre_provisional_database(path)
        populate_provisional_fixture(builder)
        builder.close()

        start_together = threading.Barrier(2)
        startup_errors = []

        def starter():
            own = get_connection(path)
            own.execute("PRAGMA busy_timeout = 10000")
            try:
                start_together.wait(timeout=10)
                create_schema(own)
            except Exception as error:  # noqa: BLE001
                startup_errors.append(f"{type(error).__name__}: {error}")
            finally:
                own.close()

        threads = [threading.Thread(target=starter) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        check(
            startup_errors == [],
            f"two simultaneous full startups both succeed ({startup_errors})",
        )
        after = get_connection(path)
        column_count = sum(
            1
            for row in after.execute("PRAGMA table_info(semester_schedules)")
            if row["name"] == "dates_provisional"
        )
        check(
            column_count == 1,
            f"two simultaneous startups still add the column exactly once ({column_count})",
        )
        after.close()


def main():
    # 1-3. Migrating an old-schema database.
    connection = old_schema_database()
    before = employee_rows(connection)
    check("is_active" not in table_columns(connection, "employees"), "old schema starts without is_active")

    applied = migrate_schema(connection)
    check(
        applied == ["employees.is_active", "employees.seed_key"],
        f"migration reports what it applied ({applied})",
    )
    check("is_active" in table_columns(connection, "employees"), "migration adds the is_active column")
    check("seed_key" in table_columns(connection, "employees"), "migration adds the seed_key column")
    check(
        all(
            row["seed_key"] == row["employee_code"]
            for row in connection.execute("SELECT employee_code, seed_key FROM employees")
        ),
        "seed_key is back-filled from employee_code for pre-existing rows",
    )
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
    check(
        legacy_applied == ["employees.is_active", "employees.seed_key"],
        f"migration runs on the populated legacy fixture ({legacy_applied})",
    )
    check(
        {"is_active", "seed_key"} <= table_columns(legacy, "employees"),
        "populated legacy fixture gains is_active and seed_key",
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

    # 7. Concurrent migration must not collide on ALTER TABLE.
    check_concurrent_column_migration()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll migration checks passed (in-memory databases only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
