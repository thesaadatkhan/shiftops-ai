"""Regression checks for Phase 7 increment 3: proposal creation, approval,
rejection and explicit replacement (`proposals.py`).

Runs entirely against throwaway in-memory or temporary-file databases with
purpose-built workers, shifts, schedules, leave and assignments. The
project's own `backend/shiftops.db` is never opened, read or modified.

Checks:

 1. Proposal content is bound at creation and can be retrieved unchanged.
 2. Creating a proposal alone writes no assignment.
 3. Successful approval persists exactly the proposed assignments and
    records audit history, atomically.
 4. Repeated approval is idempotent - no duplicate assignments, no error.
 5. A stale employee/timetable/leave change since the proposal was created
    causes full refusal with no partial writes, even when only one of
    several proposed assignments in the same proposal is affected.
 6. Overlapping and duplicated proposed assignments (inserted directly,
    bypassing generation) are both refused at approval time.
 7. Pre-existing assignments are preserved through proposal creation and
    approval - never re-proposed, never touched.
 8. Explicit replacement succeeds atomically and records before/after
    values; a failed replacement leaves the outgoing assignment untouched.
 9. Unknown/invalid proposal, shift or employee references, and a submitted
    approval payload that does not match the stored proposal, each return
    the correct, distinct error.
10. Concurrent approval of the same proposal cannot duplicate or partially
    apply it.

Run with:  python verify_proposals.py
Exits non-zero if any check fails.
"""

import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

from database import create_schema, get_connection
from proposals import (
    AssignmentNotFound,
    ProposalContentMismatch,
    ProposalNotFound,
    ProposalNotPending,
    ProposalRevalidationFailed,
    ProposalSnapshotCorrupted,
    ProposalValidationError,
    ReplacementInvalid,
    approve_proposal,
    create_proposal,
    get_proposal,
    list_proposals_for_week,
    reject_proposal,
    replace_assignment,
)
from scheduling import prepare_week

DATETIME_FORMAT = "%Y-%m-%d %H:%M"
WEEK = "2026-11-02"

failures = []
_total = {"n": 0}


def check(condition, description):
    _total["n"] += 1
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fixture_database():
    connection = get_connection(":memory:")
    create_schema(connection)
    return connection


def add_employee(connection, code, limit=20, active=True, student_type="undergraduate"):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active) VALUES (?, ?, ?, ?, ?)",
        (code, f"Fixture {code}", student_type, limit, 1 if active else 0),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]


def add_schedule(connection, employee_id, start_date=WEEK, end_date="2026-11-08", confirmed=True, provisional=False):
    confirmed_at = "2026-01-01 00:00" if confirmed else None
    connection.execute(
        "INSERT INTO semester_schedules"
        " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
        " VALUES (?, ?, ?, ?, ?)",
        (employee_id, start_date, end_date, confirmed_at, 1 if provisional else 0),
    )
    connection.commit()


def confirmed_worker(connection, code, limit=20):
    employee_id = add_employee(connection, code, limit=limit)
    add_schedule(connection, employee_id)
    return employee_id


def add_leave(connection, employee_id, start, end):
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime)"
        " VALUES (?, ?, ?)",
        (employee_id, start.strftime(DATETIME_FORMAT), end.strftime(DATETIME_FORMAT)),
    )
    connection.commit()


def add_assignment(connection, employee_id, shift_id):
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (employee_id, shift_id),
    )
    connection.commit()


def shift_id_for(connection, start_datetime, hall="Andromeda"):
    return connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ?",
        (hall, start_datetime),
    ).fetchone()["id"]


def assignment_pairs(connection):
    return {
        (row["employee_id"], row["shift_id"])
        for row in connection.execute("SELECT employee_id, shift_id FROM assignments")
    }


def approval_payload(proposal):
    return {
        "assignments": [
            {"shift_id": row["shift_id"], "employee_code": row["employee_code"]}
            for row in proposal["assignments"]
        ]
    }


def check_proposal_content_bound_and_retrievable():
    connection = fixture_database()
    confirmed_worker(connection, "SW-001")
    prepare_week(connection, WEEK)

    created = create_proposal(connection, WEEK)
    fetched = get_proposal(connection, created["id"])
    check(created == fetched, "the freshly created proposal and a separate read of it are identical")
    check(created["status"] == "pending", f"a new proposal starts pending ({created['status']})")
    check(len(created["assignments"]) > 0, "the fixture actually proposed at least one assignment")
    connection.close()


def check_draft_snapshot_persisted_and_recoverable():
    """Phase 7 increment 4: the immutable review snapshot is stored at
    creation, survives a fresh read, matches the coverage data the draft
    actually computed, and stays attached through approval/rejection."""
    connection = fixture_database()
    confirmed_worker(connection, "SW-001")
    prepare_week(connection, WEEK)

    created = create_proposal(connection, WEEK)
    check(created["review"] is not None, "a freshly created proposal carries a review snapshot")
    check(
        isinstance(created["review"].get("shifts"), list)
        and isinstance(created["review"].get("summary"), dict),
        "the review snapshot has the expected shifts/summary shape",
    )
    check(
        created["review"]["summary"]["proposed_filled_positions"] == len(created["assignments"]),
        "the snapshot's proposed-position total matches the number of stored proposal_assignments rows",
    )

    fetched = get_proposal(connection, created["id"])
    check(fetched["review"] == created["review"], "re-reading the proposal returns the identical review snapshot")

    approved = approve_proposal(connection, created["id"], approval_payload(created))
    check(approved["review"] == created["review"], "approval returns the same review snapshot, unchanged")
    connection.close()


def check_legacy_proposal_without_snapshot_still_readable():
    """A proposal row created before `draft_snapshot` existed (or any row
    with a NULL snapshot) must still be fully readable, with `review: None`
    rather than an error - additive migrations must not break old rows."""
    connection = fixture_database()
    confirmed_worker(connection, "SW-001")
    prepare_week(connection, WEEK)
    shift = shift_id_for(connection, "2026-11-02 17:00")
    employee_id = connection.execute(
        "SELECT id FROM employees WHERE employee_code = 'SW-001'"
    ).fetchone()["id"]

    connection.execute(
        "INSERT INTO schedule_proposals (week_start, created_at, status, draft_snapshot)"
        " VALUES (?, '2026-01-01 00:00', 'pending', NULL)",
        (WEEK,),
    )
    proposal_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    connection.execute(
        "INSERT INTO proposal_assignments (proposal_id, shift_id, employee_id) VALUES (?, ?, ?)",
        (proposal_id, shift, employee_id),
    )
    connection.commit()

    fetched = get_proposal(connection, proposal_id)
    check(fetched["review"] is None, "a legacy proposal with no stored snapshot reads back with review=None")
    check(len(fetched["assignments"]) == 1, "its normalized proposal_assignments content still reads correctly")
    connection.close()


def check_malformed_snapshot_fails_clearly():
    """A corrupted `draft_snapshot` value must raise the dedicated, controlled
    error rather than a raw JSON/attribute error or a fabricated review."""
    connection = fixture_database()
    confirmed_worker(connection, "SW-001")
    prepare_week(connection, WEEK)
    created = create_proposal(connection, WEEK)

    connection.execute(
        "UPDATE schedule_proposals SET draft_snapshot = 'not json at all' WHERE id = ?",
        (created["id"],),
    )
    connection.commit()
    try:
        get_proposal(connection, created["id"])
        check(False, "a non-JSON snapshot raises ProposalSnapshotCorrupted")
    except ProposalSnapshotCorrupted:
        check(True, "a non-JSON snapshot raises ProposalSnapshotCorrupted")

    connection.execute(
        "UPDATE schedule_proposals SET draft_snapshot = '{\"nope\": true}' WHERE id = ?",
        (created["id"],),
    )
    connection.commit()
    try:
        get_proposal(connection, created["id"])
        check(False, "a JSON snapshot missing shifts/summary raises ProposalSnapshotCorrupted")
    except ProposalSnapshotCorrupted:
        check(True, "a JSON snapshot missing shifts/summary raises ProposalSnapshotCorrupted")
    connection.close()


def check_list_proposals_for_week_ordered_and_filtered():
    """`list_proposals_for_week` returns only that week's proposals, newest
    first, and lets a caller recover a pending proposal after losing its
    in-memory reference - the refresh/remount recovery path."""
    connection = fixture_database()
    confirmed_worker(connection, "SW-001")
    other_week = "2026-11-09"
    prepare_week(connection, WEEK)
    prepare_week(connection, other_week)

    first = create_proposal(connection, WEEK)
    second = create_proposal(connection, WEEK)
    other_week_proposal = create_proposal(connection, other_week)

    listed = list_proposals_for_week(connection, WEEK)
    check(
        [p["id"] for p in listed] == [second["id"], first["id"]],
        "proposals for the week come back newest-first, deterministically ordered",
    )
    check(
        other_week_proposal["id"] not in [p["id"] for p in listed],
        "a proposal belonging to a different week is not included",
    )

    reject_proposal(connection, first["id"])
    recovered = list_proposals_for_week(connection, WEEK)
    check(
        {p["id"]: p["status"] for p in recovered} == {second["id"]: "pending", first["id"]: "rejected"},
        "recovering the week's proposals after a decision shows both, with current status - simulating refresh recovery",
    )
    connection.close()


def check_create_writes_no_assignments():
    connection = fixture_database()
    confirmed_worker(connection, "SW-001")
    prepare_week(connection, WEEK)

    before = assignment_pairs(connection)
    create_proposal(connection, WEEK)
    after = assignment_pairs(connection)
    check(before == after == set(), "creating a proposal writes no row to assignments")
    connection.close()


def check_successful_approval_persists_and_audits():
    connection = fixture_database()
    confirmed_worker(connection, "SW-001")
    confirmed_worker(connection, "SW-002")
    prepare_week(connection, WEEK)

    proposal = create_proposal(connection, WEEK)
    expected_pairs = {(a["employee_id"], a["shift_id"]) for a in proposal["assignments"]}

    approved = approve_proposal(connection, proposal["id"], approval_payload(proposal))
    check(approved["status"] == "approved", f"approval reports the new status ({approved['status']})")

    stored_pairs = assignment_pairs(connection)
    check(stored_pairs == expected_pairs, "exactly the proposed assignments are persisted, nothing more or less")

    audit_actions = [row["action"] for row in connection.execute("SELECT action FROM assignment_audit ORDER BY id")]
    check(audit_actions[0] == "proposal_created", f"the first audit row records creation ({audit_actions[0]})")
    check(
        audit_actions.count("assignment_created") == len(expected_pairs),
        f"one assignment_created audit row per persisted assignment ({audit_actions.count('assignment_created')} vs {len(expected_pairs)})",
    )
    check(audit_actions[-1] == "proposal_approved", f"the last audit row records the approval ({audit_actions[-1]})")
    connection.close()


def check_repeated_approval_idempotent():
    connection = fixture_database()
    confirmed_worker(connection, "SW-001")
    prepare_week(connection, WEEK)

    proposal = create_proposal(connection, WEEK)
    payload = approval_payload(proposal)

    first = approve_proposal(connection, proposal["id"], payload)
    after_first = assignment_pairs(connection)
    second = approve_proposal(connection, proposal["id"], payload)
    after_second = assignment_pairs(connection)

    check(first["status"] == second["status"] == "approved", "both calls report approved")
    check(after_first == after_second, "repeating approval creates no duplicate or additional assignment")
    connection.close()


def check_stale_change_refuses_whole_approval():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    w2 = confirmed_worker(connection, "SW-002")
    prepare_week(connection, WEEK)

    proposal = create_proposal(connection, WEEK)
    check(len(proposal["assignments"]) >= 2, "fixture sanity: at least two proposed assignments to make one of them go stale")

    # Deactivate one of the two proposed workers AFTER the proposal was
    # created but BEFORE approval - this worker's own proposed assignment
    # must now fail revalidation, and the whole approval must be refused,
    # not just that one row.
    stale_employee_code = proposal["assignments"][0]["employee_code"]
    connection.execute("UPDATE employees SET is_active = 0 WHERE employee_code = ?", (stale_employee_code,))
    connection.commit()

    before = assignment_pairs(connection)
    try:
        approve_proposal(connection, proposal["id"], approval_payload(proposal))
        check(False, "approval should have been refused after a proposed worker went inactive")
    except ProposalRevalidationFailed as error:
        check(True, "approval is refused with ProposalRevalidationFailed")
        check(
            any(c.get("employee_code") == stale_employee_code and "worker_inactive" in c.get("reason_codes", []) for c in error.conflicts),
            f"the conflict list names the specific stale worker and reason ({error.conflicts})",
        )
    after = assignment_pairs(connection)
    check(before == after == set(), "NO assignment was written - not even the still-valid ones from the same proposal")

    proposal_status = connection.execute(
        "SELECT status FROM schedule_proposals WHERE id = ?", (proposal["id"],)
    ).fetchone()["status"]
    check(proposal_status == "pending", "the proposal itself remains pending, not silently marked approved or rejected")
    connection.close()


def check_stale_leave_added_refuses_approval():
    """Approved leave added AFTER proposal creation, overlapping one
    proposed shift, refuses the whole approval - not just that one row."""
    connection = fixture_database()
    confirmed_worker(connection, "SW-001")
    confirmed_worker(connection, "SW-002")
    prepare_week(connection, WEEK)

    proposal = create_proposal(connection, WEEK)
    check(len(proposal["assignments"]) >= 2, "fixture sanity: at least two proposed assignments")
    stale_row = proposal["assignments"][0]
    stale_start = datetime.strptime(stale_row["start_datetime"], DATETIME_FORMAT)
    add_leave(connection, stale_row["employee_id"], stale_start - timedelta(hours=1), stale_start + timedelta(hours=1))

    try:
        approve_proposal(connection, proposal["id"], approval_payload(proposal))
        check(False, "approval should have been refused after leave was added over a proposed shift")
    except ProposalRevalidationFailed as error:
        check(
            any(c.get("employee_id") == stale_row["employee_id"] and "leave_conflict" in c.get("reason_codes", []) for c in error.conflicts),
            f"the conflict names the leave-conflicted worker and shift ({error.conflicts})",
        )
    check(assignment_pairs(connection) == set(), "no assignment was written after the leave conflict")
    connection.close()


def check_stale_staffing_consumed_refuses_approval():
    """A shift the proposal targets gets manually staffed by someone else
    AFTER proposal creation - approval must refuse the whole thing, since
    the position this proposal wanted to fill no longer exists."""
    connection = fixture_database()
    confirmed_worker(connection, "SW-001")
    other = confirmed_worker(connection, "SW-002")
    prepare_week(connection, WEEK)

    proposal = create_proposal(connection, WEEK)
    worker = connection.execute("SELECT id FROM employees WHERE employee_code = 'SW-001'").fetchone()["id"]
    own_rows = [a for a in proposal["assignments"] if a["employee_id"] == worker]
    check(own_rows != [], "fixture sanity: SW-001 was proposed for at least one shift")
    stale_row = own_rows[0]

    # Someone else (SW-002, not proposed for this shift by the draft at all)
    # is manually assigned to the exact shift this proposal wanted SW-001 to
    # fill - a required_staff=1 shift can no longer take it.
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (other, stale_row["shift_id"]),
    )
    connection.commit()

    try:
        approve_proposal(connection, proposal["id"], approval_payload(proposal))
        check(False, "approval should have been refused after the shift was staffed elsewhere")
    except ProposalRevalidationFailed as error:
        check(
            any(c.get("shift_id") == stale_row["shift_id"] and "staffing_no_longer_available" in c.get("reason_codes", []) for c in error.conflicts),
            f"the conflict names the now-overstaffed shift ({error.conflicts})",
        )
    check(
        assignment_pairs(connection) == {(other, stale_row["shift_id"])},
        "only the manually-made assignment exists - the proposal contributed nothing",
    )
    connection.close()


def check_stale_weekly_hours_refuses_approval():
    """A worker picks up enough OTHER hours after proposal creation that
    approving their proposed shift(s) would now exceed their weekly limit."""
    connection = fixture_database()
    worker = confirmed_worker(connection, "SW-001", limit=20)
    prepare_week(connection, WEEK)

    proposal = create_proposal(connection, WEEK)
    own_rows = [a for a in proposal["assignments"] if a["employee_id"] == worker]
    check(own_rows != [], "fixture sanity: SW-001 was proposed for at least one shift")
    proposed_hours = sum(
        (
            datetime.strptime(a["end_datetime"], DATETIME_FORMAT)
            - datetime.strptime(a["start_datetime"], DATETIME_FORMAT)
        ).total_seconds()
        // 3600
        for a in own_rows
    )

    # The worker's weekly limit is lowered (simulating their cumulative
    # weekly hours becoming invalid by the time of approval) to strictly
    # less than what their own already-proposed shifts alone would total -
    # deterministic regardless of exactly which shifts the optimizer picked.
    connection.execute(
        "UPDATE employees SET weekly_hour_limit = ? WHERE id = ?",
        (int(proposed_hours) - 1, worker),
    )
    connection.commit()

    try:
        approve_proposal(connection, proposal["id"], approval_payload(proposal))
        check(False, "approval should have been refused once the worker's weekly hours would be exceeded")
    except ProposalRevalidationFailed as error:
        check(
            any(c.get("employee_id") == worker and "weekly_hour_limit_exceeded" in c.get("reason_codes", []) for c in error.conflicts),
            f"the conflict names the worker and the weekly-hour-limit reason ({error.conflicts})",
        )
    check(assignment_pairs(connection) == set(), "none of the proposal's shifts were written once the limit was exceeded")
    connection.close()


def check_overlapping_and_duplicate_proposed_assignments_refused():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    prepare_week(connection, WEEK)

    # Two overlapping shifts, both real, both open - inserted directly into
    # proposal_assignments (bypassing generation, which would never itself
    # produce this) to prove approve_proposal's OWN revalidation catches it.
    shift_a = shift_id_for(connection, "2026-11-02 17:00")
    later_shifts = connection.execute(
        "SELECT id, start_datetime FROM shifts WHERE hall = 'Capella' AND start_datetime >= '2026-11-02 17:00'"
        " AND start_datetime < '2026-11-02 20:00' ORDER BY start_datetime"
    ).fetchall()
    check(later_shifts != [], "fixture sanity: a same-evening Capella shift exists to overlap with")
    shift_b = later_shifts[0]["id"]

    connection.execute(
        "INSERT INTO schedule_proposals (week_start, created_at, status) VALUES (?, '2026-01-01 00:00', 'pending')",
        (WEEK,),
    )
    proposal_id = connection.execute("SELECT id FROM schedule_proposals ORDER BY id DESC LIMIT 1").fetchone()["id"]
    connection.execute(
        "INSERT INTO proposal_assignments (proposal_id, shift_id, employee_id) VALUES (?, ?, ?), (?, ?, ?)",
        (proposal_id, shift_a, w1, proposal_id, shift_b, w1),
    )
    connection.commit()

    payload = {
        "assignments": [
            {"shift_id": shift_a, "employee_code": "SW-001"},
            {"shift_id": shift_b, "employee_code": "SW-001"},
        ]
    }
    try:
        approve_proposal(connection, proposal_id, payload)
        check(False, "an overlapping manually-inserted proposal should have been refused")
    except ProposalRevalidationFailed as error:
        check(
            any("proposed_assignment_overlap" in c.get("reason_codes", []) for c in error.conflicts),
            f"the overlap between the two proposed shifts is caught and named ({error.conflicts})",
        )
    check(assignment_pairs(connection) == set(), "no assignment was written for the overlapping proposal")

    # A literal duplicate entry in the SUBMITTED payload is rejected before
    # any DB read even happens (400-shaped, ProposalValidationError).
    duplicate_payload = {
        "assignments": [
            {"shift_id": shift_a, "employee_code": "SW-001"},
            {"shift_id": shift_a, "employee_code": "SW-001"},
        ]
    }
    try:
        approve_proposal(connection, proposal_id, duplicate_payload)
        check(False, "a literal duplicate entry in the submitted payload should have been rejected")
    except ProposalValidationError:
        check(True, "a duplicate entry in the submitted approval payload is rejected as malformed")
    connection.close()


def check_proposal_week_ownership_enforced():
    """A deliberately malformed stored proposal - one of its rows names a
    shift belonging to a DIFFERENT week than the proposal's own week_start -
    is refused whole, atomically, rather than silently mixing another week's
    shift into this one."""
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    prepare_week(connection, WEEK)
    prepare_week(connection, "2026-11-09")  # a second, later week

    in_week_shift = shift_id_for(connection, "2026-11-02 17:00")
    out_of_week_shift = shift_id_for(connection, "2026-11-09 17:00")

    connection.execute(
        "INSERT INTO schedule_proposals (week_start, created_at, status) VALUES (?, '2026-01-01 00:00', 'pending')",
        (WEEK,),
    )
    proposal_id = connection.execute("SELECT id FROM schedule_proposals ORDER BY id DESC LIMIT 1").fetchone()["id"]
    connection.execute(
        "INSERT INTO proposal_assignments (proposal_id, shift_id, employee_id) VALUES (?, ?, ?), (?, ?, ?)",
        (proposal_id, in_week_shift, w1, proposal_id, out_of_week_shift, w1),
    )
    connection.commit()

    payload = {
        "assignments": [
            {"shift_id": in_week_shift, "employee_code": "SW-001"},
            {"shift_id": out_of_week_shift, "employee_code": "SW-001"},
        ]
    }
    try:
        approve_proposal(connection, proposal_id, payload)
        check(False, "a proposal naming a shift outside its own week should have been refused")
    except ProposalRevalidationFailed as error:
        check(
            any(
                c.get("reason_codes") == ["proposal_week_mismatch"] and c.get("shift_id") == out_of_week_shift
                for c in error.conflicts
            ),
            f"the out-of-week shift is named with proposal_week_mismatch ({error.conflicts})",
        )

    check(assignment_pairs(connection) == set(), "no assignment was written - not even the in-week one from the same proposal")
    proposal_status = connection.execute(
        "SELECT status FROM schedule_proposals WHERE id = ?", (proposal_id,)
    ).fetchone()["status"]
    check(proposal_status == "pending", "the malformed proposal remains pending, not silently approved")
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM assignment_audit WHERE action = 'proposal_approved'"
        ).fetchone()["n"]
        == 0,
        "no proposal_approved audit event was written",
    )
    connection.close()


def check_existing_assignments_preserved():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    w2 = confirmed_worker(connection, "SW-002")
    prepare_week(connection, WEEK)

    pre_existing_shift = shift_id_for(connection, "2026-11-02 17:00")
    add_assignment(connection, w1, pre_existing_shift)

    proposal = create_proposal(connection, WEEK)
    check(
        all(a["shift_id"] != pre_existing_shift for a in proposal["assignments"]),
        "the already-filled shift is never re-proposed",
    )

    approved = approve_proposal(connection, proposal["id"], approval_payload(proposal))
    check(
        (w1, pre_existing_shift) in assignment_pairs(connection),
        "the pre-existing assignment survives proposal creation and approval untouched",
    )
    audit_for_existing = connection.execute(
        "SELECT COUNT(*) AS n FROM assignment_audit WHERE shift_id = ? AND employee_id_after = ?",
        (pre_existing_shift, w1),
    ).fetchone()["n"]
    check(audit_for_existing == 0, "the pre-existing assignment is never recorded as a newly created one")
    connection.close()


def check_explicit_replacement_atomic_with_before_after():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    w2 = confirmed_worker(connection, "SW-002")
    prepare_week(connection, WEEK)
    shift = shift_id_for(connection, "2026-11-02 17:00")
    add_assignment(connection, w1, shift)

    result = replace_assignment(connection, shift, "SW-001", "SW-002")
    check(result["shift_id"] == shift and result["outgoing_employee_code"] == "SW-001" and result["incoming_employee_code"] == "SW-002", f"the replacement result names shift/outgoing/incoming correctly ({result})")
    check(assignment_pairs(connection) == {(w2, shift)}, "the assignment now belongs to the incoming worker only")

    audit_row = connection.execute(
        "SELECT employee_id_before, employee_id_after, action FROM assignment_audit"
        " WHERE action = 'assignment_replaced' AND shift_id = ?",
        (shift,),
    ).fetchone()
    check(
        audit_row["employee_id_before"] == w1 and audit_row["employee_id_after"] == w2,
        f"the audit row records the exact before/after employee ids ({dict(audit_row)})",
    )

    # A failed replacement (incoming worker ineligible) leaves the CURRENT
    # assignment (now SW-002's) untouched.
    add_leave(connection, w1, datetime(2026, 11, 2, 16, 0), datetime(2026, 11, 2, 23, 0))
    try:
        replace_assignment(connection, shift, "SW-002", "SW-001")
        check(False, "replacing with an ineligible worker should have been refused")
    except ReplacementInvalid as error:
        check("leave_conflict" in error.detail.get("reason_codes", []), f"the refusal names the actual conflict ({error.detail})")
    check(assignment_pairs(connection) == {(w2, shift)}, "the outgoing (still current) assignment is untouched after a failed replacement")
    connection.close()


def check_replacement_constraint_safety():
    """Two constraint-shaped replacement refusals, both controlled 409s that
    preserve the current assignment and write no audit row - never a raw
    database uniqueness error."""
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    w2 = confirmed_worker(connection, "SW-002")
    prepare_week(connection, WEEK)
    shift = shift_id_for(connection, "2026-11-02 17:00")
    add_assignment(connection, w1, shift)

    before_audit = connection.execute("SELECT COUNT(*) AS n FROM assignment_audit").fetchone()["n"]

    try:
        replace_assignment(connection, shift, "SW-001", "SW-001")
        check(False, "replacing a worker with themselves should have been refused")
    except ReplacementInvalid as error:
        check(
            "replacement_same_worker" in error.detail.get("reason_codes", []),
            f"the same-worker replacement is refused with the correct reason ({error.detail})",
        )
    check(assignment_pairs(connection) == {(w1, shift)}, "the current assignment is untouched after a same-worker refusal")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM assignment_audit").fetchone()["n"] == before_audit,
        "no audit row was written for the same-worker refusal",
    )

    add_assignment(connection, w2, shift)  # SW-002 already holds this shift too (required_staff allows it in this fixture)
    try:
        replace_assignment(connection, shift, "SW-001", "SW-002")
        check(False, "replacing with a worker who already holds the shift should have been refused")
    except ReplacementInvalid as error:
        check(
            "replacement_already_assigned" in error.detail.get("reason_codes", []),
            f"the already-assigned incoming worker is refused with the correct reason ({error.detail})",
        )
    check(assignment_pairs(connection) == {(w1, shift), (w2, shift)}, "both existing assignments are untouched after the already-assigned refusal")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM assignment_audit").fetchone()["n"] == before_audit,
        "no audit row was written for the already-assigned refusal",
    )
    connection.close()


def check_invalid_and_ownership_errors():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    prepare_week(connection, WEEK)
    proposal = create_proposal(connection, WEEK)

    try:
        get_proposal(connection, 999999)
        check(False, "an unknown proposal id should have raised ProposalNotFound")
    except ProposalNotFound:
        check(True, "GET on an unknown proposal id raises ProposalNotFound")

    try:
        approve_proposal(connection, 999999, approval_payload(proposal))
        check(False, "approving an unknown proposal id should have raised ProposalNotFound")
    except ProposalNotFound:
        check(True, "approving an unknown proposal id raises ProposalNotFound")

    try:
        reject_proposal(connection, 999999)
        check(False, "rejecting an unknown proposal id should have raised ProposalNotFound")
    except ProposalNotFound:
        check(True, "rejecting an unknown proposal id raises ProposalNotFound")

    mismatched_payload = {"assignments": []}
    if proposal["assignments"]:
        try:
            approve_proposal(connection, proposal["id"], mismatched_payload)
            check(False, "approving with mismatched content should have been refused")
        except ProposalContentMismatch:
            check(True, "a submitted payload not matching the stored proposal raises ProposalContentMismatch")

    try:
        replace_assignment(connection, 999999, "SW-001", "SW-001")
        check(False, "replacing on an unknown shift should have raised AssignmentNotFound")
    except AssignmentNotFound:
        check(True, "replacing on an unknown shift id raises AssignmentNotFound")

    shift = shift_id_for(connection, "2026-11-02 17:00")
    try:
        replace_assignment(connection, shift, "SW-999", "SW-001")
        check(False, "replacing an unknown outgoing employee code should have raised AssignmentNotFound")
    except AssignmentNotFound:
        check(True, "an unknown outgoing employee code raises AssignmentNotFound")

    try:
        replace_assignment(connection, shift, "SW-001", "SW-001")
        check(False, "replacing a worker who does not currently hold the shift should have raised AssignmentNotFound")
    except AssignmentNotFound:
        check(True, "a worker who does not currently hold the named shift raises AssignmentNotFound on replace")

    reject_proposal(connection, proposal["id"])
    try:
        approve_proposal(connection, proposal["id"], approval_payload(proposal))
        check(False, "approving an already-rejected proposal should have been refused")
    except ProposalNotPending:
        check(True, "approving an already-rejected proposal raises ProposalNotPending")
    connection.close()


def check_concurrent_approval_no_duplicate():
    """Two threads race to approve the SAME proposal on a real file-backed
    database (in-memory databases cannot be shared across connections).
    Exactly one set of assignments must result - never two, never a
    half-applied one."""
    temporary = tempfile.TemporaryDirectory()
    path = Path(temporary.name) / "concurrent-approval.db"

    builder = get_connection(path)
    create_schema(builder)
    confirmed_worker(builder, "SW-001")
    confirmed_worker(builder, "SW-002")
    prepare_week(builder, WEEK)
    proposal = create_proposal(builder, WEEK)
    payload = approval_payload(proposal)
    builder.close()

    both_ready = threading.Barrier(2)
    results = {}
    errors = []

    def racer(name):
        own = get_connection(path)
        own.execute("PRAGMA busy_timeout = 10000")
        try:
            both_ready.wait(timeout=10)
            results[name] = approve_proposal(own, proposal["id"], payload)
        except Exception as error:  # noqa: BLE001 - recorded, not swallowed
            errors.append(f"{name}: {type(error).__name__}: {error}")
        finally:
            own.close()

    threads = [threading.Thread(target=racer, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    check(errors == [], f"both concurrent approval attempts completed without an uncaught error ({errors})")
    check(
        results.get("a", {}).get("status") == "approved" and results.get("b", {}).get("status") == "approved",
        f"both calls report the proposal as approved ({results})",
    )

    checker = get_connection(path)
    try:
        stored_pairs = {
            (row["employee_id"], row["shift_id"])
            for row in checker.execute("SELECT employee_id, shift_id FROM assignments")
        }
        expected_pairs = {(a["employee_id"], a["shift_id"]) for a in proposal["assignments"]}
        check(stored_pairs == expected_pairs, f"exactly the proposed assignments exist once each, no duplicates ({stored_pairs})")

        created_audit_count = checker.execute(
            "SELECT COUNT(*) AS n FROM assignment_audit WHERE action = 'assignment_created'"
        ).fetchone()["n"]
        check(
            created_audit_count == len(expected_pairs),
            f"assignment_created audit rows were written exactly once each, not duplicated by the race ({created_audit_count} vs {len(expected_pairs)})",
        )
    finally:
        checker.close()


def main_entry():
    check_proposal_content_bound_and_retrievable()
    check_draft_snapshot_persisted_and_recoverable()
    check_legacy_proposal_without_snapshot_still_readable()
    check_malformed_snapshot_fails_clearly()
    check_list_proposals_for_week_ordered_and_filtered()
    check_create_writes_no_assignments()
    check_successful_approval_persists_and_audits()
    check_repeated_approval_idempotent()
    check_stale_change_refuses_whole_approval()
    check_stale_leave_added_refuses_approval()
    check_stale_staffing_consumed_refuses_approval()
    check_stale_weekly_hours_refuses_approval()
    check_overlapping_and_duplicate_proposed_assignments_refused()
    check_proposal_week_ownership_enforced()
    check_existing_assignments_preserved()
    check_explicit_replacement_atomic_with_before_after()
    check_replacement_constraint_safety()
    check_invalid_and_ownership_errors()
    check_concurrent_approval_no_duplicate()

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"\nAll {_total['n']} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
