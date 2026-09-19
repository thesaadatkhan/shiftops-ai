"""Seed the synthetic workforce, required shifts, preferences, and leave.

Two modes, because "make the database match the generator" and "make sure the
seed data is present" are different jobs with very different risks.

    python seed.py            routine seeding (safe, insert-only)
    python seed.py --repair   rewrite the seed workers from the generator

Routine seeding never modifies or deletes anything that already exists. A
worker is either absent - in which case they are created along with their
courses, class meetings, approved leave and shift preferences - or already
present, in which case they are skipped entirely. Edits you make to an
existing worker, and any classes, leave or preferences you add to one, survive
re-running it.

Repair mode reconciles the seed workers back to the generator's values,
deleting rows the generator does not produce. That is the only way to correct
data an earlier, buggy version of the generator wrote, since an insert can
never fix a row that is already wrong. It is deliberately not the default: run
it only when you actually want the generator's values to win.

Both modes insert the required shifts with INSERT OR IGNORE. Shifts are the
shared coverage requirement rather than any worker's own data, and neither
mode ever deletes one. Neither mode ever touches the `assignments` table or a
worker whose employee code the generator does not produce.

Run with:  python seed.py [--repair]
"""

import argparse

from database import create_schema, get_connection
from synthetic_data import expand_preferences, generate_required_shifts, generate_workers

COUNTED_TABLES = (
    "employees",
    "courses",
    "class_meetings",
    "shifts",
    "shift_preferences",
    "approved_leave",
    "assignments",
)

REPAIR_WARNING = """\
Repair mode replaces these records for the seed workers with the generator's
values:
  - name and student type
  - courses and class meetings
  - approved leave periods
  - shift preferences
Rows added for those workers that the generator does not produce are DELETED.
Not touched: shifts, assignments, and any worker the generator does not
produce."""


def seed_shifts(connection, shifts):
    for shift in shifts:
        connection.execute(
            """
            INSERT OR IGNORE INTO shifts
                (hall, start_datetime, end_datetime, required_staff)
            VALUES (?, ?, ?, ?)
            """,
            (
                shift["hall"],
                shift["start_datetime"],
                shift["end_datetime"],
                shift["required_staff"],
            ),
        )


def shift_id_lookup(connection):
    rows = connection.execute(
        "SELECT id, hall, start_datetime, end_datetime FROM shifts"
    ).fetchall()
    return {
        (row["hall"], row["start_datetime"], row["end_datetime"]): row["id"]
        for row in rows
    }


def find_employee_id(connection, employee_code):
    row = connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (employee_code,)
    ).fetchone()
    return row["id"] if row else None


def desired_preference_rows(worker, shifts, shift_ids):
    return {
        (shift_ids[shift_key], level)
        for shift_key, level in expand_preferences(worker["preferences"], shifts).items()
    }


# --------------------------------------------------------------------------
# Routine seeding: insert-only, never modifies or deletes existing rows
# --------------------------------------------------------------------------


def create_worker(connection, worker, shifts, shift_ids):
    """Create a worker and all of their related records.

    Only called for workers that do not exist yet, so there is nothing to
    overwrite.
    """
    connection.execute(
        """
        INSERT INTO employees (employee_code, full_name, student_type)
        VALUES (?, ?, ?)
        """,
        (worker["employee_code"], worker["full_name"], worker["student_type"]),
    )
    employee_id = find_employee_id(connection, worker["employee_code"])

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
                INSERT INTO class_meetings
                    (course_id, day_of_week, start_time, end_time)
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

    for shift_id, level in desired_preference_rows(worker, shifts, shift_ids):
        connection.execute(
            """
            INSERT INTO shift_preferences (employee_id, shift_id, preference)
            VALUES (?, ?, ?)
            """,
            (employee_id, shift_id, level),
        )


def seed_missing_workers(connection, workers, shifts, shift_ids):
    """Create only the workers that are absent. Existing ones are skipped."""
    created = skipped = 0
    for worker in workers:
        if find_employee_id(connection, worker["employee_code"]) is not None:
            skipped += 1
            continue
        create_worker(connection, worker, shifts, shift_ids)
        created += 1
    return created, skipped


# --------------------------------------------------------------------------
# Repair mode: reconcile the seed workers back to the generator's values
# --------------------------------------------------------------------------


def reconcile_rows(connection, table, owner_column, owner_id, columns, desired):
    """Make one owner's rows in `table` match `desired` exactly.

    `table`, `owner_column` and `columns` are module constants, never user
    input, so composing them into the SQL text is safe; all values are still
    passed as bound parameters.
    """
    column_list = ", ".join(columns)
    stored = {
        tuple(row)
        for row in connection.execute(
            f"SELECT {column_list} FROM {table} WHERE {owner_column} = ?",
            (owner_id,),
        ).fetchall()
    }

    outdated = stored - desired
    missing = desired - stored

    match_columns = " AND ".join(f"{column} = ?" for column in columns)
    for row in outdated:
        connection.execute(
            f"DELETE FROM {table} WHERE {owner_column} = ? AND {match_columns}",
            (owner_id, *row),
        )

    placeholders = ", ".join("?" for _ in columns)
    for row in missing:
        connection.execute(
            f"INSERT OR IGNORE INTO {table} ({owner_column}, {column_list}) "
            f"VALUES (?, {placeholders})",
            (owner_id, *row),
        )

    return len(outdated), len(missing)


def reconcile_courses(connection, employee_id, courses):
    """Match one worker's courses and their class meetings to `courses`."""
    desired = {label: set(meetings) for label, meetings in courses}
    stored = {
        row["course_label"]: row["id"]
        for row in connection.execute(
            "SELECT id, course_label FROM courses WHERE employee_id = ?",
            (employee_id,),
        ).fetchall()
    }

    removed = added = 0

    for label, course_id in stored.items():
        if label not in desired:
            # Remove the meetings first: they reference the course row.
            connection.execute(
                "DELETE FROM class_meetings WHERE course_id = ?", (course_id,)
            )
            connection.execute("DELETE FROM courses WHERE id = ?", (course_id,))
            removed += 1

    for label, meetings in desired.items():
        course_id = stored.get(label)
        if course_id is None:
            connection.execute(
                "INSERT INTO courses (employee_id, course_label) VALUES (?, ?)",
                (employee_id, label),
            )
            course_id = connection.execute(
                "SELECT id FROM courses WHERE employee_id = ? AND course_label = ?",
                (employee_id, label),
            ).fetchone()["id"]
            added += 1

        meeting_removed, meeting_added = reconcile_rows(
            connection,
            "class_meetings",
            "course_id",
            course_id,
            ("day_of_week", "start_time", "end_time"),
            meetings,
        )
        removed += meeting_removed
        added += meeting_added

    return removed, added


def repair_worker(connection, worker, shifts, shift_ids):
    """Force one seed worker's records back to the generator's values."""
    employee_id = find_employee_id(connection, worker["employee_code"])
    if employee_id is None:
        create_worker(connection, worker, shifts, shift_ids)
        return 0, 0

    connection.execute(
        """
        UPDATE employees SET full_name = ?, student_type = ?
        WHERE id = ? AND (full_name <> ? OR student_type <> ?)
        """,
        (
            worker["full_name"],
            worker["student_type"],
            employee_id,
            worker["full_name"],
            worker["student_type"],
        ),
    )

    removed, added = reconcile_courses(connection, employee_id, worker["courses"])

    leave_removed, leave_added = reconcile_rows(
        connection,
        "approved_leave",
        "employee_id",
        employee_id,
        ("start_datetime", "end_datetime"),
        set(worker["approved_leave"]),
    )

    preference_removed, preference_added = reconcile_rows(
        connection,
        "shift_preferences",
        "employee_id",
        employee_id,
        ("shift_id", "preference"),
        desired_preference_rows(worker, shifts, shift_ids),
    )

    return (
        removed + leave_removed + preference_removed,
        added + leave_added + preference_added,
    )


# --------------------------------------------------------------------------


def seed(connection, repair=False):
    """Seed into an open connection. Returns a short report of what changed."""
    shifts = generate_required_shifts()
    workers = generate_workers(shifts)

    seed_shifts(connection, shifts)
    connection.commit()
    shift_ids = shift_id_lookup(connection)

    if repair:
        removed = added = 0
        for worker in workers:
            worker_removed, worker_added = repair_worker(
                connection, worker, shifts, shift_ids
            )
            removed += worker_removed
            added += worker_added
        report = f"repair: {removed} outdated row(s) removed, {added} row(s) added"
    else:
        created, skipped = seed_missing_workers(connection, workers, shifts, shift_ids)
        report = (
            f"routine seeding: {created} worker(s) created, "
            f"{skipped} existing worker(s) left untouched"
        )

    connection.commit()
    return report


def table_counts(connection):
    return {
        table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in COUNTED_TABLES
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--repair",
        action="store_true",
        help=(
            "rewrite the seed workers' records from the generator, deleting "
            "rows the generator does not produce (see the warning it prints)"
        ),
    )
    arguments = parser.parse_args()

    if arguments.repair:
        print(REPAIR_WARNING)
        print()

    connection = get_connection()
    try:
        create_schema(connection)
        report = seed(connection, repair=arguments.repair)
        counts = table_counts(connection)
    finally:
        connection.close()

    for table, count in counts.items():
        print(f"{table}: {count} rows")
    print(report)


if __name__ == "__main__":
    main()
