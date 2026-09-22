"""Read-only validation for a built or installed canonical demo database.

The target is opened with SQLite ``mode=ro``. No schema creation, migration,
seeding or other write occurs. Run with: python validate_canonical_demo.py PATH
"""

import sqlite3
import sys
from pathlib import Path

from eligibility import evaluate_shift_eligibility, shift_coverage
from optimizer import generate_draft
from seed import CANONICAL_ASSIGNED_SHIFTS, CANONICAL_UNCOVERED_SHIFTS
from synthetic_data import DEMO_WEEK_STARTS


EXPECTED_COUNTS = {
    "employees": 30,
    "semester_schedules": 30,
    "class_blocks": 147,
    "shifts": 198,
    "shift_preferences": 372,
    "approved_leave": 30,
    "assignments": 108,
}


def validate(path):
    target = Path(path).resolve()
    if not target.is_file():
        raise ValueError(f"Database file does not exist: {target}")
    connection = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    failures = []
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in EXPECTED_COUNTS
        }
        if counts != EXPECTED_COUNTS:
            failures.append(f"row counts differ: {counts}")

        schedule_problem = connection.execute(
            "SELECT COUNT(*) AS n FROM semester_schedules"
            " WHERE confirmed_at IS NULL OR dates_provisional != 0"
            " OR start_date > '2026-09-21' OR end_date < '2026-10-04'"
        ).fetchone()["n"]
        if schedule_problem:
            failures.append(f"{schedule_problem} semester schedules are not ready across both weeks")

        for table in ("approved_leave", "shift_preferences", "assignments"):
            distinct = connection.execute(
                f"SELECT COUNT(DISTINCT employee_id) AS n FROM {table}"
            ).fetchone()["n"]
            if distinct != 30:
                failures.append(f"{table} represents {distinct} workers rather than 30")

        assignment_rows = connection.execute(
            "SELECT s.id AS shift_id, s.hall, s.start_datetime, s.end_datetime,"
            " s.required_staff, e.id AS employee_id, e.employee_code, e.full_name,"
            " e.is_active, e.weekly_hour_limit"
            " FROM assignments a JOIN shifts s ON s.id = a.shift_id"
            " JOIN employees e ON e.id = a.employee_id"
            " ORDER BY s.start_datetime, s.hall, e.employee_code"
        ).fetchall()
        for row in assignment_rows:
            shift = {key: row[key] for key in (
                "shift_id", "hall", "start_datetime", "end_datetime", "required_staff"
            )}
            shift["id"] = shift.pop("shift_id")
            employee = {key: row[key] for key in (
                "employee_id", "employee_code", "full_name", "is_active", "weekly_hour_limit"
            )}
            employee["id"] = employee.pop("employee_id")
            result = evaluate_shift_eligibility(connection, shift, employee)
            if not result["eligible"]:
                failures.append(
                    f"stored assignment {employee['employee_code']} -> {shift['id']} "
                    f"is invalid: {result['reason_codes']}"
                )

        for hall, start_datetime in CANONICAL_UNCOVERED_SHIFTS:
            shift = connection.execute(
                "SELECT id, required_staff FROM shifts WHERE hall = ? AND start_datetime = ?",
                (hall, start_datetime),
            ).fetchone()
            assigned = connection.execute(
                "SELECT COUNT(*) AS n FROM assignments WHERE shift_id = ?", (shift["id"],)
            ).fetchone()["n"]
            eligible = len(shift_coverage(connection, shift["id"])["eligible_candidates"])
            if assigned >= shift["required_staff"]:
                failures.append(f"deliberate scenario is not uncovered: {hall} {start_datetime}")
            if eligible < 5:
                failures.append(f"scenario has only {eligible} eligible workers: {hall} {start_datetime}")

        for hall, start_datetime in CANONICAL_ASSIGNED_SHIFTS:
            assigned = connection.execute(
                "SELECT COUNT(*) AS n FROM assignments a JOIN shifts s ON s.id = a.shift_id"
                " WHERE s.hall = ? AND s.start_datetime = ?", (hall, start_datetime)
            ).fetchone()["n"]
            if assigned < 1:
                failures.append(f"assigned call-out/replacement scenario is empty: {hall} {start_datetime}")

        for week_start in DEMO_WEEK_STARTS:
            draft = generate_draft(connection, week_start.strftime("%Y-%m-%d"))
            if draft["status"] != "complete":
                failures.append(f"remaining schedule is infeasible for {week_start:%Y-%m-%d}")
    finally:
        connection.close()
    return EXPECTED_COUNTS.copy(), failures


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("Usage: python validate_canonical_demo.py PATH", file=sys.stderr)
        return 2
    try:
        counts, failures = validate(arguments[0])
    except (ValueError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
    if failures:
        print("Canonical demo validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"Canonical demo validation passed: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
