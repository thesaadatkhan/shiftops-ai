"""SQLite connection and schema for ShiftOps AI.

Date/time conventions used throughout the database:

- One fixed local simulation clock. No timezone conversion, no UTC, no DST
  handling. Every stored time is a plain wall-clock time in that one clock.
- Absolute points in time (shifts, approved leave) are stored as
  'YYYY-MM-DD HH:MM' text, so a period crossing midnight has a different
  date on each end.
- Recurring weekly class meetings are stored as day_of_week + 'HH:MM' times,
  not dates, because a class repeats every week rather than happening once.
  day_of_week is 0=Monday through 6=Sunday, matching Python's
  datetime.weekday().
- A period ending exactly when another starts does not overlap.
"""

import sqlite3
from pathlib import Path

DATABASE_PATH = Path(__file__).parent / "shiftops.db"

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY,
        employee_code TEXT NOT NULL UNIQUE,
        full_name TEXT NOT NULL,
        student_type TEXT NOT NULL
            CHECK (student_type IN ('undergraduate', 'masters')),
        weekly_hour_limit INTEGER NOT NULL DEFAULT 20
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY,
        employee_id INTEGER NOT NULL REFERENCES employees(id),
        course_label TEXT NOT NULL,
        UNIQUE (employee_id, course_label)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS class_meetings (
        id INTEGER PRIMARY KEY,
        course_id INTEGER NOT NULL REFERENCES courses(id),
        day_of_week INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        UNIQUE (course_id, day_of_week, start_time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shifts (
        id INTEGER PRIMARY KEY,
        hall TEXT NOT NULL,
        start_datetime TEXT NOT NULL,
        end_datetime TEXT NOT NULL,
        required_staff INTEGER NOT NULL DEFAULT 1,
        UNIQUE (hall, start_datetime, end_datetime)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shift_preferences (
        id INTEGER PRIMARY KEY,
        employee_id INTEGER NOT NULL REFERENCES employees(id),
        shift_id INTEGER NOT NULL REFERENCES shifts(id),
        preference TEXT NOT NULL CHECK (preference IN ('preferred', 'low')),
        UNIQUE (employee_id, shift_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approved_leave (
        id INTEGER PRIMARY KEY,
        employee_id INTEGER NOT NULL REFERENCES employees(id),
        start_datetime TEXT NOT NULL,
        end_datetime TEXT NOT NULL,
        UNIQUE (employee_id, start_datetime, end_datetime)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assignments (
        id INTEGER PRIMARY KEY,
        employee_id INTEGER NOT NULL REFERENCES employees(id),
        shift_id INTEGER NOT NULL REFERENCES shifts(id),
        UNIQUE (employee_id, shift_id)
    )
    """,
]


def get_connection(database_path=None):
    """Open a connection, defaulting to the project's database file.

    `database_path` lets tests point at a temporary or in-memory database
    without touching the real one.
    """
    connection = sqlite3.connect(database_path or DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_schema(connection):
    """Create any missing tables. Existing tables and rows are untouched."""
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.commit()


def ensure_schema():
    """Create any missing tables in the project's database file."""
    connection = get_connection()
    try:
        create_schema(connection)
    finally:
        connection.close()


if __name__ == "__main__":
    ensure_schema()
    print(f"Schema ready at {DATABASE_PATH}")
