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
        weekly_hour_limit INTEGER NOT NULL DEFAULT 20,
        is_active INTEGER NOT NULL DEFAULT 1,
        seed_key TEXT
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
    # Employee codes that belonged to a worker who has been permanently
    # deleted. Deliberately just the code and when it was retired: this is a
    # ledger of numbers that have been used up, not an archive of the deleted
    # worker, so it holds no name, student type or any other personal detail.
    #
    # It exists because Phase 5C will allocate codes automatically (D034) and
    # must never reuse one. Without this, deleting SW-031 would let the next
    # created worker be issued SW-031 again, so two different people would
    # share a code across the project's history. The allocator will take the
    # next number from the highest suffix in `employees` AND here, so a
    # deleted number stays spent. See D040.
    #
    # A new table, so CREATE TABLE IF NOT EXISTS reaches existing databases -
    # unlike a new column, which needs `migrate_schema()`.
    """
    CREATE TABLE IF NOT EXISTS retired_employee_codes (
        employee_code TEXT PRIMARY KEY,
        retired_at TEXT NOT NULL
    )
    """,
    # How far the automatic employee-code sequence has been issued (D034).
    # Exactly one row, holding the highest SW-number that has been reserved.
    #
    # A counter rather than a derived figure: the highest live employee row is
    # not the answer, because deleting the highest-numbered worker would make
    # the next create reuse their number, and neither is a row count, because
    # gaps are permanent. The counter only ever moves forward.
    #
    # Deliberately no row is written here. Creating the table is additive and
    # touches nothing; the row is created on first use, inside the same
    # transaction that issues a code. That keeps `ensure_schema()` free of
    # side effects on real data.
    """
    CREATE TABLE IF NOT EXISTS code_allocation (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        highest_issued INTEGER NOT NULL
    )
    """,
    # A worker's semester: the inclusive dates a timetable applies to, and
    # whether a supervisor has confirmed it is complete (D035).
    #
    # `confirmed_at` NULL means "not confirmed", which is deliberately
    # different from "no classes". A worker with no schedule at all has
    # missing information; a worker with a confirmed schedule and no class
    # blocks has deliberately declared they have no classes. Phase 6 needs to
    # tell those apart, so the schema must not collapse them.
    """
    CREATE TABLE IF NOT EXISTS semester_schedules (
        id INTEGER PRIMARY KEY,
        employee_id INTEGER NOT NULL REFERENCES employees(id),
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        confirmed_at TEXT,
        UNIQUE (employee_id, start_date, end_date)
    )
    """,
    # One recurring weekly class, owned by a semester schedule rather than by
    # a course: day_of_week (0=Monday, matching D025) plus 'HH:MM' start and
    # end. Course names and course entities are not operational inputs any
    # more, so there is deliberately no course reference here.
    #
    # There is NO unique constraint on (schedule_id, day_of_week, start_time,
    # end_time). Legacy `class_meetings` were unique per course, so one worker
    # could hold two identical-looking meetings under two courses. A unique
    # constraint here would force the migration to drop one of them, and
    # silently discarding stored class information is not acceptable. Every
    # legacy row is preserved one-to-one; rejecting duplicate and overlapping
    # blocks belongs to the supervisor-entry increment, where a person can be
    # told about the clash and decide.
    """
    CREATE TABLE IF NOT EXISTS class_blocks (
        id INTEGER PRIMARY KEY,
        schedule_id INTEGER NOT NULL REFERENCES semester_schedules(id),
        day_of_week INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        source_note TEXT
    )
    """,
]

# The fictional demo semester. It must contain the sample reporting week of
# Monday 2026-10-05 to Sunday 2026-10-11, and it does. Inclusive dates, in
# the project's single local clock (D025). Fictional, like everything else in
# this simulation.
DEMO_SEMESTER_START = "2026-08-24"
DEMO_SEMESTER_END = "2026-12-11"

# When a demo timetable counts as confirmed. The demo generator's timetables
# are validated by construction - fixed course loads, no overlapping classes
# (D023/D027) - so migrating them records a confirmation. A fixed timestamp,
# not `now`, so the same database migrated twice looks identical and tests do
# not depend on when they run.
DEMO_SEMESTER_CONFIRMED_AT = "2026-08-24 00:00"


def get_connection(database_path=None):
    """Open a connection, defaulting to the project's database file.

    `database_path` lets tests point at a temporary or in-memory database
    without touching the real one.
    """
    connection = sqlite3.connect(database_path or DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def table_columns(connection, table):
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def migrate_schema(connection):
    """Add columns that `CREATE TABLE IF NOT EXISTS` cannot add to an
    existing table.

    `CREATE TABLE IF NOT EXISTS` does nothing at all once a table exists, so
    a new column never reaches a database that was created before it was
    added. Each migration below therefore checks for the column first and is
    safe to run on every startup.

    Migrations only ever ADD things. Nothing here drops a table, deletes a
    row, or rebuilds the database.

    Returns the list of migrations that were applied, so callers and tests
    can see whether anything actually changed.
    """
    applied = []

    if "is_active" not in table_columns(connection, "employees"):
        # A NOT NULL column needs a default so existing rows stay valid;
        # 1 means every worker already in the database stays active.
        connection.execute(
            "ALTER TABLE employees ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
        )
        applied.append("employees.is_active")

    if "seed_key" not in table_columns(connection, "employees"):
        # Legacy provenance: records which generated worker a row came from,
        # and is NULL for workers created through the application. It no
        # longer controls anything. It was introduced when seeding ran
        # repeatedly and had to recognise a demo worker whose employee_code
        # had been edited; demo initialization now runs only on an empty
        # database, so nothing reads this column to decide whether to seed.
        # Kept because dropping it would mean rebuilding the table, and
        # knowing which rows came from the demo data is still useful.
        connection.execute("ALTER TABLE employees ADD COLUMN seed_key TEXT")
        connection.execute(
            "UPDATE employees SET seed_key = employee_code WHERE seed_key IS NULL"
        )
        applied.append("employees.seed_key")

    # Created here rather than alongside the CREATE TABLE statements: those
    # run before this function, so on a database that predates seed_key the
    # index would reference a column that does not exist yet.
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS employees_seed_key "
        "ON employees (seed_key) WHERE seed_key IS NOT NULL"
    )

    connection.commit()
    return applied


def expected_demo_timetables():
    """Every demo worker's validated timetable, straight from the generator.

    Returned as {employee_code: sorted [(day_of_week, start, end), ...]}, with
    duplicates kept, so a comparison against stored rows notices a missing,
    extra, duplicated or altered meeting rather than just a different count.

    Imported lazily. `synthetic_data` is pure and imports nothing from here,
    but keeping the import inside the function means `database` stays usable
    on its own and nothing is generated unless a migration actually needs it.
    """
    from synthetic_data import generate_required_shifts, generate_workers

    shifts = generate_required_shifts()
    expected = {}
    for worker in generate_workers(shifts):
        meetings = []
        for _course_label, occurrences in worker["courses"]:
            meetings.extend(tuple(occurrence) for occurrence in occurrences)
        expected[worker["employee_code"]] = sorted(meetings)
    return expected


def stored_meetings(connection, employee_id):
    """A worker's legacy class meetings as sorted (day, start, end) tuples."""
    return sorted(
        (row["day_of_week"], row["start_time"], row["end_time"])
        for row in connection.execute(
            """
            SELECT m.day_of_week, m.start_time, m.end_time
            FROM class_meetings m
            JOIN courses c ON c.id = m.course_id
            WHERE c.employee_id = ?
            """,
            (employee_id,),
        )
    )


def is_intact_demo_timetable(connection, employee, expected):
    """Whether this worker's stored meetings ARE the validated demo timetable.

    `seed_key` alone is not enough and is deliberately not trusted on its own.
    It records provenance, not integrity: an early migration backfilled it
    from `employee_code` for every row, so a hand-made worker can carry one,
    and a genuine demo worker's meetings may since have been edited, deleted
    or duplicated by hand.

    So the key must name a worker the generator actually produces, AND the
    stored meetings must equal that worker's generated timetable exactly,
    compared with multiplicity. Anything else - a missing meeting, an extra
    one, a duplicate, a changed time - fails the comparison and the timetable
    is left unconfirmed rather than being vouched for.
    """
    key = employee["seed_key"]
    if key is None or key not in expected:
        return False
    return stored_meetings(connection, employee["id"]) == expected[key]


def migrate_class_schedules(connection):
    """Move course-linked class meetings into employee-owned semester
    schedules and recurring class blocks (D035).

    **Why this runs in its own explicit transaction.** `create_schema()`
    commits after the CREATE TABLE statements, and Python's sqlite3 leaves
    DDL in autocommit while opening a transaction only for DML. Relying on
    those boundaries would leave the data transformation partly committed if
    it failed halfway. `BEGIN IMMEDIATE` here makes the whole transformation
    genuinely all-or-nothing, and takes the write lock up front so another
    writer cannot interleave.

    **Repeatable, and safe when two callers start together.** Only employees
    who have class meetings and do not yet have a semester schedule are
    migrated, so running it again - on every startup, say - moves nothing and
    duplicates nothing. That list is read *inside* the transaction, after the
    write lock is held, so a second caller cannot act on a view of the
    database that the first has already changed.

    **Nothing is discarded.** Every legacy meeting becomes exactly one class
    block, keeping its weekday and its start and end times unchanged. Rows
    that look identical are both kept; see the note on `class_blocks` about
    why there is no unique constraint. The legacy `courses` and
    `class_meetings` rows are left in place as evidence of where the blocks
    came from - nothing reads them operationally any more, and dropping them
    would destroy information this migration cannot recreate.

    **Confirmation is not invented.** A migrated schedule is confirmed only
    when the stored meetings are PROVED to be a validated demo timetable: the
    worker's `seed_key` must name a worker the generator produces, and their
    stored meetings must equal that generated timetable exactly, compared
    with multiplicity. `seed_key` on its own is provenance, not integrity -
    an early migration backfilled it from `employee_code` for every row, and
    a demo worker's meetings may since have been edited. Anything that does
    not match exactly is migrated unconfirmed, with its meetings preserved
    untouched. A worker with no class meetings at all gets no schedule, which
    reads as missing information rather than as a deliberate "no classes" -
    those are different states and Phase 6 must be able to tell them apart.

    Returns the number of employees migrated.
    """
    previous_isolation = connection.isolation_level
    connection.isolation_level = None
    migrated = 0

    try:
        # The write lock is taken FIRST, and only then is it decided who
        # still needs migrating. Reading that list before the lock was a real
        # race: two servers starting together both saw the same worker as
        # pending, and the second to commit hit the unique constraint on
        # (employee_id, start_date, end_date). The transaction protected the
        # inserts, but the decision about what to insert was made outside it.
        #
        # Inside the lock, the second caller blocks until the first commits,
        # then reads a database that already has the schedule and finds
        # nothing pending. Both calls succeed and the work happens once.
        connection.execute("BEGIN IMMEDIATE")
        try:
            pending = connection.execute(
                """
                SELECT DISTINCT c.employee_id
                FROM class_meetings m
                JOIN courses c ON c.id = m.course_id
                WHERE c.employee_id NOT IN (SELECT employee_id FROM semester_schedules)
                """
            ).fetchall()

            if not pending:
                # Nothing to do. End the transaction rather than leaving it
                # open, so the write lock is released on the way out.
                connection.execute("COMMIT")
                return 0

            # Only generated once there is something to migrate, which in a
            # database's life is at most once.
            expected = expected_demo_timetables()

            for row in pending:
                employee_id = row["employee_id"]
                employee = connection.execute(
                    "SELECT id, seed_key FROM employees WHERE id = ?", (employee_id,)
                ).fetchone()
                if employee is None:
                    # A meeting whose owner no longer exists. Left exactly as
                    # it is rather than attached to an invented worker.
                    continue

                from_demo = is_intact_demo_timetable(connection, employee, expected)
                connection.execute(
                    """
                    INSERT INTO semester_schedules
                        (employee_id, start_date, end_date, confirmed_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        employee_id,
                        DEMO_SEMESTER_START,
                        DEMO_SEMESTER_END,
                        DEMO_SEMESTER_CONFIRMED_AT if from_demo else None,
                    ),
                )
                schedule_id = connection.execute(
                    "SELECT id FROM semester_schedules WHERE employee_id = ?"
                    " AND start_date = ? AND end_date = ?",
                    (employee_id, DEMO_SEMESTER_START, DEMO_SEMESTER_END),
                ).fetchone()["id"]

                note = (
                    "migrated from demo course data, matched the generated timetable"
                    if from_demo
                    else "migrated from legacy course data, timetable unconfirmed"
                )
                connection.execute(
                    """
                    INSERT INTO class_blocks
                        (schedule_id, day_of_week, start_time, end_time, source_note)
                    SELECT ?, m.day_of_week, m.start_time, m.end_time, ?
                    FROM class_meetings m
                    JOIN courses c ON c.id = m.course_id
                    WHERE c.employee_id = ?
                    ORDER BY m.id
                    """,
                    (schedule_id, note, employee_id),
                )
                migrated += 1

            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.isolation_level = previous_isolation

    return migrated


def create_schema(connection):
    """Create any missing tables, apply column migrations, then move class
    data into the semester model.

    Existing tables and rows are untouched apart from that documented
    transformation, which is itself repeatable and all-or-nothing.
    """
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.commit()
    applied = migrate_schema(connection)
    migrated = migrate_class_schedules(connection)
    if migrated:
        applied.append(f"class_blocks.migrated_employees={migrated}")
    return applied


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
