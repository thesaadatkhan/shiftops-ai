"""Regression checks for permanently deleting an employee.

Everything here runs against throwaway in-memory or temporary databases.
The project's own `backend/shiftops.db` is never opened, read or modified.

Checks:

1. A worker with courses, class meetings, preferences and leave is deleted
   along with exactly those records.
2. Shared shifts survive, and so does every other worker and their records.
3. Deletion is refused when the worker has an assignment, whether it is
   historical, in progress, or still ahead. Nothing is removed.
4. An unknown employee code reports not-found, and deleting twice reports
   not-found the second time rather than corrupting anything.
5. A failure partway through rolls the whole thing back - the worker and
   every one of their records are still there.
6. The assignment check runs inside the write transaction, so it cannot be
   invalidated by a concurrent insert.
7. Deletion persists across closing and reopening the database file.
8. The retired employee code survives the deletion and the restart, and
   demo initialization refuses on a database that holds one.
9. A worker referenced by a schedule proposal (Phase 7 increment 3), even
   with no live assignment, is refused with the same controlled error.
10. A worker referenced only by an assignment-audit record is refused the
    same way; a worker with no assignment, proposal, or audit history at
    all remains deletable.
11. A worker referenced as the OUTGOING worker in a Phase 9 agent proposal
    (`agent_proposals.outgoing_employee_id`) is refused the same way, and
    the agent proposal itself is left untouched.
12. A worker referenced as the INCOMING worker in a Phase 9 agent proposal
    (`agent_proposals.incoming_employee_id`) is refused the same way.

Run with:  python verify_employee_deletion.py
Exits non-zero if any check fails.
"""

import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from database import (
    DEMO_SEMESTER_END,
    DEMO_SEMESTER_START,
    create_schema,
    get_connection,
)
from employees import (
    DeletionBlocked,
    EmployeeNotFound,
    create_employee,
    delete_employee,
    find_by_code,
    retired_employee_codes,
)
from seed import DatabaseNotEmpty, initialize_demo_data

RETIRED_AT = datetime(2026, 10, 7, 12, 0)

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fixture():
    connection = get_connection(":memory:")
    create_schema(connection)
    return connection


def add_worker(connection, code, name):
    """Create a worker through the real path and confirm the code issued.

    Employee codes are allocated by the backend now (D034), so `code` is the
    code this fixture EXPECTS, not one it supplies. A mismatch is raised
    rather than ignored, so these checks can never end up asserting against a
    worker other than the one they meant.
    """
    created = create_employee(
        connection, {"full_name": name, "student_type": "undergraduate"}
    )
    if created["employee_code"] != code:
        raise AssertionError(
            f"fixture expected {code} to be issued, got {created['employee_code']}"
        )
    return created


def add_shift(connection, hall, start, end):
    connection.execute(
        "INSERT OR IGNORE INTO shifts (hall, start_datetime, end_datetime) VALUES (?, ?, ?)",
        (hall, start, end),
    )
    return connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ?", (hall, start)
    ).fetchone()["id"]


def give_records(connection, employee_id, label="Writing A"):
    """One course with two meetings, one leave period, one preference."""
    connection.execute(
        "INSERT INTO courses (employee_id, course_label) VALUES (?, ?)",
        (employee_id, label),
    )
    course_id = connection.execute(
        "SELECT id FROM courses WHERE employee_id = ? AND course_label = ?",
        (employee_id, label),
    ).fetchone()["id"]
    for day in (0, 2):
        connection.execute(
            "INSERT INTO class_meetings (course_id, day_of_week, start_time, end_time)"
            " VALUES (?, ?, ?, ?)",
            (course_id, day, "09:00", "10:15"),
        )

    # The semester model as well as the legacy rows, so deletion is checked
    # against both kinds of owned timetable data.
    connection.execute(
        "INSERT INTO semester_schedules (employee_id, start_date, end_date, confirmed_at)"
        " VALUES (?, ?, ?, NULL)",
        (employee_id, DEMO_SEMESTER_START, DEMO_SEMESTER_END),
    )
    schedule_id = connection.execute(
        "SELECT id FROM semester_schedules WHERE employee_id = ?", (employee_id,)
    ).fetchone()["id"]
    for day in (0, 2):
        connection.execute(
            "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time)"
            " VALUES (?, ?, ?, ?)",
            (schedule_id, day, "09:00", "10:15"),
        )
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime)"
        " VALUES (?, ?, ?)",
        (employee_id, "2026-10-10 08:00", "2026-10-10 14:00"),
    )
    shift_id = add_shift(connection, "Vega", "2026-10-05 17:00", "2026-10-05 22:00")
    connection.execute(
        "INSERT INTO shift_preferences (employee_id, shift_id, preference)"
        " VALUES (?, ?, ?)",
        (employee_id, shift_id, "preferred"),
    )
    connection.commit()
    return shift_id


def counts(connection):
    tables = (
        "employees",
        "semester_schedules",
        "class_blocks",
        "courses",
        "class_meetings",
        "shifts",
        "shift_preferences",
        "approved_leave",
        "assignments",
        "schedule_proposals",
        "proposal_assignments",
        "assignment_audit",
    )
    return {
        table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in tables
    }


def assign(connection, employee_id, shift_id):
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (employee_id, shift_id),
    )
    connection.commit()


class FailingConnection:
    """A real connection that raises on the Nth DELETE it is asked to run.

    Used to prove the rollback works. `sqlite3.Connection.execute` is
    read-only, so the failure cannot be patched onto the connection itself;
    this forwards every call to the real one instead.
    """

    def __init__(self, real, fail_on_delete):
        self._real = real
        self._fail_on_delete = fail_on_delete
        self.deletes = 0

    def execute(self, sql, *args):
        if sql.strip().upper().startswith("DELETE"):
            self.deletes += 1
            if self.deletes == self._fail_on_delete:
                raise sqlite3.OperationalError("injected failure")
        return self._real.execute(sql, *args)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()


def blocked_case(label, start, end):
    """Deletion must be refused whatever the shift's timing."""
    connection = fixture()
    worker = add_worker(connection, "SW-001", "Alice Adams")
    give_records(connection, worker["id"])
    shift_id = add_shift(connection, "Capella", start, end)
    assign(connection, worker["id"], shift_id)
    before = counts(connection)

    try:
        delete_employee(connection, "SW-001", retired_at=RETIRED_AT)
        check(False, f"deletion should be refused for a {label} assignment")
    except DeletionBlocked as error:
        message = str(error)
        check("Deactivate them instead" in message, f"a {label} assignment blocks deletion")
        check(
            "history must be preserved" in message.lower(),
            f"the {label} refusal explains that history is preserved",
        )

    check(counts(connection) == before, f"nothing was removed after the {label} refusal")
    check(
        find_by_code(connection, "SW-001") is not None,
        f"the worker still exists after the {label} refusal",
    )
    check(
        retired_employee_codes(connection) == [],
        f"a refused deletion does not retire the code ({label})",
    )
    connection.close()


def main():
    # 1-2. A successful deletion removes the worker's own records only.
    connection = fixture()
    alice = add_worker(connection, "SW-001", "Alice Adams")
    bob = add_worker(connection, "SW-002", "Bob Brown")
    shared_shift = give_records(connection, alice["id"])
    give_records(connection, bob["id"], label="Chemistry B")
    # Bob prefers the same shift Alice does - deleting her must not touch it.
    connection.execute(
        "INSERT OR IGNORE INTO shift_preferences (employee_id, shift_id, preference)"
        " VALUES (?, ?, ?)",
        (bob["id"], shared_shift, "low"),
    )
    connection.commit()
    before = counts(connection)

    result = delete_employee(connection, "SW-001", retired_at=RETIRED_AT)
    after = counts(connection)

    check(find_by_code(connection, "SW-001") is None, "the worker is gone")
    check(result["full_name"] == "Alice Adams", "the result names the deleted worker")
    check(
        result["removed"] == {
            "semester_schedules": 1,
            "class_blocks": 2,
            "courses": 1,
            "class_meetings": 2,
            "shift_preferences": 1,
            "approved_leave": 1,
        },
        f"the result reports what was removed {result['removed']}",
    )
    check(after["employees"] == before["employees"] - 1, "exactly one employee was removed")
    check(after["courses"] == before["courses"] - 1, "only their course was removed")
    check(
        after["class_meetings"] == before["class_meetings"] - 2,
        "only their class meetings were removed",
    )
    check(
        after["approved_leave"] == before["approved_leave"] - 1,
        "only their approved leave was removed",
    )
    check(
        after["shift_preferences"] == before["shift_preferences"] - 1,
        "only their shift preference was removed",
    )
    check(after["shifts"] == before["shifts"], "shared shifts are untouched")

    survivor = find_by_code(connection, "SW-002")
    check(survivor is not None and survivor["id"] == bob["id"], "the other worker is untouched")
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM shift_preferences"
            " WHERE employee_id = ? AND shift_id = ?",
            (bob["id"], shared_shift),
        ).fetchone()["n"] == 1,
        "the other worker keeps their preference on the shift they shared",
    )
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM courses WHERE employee_id = ?", (bob["id"],)
        ).fetchone()["n"] == 1,
        "the other worker keeps their course",
    )

    # 8a. The code is retired.
    check(
        retired_employee_codes(connection) == ["SW-001"],
        f"the deleted code is retired {retired_employee_codes(connection)}",
    )
    retired_row = connection.execute(
        "SELECT retired_at FROM retired_employee_codes WHERE employee_code = 'SW-001'"
    ).fetchone()
    check(retired_row["retired_at"] == "2026-10-07 12:00", "the retirement time is recorded")
    check(
        {row[1] for row in connection.execute("PRAGMA table_info(retired_employee_codes)")}
        == {"employee_code", "retired_at"},
        "the ledger stores only the code and the time, no personal details",
    )

    # 4. Deleting again reports not-found rather than misbehaving.
    try:
        delete_employee(connection, "SW-001", retired_at=RETIRED_AT)
        check(False, "a repeated delete should report not-found")
    except EmployeeNotFound as error:
        check("SW-001" in str(error), "a repeated delete reports not-found")
    try:
        delete_employee(connection, "SW-NOPE", retired_at=RETIRED_AT)
        check(False, "an unknown code should report not-found")
    except EmployeeNotFound as error:
        check("SW-NOPE" in str(error), "an unknown code reports not-found")
    check(counts(connection) == after, "the failed deletes changed nothing")
    connection.close()

    # 3. Assignments block deletion whatever their timing.
    blocked_case("historical", "2026-10-06 17:00", "2026-10-06 22:00")
    blocked_case("in-progress", "2026-10-07 09:00", "2026-10-07 15:00")
    blocked_case("future", "2026-10-09 17:00", "2026-10-09 22:00")

    # 5. A failure partway through rolls everything back.
    connection = fixture()
    worker = add_worker(connection, "SW-001", "Alice Adams")
    give_records(connection, worker["id"])
    before = counts(connection)

    # `sqlite3.Connection.execute` cannot be reassigned, so the failure is
    # injected through a thin stand-in that forwards everything else.
    failing = FailingConnection(connection, fail_on_delete=2)
    try:
        delete_employee(failing, "SW-001", retired_at=RETIRED_AT)
        check(False, "the injected failure should have propagated")
    except sqlite3.OperationalError as error:
        check("injected failure" in str(error), "the injected failure propagates")
    check(
        failing.deletes == 2,
        f"the failure landed after a real delete had run ({failing.deletes})",
    )

    check(counts(connection) == before, f"the failed deletion rolled back completely {counts(connection)}")
    check(find_by_code(connection, "SW-001") is not None, "the worker survives a rolled-back deletion")
    check(
        retired_employee_codes(connection) == [],
        "a rolled-back deletion does not retire the code",
    )
    connection.close()

    # 6. The assignment check happens inside the write transaction. A second
    #    connection trying to insert an assignment mid-deletion is locked out,
    #    so it cannot invalidate the check after it has run.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "locking.db"
        first = get_connection(path)
        create_schema(first)
        worker = add_worker(first, "SW-001", "Alice Adams")
        shift_id = add_shift(first, "Capella", "2026-10-09 17:00", "2026-10-09 22:00")
        first.commit()
        employee_id = worker["id"]

        # Hold the deletion's transaction open at the point after the check.
        first.execute("BEGIN IMMEDIATE")
        blocking = first.execute(
            "SELECT COUNT(*) AS n FROM assignments WHERE employee_id = ?", (employee_id,)
        ).fetchone()["n"]
        check(blocking == 0, "no assignment exists when the check runs")

        second = get_connection(path)
        second.execute("PRAGMA busy_timeout = 250")
        try:
            second.execute(
                "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
                (employee_id, shift_id),
            )
            second.commit()
            check(False, "a concurrent assignment insert should be locked out")
        except sqlite3.OperationalError as error:
            check(
                "locked" in str(error).lower(),
                f"BEGIN IMMEDIATE locks out a concurrent assignment insert -> {error}",
            )
        second.close()
        first.rollback()
        first.close()

    # 7 + 8b. Deletion and the retired code both survive a restart.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "deleted.db"
        first = get_connection(path)
        create_schema(first)
        add_worker(first, "SW-001", "Jack Ma")
        add_worker(first, "SW-002", "Kept Worker")
        give_records(first, find_by_code(first, "SW-001")["id"])
        delete_employee(first, "SW-001", retired_at=RETIRED_AT)
        first.close()

        reopened = get_connection(path)
        check(find_by_code(reopened, "SW-001") is None, "the deletion survives reopening")
        check(
            find_by_code(reopened, "SW-002") is not None,
            "the remaining worker survives reopening",
        )
        check(
            retired_employee_codes(reopened) == ["SW-001"],
            "the retired code survives reopening",
        )
        check(
            reopened.execute("SELECT COUNT(*) AS n FROM courses").fetchone()["n"] == 0,
            "the deleted worker's courses are still gone after reopening",
        )
        # The number stays spent. An allocator reading live employees alone
        # would wrongly conclude SW-001 is free; the ledger is what stops it.
        highest_live = reopened.execute(
            "SELECT MAX(employee_code) AS c FROM employees"
        ).fetchone()["c"]
        check(
            highest_live == "SW-002" and "SW-001" in retired_employee_codes(reopened),
            "SW-001 is absent from employees but still recorded as spent",
        )
        # And allocation actually honours it, rather than merely recording it.
        next_worker = create_employee(
            reopened, {"full_name": "After Deletion", "student_type": "masters"}
        )
        check(
            next_worker["employee_code"] == "SW-003",
            f"the next issued code skips the retired SW-001 ({next_worker['employee_code']})",
        )
        reopened.close()

    # 8c. Demo initialization refuses on a database holding a retired code.
    connection = fixture()
    connection.execute(
        "INSERT INTO retired_employee_codes (employee_code, retired_at) VALUES (?, ?)",
        ("SW-005", "2026-10-07 12:00"),
    )
    connection.commit()
    try:
        initialize_demo_data(connection)
        check(False, "initialization should refuse when a code has been retired")
    except DatabaseNotEmpty as error:
        check(
            "retired_employee_codes" in str(error),
            f"initialization refuses and names the retired-code ledger -> {error}",
        )
    check(
        connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 0,
        "the refused initialization wrote nothing",
    )
    connection.close()

    # 12. A freshly SEEDED worker has semester records but no legacy course
    #     rows at all. The deletion summary must still describe their classes
    #     rather than claiming they had none.
    connection = fixture()
    initialize_demo_data(connection)
    check(
        connection.execute("SELECT COUNT(*) AS n FROM courses").fetchone()["n"] == 0,
        "a seeded database holds no legacy course rows",
    )
    seeded = find_by_code(connection, "SW-002")
    blocks_before = connection.execute(
        "SELECT COUNT(*) AS n FROM class_blocks b"
        " JOIN semester_schedules s ON s.id = b.schedule_id WHERE s.employee_id = ?",
        (seeded["id"],),
    ).fetchone()["n"]
    check(blocks_before > 0, f"the seeded worker has class blocks ({blocks_before})")

    # Canonical Phase 9A demo workers have valid background assignments.
    # Remove this worker's fixture assignments explicitly so this section
    # continues to exercise the allowed deletion summary path; assignment-
    # blocked deletion is covered separately above.
    connection.execute("DELETE FROM assignments WHERE employee_id = ?", (seeded["id"],))
    connection.commit()

    removed = delete_employee(connection, "SW-002")["removed"]
    check(
        removed["class_blocks"] == blocks_before,
        f"deleting a seeded worker reports their class blocks ({removed})",
    )
    check(removed["semester_schedules"] == 1, "and their semester schedule")
    check(
        removed["courses"] == 0 and removed["class_meetings"] == 0,
        "with no legacy rows to report",
    )
    check(
        sum(removed.values()) > 0,
        "the summary is not empty, so the interface cannot say they had no classes",
    )
    check(find_by_code(connection, "SW-002") is None, "the seeded worker is gone")
    check(
        find_by_code(connection, "SW-001") is not None
        and connection.execute(
            "SELECT COUNT(*) AS n FROM class_blocks b"
            " JOIN semester_schedules s ON s.id = b.schedule_id WHERE s.employee_id = ?",
            (find_by_code(connection, "SW-001")["id"],),
        ).fetchone()["n"] > 0,
        "another seeded worker keeps their blocks",
    )
    check(
        connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"] == 198,
        "shared shifts survive",
    )
    check(
        retired_employee_codes(connection) == ["SW-002"],
        "the seeded worker's code is retired",
    )
    connection.close()

    # 9. A worker referenced only by a schedule proposal (no live assignment)
    #    cannot be deleted either - deleting past that reference would
    #    otherwise reach an unhandled SQLite foreign-key error.
    connection = fixture()
    worker = add_worker(connection, "SW-001", "Alice Adams")
    give_records(connection, worker["id"])
    connection.execute(
        "INSERT INTO schedule_proposals (week_start, created_at, status)"
        " VALUES ('2026-10-05', '2026-10-01 00:00', 'pending')"
    )
    proposal_id = connection.execute("SELECT id FROM schedule_proposals").fetchone()["id"]
    shift_id = add_shift(connection, "Capella", "2026-10-09 17:00", "2026-10-09 22:00")
    connection.execute(
        "INSERT INTO proposal_assignments (proposal_id, shift_id, employee_id) VALUES (?, ?, ?)",
        (proposal_id, shift_id, worker["id"]),
    )
    connection.commit()
    before = counts(connection)

    try:
        delete_employee(connection, "SW-001", retired_at=RETIRED_AT)
        check(False, "deletion should be refused when a schedule proposal names this worker")
    except DeletionBlocked as error:
        message = str(error)
        check("proposal reference" in message, f"the refusal names the proposal reference ({message})")
        check("history must be preserved" in message.lower(), "the refusal explains that history is preserved")

    check(counts(connection) == before, "nothing was removed after the proposal-history refusal")
    check(find_by_code(connection, "SW-001") is not None, "the worker still exists after the proposal-history refusal")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM proposal_assignments").fetchone()["n"] == 1,
        "the proposal reference itself is untouched",
    )
    connection.close()

    # 10. A worker referenced only by an assignment-audit record (no live
    #     assignment, no proposal reference) is blocked the same way.
    connection = fixture()
    outgoing = add_worker(connection, "SW-001", "Alice Adams")
    incoming = add_worker(connection, "SW-002", "Bob Brown")
    give_records(connection, outgoing["id"])
    shift_id = add_shift(connection, "Capella", "2026-10-09 17:00", "2026-10-09 22:00")
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (incoming["id"], shift_id),
    )
    connection.execute(
        "INSERT INTO assignment_audit"
        " (occurred_at, action, shift_id, employee_id_before, employee_id_after, detail)"
        " VALUES ('2026-10-01 00:00', 'assignment_replaced', ?, ?, ?, 'test')",
        (shift_id, outgoing["id"], incoming["id"]),
    )
    connection.commit()
    before = counts(connection)

    try:
        delete_employee(connection, "SW-001", retired_at=RETIRED_AT)
        check(False, "deletion should be refused when an audit record names this worker")
    except DeletionBlocked as error:
        message = str(error)
        check("audit record" in message, f"the refusal names the audit record ({message})")
        check("history must be preserved" in message.lower(), "the refusal explains that history is preserved")

    check(counts(connection) == before, "nothing was removed after the audit-history refusal")
    check(find_by_code(connection, "SW-001") is not None, "the outgoing worker still exists after the audit-history refusal")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM assignment_audit").fetchone()["n"] == 1,
        "the audit record itself is untouched",
    )

    # SW-002 (the incoming worker, holding a live assignment) is also
    # blocked, for the pre-existing assignment reason - and, distinctly, a
    # worker with NO scheduling history of any kind (assignment, proposal or
    # audit) remains deletable exactly as before.
    fresh = add_worker(connection, "SW-003", "Carol Chen")
    give_records(connection, fresh["id"], label="Statistics C")
    connection.commit()
    result = delete_employee(connection, "SW-003", retired_at=RETIRED_AT)
    check(find_by_code(connection, "SW-003") is None, "a worker with no assignment, proposal or audit history remains deletable")
    check(result["full_name"] == "Carol Chen", "the deletion result names the correct worker")
    connection.close()

    # 11. A worker referenced as the OUTGOING worker in a Phase 9 agent
    #     proposal (no live assignment - the replacement already happened,
    #     conceptually) cannot be deleted either.
    connection = fixture()
    outgoing_agent = add_worker(connection, "SW-001", "Alice Adams")
    incoming_agent = add_worker(connection, "SW-002", "Bob Brown")
    give_records(connection, outgoing_agent["id"])
    shift_id = add_shift(connection, "Capella", "2026-10-09 17:00", "2026-10-09 22:00")
    connection.execute(
        "INSERT INTO agent_tasks (created_at, updated_at, status, request_text)"
        " VALUES ('2026-10-01 00:00', '2026-10-01 00:00', 'awaiting_approval', 'test task')"
    )
    task_id = connection.execute("SELECT id FROM agent_tasks").fetchone()["id"]
    connection.execute(
        "INSERT INTO agent_proposals"
        " (task_id, created_at, status, action_type, shift_id,"
        "  outgoing_employee_id, incoming_employee_id, rationale)"
        " VALUES (?, '2026-10-01 00:00', 'pending', 'replace_assignment', ?, ?, ?, 'test')",
        (task_id, shift_id, outgoing_agent["id"], incoming_agent["id"]),
    )
    connection.commit()
    before = counts(connection)

    try:
        delete_employee(connection, "SW-001", retired_at=RETIRED_AT)
        check(False, "deletion should be refused when an agent proposal names this worker as outgoing")
    except DeletionBlocked as error:
        message = str(error)
        check("AI agent proposal reference" in message, f"the refusal names the agent proposal reference ({message})")
        check("history must be preserved" in message.lower(), "the refusal explains that history is preserved")

    check(counts(connection) == before, "nothing was removed after the outgoing-agent-proposal refusal")
    check(find_by_code(connection, "SW-001") is not None, "the outgoing worker still exists after the refusal")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM agent_proposals").fetchone()["n"] == 1,
        "the agent proposal itself is untouched",
    )

    # 12. The INCOMING worker named by that same agent proposal is refused
    #     deletion too.
    try:
        delete_employee(connection, "SW-002", retired_at=RETIRED_AT)
        check(False, "deletion should be refused when an agent proposal names this worker as incoming")
    except DeletionBlocked as error:
        message = str(error)
        check("AI agent proposal reference" in message, f"the refusal names the agent proposal reference ({message})")

    check(find_by_code(connection, "SW-002") is not None, "the incoming worker still exists after the refusal")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM agent_proposals").fetchone()["n"] == 1,
        "the agent proposal is still untouched after both refusals",
    )
    connection.close()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll employee deletion checks passed (isolated databases only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
