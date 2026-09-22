"""Regression checks for Phase 9 increment 2's supervisor decision API
(`agent_service.approve_agent_proposal`/`reject_agent_proposal`).

Runs entirely against throwaway in-memory or temp-file databases with
purpose-built workers, shifts and schedules. The project's own
`backend/shiftops.db` is never opened, read, or modified. No real OpenAI
API key is used and no network request is made anywhere in this file -
approval/rejection involve no model call at all.

Checks:

 1. Successful replacement approval: assignment atomically replaced,
    proposal/task status updated, audit rows linked by `agent_proposal_id`.
 2. Successful uncovered-shift fill: one assignment created, no outgoing
    worker referenced.
 3. Explicit rejection: zero assignment changes, task closed.
 4. Approval-content mismatch (wrong shift/worker) is a controlled
    content-mismatch refusal; nothing is written.
 5. Stale outgoing assignment (outgoing worker no longer holds the shift by
    approval time) is a controlled revalidation conflict.
 6-11. Each of: incoming worker becomes inactive, timetable becomes
    unconfirmed, timetable becomes provisional, a new class conflict, a new
    approved-leave conflict, a new overlapping assignment, and a weekly-
    hour-limit conflict - each is a controlled 409-shaped revalidation
    conflict naming the real `evaluate_shift_eligibility` reason code, and
    each writes nothing.
12. An uncovered position consumed by someone else between proposal
    creation and approval is a controlled revalidation conflict.
13. State transition rules: approved cannot later be rejected; rejected
    cannot later be approved.
14. Repeated approval of an already-successful proposal is an idempotent
    success - no second assignment, no duplicate audit row.
15. Repeated rejection of an already-rejected proposal is idempotent - no
    duplicate audit row.
16. Two connections approving the SAME proposal concurrently produce
    exactly one assignment change; the second gets the stored successful
    result, never a raw uniqueness or locking failure.
17. Approval and rejection racing each other resolve to exactly one final
    decision - never both.
18. Audit linkage: the mutation audit row's `employee_id_before`/
    `employee_id_after`/`shift_id`/`agent_proposal_id` are exactly right.
19. Successful post-write verification is recorded and a readback is
    returned.
20. A forced post-write verification failure (via the injectable
    `verify_fn` seam) is recorded SEPARATELY from execution - the
    assignment write is never reported as failed or rolled back, and the
    database is not corrupted to produce this.
21. There is no tool or model-reachable path that can approve or execute a
    proposal - `agent_tools` has no such tool, and a model requesting one
    by name gets a controlled unknown-tool refusal.
22. An "applied but never verified" recovery gap: a proposal whose
    assignment write and 'approved'/'applied' state genuinely committed but
    whose `verification_outcome` is still NULL (simulating a process or
    second-write failure between the execution commit and the verification
    write). A repeated exact approval must repeat no assignment mutation
    and no duplicate execution/approval audit row, run only the missing
    post-write verification, and return the reconciled result.
23. A recorded `verification_failed` outcome is a completed fact, not a
    retry trigger: an ordinary approval retry after one is recorded must
    not rerun verification or touch the assignment again.

Run with:  python verify_agent_decision.py
Exits non-zero if any check fails.
"""

import sys
import tempfile
import threading
from pathlib import Path

import agent_service
import agent_tools
import proposals as proposals_module
from database import create_schema, get_connection

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fixture_database(path=":memory:"):
    connection = get_connection(path)
    create_schema(connection)
    return connection


def add_employee(connection, code, name, limit=20, active=True):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active) VALUES (?, ?, 'undergraduate', ?, ?)",
        (code, name, limit, 1 if active else 0),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]


def add_ready_schedule(connection, employee_id, start="2026-08-24", end="2026-12-11"):
    connection.execute(
        "INSERT INTO semester_schedules"
        " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
        " VALUES (?, ?, ?, '2026-01-01 00:00', 0)",
        (employee_id, start, end),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM semester_schedules WHERE employee_id = ? ORDER BY id DESC", (employee_id,)
    ).fetchone()["id"]


def add_shift(connection, hall, start, end, required_staff=1):
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES (?, ?, ?, ?)",
        (hall, start, end, required_staff),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ?", (hall, start)
    ).fetchone()["id"]


def add_assignment(connection, employee_id, shift_id):
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (employee_id, shift_id),
    )
    connection.commit()


def add_task(connection, request_text="test task"):
    connection.execute(
        "INSERT INTO agent_tasks (created_at, updated_at, status, request_text)"
        " VALUES ('2026-01-01 00:00', '2026-01-01 00:00', 'awaiting_approval', ?)",
        (request_text,),
    )
    connection.commit()
    return connection.execute("SELECT id FROM agent_tasks ORDER BY id DESC").fetchone()["id"]


def assignment_count(connection):
    return connection.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"]


def audit_count(connection):
    return connection.execute("SELECT COUNT(*) AS n FROM assignment_audit").fetchone()["n"]


def build_replacement_fixture(connection, outgoing_limit=20, incoming_limit=20):
    """Outgoing worker holding a shift, incoming worker eligible to replace
    them, an awaiting_approval task, and a real pending proposal for it."""
    outgoing_id = add_employee(connection, "SW-010", "Jordan Kim", limit=outgoing_limit)
    add_ready_schedule(connection, outgoing_id)
    incoming_id = add_employee(connection, "SW-011", "Sam Osei", limit=incoming_limit)
    add_ready_schedule(connection, incoming_id)
    shift_id = add_shift(connection, "Capella", "2026-10-06 22:00", "2026-10-07 03:00")
    add_assignment(connection, outgoing_id, shift_id)
    task_id = add_task(connection)
    proposal = agent_tools.call_tool(
        connection, "propose_replacement",
        {"shift_id": shift_id, "outgoing_employee_code": "SW-010", "incoming_employee_code": "SW-011"},
        task_id=task_id,
    )
    payload = {"shift_id": shift_id, "outgoing_employee_code": "SW-010", "incoming_employee_code": "SW-011"}
    return {
        "task_id": task_id, "proposal_id": proposal["id"], "shift_id": shift_id,
        "outgoing_id": outgoing_id, "incoming_id": incoming_id, "payload": payload,
    }


def build_fill_fixture(connection):
    """A genuinely uncovered shift, one eligible worker, a pending fill proposal."""
    incoming_id = add_employee(connection, "SW-020", "Alex Rivera")
    add_ready_schedule(connection, incoming_id)
    shift_id = add_shift(connection, "Vega", "2026-10-06 08:00", "2026-10-06 13:00", required_staff=1)
    task_id = add_task(connection)
    proposal = agent_tools.call_tool(
        connection, "propose_replacement",
        {"shift_id": shift_id, "incoming_employee_code": "SW-020"},
        task_id=task_id,
    )
    payload = {"shift_id": shift_id, "outgoing_employee_code": None, "incoming_employee_code": "SW-020"}
    return {"task_id": task_id, "proposal_id": proposal["id"], "shift_id": shift_id, "incoming_id": incoming_id, "payload": payload}


# ----------------------------------------------------------------- check 1

connection = fixture_database()
fixture = build_replacement_fixture(connection)
result = agent_service.approve_agent_proposal(connection, fixture["proposal_id"], fixture["payload"])
check(result["proposal"]["status"] == "approved", "successful replacement approval: proposal status becomes approved")
check(result["task_status"] == "closed", "successful replacement approval: task closes")
check(
    {r["employee_id"] for r in connection.execute("SELECT employee_id FROM assignments WHERE shift_id = ?", (fixture["shift_id"],))}
    == {fixture["incoming_id"]},
    "successful replacement approval: the outgoing worker no longer holds the shift, the incoming worker does",
)
check(result["proposal"]["execution_outcome"] == "applied", "execution_outcome is 'applied'")
check(result["proposal"]["verification_outcome"] == "verified", "verification_outcome is 'verified'")
check(result["readback"]["assigned_employees"][0]["employee_code"] == "SW-011", "the response includes a current readback")

# ----------------------------------------------------------------- check 2

connection = fixture_database()
fixture = build_fill_fixture(connection)
result = agent_service.approve_agent_proposal(connection, fixture["proposal_id"], fixture["payload"])
check(result["proposal"]["status"] == "approved", "successful uncovered-shift fill: proposal approved")
check(assignment_count(connection) == 1, "successful uncovered-shift fill: exactly one assignment exists")
check(
    connection.execute(
        "SELECT employee_id_before FROM assignment_audit WHERE action = 'assignment_created'"
    ).fetchone()["employee_id_before"] is None,
    "the fill's audit row has no outgoing (before) worker",
)

# ----------------------------------------------------------------- check 3

connection = fixture_database()
fixture = build_replacement_fixture(connection)
before = assignment_count(connection)
result = agent_service.reject_agent_proposal(connection, fixture["proposal_id"])
check(result["proposal"]["status"] == "rejected", "explicit rejection: proposal status becomes rejected")
check(result["task_status"] == "closed", "explicit rejection: task closes")
check(assignment_count(connection) == before, "explicit rejection: zero assignment changes")

# ----------------------------------------------------------------- check 4

connection = fixture_database()
fixture = build_replacement_fixture(connection)
bad_payload = dict(fixture["payload"], incoming_employee_code="SW-999")
try:
    agent_service.approve_agent_proposal(connection, fixture["proposal_id"], bad_payload)
    check(False, "approval-content mismatch is refused")
except agent_service.AgentProposalContentMismatch:
    check(True, "approval-content mismatch is refused")
check(assignment_count(connection) == 1, "a content-mismatch approval attempt writes nothing (outgoing still holds it)")
check(
    connection.execute("SELECT status FROM agent_proposals WHERE id = ?", (fixture["proposal_id"],)).fetchone()["status"] == "pending",
    "the proposal itself stays pending after a content-mismatch attempt",
)

# ----------------------------------------------------------------- check 5

connection = fixture_database()
fixture = build_replacement_fixture(connection)
# The outgoing worker's assignment was replaced by something else entirely
# (simulating a manual Phase 7 replacement that happened after the agent
# proposed this one) - the outgoing worker no longer holds the shift.
connection.execute("DELETE FROM assignments WHERE employee_id = ? AND shift_id = ?", (fixture["outgoing_id"], fixture["shift_id"]))
connection.commit()
try:
    agent_service.approve_agent_proposal(connection, fixture["proposal_id"], fixture["payload"])
    check(False, "a stale outgoing assignment is a controlled revalidation conflict")
except agent_service.AgentProposalRevalidationFailed as error:
    check(
        error.conflicts[0]["reason_codes"] == ["stale_state"],
        f"a stale outgoing assignment is a controlled revalidation conflict ({error.conflicts})",
    )
check(assignment_count(connection) == 0, "a stale-outgoing-assignment refusal writes nothing")

# ----------------------------------------------------------- checks 6-11

def expect_conflict(build_and_break, expected_reason_code, description):
    connection = fixture_database()
    fixture = build_replacement_fixture(connection)
    build_and_break(connection, fixture)
    before_assignments = assignment_count(connection)
    before_audit = audit_count(connection)
    try:
        agent_service.approve_agent_proposal(connection, fixture["proposal_id"], fixture["payload"])
        check(False, description)
    except agent_service.AgentProposalRevalidationFailed as error:
        codes = [code for c in error.conflicts for code in c["reason_codes"]]
        check(expected_reason_code in codes, f"{description} ({codes})")
    check(assignment_count(connection) == before_assignments, f"{description}: writes nothing to assignments")
    check(audit_count(connection) == before_audit, f"{description}: writes nothing to assignment_audit")
    check(
        connection.execute("SELECT status FROM agent_proposals WHERE id = ?", (fixture["proposal_id"],)).fetchone()["status"] == "pending",
        f"{description}: the proposal itself stays pending",
    )


expect_conflict(
    lambda c, f: (c.execute("UPDATE employees SET is_active = 0 WHERE id = ?", (f["incoming_id"],)), c.commit()),
    "worker_inactive",
    "incoming worker becomes inactive before approval",
)
expect_conflict(
    lambda c, f: (c.execute("UPDATE semester_schedules SET confirmed_at = NULL WHERE employee_id = ?", (f["incoming_id"],)), c.commit()),
    "timetable_not_confirmed",
    "incoming worker's timetable becomes unconfirmed before approval",
)
expect_conflict(
    lambda c, f: (c.execute("UPDATE semester_schedules SET dates_provisional = 1 WHERE employee_id = ?", (f["incoming_id"],)), c.commit()),
    "timetable_not_confirmed",
    "incoming worker's timetable becomes provisional before approval",
)


def _add_class_conflict(c, f):
    schedule_id = c.execute("SELECT id FROM semester_schedules WHERE employee_id = ?", (f["incoming_id"],)).fetchone()["id"]
    # The shift is Tuesday 22:00 -> Wednesday 03:00 (day_of_week 1 = Tuesday,
    # D025's Monday=0 convention) - a class overlapping its start.
    c.execute(
        "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time) VALUES (?, 1, '21:30', '23:00')",
        (schedule_id,),
    )
    c.commit()


expect_conflict(_add_class_conflict, "class_conflict", "a new class conflict appears before approval")


def _add_leave_conflict(c, f):
    c.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime) VALUES (?, '2026-10-06 20:00', '2026-10-07 04:00')",
        (f["incoming_id"],),
    )
    c.commit()


expect_conflict(_add_leave_conflict, "leave_conflict", "a new approved-leave conflict appears before approval")


def _add_assignment_conflict(c, f):
    other_shift = add_shift(c, "Helix", "2026-10-06 21:00", "2026-10-07 02:00")
    add_assignment(c, f["incoming_id"], other_shift)


expect_conflict(_add_assignment_conflict, "assignment_conflict", "a new overlapping assignment appears before approval")


def _add_hour_limit_conflict(c, f):
    c.execute("UPDATE employees SET weekly_hour_limit = 4 WHERE id = ?", (f["incoming_id"],))
    c.commit()


expect_conflict(_add_hour_limit_conflict, "weekly_hour_limit_exceeded", "a weekly-hour-limit conflict appears before approval")

# ---------------------------------------------------------------- check 12

connection = fixture_database()
fixture = build_fill_fixture(connection)
someone_else = add_employee(connection, "SW-021", "Someone Else")
add_assignment(connection, someone_else, fixture["shift_id"])
try:
    agent_service.approve_agent_proposal(connection, fixture["proposal_id"], fixture["payload"])
    check(False, "an uncovered position consumed before approval is a controlled conflict")
except agent_service.AgentProposalRevalidationFailed as error:
    codes = [code for c in error.conflicts for code in c["reason_codes"]]
    check("shift_already_covered" in codes, f"an uncovered position consumed before approval is a controlled conflict ({codes})")
check(assignment_count(connection) == 1, "the consumed-position refusal writes no additional assignment")

# ---------------------------------------------------------------- check 13

connection = fixture_database()
fixture = build_replacement_fixture(connection)
agent_service.approve_agent_proposal(connection, fixture["proposal_id"], fixture["payload"])
try:
    agent_service.reject_agent_proposal(connection, fixture["proposal_id"])
    check(False, "an approved proposal cannot later be rejected")
except agent_service.AgentProposalNotPending:
    check(True, "an approved proposal cannot later be rejected")

connection = fixture_database()
fixture = build_replacement_fixture(connection)
agent_service.reject_agent_proposal(connection, fixture["proposal_id"])
try:
    agent_service.approve_agent_proposal(connection, fixture["proposal_id"], fixture["payload"])
    check(False, "a rejected proposal cannot later be approved")
except agent_service.AgentProposalNotPending:
    check(True, "a rejected proposal cannot later be approved")
check(assignment_count(connection) == 1, "the refused approve-after-reject attempt wrote nothing (outgoing still holds it)")

# ---------------------------------------------------------------- check 14

connection = fixture_database()
fixture = build_replacement_fixture(connection)
first = agent_service.approve_agent_proposal(connection, fixture["proposal_id"], fixture["payload"])
before_assignments = assignment_count(connection)
before_audit = audit_count(connection)
second = agent_service.approve_agent_proposal(connection, fixture["proposal_id"], fixture["payload"])
check(second["proposal"]["status"] == "approved", "repeated approval is an idempotent success")
check(assignment_count(connection) == before_assignments, "repeated approval creates no second assignment")
check(audit_count(connection) == before_audit, "repeated approval writes no duplicate audit row")
check(second["proposal"]["decided_at"] == first["proposal"]["decided_at"], "the decision timestamp is not overwritten by the retry")

# ---------------------------------------------------------------- check 15

connection = fixture_database()
fixture = build_replacement_fixture(connection)
agent_service.reject_agent_proposal(connection, fixture["proposal_id"])
before_audit = audit_count(connection)
result = agent_service.reject_agent_proposal(connection, fixture["proposal_id"])
check(result["proposal"]["status"] == "rejected", "repeated rejection is idempotent")
check(audit_count(connection) == before_audit, "repeated rejection writes no duplicate audit row")

# ---------------------------------------------------------------- check 16
#
# A real two-connection concurrency test - `:memory:` databases are each a
# separate, unshared database per connection, so this needs a real temp
# FILE database.

with tempfile.TemporaryDirectory() as folder:
    concurrent_path = Path(folder) / "concurrent-approval.db"
    setup_connection = get_connection(concurrent_path)
    create_schema(setup_connection)
    fixture = build_replacement_fixture(setup_connection)
    setup_connection.close()

    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def approve_attempt():
        own_connection = get_connection(concurrent_path)
        try:
            barrier.wait()
            result = agent_service.approve_agent_proposal(own_connection, fixture["proposal_id"], fixture["payload"])
            with lock:
                outcomes.append(("ok", result["proposal"]["status"]))
        except Exception as error:  # noqa: BLE001 - recorded, not swallowed
            with lock:
                outcomes.append(("error", f"{type(error).__name__}: {error}"))
        finally:
            own_connection.close()

    threads = [threading.Thread(target=approve_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    check(len(outcomes) == 2, f"both concurrent approval attempts finished (no hang, no uncaught exception) ({outcomes})")
    check(all(kind == "ok" for kind, _ in outcomes), f"neither concurrent attempt raised a raw uniqueness/locking error ({outcomes})")
    check(all(status == "approved" for _, status in outcomes), f"both concurrent attempts see the proposal as approved ({outcomes})")

    verify_connection = get_connection(concurrent_path)
    check(
        verify_connection.execute("SELECT COUNT(*) AS n FROM assignments WHERE shift_id = ?", (fixture["shift_id"],)).fetchone()["n"] == 1,
        "exactly one assignment change resulted from both concurrent approval attempts",
    )
    check(
        verify_connection.execute(
            "SELECT COUNT(*) AS n FROM assignment_audit WHERE action = 'assignment_replaced' AND agent_proposal_id = ?",
            (fixture["proposal_id"],),
        ).fetchone()["n"] == 1,
        "exactly one 'assignment_replaced' audit row exists, not two",
    )
    verify_connection.close()

# ---------------------------------------------------------------- check 17

with tempfile.TemporaryDirectory() as folder:
    race_path = Path(folder) / "approve-reject-race.db"
    setup_connection = get_connection(race_path)
    create_schema(setup_connection)
    fixture = build_replacement_fixture(setup_connection)
    setup_connection.close()

    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def approve_racer():
        own_connection = get_connection(race_path)
        try:
            barrier.wait()
            result = agent_service.approve_agent_proposal(own_connection, fixture["proposal_id"], fixture["payload"])
            with lock:
                outcomes.append(("approve", "ok", result["proposal"]["status"]))
        except agent_service.AgentProposalNotPending:
            with lock:
                outcomes.append(("approve", "refused", None))
        finally:
            own_connection.close()

    def reject_racer():
        own_connection = get_connection(race_path)
        try:
            barrier.wait()
            result = agent_service.reject_agent_proposal(own_connection, fixture["proposal_id"])
            with lock:
                outcomes.append(("reject", "ok", result["proposal"]["status"]))
        except agent_service.AgentProposalNotPending:
            with lock:
                outcomes.append(("reject", "refused", None))
        finally:
            own_connection.close()

    threads = [threading.Thread(target=approve_racer), threading.Thread(target=reject_racer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    check(len(outcomes) == 2, f"both racing decisions finished (no hang, no uncaught exception) ({outcomes})")
    successes = [o for o in outcomes if o[1] == "ok"]
    check(len(successes) == 1, f"exactly one of approve/reject actually took effect ({outcomes})")

    verify_connection = get_connection(race_path)
    final_status = verify_connection.execute(
        "SELECT status FROM agent_proposals WHERE id = ?", (fixture["proposal_id"],)
    ).fetchone()["status"]
    check(final_status in ("approved", "rejected"), f"the proposal reached exactly one final decision ({final_status})")
    check(
        (final_status == "approved") == (successes[0][0] == "approve"),
        "the final stored status matches whichever decision actually won the race",
    )
    verify_connection.close()

# ---------------------------------------------------------------- check 18

connection = fixture_database()
fixture = build_replacement_fixture(connection)
agent_service.approve_agent_proposal(connection, fixture["proposal_id"], fixture["payload"])
audit_row = connection.execute(
    "SELECT shift_id, employee_id_before, employee_id_after, agent_proposal_id"
    " FROM assignment_audit WHERE action = 'assignment_replaced'"
).fetchone()
check(audit_row["shift_id"] == fixture["shift_id"], "the audit row names the exact shift")
check(audit_row["employee_id_before"] == fixture["outgoing_id"], "the audit row names the exact outgoing (before) employee")
check(audit_row["employee_id_after"] == fixture["incoming_id"], "the audit row names the exact incoming (after) employee")
check(audit_row["agent_proposal_id"] == fixture["proposal_id"], "the audit row links back to the exact agent proposal")
decision_row = connection.execute(
    "SELECT agent_proposal_id FROM assignment_audit WHERE action = 'proposal_approved'"
).fetchone()
check(decision_row["agent_proposal_id"] == fixture["proposal_id"], "the decision audit row also links to the exact agent proposal")

# ---------------------------------------------------------------- check 19

connection = fixture_database()
fixture = build_replacement_fixture(connection)
result = agent_service.approve_agent_proposal(connection, fixture["proposal_id"], fixture["payload"])
check(result["proposal"]["verification_outcome"] == "verified", "successful post-write verification is recorded")
check(result["readback"] is not None, "a successful verification includes a readback")

# ---------------------------------------------------------------- check 20


def failing_verify(connection, shift_id, outgoing_employee_code, incoming_employee_code):
    raise agent_service.AgentVerificationFailed("simulated readback failure for testing")


connection = fixture_database()
fixture = build_replacement_fixture(connection)
result = agent_service.approve_agent_proposal(
    connection, fixture["proposal_id"], fixture["payload"], verify_fn=failing_verify
)
check(result["proposal"]["execution_outcome"] == "applied", "a forced verification failure still reports the write as applied")
check(
    result["proposal"]["verification_outcome"] is not None and "verification_failed" in result["proposal"]["verification_outcome"],
    f"a forced verification failure is recorded separately from execution ({result['proposal']['verification_outcome']})",
)
check(result["task_status"] == "blocked", "a verification failure leaves the task blocked, not closed")
check(
    {r["employee_id"] for r in connection.execute("SELECT employee_id FROM assignments WHERE shift_id = ?", (fixture["shift_id"],))} == {fixture["incoming_id"]},
    "the underlying assignment write is NOT rolled back or retried after a forced verification failure",
)
check(
    connection.execute("SELECT COUNT(*) AS n FROM assignment_audit WHERE action = 'assignment_replaced'").fetchone()["n"] == 1,
    "no blind retry occurred - still exactly one mutation audit row",
)

# ---------------------------------------------------------------- check 21

check(
    "approve_agent_proposal" not in agent_tools._ARG_SCHEMAS and "reject_agent_proposal" not in agent_tools._ARG_SCHEMAS,
    "there is no tool that can approve or reject a proposal",
)
try:
    agent_tools.call_tool(connection, "approve_agent_proposal", {"proposal_id": fixture["proposal_id"]})
    check(False, "a model tool call attempting to approve is rejected")
except agent_tools.ToolError as error:
    check(error.code == "unknown_tool", f"a model tool call attempting to approve is rejected ({error.code})")


# ---------------------------------------------------------------- check 22


def _apply_without_verifying(connection, fixture):
    """Simulate a process/second-write failure between the execution commit
    and the verification write: the assignment mutation and the
    'approved'/'applied' proposal state are committed exactly as
    `approve_agent_proposal`'s own transaction would leave them, but
    `verification_outcome` is left NULL, as a genuinely committed prior
    call's crash would leave it."""
    proposals_module._replace_assignment_locked(
        connection, fixture["shift_id"], "SW-010", "SW-011", agent_proposal_id=fixture["proposal_id"],
    )
    connection.execute(
        "UPDATE agent_proposals SET status = 'approved', decided_at = '2026-01-01 00:00',"
        " executed_at = '2026-01-01 00:00', execution_outcome = 'applied' WHERE id = ?",
        (fixture["proposal_id"],),
    )
    connection.execute(
        "INSERT INTO assignment_audit (occurred_at, action, agent_proposal_id, detail)"
        " VALUES ('2026-01-01 00:00', 'proposal_approved', ?, 'simulated partial commit for test')",
        (fixture["proposal_id"],),
    )


connection = fixture_database()
fixture = build_replacement_fixture(connection)
agent_service._in_transaction(connection, lambda: _apply_without_verifying(connection, fixture))

check(
    connection.execute(
        "SELECT verification_outcome FROM agent_proposals WHERE id = ?", (fixture["proposal_id"],)
    ).fetchone()["verification_outcome"] is None,
    "applied-but-unverified fixture: verification_outcome starts NULL",
)
before_assignments = assignment_count(connection)
before_audit = audit_count(connection)

result = agent_service.approve_agent_proposal(connection, fixture["proposal_id"], fixture["payload"])

check(assignment_count(connection) == before_assignments, "reconciling an applied-but-unverified proposal repeats no assignment mutation")
check(audit_count(connection) == before_audit, "reconciling an applied-but-unverified proposal writes no duplicate execution/approval audit row")
check(result["proposal"]["verification_outcome"] == "verified", "reconciling an applied-but-unverified proposal runs verification and records success")
check(result["task_status"] == "closed", "reconciling to a verified outcome closes the task")
check(result["readback"] is not None, "the reconciled response includes a readback")

# ---------------------------------------------------------------- check 23

connection = fixture_database()
fixture = build_replacement_fixture(connection)
agent_service._in_transaction(connection, lambda: _apply_without_verifying(connection, fixture))
before_assignments = assignment_count(connection)
before_audit = audit_count(connection)

result = agent_service.approve_agent_proposal(
    connection, fixture["proposal_id"], fixture["payload"], verify_fn=failing_verify
)
check(
    result["proposal"]["verification_outcome"] is not None
    and "verification_failed" in result["proposal"]["verification_outcome"],
    "reconciling an applied-but-unverified proposal can also record a verification failure",
)
check(result["task_status"] == "blocked", "a reconciled verification failure leaves the task blocked")
recorded = result["proposal"]["verification_outcome"]

# Ordinary approval retry, no forced-failure verify_fn this time: a
# recorded verification_failed outcome must NOT be automatically rerun.
result2 = agent_service.approve_agent_proposal(connection, fixture["proposal_id"], fixture["payload"])
check(
    result2["proposal"]["verification_outcome"] == recorded,
    "a recorded verification_failed outcome is not automatically rerun on an ordinary approval retry",
)
check(assignment_count(connection) == before_assignments, "retrying after a recorded verification_failed repeats no assignment mutation")
check(audit_count(connection) == before_audit, "retrying after a recorded verification_failed writes no duplicate audit row")


if failures:
    print(f"\n{len(failures)} check(s) failed:")
    for description in failures:
        print(f"  - {description}")
    sys.exit(1)

print("\nAll agent decision checks passed (throwaway in-memory/temp-file databases only, no real API key or network request).")
