"""Explicit week-level scheduling operations (Phase 7 increment 1).

Preparing a reporting week's required shifts is a separate, explicit,
transactional operation - never a side effect of reading or selecting a
week (`get_week_schedule` performs no writes at all), and never routed
through demo initialization (`seed.py`) or reseeding. Only `prepare_week`
inserts shifts, and only for the one week it is asked to prepare.

Both functions validate `week_start` through `weeks.parse_week_start`, the
same rule the optional employee-reporting `week_start` parameter uses, so a
malformed date or non-Monday is rejected identically everywhere.
"""

from datetime import datetime

from eligibility import evaluate_shift_eligibility
from reporting import shift_duration_hours
from synthetic_data import TIME_FORMAT, generate_required_shifts
from weeks import DATE_FORMAT, parse_week_start, week_bounds


def _in_transaction(connection, work):
    """Run `work` inside one BEGIN IMMEDIATE transaction.

    The same shape `timetables._in_transaction` and `employees.create_employee`
    use: the write lock is taken before anything is read, so a concurrent
    caller cannot act on a view of the database this operation has already
    changed, and any failure rolls the whole operation back rather than
    leaving a week half-prepared.
    """
    previous_isolation = connection.isolation_level
    connection.isolation_level = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = work()
            connection.execute("COMMIT")
            return result
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.isolation_level = previous_isolation


def prepare_week(connection, week_start_text):
    """Insert any required shifts missing for one Monday week. Idempotent.

    Never deletes or overwrites an existing shift, and never touches
    assignments, employees, timetables, preferences, leave, allocator state,
    or any other week. The set of already-stored shifts for this week is read
    *inside* the same transaction that takes the write lock, so preparing the
    same week again - even from two callers at once - inserts nothing further
    the second time. Dated shift preferences are never copied here; only the
    sample week carries the demo preference rows, and this never creates any.
    """
    week_start = parse_week_start(week_start_text)
    _, week_end = week_bounds(week_start)

    def work():
        existing = {
            (row["hall"], row["start_datetime"], row["end_datetime"])
            for row in connection.execute(
                "SELECT hall, start_datetime, end_datetime FROM shifts"
                " WHERE start_datetime >= ? AND start_datetime < ?",
                (week_start.strftime(TIME_FORMAT), week_end.strftime(TIME_FORMAT)),
            )
        }
        required = generate_required_shifts(week_start)

        inserted = 0
        for shift in required:
            key = (shift["hall"], shift["start_datetime"], shift["end_datetime"])
            if key in existing:
                continue
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
            inserted += 1

        return {
            "week_start": week_start.strftime(DATE_FORMAT),
            "week_end": week_end.strftime(DATE_FORMAT),
            "shifts_required": len(required),
            "shifts_inserted": inserted,
            "shifts_already_present": len(required) - inserted,
        }

    return _in_transaction(connection, work)


def get_week_schedule(connection, week_start_text):
    """Read-only: the stored schedule for one Monday week. Never writes.

    Returns every stored shift whose start belongs to the week - D025: a
    cross-midnight shift belongs to the week containing its start - sorted by
    start datetime, then hall, then id, together with current assignment
    information and whether each shift is covered. A week nobody has
    prepared yet returns an empty but valid `shifts` list; this never
    generates or inserts anything.

    **Each assigned worker also carries `conflicts`** (Codex review finding
    2): a class/semester edit or a new approved-leave period can invalidate
    an assignment that was perfectly valid when it was approved, and nothing
    previously surfaced that. `conflicts` is `null` when the assignment
    still passes every current hard eligibility rule, or the same structured
    `{reason_codes, reasons}` shape Coverage and proposal revalidation
    already use when it does not - computed by calling
    `eligibility.evaluate_shift_eligibility` again, the SAME function every
    other hard-rule check in this project uses, never a second
    implementation. This is purely informational: it never removes,
    replaces, or otherwise changes the assignment, and it never affects
    `assigned_count` or `covered` - a currently-inactive worker's past
    assignment still counts as staffing exactly as before. Resolving a
    flagged conflict is the supervisor's explicit choice, through the
    existing replacement flow.
    """
    week_start = parse_week_start(week_start_text)
    _, week_end = week_bounds(week_start)

    shifts = connection.execute(
        "SELECT id, hall, start_datetime, end_datetime, required_staff FROM shifts"
        " WHERE start_datetime >= ? AND start_datetime < ?"
        " ORDER BY start_datetime, hall, id",
        (week_start.strftime(TIME_FORMAT), week_end.strftime(TIME_FORMAT)),
    ).fetchall()

    # The schema permits more than one assignment row for the same shift
    # (`assignments` is only unique per employee/shift pair, not per shift),
    # so every assignment is collected into a list per shift rather than a
    # single dict - overwriting one assignment with another when a shift has
    # more than one would silently discard a real stored record.
    assigned_by_shift = {}
    shift_ids = [row["id"] for row in shifts]
    if shift_ids:
        placeholders = ",".join("?" for _ in shift_ids)
        for row in connection.execute(
            f"""
            SELECT a.shift_id, e.id AS employee_id, e.employee_code, e.full_name,
                   e.is_active, e.weekly_hour_limit
            FROM assignments a
            JOIN employees e ON e.id = a.employee_id
            WHERE a.shift_id IN ({placeholders})
            """,
            shift_ids,
        ):
            assigned_by_shift.setdefault(row["shift_id"], []).append(
                {
                    "employee_id": row["employee_id"],
                    "employee_code": row["employee_code"],
                    "full_name": row["full_name"],
                    "is_active": row["is_active"],
                    "weekly_hour_limit": row["weekly_hour_limit"],
                }
            )

    def shift_payload(row):
        start = datetime.strptime(row["start_datetime"], TIME_FORMAT)
        end = datetime.strptime(row["end_datetime"], TIME_FORMAT)
        duration = shift_duration_hours(start, end, label=f"{row['id']} at {row['hall']}")
        # Deterministic order - by employee code, then id - so the same
        # shift's assignment list reads identically on every call regardless
        # of insertion order.
        assigned = sorted(
            assigned_by_shift.get(row["id"], []),
            key=lambda worker: (worker["employee_code"], worker["employee_id"]),
        )
        assigned_with_conflicts = []
        for worker in assigned:
            result = evaluate_shift_eligibility(
                connection,
                row,
                {
                    "id": worker["employee_id"],
                    "employee_code": worker["employee_code"],
                    "full_name": worker["full_name"],
                    "is_active": worker["is_active"],
                    "weekly_hour_limit": worker["weekly_hour_limit"],
                },
            )
            assigned_with_conflicts.append(
                {
                    "employee_id": worker["employee_id"],
                    "employee_code": worker["employee_code"],
                    "full_name": worker["full_name"],
                    "conflicts": None
                    if result["eligible"]
                    else {
                        "reason_codes": result["reason_codes"],
                        "reasons": result["reasons"],
                    },
                }
            )
        return {
            "id": row["id"],
            "hall": row["hall"],
            "start_datetime": row["start_datetime"],
            "end_datetime": row["end_datetime"],
            "duration_hours": duration,
            "required_staff": row["required_staff"],
            "assigned_employees": assigned_with_conflicts,
            "assigned_count": len(assigned),
            "covered": len(assigned) >= row["required_staff"],
        }

    return {
        "week_start": week_start.strftime(DATE_FORMAT),
        "week_end": week_end.strftime(DATE_FORMAT),
        "shifts": [shift_payload(row) for row in shifts],
    }
