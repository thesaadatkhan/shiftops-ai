"""Focused checks for the supervisor manual uncovered-shift fill operation.

All checks use isolated in-memory databases. The managed shiftops.db is never
opened. Run with: python verify_manual_assignment.py
"""

import sys
from datetime import datetime

from database import create_schema, get_connection
from proposals import AssignmentNotFound, ReplacementInvalid, create_assignment


failures = []
checks = 0


def check(condition, description):
    global checks
    checks += 1
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fixture():
    connection = get_connection(":memory:")
    create_schema(connection)
    for code in ("SW-001", "SW-002", "SW-003"):
        connection.execute(
            "INSERT INTO employees (employee_code, full_name, student_type)"
            " VALUES (?, ?, 'undergraduate')",
            (code, f"Worker {code}"),
        )
        employee_id = connection.execute(
            "SELECT id FROM employees WHERE employee_code = ?", (code,)
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO semester_schedules"
            " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
            " VALUES (?, '2026-09-21', '2026-10-04', '2026-09-01 09:00', 0)",
            (employee_id,),
        )
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime)"
        " SELECT id, '2026-09-21 16:00', '2026-09-21 23:00'"
        " FROM employees WHERE employee_code = 'SW-003'"
    )
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES ('Vega', '2026-09-21 17:00', '2026-09-21 22:00', 1)"
    )
    connection.commit()
    shift_id = connection.execute("SELECT id FROM shifts").fetchone()["id"]
    return connection, shift_id


def main():
    connection, shift_id = fixture()
    result = create_assignment(
        connection, shift_id, "SW-001", reference_time=datetime(2026, 9, 20, 12, 0)
    )
    check(result == {
        "shift_id": shift_id,
        "incoming_employee_code": "SW-001",
        "occurred_at": "2026-09-20 12:00",
    }, "success returns the exact saved assignment fact")
    check(connection.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"] == 1,
          "success writes exactly one assignment")
    audit = connection.execute(
        "SELECT action, employee_id_before, employee_id_after, proposal_id, agent_proposal_id"
        " FROM assignment_audit"
    ).fetchone()
    sw001 = connection.execute(
        "SELECT id FROM employees WHERE employee_code = 'SW-001'"
    ).fetchone()["id"]
    check(audit["action"] == "assignment_created" and audit["employee_id_before"] is None,
          "success records an assignment_created audit fact")
    check(audit["employee_id_after"] == sw001 and audit["proposal_id"] is None
          and audit["agent_proposal_id"] is None,
          "manual audit identifies the incoming worker without a proposal link")

    before = tuple(connection.iterdump())
    try:
        create_assignment(connection, shift_id, "SW-002")
        check(False, "a covered shift refuses another worker")
    except ReplacementInvalid as error:
        check(error.detail["reason_codes"] == ["shift_already_covered"],
              "a covered shift returns the stable conflict code")
    check(tuple(connection.iterdump()) == before, "covered-shift refusal writes nothing")

    second, second_shift = fixture()
    before = tuple(second.iterdump())
    try:
        create_assignment(second, second_shift, "SW-003")
        check(False, "a worker on leave is refused")
    except ReplacementInvalid as error:
        check("leave_conflict" in error.detail["reason_codes"],
              "current eligibility is rechecked inside the transaction")
    check(tuple(second.iterdump()) == before, "eligibility refusal writes nothing")

    try:
        create_assignment(second, 999999, "SW-001")
        check(False, "an unknown shift is refused")
    except AssignmentNotFound:
        check(True, "an unknown shift is a controlled not-found result")
    try:
        create_assignment(second, second_shift, "SW-999")
        check(False, "an unknown worker is refused")
    except AssignmentNotFound:
        check(True, "an unknown worker is a controlled not-found result")
    connection.close()
    second.close()

    if failures:
        print(f"\nFAILED ({len(failures)} of {checks})")
        return 1
    print(f"\nAll {checks} manual-assignment checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
