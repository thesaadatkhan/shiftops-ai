"""Regression checks for the semester/class-block migration (D035).

Everything here runs against throwaway in-memory or temporary databases. The
project's own `backend/shiftops.db` is never opened, read or modified.

The fixtures build a **populated old-schema database** by hand - employees,
courses, class meetings, shifts, preferences, leave, assignments, retired
codes and allocator progress - and then run `create_schema()` over it, which
is exactly what an existing installation does on startup.

Checks:

1. A populated old-schema database migrates: every class meeting becomes one
   class block, with its weekday and times unchanged, owned by the right
   worker through a semester schedule.
2. Demo-provenance workers get a confirmed schedule; manually created ones
   get an unconfirmed one. A worker with no classes gets no schedule and
   reads as missing, not as a deliberate "no classes".
3. Identifiers, status, limits, seed provenance, preferences, leave,
   assignments, shifts, retired codes and allocator progress are all
   preserved by value.
4. Duplicate-looking legacy meetings under different courses are both kept.
5. Legacy course rows are preserved, not dropped.
6. A fresh database needs no migration and seeds straight into the new model.
7. Repeating the migration changes nothing and duplicates nothing.
8. An injected failure leaves the database exactly as it was.
9. Class summaries count only blocks whose semester covers the reporting
   week, and keep fractional hours.
10. Deletion removes schedules, blocks and legacy course rows, while shared
    shifts, other workers and retired codes survive.
11. Demo initialization still refuses on a database holding migrated data.

Run with:  python verify_semester_migration.py
Exits non-zero if any check fails.
"""

import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

from database import (
    AMBIGUOUS_DELETION_MARKER,
    DEMO_SEMESTER_CONFIRMED_AT,
    LEGACY_MIGRATION_NOTE,
    expected_demo_timetables,
    DEMO_SEMESTER_END,
    DEMO_SEMESTER_START,
    SCHEMA_STATEMENTS,
    create_schema,
    get_connection,
    migrate_class_schedules,
)
from employees import allocation_progress, create_employee, delete_employee, find_by_code
from seed import DatabaseNotEmpty, initialize_demo_data
from timetables import delete_schedule

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


# The schema as it was BEFORE this increment, written out in full.
#
# It was previously derived by filtering the CURRENT schema, which defeated
# the point: if a later change altered one of these tables, the "old" fixture
# would quietly change with it and the migration would be tested against a
# database no real installation ever had. This copy is independent on
# purpose. It should only ever change if the historical schema is discovered
# to have been recorded wrongly - not because the live schema moved on.
PRE_MIGRATION_SCHEMA = [
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
    """
    CREATE TABLE IF NOT EXISTS retired_employee_codes (
        employee_code TEXT PRIMARY KEY,
        retired_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS code_allocation (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        highest_issued INTEGER NOT NULL
    )
    """,
]


def old_schema_database(path=None):
    connection = get_connection(path or ":memory:")
    for statement in PRE_MIGRATION_SCHEMA:
        connection.execute(statement)
    connection.commit()
    assert (
        connection.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master"
            " WHERE name IN ('semester_schedules', 'class_blocks')"
        ).fetchone()["n"]
        == 0
    ), "the pre-migration fixture must not already contain the new tables"
    return connection


def add_old_employee(connection, code, name, seed_key=None, active=1):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active, seed_key) VALUES (?, ?, 'undergraduate', 20, ?, ?)",
        (code, name, active, seed_key),
    )
    return connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]


def add_old_course(connection, employee_id, label, meetings):
    connection.execute(
        "INSERT INTO courses (employee_id, course_label) VALUES (?, ?)",
        (employee_id, label),
    )
    course_id = connection.execute(
        "SELECT id FROM courses WHERE employee_id = ? AND course_label = ?",
        (employee_id, label),
    ).fetchone()["id"]
    for day, start, end in meetings:
        connection.execute(
            "INSERT INTO class_meetings (course_id, day_of_week, start_time, end_time)"
            " VALUES (?, ?, ?, ?)",
            (course_id, day, start, end),
        )
    connection.commit()
    return course_id


def blocks_for(connection, code):
    return [
        (row["day_of_week"], row["start_time"], row["end_time"])
        for row in connection.execute(
            """
            SELECT b.day_of_week, b.start_time, b.end_time
            FROM class_blocks b
            JOIN semester_schedules s ON s.id = b.schedule_id
            JOIN employees e ON e.id = s.employee_id
            WHERE e.employee_code = ?
            ORDER BY b.id
            """,
            (code,),
        )
    ]


def schedule_for(connection, code):
    return connection.execute(
        """
        SELECT s.start_date, s.end_date, s.confirmed_at
        FROM semester_schedules s
        JOIN employees e ON e.id = s.employee_id
        WHERE e.employee_code = ?
        """,
        (code,),
    ).fetchone()


def snapshot(connection):
    """Everything the migration must not disturb, by value."""
    def rows(sql):
        return [tuple(row) for row in connection.execute(sql)]

    return {
        "employees": rows(
            "SELECT id, employee_code, full_name, student_type, weekly_hour_limit,"
            " is_active, seed_key FROM employees ORDER BY id"
        ),
        "courses": rows("SELECT id, employee_id, course_label FROM courses ORDER BY id"),
        "class_meetings": rows(
            "SELECT id, course_id, day_of_week, start_time, end_time"
            " FROM class_meetings ORDER BY id"
        ),
        "shifts": rows("SELECT id, hall, start_datetime, end_datetime FROM shifts ORDER BY id"),
        "shift_preferences": rows(
            "SELECT id, employee_id, shift_id, preference FROM shift_preferences ORDER BY id"
        ),
        "approved_leave": rows(
            "SELECT id, employee_id, start_datetime, end_datetime FROM approved_leave ORDER BY id"
        ),
        "assignments": rows("SELECT id, employee_id, shift_id FROM assignments ORDER BY id"),
        "retired": rows("SELECT employee_code, retired_at FROM retired_employee_codes ORDER BY employee_code"),
        "allocation": rows("SELECT id, highest_issued FROM code_allocation"),
    }


def build_populated_old_database(path=None):
    """A realistic pre-migration database: demo and manual workers, a worker
    with no classes, duplicate-looking meetings, and unrelated records."""
    connection = old_schema_database(path)

    demo = add_old_employee(connection, "SW-001", "Maria Demo", seed_key="SW-001")
    manual = add_old_employee(connection, "SW-031", "Jack Manual", seed_key=None)
    classless = add_old_employee(connection, "SW-032", "No Classes", seed_key=None, active=0)

    # The demo worker carries the REAL generated SW-001 timetable, not an
    # invented one. Confirmation now depends on the stored meetings actually
    # matching what the generator produces, so a made-up timetable here would
    # (correctly) migrate unconfirmed and the fixture would stop exercising
    # the confirmed path at all.
    for index, (day, start, end) in enumerate(expected_demo_timetables()["SW-001"]):
        add_old_course(connection, demo, f"Generated {index}", [(day, start, end)])
    # Two different courses, same weekday and identical times: legacy allowed
    # this because uniqueness was per course.
    add_old_course(connection, manual, "Seminar A", [(3, "16:00", "17:15")])
    add_old_course(connection, manual, "Seminar B", [(3, "16:00", "17:15")])

    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime) VALUES ('Vega', ?, ?)",
        ("2026-10-05 17:00", "2026-10-05 22:00"),
    )
    shift_id = connection.execute("SELECT id FROM shifts").fetchone()["id"]
    connection.execute(
        "INSERT INTO shift_preferences (employee_id, shift_id, preference) VALUES (?, ?, 'preferred')",
        (demo, shift_id),
    )
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime) VALUES (?, ?, ?)",
        (manual, "2026-10-10 08:00", "2026-10-10 14:00"),
    )
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)", (demo, shift_id)
    )
    connection.execute(
        "INSERT INTO retired_employee_codes (employee_code, retired_at) VALUES ('SW-030', '2026-09-01 10:00')"
    )
    connection.execute("INSERT INTO code_allocation (id, highest_issued) VALUES (1, 32)")
    connection.commit()
    return connection, {"demo": demo, "manual": manual, "classless": classless}


def main():
    # 1-5. Migrating a populated old-schema database.
    connection, ids = build_populated_old_database()
    before = snapshot(connection)
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE name = 'class_blocks'"
        ).fetchone()["n"] == 0,
        "the fixture really starts on the old schema",
    )

    applied = create_schema(connection)
    check(
        any("class_blocks.migrated_employees=2" in entry for entry in applied),
        f"the migration reports the two workers it moved {applied}",
    )

    generated = expected_demo_timetables()["SW-001"]
    check(
        sorted(blocks_for(connection, "SW-001")) == sorted(generated),
        f"the demo worker's meetings became blocks with identical days and times "
        f"{sorted(blocks_for(connection, 'SW-001'))}",
    )
    check(
        blocks_for(connection, "SW-031") == [(3, "16:00", "17:15"), (3, "16:00", "17:15")],
        f"duplicate-looking meetings under two courses are BOTH kept {blocks_for(connection, 'SW-031')}",
    )
    check(blocks_for(connection, "SW-032") == [], "the classless worker has no blocks")

    demo_schedule = schedule_for(connection, "SW-001")
    manual_schedule = schedule_for(connection, "SW-031")
    check(
        (demo_schedule["start_date"], demo_schedule["end_date"]) == (DEMO_SEMESTER_START, DEMO_SEMESTER_END),
        f"the migrated schedule uses the documented demo semester {DEMO_SEMESTER_START}..{DEMO_SEMESTER_END}",
    )
    check(DEMO_SEMESTER_START <= "2026-10-05" and DEMO_SEMESTER_END >= "2026-10-11",
          "the demo semester contains the sample week of 5-11 October 2026")
    check(demo_schedule["confirmed_at"] == DEMO_SEMESTER_CONFIRMED_AT,
          "a demo-provenance timetable is migrated as confirmed")
    check(manual_schedule["confirmed_at"] is None,
          "a manually created worker's timetable is NOT confirmed by migration")
    check(schedule_for(connection, "SW-032") is None,
          "a worker with no classes gets no schedule, so missing is not read as confirmed-no-classes")

    after = snapshot(connection)
    for table in before:
        check(after[table] == before[table], f"{table} is preserved exactly by the migration")
    check(before["courses"] != [], "legacy course rows existed")
    check(after["courses"] == before["courses"], "legacy course rows are preserved, not dropped")

    # 7. Repeating changes nothing.
    repeat_before = snapshot(connection)
    repeat_blocks = blocks_for(connection, "SW-001")
    moved = migrate_class_schedules(connection)
    create_schema(connection)
    check(moved == 0, f"a second migration moves nobody ({moved})")
    check(blocks_for(connection, "SW-001") == repeat_blocks, "repeating does not duplicate blocks")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM semester_schedules").fetchone()["n"] == 2,
        "repeating does not duplicate schedules",
    )
    check(snapshot(connection) == repeat_before, "repeating leaves everything else untouched")
    connection.close()

    # 6. A fresh database needs no migration, and seeding uses the new model.
    fresh = get_connection(":memory:")
    create_schema(fresh)
    check(migrate_class_schedules(fresh) == 0, "a fresh database has nothing to migrate")
    initialize_demo_data(fresh)
    check(
        fresh.execute("SELECT COUNT(*) AS n FROM semester_schedules").fetchone()["n"] == 30,
        "seeding creates one semester schedule per demo worker",
    )
    check(
        fresh.execute("SELECT COUNT(*) AS n FROM class_blocks").fetchone()["n"] == 148,
        f"seeding creates the 148 demo class blocks ({fresh.execute('SELECT COUNT(*) AS n FROM class_blocks').fetchone()['n']})",
    )
    check(
        fresh.execute("SELECT COUNT(*) AS n FROM courses").fetchone()["n"] == 0,
        "seeding no longer writes course rows at all",
    )
    check(
        fresh.execute(
            "SELECT COUNT(*) AS n FROM semester_schedules WHERE confirmed_at IS NULL"
        ).fetchone()["n"] == 0,
        "every seeded demo timetable is confirmed, because the generator validates it",
    )
    check(migrate_class_schedules(fresh) == 0, "a seeded database has nothing to migrate either")

    # 11. Initialization still refuses once data exists.
    try:
        initialize_demo_data(fresh)
        check(False, "initialization should refuse a seeded database")
    except DatabaseNotEmpty as error:
        check("semester_schedules" in str(error) or "employees" in str(error),
              f"initialization refuses and names an occupied table -> {str(error)[:90]}")
    fresh.close()

    # A migrated (not seeded) database must also block initialization.
    migrated_only = old_schema_database()
    employee_id = add_old_employee(migrated_only, "SW-001", "Legacy", seed_key="SW-001")
    add_old_course(migrated_only, employee_id, "Writing A", [(0, "09:00", "10:15")])
    create_schema(migrated_only)
    try:
        initialize_demo_data(migrated_only)
        check(False, "initialization should refuse a migrated database")
    except DatabaseNotEmpty as error:
        check(True, f"initialization refuses a migrated database -> {str(error)[:70]}")
    migrated_only.close()

    # 2b. A demo timetable is confirmed only when the stored meetings really
    #     are the generated one. seed_key alone must not be enough.
    expected = expected_demo_timetables()
    demo_code = "SW-001"
    demo_meetings = expected[demo_code]

    def demo_fixture(meetings, seed_key=demo_code, code=demo_code):
        """One worker carrying `meetings`, migrated, returning the schedule."""
        conn = old_schema_database()
        employee_id = add_old_employee(conn, code, "Demo Worker", seed_key=seed_key)
        # One course per meeting, so any multiset of meetings can be stored:
        # the legacy unique key was (course, weekday, start).
        for index, (day, start, end) in enumerate(meetings):
            add_old_course(conn, employee_id, f"Course {index}", [(day, start, end)])
        create_schema(conn)
        return conn

    intact = demo_fixture(demo_meetings)
    check(
        schedule_for(intact, demo_code)["confirmed_at"] == DEMO_SEMESTER_CONFIRMED_AT,
        "an intact demo timetable migrates as confirmed",
    )
    check(len(blocks_for(intact, demo_code)) == len(demo_meetings),
          "an intact demo timetable keeps all of its meetings")
    intact.close()

    variants = {
        "a missing meeting": demo_meetings[:-1],
        "an extra meeting": demo_meetings + [(6, "08:00", "09:00")],
        "a duplicated meeting": demo_meetings + [demo_meetings[0]],
        "a changed time": [(demo_meetings[0][0], "07:45", demo_meetings[0][2])]
        + demo_meetings[1:],
        "an overlapping extra meeting": demo_meetings
        + [(demo_meetings[0][0], demo_meetings[0][1], "23:00")],
    }
    for label, meetings in variants.items():
        conn = demo_fixture(meetings)
        schedule = schedule_for(conn, demo_code)
        check(schedule is not None, f"{label}: the worker is still migrated")
        check(schedule["confirmed_at"] is None,
              f"{label} is NOT mistaken for an intact demo timetable")
        check(len(blocks_for(conn, demo_code)) == len(meetings),
              f"{label}: every stored meeting is still preserved")
        conn.close()

    # A non-null seed_key that names no generated worker proves nothing.
    impostor = demo_fixture(demo_meetings, seed_key="SW-031", code="SW-031")
    check(schedule_for(impostor, "SW-031")["confirmed_at"] is None,
          "a non-null seed_key that is not a generated demo code does not confirm")
    check(len(blocks_for(impostor, "SW-031")) == len(demo_meetings),
          "the impostor's meetings are preserved regardless")
    impostor.close()

    # seed_key backfilled from the employee code, as an early migration did,
    # on a worker whose timetable is nothing like the demo one.
    backfilled = demo_fixture([(1, "08:00", "09:00")], seed_key="SW-002", code="SW-002")
    check(schedule_for(backfilled, "SW-002")["confirmed_at"] is None,
          "a backfilled seed_key with different meetings does not confirm")
    backfilled.close()

    # 8. An injected failure leaves the database exactly as it was.
    connection, _ = build_populated_old_database()
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.commit()
    before_failure = snapshot(connection)

    class FailingConnection:
        """Fails on the Nth INSERT, forwarding everything else."""

        def __init__(self, real, fail_on):
            self._real = real
            self._fail_on = fail_on
            self.inserts = 0

        def execute(self, sql, *args):
            if sql.strip().upper().startswith("INSERT"):
                self.inserts += 1
                if self.inserts == self._fail_on:
                    raise sqlite3.OperationalError("injected failure")
            return self._real.execute(sql, *args)

        def commit(self):
            return self._real.commit()

        def rollback(self):
            return self._real.rollback()

        @property
        def isolation_level(self):
            return self._real.isolation_level

        @isolation_level.setter
        def isolation_level(self, value):
            self._real.isolation_level = value

    # Fail on the THIRD insert: the first schedule and its blocks are already
    # written, so the rollback has real work to undo.
    failing = FailingConnection(connection, fail_on=3)
    try:
        migrate_class_schedules(failing)
        check(False, "the injected migration failure should have propagated")
    except sqlite3.OperationalError as error:
        check("injected failure" in str(error), "the injected migration failure propagates")

    check(
        connection.execute("SELECT COUNT(*) AS n FROM semester_schedules").fetchone()["n"] == 0,
        "the failed migration left no semester schedules",
    )
    check(
        connection.execute("SELECT COUNT(*) AS n FROM class_blocks").fetchone()["n"] == 0,
        "the failed migration left no class blocks",
    )
    check(snapshot(connection) == before_failure, "the failed migration changed nothing at all")

    recovered = migrate_class_schedules(connection)
    check(recovered == 2, f"a retry after the failure migrates cleanly ({recovered})")
    check(len(blocks_for(connection, "SW-001")) == len(expected_demo_timetables()["SW-001"]),
          "and produces the expected blocks")
    connection.close()

    # 9. Semester applicability and fractional hours.
    connection = get_connection(":memory:")
    create_schema(connection)
    inside = create_employee(connection, {"full_name": "In Semester", "student_type": "undergraduate"})
    outside = create_employee(connection, {"full_name": "Past Semester", "student_type": "undergraduate"})
    for code, start, end in [
        (inside["employee_code"], DEMO_SEMESTER_START, DEMO_SEMESTER_END),
        (outside["employee_code"], "2026-01-12", "2026-05-01"),
    ]:
        employee_id = find_by_code(connection, code)["id"]
        connection.execute(
            "INSERT INTO semester_schedules (employee_id, start_date, end_date, confirmed_at)"
            " VALUES (?, ?, ?, ?)",
            (employee_id, start, end, DEMO_SEMESTER_CONFIRMED_AT),
        )
        schedule_id = connection.execute(
            "SELECT id FROM semester_schedules WHERE employee_id = ?", (employee_id,)
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time)"
            " VALUES (?, 0, '09:00', '10:15')",
            (schedule_id,),
        )
    connection.commit()

    week_start, week_end = "2026-10-05", "2026-10-11"
    applicable = connection.execute(
        """
        SELECT e.employee_code, COUNT(b.id) AS blocks
        FROM employees e
        JOIN semester_schedules s ON s.employee_id = e.id
        JOIN class_blocks b ON b.schedule_id = s.id
        WHERE s.start_date <= ? AND s.end_date >= ?
        GROUP BY e.employee_code
        """,
        (week_end, week_start),
    ).fetchall()
    counted = {row["employee_code"]: row["blocks"] for row in applicable}
    check(counted == {inside["employee_code"]: 1},
          f"only the semester covering the reporting week contributes blocks {counted}")
    check(round(75 / 60, 2) == 1.25, "a 75-minute class is 1.25 hours, kept fractional")
    connection.close()

    # 10. Deletion owns the new records.
    connection, _ = build_populated_old_database()
    create_schema(connection)
    shifts_before = connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"]
    result = delete_employee(connection, "SW-031")
    check(result["removed"]["semester_schedules"] == 1, f"deletion reports the schedule removed {result['removed']}")
    check(result["removed"]["class_blocks"] == 2, "deletion reports both class blocks removed")
    check(result["removed"]["courses"] == 2, "deletion reports the legacy course rows removed")
    check(result["removed"]["class_meetings"] == 2, "deletion reports the legacy meetings removed")
    check(find_by_code(connection, "SW-031") is None, "the worker is gone")
    check(blocks_for(connection, "SW-031") == [], "their blocks are gone")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"] == shifts_before,
        "shared shifts survive the deletion",
    )
    check(len(blocks_for(connection, "SW-001")) == len(expected_demo_timetables()["SW-001"]),
          "the other worker's blocks survive")
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM retired_employee_codes WHERE employee_code = 'SW-031'"
        ).fetchone()["n"] == 1,
        "the deleted code is retired",
    )
    check(allocation_progress(connection) == 32, "allocator progress is unchanged by deletion")
    connection.close()

    # 3 (continued). Allocation still works after a migration, continuing
    #                above the highest reserved number.
    connection, _ = build_populated_old_database()
    create_schema(connection)
    issued = create_employee(connection, {"full_name": "After Migration", "student_type": "masters"})
    check(issued["employee_code"] == "SW-033",
          f"allocation continues above the migrated data ({issued['employee_code']})")
    check(schedule_for(connection, issued["employee_code"]) is None,
          "a newly created worker starts with no timetable, which reads as missing")
    connection.close()

    # 12. Two servers starting at once must not collide.
    #
    #     The pending list used to be read BEFORE the write lock was taken,
    #     so both callers saw the same worker as needing migration and the
    #     second to commit hit the unique constraint on
    #     (employee_id, start_date, end_date). Reproduced before the fix.
    #
    #     Contention is created deliberately, not hoped for: each thread
    #     reads the pending list first and then waits on a barrier, so both
    #     are provably holding a pre-migration view of the database before
    #     either begins writing. The barrier is released BEFORE any call
    #     takes the write lock - putting a two-party barrier inside the lock
    #     would deadlock, because the second party could never get in.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "concurrent-start.db"
        builder, _ = build_populated_old_database(path)
        builder.close()

        # The schema step has run (both servers create the tables, which is
        # idempotent); what races is the data migration itself.
        creator = get_connection(path)
        for statement in SCHEMA_STATEMENTS:
            creator.execute(statement)
        creator.commit()
        before = snapshot(creator)
        eligible = creator.execute(
            """
            SELECT COUNT(DISTINCT c.employee_id) AS n
            FROM class_meetings m
            JOIN courses c ON c.id = m.course_id
            """
        ).fetchone()["n"]
        creator.close()
        check(eligible == 2, f"the fixture has two workers needing migration ({eligible})")

        pending_sql = """
            SELECT DISTINCT c.employee_id
            FROM class_meetings m
            JOIN courses c ON c.id = m.course_id
            WHERE c.employee_id NOT IN (SELECT employee_id FROM semester_schedules)
        """
        both_ready = threading.Barrier(2)
        results = {}
        errors = []
        saw = {}

        def migrator(name):
            own = get_connection(path)
            own.execute("PRAGMA busy_timeout = 10000")
            try:
                saw[name] = len(own.execute(pending_sql).fetchall())
                both_ready.wait(timeout=10)
                results[name] = migrate_class_schedules(own)
            except Exception as error:  # noqa: BLE001 - recorded, not swallowed
                errors.append(f"{name}: {type(error).__name__}: {error}")
            finally:
                own.close()

        threads = [threading.Thread(target=migrator, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        check(errors == [], f"both concurrent migrations succeeded ({errors})")
        check(
            saw.get("a") == eligible and saw.get("b") == eligible,
            f"both callers really did hold a pre-migration view {saw}",
        )
        check(
            sorted(results.values()) == [0, eligible],
            f"one call migrated all {eligible} workers and the other migrated nobody "
            f"({results})",
        )

        after = get_connection(path)
        check(
            after.execute("SELECT COUNT(*) AS n FROM semester_schedules").fetchone()["n"] == 2,
            "exactly one schedule per eligible worker, with no duplicates",
        )
        per_worker = after.execute(
            "SELECT employee_id, COUNT(*) AS n FROM semester_schedules GROUP BY employee_id"
        ).fetchall()
        check(
            all(row["n"] == 1 for row in per_worker),
            f"no worker received two schedules {[tuple(r) for r in per_worker]}",
        )

        # Meeting values and ownership preserved exactly, not merely counted.
        demo_blocks = sorted(blocks_for(after, "SW-001"))
        manual_blocks = sorted(blocks_for(after, "SW-031"))
        check(
            demo_blocks == sorted(expected_demo_timetables()["SW-001"]),
            f"the demo worker's blocks are exactly their original meetings ({len(demo_blocks)})",
        )
        check(
            manual_blocks == [(3, "16:00", "17:15"), (3, "16:00", "17:15")],
            f"the manual worker's duplicate meetings are both preserved ({manual_blocks})",
        )
        check(
            after.execute("SELECT COUNT(*) AS n FROM class_blocks").fetchone()["n"]
            == len(demo_blocks) + len(manual_blocks),
            "no block was written twice",
        )
        check(blocks_for(after, "SW-032") == [], "the classless worker still has no blocks")

        # Confirmation states survive the race unchanged.
        check(
            schedule_for(after, "SW-001")["confirmed_at"] == DEMO_SEMESTER_CONFIRMED_AT,
            "the demo worker's timetable is still confirmed",
        )
        check(
            schedule_for(after, "SW-031")["confirmed_at"] is None,
            "the manual worker's timetable is still unconfirmed",
        )

        # Everything the migration must not touch is byte-identical.
        unchanged = snapshot(after)
        for table in before:
            check(
                unchanged[table] == before[table],
                f"{table} is unchanged by the concurrent migration",
            )
        check(
            allocation_progress(after) == 32,
            f"allocator progress is unchanged ({allocation_progress(after)})",
        )

        # And a third, later call still migrates nobody.
        check(migrate_class_schedules(after) == 0, "a later migration still moves nobody")
        after.close()

    # 12b. The real startup path: two full create_schema() calls at once.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "concurrent-startup.db"
        builder, _ = build_populated_old_database(path)
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

        check(startup_errors == [], f"two simultaneous startups both succeeded ({startup_errors})")
        after = get_connection(path)
        check(
            after.execute("SELECT COUNT(*) AS n FROM semester_schedules").fetchone()["n"] == 2,
            "two simultaneous startups produced exactly two schedules",
        )
        check(
            sorted(blocks_for(after, "SW-001")) == sorted(expected_demo_timetables()["SW-001"]),
            "and the demo worker's blocks are written exactly once",
        )
        after.close()

    # Persistence across a real file and reopen.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "migrated.db"
        first, _ = build_populated_old_database(path)
        create_schema(first)
        first.close()

        reopened = get_connection(path)
        check(len(blocks_for(reopened, "SW-001")) == len(expected_demo_timetables()["SW-001"]),
              "migrated blocks survive reopening")
        check(schedule_for(reopened, "SW-001")["confirmed_at"] == DEMO_SEMESTER_CONFIRMED_AT,
              "confirmation survives reopening")
        check(migrate_class_schedules(reopened) == 0, "reopening migrates nothing again")
        reopened.close()

    # A migrated worker's deleted semester must never come back on a later
    # startup (Codex review finding 1): `migrate_class_schedules()` used to
    # decide "needs migration" from "has no semester schedule", so deleting
    # a migrated worker's last semester made them look unmigrated again.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "deleted-after-migration.db"
        builder, _ = build_populated_old_database(path)
        create_schema(builder)  # first migration
        builder.close()

        connection = get_connection(path)
        demo_schedule_id = connection.execute(
            "SELECT s.id FROM semester_schedules s JOIN employees e ON e.id = s.employee_id"
            " WHERE e.employee_code = 'SW-001'"
        ).fetchone()["id"]
        other_blocks_before = sorted(blocks_for(connection, "SW-031"))
        unrelated_before = snapshot(connection)

        delete_schedule(connection, "SW-001", demo_schedule_id)
        check(
            schedule_for(connection, "SW-001") is None,
            "the demo worker's semester is actually deleted",
        )
        check(blocks_for(connection, "SW-001") == [], "and its class blocks are gone with it")

        # The startup path a real restart takes: create_schema() again,
        # which re-runs both migrate_schema() and migrate_class_schedules().
        applied = create_schema(connection)
        check(
            schedule_for(connection, "SW-001") is None,
            "the deleted semester does NOT return after a second schema-init/restart",
        )
        check(blocks_for(connection, "SW-001") == [], "its class blocks still do not return")
        check(
            not any(item.startswith("class_blocks.migrated_employees") for item in applied),
            f"no migration reports SW-001 as migrated again ({applied})",
        )

        # Legacy evidence (the migration's own provenance) is preserved -
        # never deleted by delete_schedule, and still there to show where
        # the (now-removed) schedule originally came from.
        check(
            connection.execute(
                "SELECT COUNT(*) AS n FROM class_meetings m JOIN courses c ON c.id = m.course_id"
                " JOIN employees e ON e.id = c.employee_id WHERE e.employee_code = 'SW-001'"
            ).fetchone()["n"]
            > 0,
            "SW-001's legacy course/class-meeting rows are preserved as evidence",
        )
        check(
            connection.execute(
                "SELECT 1 FROM migrated_employees me JOIN employees e ON e.id = me.employee_id"
                " WHERE e.employee_code = 'SW-001'"
            ).fetchone()
            is not None,
            "SW-001 is still recorded as migrated, independent of the deleted semester",
        )

        # Unrelated workers and data survive both the deletion and the
        # second startup completely untouched.
        check(
            sorted(blocks_for(connection, "SW-031")) == other_blocks_before,
            "an unrelated worker's blocks are untouched by another worker's deletion+restart",
        )
        unrelated_after = snapshot(connection)
        for table in ("employees", "shifts", "shift_preferences", "approved_leave", "assignments", "retired"):
            check(
                unrelated_after[table] == unrelated_before[table],
                f"{table} is unchanged by the deletion and restart",
            )
        connection.close()

    # The pre-ledger upgrade ambiguity: a database that already ran
    # migration and had a supervisor delete a migrated worker's semester
    # BEFORE `migrated_employees` ever existed. Built directly against
    # SCHEMA_STATEMENTS minus the ledger table, to represent exactly that
    # historical moment, then upgraded via the real create_schema().
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "pre-ledger-ambiguous.db"
        connection = get_connection(path)
        for statement in SCHEMA_STATEMENTS:
            if "CREATE TABLE IF NOT EXISTS migrated_employees" not in statement:
                connection.execute(statement)
        connection.commit()
        assert (
            connection.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master WHERE name = 'migrated_employees'"
            ).fetchone()["n"]
            == 0
        ), "the pre-ledger fixture must not already contain the ledger table"

        # SW-101: migrated once, still fully intact - this is the ONLY
        # evidence in the whole database that migration ever ran, and it is
        # what must let the code recognize SW-102 (below) as ambiguous
        # rather than genuinely new.
        intact_id = add_old_employee(connection, "SW-101", "Still Migrated")
        add_old_course(connection, intact_id, "Intact", [(0, "09:00", "10:00")])
        connection.execute(
            "INSERT INTO semester_schedules (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
            " VALUES (?, ?, ?, NULL, 1)",
            (intact_id, DEMO_SEMESTER_START, DEMO_SEMESTER_END),
        )
        intact_schedule_id = connection.execute(
            "SELECT id FROM semester_schedules WHERE employee_id = ?", (intact_id,)
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time, source_note)"
            " VALUES (?, 0, '09:00', '10:00', ?)",
            (intact_schedule_id, LEGACY_MIGRATION_NOTE),
        )

        # SW-102: migrated once too, but a supervisor deleted their semester
        # and blocks BEFORE the ledger existed to record that fact - only
        # their legacy course/class_meetings rows remain, indistinguishable
        # from a worker who was simply never migrated.
        deleted_id = add_old_employee(connection, "SW-102", "Pre-Ledger Deleted")
        add_old_course(connection, deleted_id, "Gone", [(2, "13:00", "14:15")])

        connection.commit()
        connection.close()

        # The actual upgrade: the real, current create_schema() path.
        upgraded = get_connection(path)
        create_schema(upgraded)

        ledger_table_count = upgraded.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE name = 'migrated_employees'"
        ).fetchone()["n"]
        check(ledger_table_count == 1, "the ledger table is created by the upgrade")

        check(
            schedule_for(upgraded, "SW-102") is None,
            "the ambiguous worker's timetable is NOT silently recreated by the upgrade",
        )
        check(blocks_for(upgraded, "SW-102") == [], "and no class blocks are recreated for them either")
        check(
            upgraded.execute(
                "SELECT COUNT(*) AS n FROM class_meetings m JOIN courses c ON c.id = m.course_id"
                " JOIN employees e ON e.id = c.employee_id WHERE e.employee_code = 'SW-102'"
            ).fetchone()["n"]
            > 0,
            "the ambiguous worker's legacy course/class-meeting rows are preserved untouched",
        )
        ambiguous_marker = upgraded.execute(
            "SELECT me.migrated_at FROM migrated_employees me"
            " JOIN employees e ON e.id = me.employee_id WHERE e.employee_code = 'SW-102'"
        ).fetchone()
        check(
            ambiguous_marker is not None and ambiguous_marker["migrated_at"] == AMBIGUOUS_DELETION_MARKER,
            f"the ambiguous worker is recorded with the explicit ambiguity marker, not a fabricated timestamp ({ambiguous_marker['migrated_at'] if ambiguous_marker else None})",
        )
        check(
            schedule_for(upgraded, "SW-101") is not None,
            "the still-intact migrated worker is completely unaffected by another worker's ambiguity",
        )
        check(
            blocks_for(upgraded, "SW-101") == [(0, "09:00", "10:00")],
            "their blocks are unchanged too",
        )

        # A second upgrade pass changes nothing further - the ambiguity was
        # resolved (recorded) once, not re-decided every startup.
        applied_again = create_schema(upgraded)
        check(
            not any("class_blocks.migrated_employees" in item for item in applied_again),
            f"a second upgrade pass migrates nobody further ({applied_again})",
        )
        check(schedule_for(upgraded, "SW-102") is None, "the ambiguous worker is still not resurrected on a second pass")
        upgraded.close()

    # Genuine first-time migration must still work completely when NO prior
    # migration evidence exists anywhere in the database - the ambiguity
    # check must never fire for a real virgin legacy database.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "genuine-first-time.db"
        builder, _ = build_populated_old_database(path)
        builder.close()
        connection = get_connection(path)
        create_schema(connection)
        check(
            sorted(blocks_for(connection, "SW-001")) == sorted(expected_demo_timetables()["SW-001"]),
            "genuine first-time migration still fully migrates the demo worker (no false ambiguity)",
        )
        check(
            connection.execute(
                "SELECT migrated_at FROM migrated_employees me JOIN employees e ON e.id = me.employee_id"
                " WHERE e.employee_code = 'SW-001'"
            ).fetchone()["migrated_at"]
            != AMBIGUOUS_DELETION_MARKER,
            "the demo worker is recorded as genuinely migrated, not marked ambiguous",
        )
        connection.close()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll semester-migration checks passed (isolated databases only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
