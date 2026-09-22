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

import random
import sys

from database import (
    DEMO_SEMESTER_CONFIRMED_AT,
    DEMO_SEMESTER_END,
    DEMO_SEMESTER_START,
    create_schema,
    get_connection,
)
from employees import allocation_progress, reserve_up_to, sequence_number
from eligibility import evaluate_shift_eligibility, shift_coverage
from optimizer import generate_draft
from synthetic_data import (
    DEMO_WEEK_STARTS,
    expand_preferences,
    generate_required_shifts,
    generate_workers,
)

ASSIGNMENT_SEED = 20260924

# Deliberate demo scenarios. These stay uncovered in the initial database so
# Coverage, manual fill, schedule generation and the AI can all demonstrate
# meaningful work. They are facts of the canonical fixture, not RNG output.
CANONICAL_UNCOVERED_SHIFTS = (
    ("Vega", "2026-09-21 17:00"),
    ("Capella", "2026-09-22 22:00"),
    ("Helix", "2026-09-24 17:00"),
    ("Sirius", "2026-09-26 08:00"),
    ("Andromeda", "2026-09-28 17:00"),
    ("Vega", "2026-10-01 22:00"),
)

# These two assignments are always present and serve as stable call-out /
# replacement demonstrations. The worker is selected by the deterministic
# optimizer, but the scenario shift itself is hand-selected and never left to
# the filler RNG.
CANONICAL_ASSIGNED_SHIFTS = (
    ("Capella", "2026-09-25 23:00"),
    ("Andromeda", "2026-10-03 11:00"),
)

# Every table demo initialization writes to.
WORKFORCE_TABLES = (
    "employees",
    "semester_schedules",
    "class_blocks",
    "shifts",
    "shift_preferences",
    "approved_leave",
    "assignments",
)

# Legacy tables initialization no longer writes to, but which still mean the
# database is in use: a pre-migration database holds its timetables here.
LEGACY_WORKFORCE_TABLES = ("courses", "class_meetings")

# Every table that must be empty before initialization may run: the tables
# above, plus the retired-code ledger.
#
# The ledger is included even though initialization never writes to it. A
# database that has retired an employee code has been used, and initializing
# demo data into it could hand SW-005 to a demo worker after a real one had
# already been deleted under that code - which is exactly the reuse D040
# exists to prevent.
TABLES_THAT_MUST_BE_EMPTY = (
    WORKFORCE_TABLES + LEGACY_WORKFORCE_TABLES + ("retired_employee_codes",)
)


class DatabaseNotEmpty(RuntimeError):
    """Demo initialization was attempted on a database that already has data."""


def non_empty_tables(connection):
    """Tables that must be empty but already hold rows, as {table: count}.

    `code_allocation` is checked by its value rather than by its row count.
    The table simply existing, or holding a counter still at zero, means
    nothing has been issued and initialization may proceed. A counter above
    zero means employee codes have been handed out in this database, and
    seeding demo workers into it could issue one of those numbers a second
    time - so it counts as in use, even if every employee has since been
    deleted.
    """
    counts = {}
    for table in TABLES_THAT_MUST_BE_EMPTY:
        count = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        if count:
            counts[table] = count

    issued = allocation_progress(connection)
    if issued:
        counts["issued employee codes"] = issued

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

    # The demo timetable goes straight into the semester model. Course labels
    # from the generator are kept only as a note on each block: they are no
    # longer an operational input (D035), and nothing reads them.
    #
    # The timetable is confirmed because the generator builds it to the
    # documented conventions - fixed course loads, non-overlapping classes -
    # so it is validated by construction, not merely present.
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
            DEMO_SEMESTER_CONFIRMED_AT,
        ),
    )
    schedule_id = connection.execute(
        "SELECT id FROM semester_schedules WHERE employee_id = ?", (employee_id,)
    ).fetchone()["id"]

    for course_label, meetings in worker["courses"]:
        for day_of_week, start_time, end_time in meetings:
            connection.execute(
                """
                INSERT INTO class_blocks
                    (schedule_id, day_of_week, start_time, end_time, source_note)
                VALUES (?, ?, ?, ?, ?)
                """,
                (schedule_id, day_of_week, start_time, end_time, course_label),
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


def _shift_key(shift):
    return shift["hall"], shift["start_datetime"]


def insert_canonical_assignments(connection):
    """Insert a deterministic feasible subset of two complete optimizer plans.

    The optimizer first proves that each empty canonical week can be covered.
    A fixed-seed subset becomes background assignments; named scenario shifts
    are forced covered or uncovered. Because every stored row is a subset of
    a proven feasible solution, the omitted assignments remain feasible.
    Each row is nevertheless checked again through the real eligibility
    function immediately before insertion.
    """
    rng = random.Random(ASSIGNMENT_SEED)
    planned = []
    for week_start in DEMO_WEEK_STARTS:
        week_text = week_start.strftime("%Y-%m-%d")
        draft = generate_draft(connection, week_text)
        if draft["status"] != "complete":
            raise RuntimeError(f"Canonical week {week_text} is not fully schedulable.")
        for shift in draft["shifts"]:
            for worker in shift["proposed_assignments"]:
                planned.append((shift, worker))

    uncovered = set(CANONICAL_UNCOVERED_SHIFTS)
    forced = set(CANONICAL_ASSIGNED_SHIFTS)
    selected = []
    for shift, worker in planned:
        key = _shift_key(shift)
        if key in uncovered:
            continue
        if key in forced or rng.random() < 0.55:
            selected.append((shift, worker))

    # Every synthetic worker should have at least one existing assignment
    # somewhere in the two-week demo. Pull from the already-proven full plan,
    # while preserving the deliberate uncovered scenarios.
    selected_codes = {worker["employee_code"] for _, worker in selected}
    for shift, worker in planned:
        if worker["employee_code"] in selected_codes or _shift_key(shift) in uncovered:
            continue
        selected.append((shift, worker))
        selected_codes.add(worker["employee_code"])

    for shift_payload, worker_payload in sorted(
        selected, key=lambda item: (item[0]["start_datetime"], item[0]["hall"])
    ):
        shift = connection.execute(
            "SELECT id, hall, start_datetime, end_datetime, required_staff"
            " FROM shifts WHERE id = ?",
            (shift_payload["id"],),
        ).fetchone()
        employee = connection.execute(
            "SELECT id, employee_code, full_name, is_active, weekly_hour_limit"
            " FROM employees WHERE employee_code = ?",
            (worker_payload["employee_code"],),
        ).fetchone()
        eligibility = evaluate_shift_eligibility(connection, shift, employee)
        if not eligibility["eligible"]:
            raise RuntimeError(
                f"Canonical assignment {employee['employee_code']} -> {shift['id']} "
                f"failed eligibility: {eligibility['reason_codes']}"
            )
        connection.execute(
            "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
            (employee["id"], shift["id"]),
        )

    missing_workers = connection.execute(
        "SELECT employee_code FROM employees WHERE id NOT IN"
        " (SELECT DISTINCT employee_id FROM assignments) ORDER BY employee_code"
    ).fetchall()
    if missing_workers:
        raise RuntimeError(
            "Canonical assignment plan left workers unassigned: "
            + ", ".join(row["employee_code"] for row in missing_workers)
        )

    # Acceptance gate for the Jack-Ma failure mode: several deliberately
    # uncovered shifts across both weeks must each have a useful candidate
    # pool, not one exceptional ready worker.
    for hall, start_datetime in CANONICAL_UNCOVERED_SHIFTS:
        row = connection.execute(
            "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ?",
            (hall, start_datetime),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Missing canonical scenario shift {hall} {start_datetime}.")
        coverage = shift_coverage(connection, row["id"])
        if len(coverage["eligible_candidates"]) < 5:
            raise RuntimeError(
                f"Canonical scenario {hall} {start_datetime} has only "
                f"{len(coverage['eligible_candidates'])} eligible workers; expected at least 5."
            )

    # The remaining positions must still be fully coverable after background
    # assignments are stored.
    for week_start in DEMO_WEEK_STARTS:
        week_text = week_start.strftime("%Y-%m-%d")
        if generate_draft(connection, week_text)["status"] != "complete":
            raise RuntimeError(f"Canonical week {week_text} became infeasible after seeding.")


def initialize_demo_data(connection):
    """Write the whole demo dataset, or nothing at all.

    Everything happens inside one transaction opened with BEGIN IMMEDIATE, so
    the "is this database empty?" check and the inserts that depend on it
    cannot be separated by another writer. If any insert fails, the whole
    dataset is rolled back - there is no half-seeded state where, say, the
    shifts exist but the workers do not.

    Raises DatabaseNotEmpty if any workforce table already holds rows.
    """
    shifts = [
        shift
        for week_start in DEMO_WEEK_STARTS
        for shift in generate_required_shifts(week_start)
    ]
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

            insert_canonical_assignments(connection)

            # Reserve the numbers the demo codes occupy, in this same
            # transaction. Without it the first worker added afterwards would
            # be issued SW-001, which a demo worker already holds.
            reserved = [
                number
                for number in (
                    sequence_number(worker["employee_code"]) for worker in workers
                )
                if number is not None
            ]
            if reserved:
                reserve_up_to(connection, max(reserved))

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
