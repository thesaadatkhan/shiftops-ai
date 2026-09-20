"""Initialize the demo workforce, required shifts, preferences, and leave.

This runs **once**, against an **empty** database, and is the only way demo
data ever enters the application. After that, workers are managed through the
employee endpoints and the records in the database are the source of truth.

    python seed.py

There is deliberately no repair, reset, force or "fill in what is missing"
mode. Those existed when seeding was expected to run repeatedly, and they
made it possible for a re-run to overwrite edits, resurrect deleted workers,
or inject demo workers into a database someone was managing by hand. Removing
them removes that whole class of problem: if any workforce or scheduling
table already holds rows, initialization refuses and changes nothing.

Checking only the employee count would not be enough - a database can have no
employees but still hold shifts or assignments from earlier work - so every
table below is checked.

Neither application startup nor any employee-management action calls this
module. It is a command you run on purpose.
"""

import sys

from database import create_schema, get_connection
from synthetic_data import expand_preferences, generate_required_shifts, generate_workers

# Every table demo initialization writes to.
WORKFORCE_TABLES = (
    "employees",
    "courses",
    "class_meetings",
    "shifts",
    "shift_preferences",
    "approved_leave",
    "assignments",
)

# Every table that must be empty before initialization may run: the tables
# above, plus the retired-code ledger.
#
# The ledger is included even though initialization never writes to it. A
# database that has retired an employee code has been used, and initializing
# demo data into it could hand SW-005 to a demo worker after a real one had
# already been deleted under that code - which is exactly the reuse D040
# exists to prevent.
TABLES_THAT_MUST_BE_EMPTY = WORKFORCE_TABLES + ("retired_employee_codes",)


class DatabaseNotEmpty(RuntimeError):
    """Demo initialization was attempted on a database that already has data."""


def non_empty_tables(connection):
    """Tables that must be empty but already hold rows, as {table: count}."""
    counts = {}
    for table in TABLES_THAT_MUST_BE_EMPTY:
        count = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        if count:
            counts[table] = count
    return counts


def table_counts(connection):
    return {
        table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in WORKFORCE_TABLES
    }


def insert_shifts(connection, shifts):
    for shift in shifts:
        connection.execute(
            """
            INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)
            VALUES (?, ?, ?, ?)
            """,
            (
                shift["hall"],
                shift["start_datetime"],
                shift["end_datetime"],
                shift["required_staff"],
            ),
        )

    return {
        (row["hall"], row["start_datetime"], row["end_datetime"]): row["id"]
        for row in connection.execute(
            "SELECT id, hall, start_datetime, end_datetime FROM shifts"
        )
    }


def insert_worker(connection, worker, shifts, shift_ids):
    """Insert one demo worker with their courses, meetings, leave and preferences."""
    connection.execute(
        """
        INSERT INTO employees (employee_code, full_name, student_type, seed_key)
        VALUES (?, ?, ?, ?)
        """,
        (
            worker["employee_code"],
            worker["full_name"],
            worker["student_type"],
            # Legacy provenance only: records that this row came from the demo
            # generator. Nothing reads it to decide whether to seed, because
            # initialization now runs only on an empty database.
            worker["employee_code"],
        ),
    )
    employee_id = connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?",
        (worker["employee_code"],),
    ).fetchone()["id"]

    for course_label, meetings in worker["courses"]:
        connection.execute(
            "INSERT INTO courses (employee_id, course_label) VALUES (?, ?)",
            (employee_id, course_label),
        )
        course_id = connection.execute(
            "SELECT id FROM courses WHERE employee_id = ? AND course_label = ?",
            (employee_id, course_label),
        ).fetchone()["id"]

        for day_of_week, start_time, end_time in meetings:
            connection.execute(
                """
                INSERT INTO class_meetings (course_id, day_of_week, start_time, end_time)
                VALUES (?, ?, ?, ?)
                """,
                (course_id, day_of_week, start_time, end_time),
            )

    for start_datetime, end_datetime in worker["approved_leave"]:
        connection.execute(
            """
            INSERT INTO approved_leave (employee_id, start_datetime, end_datetime)
            VALUES (?, ?, ?)
            """,
            (employee_id, start_datetime, end_datetime),
        )

    for shift_key, level in expand_preferences(worker["preferences"], shifts).items():
        connection.execute(
            "INSERT INTO shift_preferences (employee_id, shift_id, preference) VALUES (?, ?, ?)",
            (employee_id, shift_ids[shift_key], level),
        )


def initialize_demo_data(connection):
    """Write the whole demo dataset, or nothing at all.

    Everything happens inside one transaction opened with BEGIN IMMEDIATE, so
    the "is this database empty?" check and the inserts that depend on it
    cannot be separated by another writer. If any insert fails, the whole
    dataset is rolled back - there is no half-seeded state where, say, the
    shifts exist but the workers do not.

    Raises DatabaseNotEmpty if any workforce table already holds rows.
    """
    shifts = generate_required_shifts()
    workers = generate_workers(shifts)

    # Explicit transaction control: sqlite3's implicit handling would commit
    # at points we do not choose, and would not hold a write lock across the
    # emptiness check.
    previous_isolation = connection.isolation_level
    connection.isolation_level = None

    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            occupied = non_empty_tables(connection)
            if occupied:
                raise DatabaseNotEmpty(
                    "This database already contains "
                    + ", ".join(f"{count} {table}" for table, count in occupied.items())
                    + ". Demo initialization only runs on an empty database, so that "
                    "it can never overwrite edits, restore deleted workers, or add "
                    "demo workers to a database you are managing yourself. Manage "
                    "workers through the application instead."
                )

            shift_ids = insert_shifts(connection, shifts)
            for worker in workers:
                insert_worker(connection, worker, shifts, shift_ids)

            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.isolation_level = previous_isolation

    return table_counts(connection)


def main():
    connection = get_connection()
    try:
        create_schema(connection)
        try:
            counts = initialize_demo_data(connection)
        except DatabaseNotEmpty as error:
            print(f"Demo initialization skipped.\n\n{error}")
            return 1
    finally:
        connection.close()

    for table, count in counts.items():
        print(f"{table}: {count} rows")
    print("\nDemo data initialized. Manage workers through the application from here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
