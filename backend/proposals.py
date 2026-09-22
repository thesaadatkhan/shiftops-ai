"""Proposal creation, approval, rejection, and explicit replacement (Phase 7
increment 3) - the layer that turns a computed, unstored draft
(`optimizer.generate_draft`) into real, persisted `assignments` rows only
after explicit supervisor approval.

**Draft generation stays separate from persistence.** `create_proposal()`
calls `optimizer.generate_draft()` - a pure read - and only afterwards opens
a write transaction to store what it returned. Nothing about how a draft is
computed lives in this module, and nothing here ever re-runs the optimizer:
approving a proposal always operates on the exact rows persisted when the
proposal was created, never a freshly regenerated draft.

**A proposal is immutable once created.** `schedule_proposals` and
`proposal_assignments` are written once, together, in one transaction, and
`proposal_assignments` is never updated afterwards. Approving or rejecting
only ever changes `schedule_proposals.status` and `decided_at`. This is what
makes "bind approval to the exact proposal content" possible: there is
nothing to silently drift.

**Approval requires the caller to submit back the exact assignments they
believe they are approving** - the same pattern `timetables.confirm_schedule`
already established for semester confirmation - so a stale or malformed
client view can never approve content nobody actually looked at. That
submitted content is compared against the stored proposal; a mismatch is a
409, not a silent substitution.

**Approval revalidates every proposed assignment against CURRENT stored
state** before writing anything: active status, confirmed/non-provisional
timetable coverage, class conflict, leave conflict, conflict with an
existing assignment, and the weekly-hour limit are all re-checked by calling
`eligibility.evaluate_shift_eligibility` again - the same function
`optimizer.py` uses at generation time, and the same function Coverage uses
- so approval can never grant an assignment eligibility never actually
allowed. Three further checks close the gap a single per-pair eligibility
call cannot see, mirroring exactly the constraints `optimizer.py` adds at
generation time, but re-derived against what is true RIGHT NOW rather than
at generation time: no two shifts in the SAME proposal may overlap for one
worker; the sum of every shift in the proposal proposed to one worker, on
top of their current stored hours, must not exceed their weekly limit; and
each shift's current existing-assignment count plus however many the
proposal adds to it must not exceed `required_staff` (a shift can have gone
from open to already-staffed since the draft was generated). Any conflict at
all refuses the WHOLE approval - never a partial write - and is returned as
structured, per-assignment detail a supervisor can act on.

**Approval is atomic and idempotent.** Revalidation and every resulting
`assignments`/`assignment_audit` write happen inside one `BEGIN IMMEDIATE`
transaction, so a concurrent second approval attempt on the same proposal
either serializes behind the first (and then finds the proposal already
`approved`, returning the same result without writing again) or fails
outright - never a duplicate or half-applied set of assignments.

**Replacement is a separate, explicit operation**, not something approval or
generation does implicitly: `replace_assignment()` validates the incoming
worker's hard eligibility, preserves the outgoing assignment until the
transaction actually commits, and records the exact before/after employee
ids in one `assignment_audit` row. It never silently deletes or overwrites
an assignment on a failed validation.

**The audit trail records what happened, never why the optimizer chose it.**
`assignment_audit` rows are short factual sentences (a proposal was created
for a given week with N proposed assignments; a proposal was approved and M
assignments were created; a specific shift's assignment was replaced) - no
solver internals, no hidden reasoning.
"""

import json
from datetime import datetime

from eligibility import evaluate_shift_eligibility
from employees import find_by_code
from optimizer import generate_draft
from reporting import assigned_hours_for_employee, shift_duration_hours
from synthetic_data import TIME_FORMAT
from weeks import parse_week_start, week_bounds

TIMESTAMP_FORMAT = TIME_FORMAT


class ProposalNotFound(LookupError):
    """No such proposal. Maps to HTTP 404."""


class ProposalValidationError(ValueError):
    """The submitted approval payload is malformed. Maps to HTTP 400."""


class ProposalContentMismatch(RuntimeError):
    """The submitted assignments do not match this proposal's stored
    content. Maps to HTTP 409."""


class ProposalNotPending(RuntimeError):
    """The proposal has already been decided the other way. Maps to
    HTTP 409."""


class ProposalRevalidationFailed(RuntimeError):
    """One or more proposed assignments are no longer valid against current
    state. Maps to HTTP 409 with `.conflicts` as structured detail."""

    def __init__(self, conflicts):
        self.conflicts = conflicts
        super().__init__(f"{len(conflicts)} conflict(s) found during revalidation.")


class ProposalSnapshotCorrupted(RuntimeError):
    """A stored draft review snapshot could not be parsed as the expected
    shape. Maps to HTTP 500 - a controlled refusal to fabricate review data,
    never a raw JSON/attribute error surfaced to the caller."""


class AssignmentNotFound(LookupError):
    """No such shift, worker, or existing assignment to replace. Maps to
    HTTP 404."""


class ReplacementInvalid(RuntimeError):
    """The replacement worker fails hard eligibility. Maps to HTTP 409 with
    `.detail` as structured detail."""

    def __init__(self, detail):
        self.detail = detail
        super().__init__(str(detail))


def _in_transaction(connection, work):
    """Run `work` inside one BEGIN IMMEDIATE transaction.

    The same shape `timetables._in_transaction` and `scheduling._in_transaction`
    use: the write lock is taken before anything is read, so a concurrent
    caller cannot act on a view of the database this operation has already
    changed, and any failure rolls the whole operation back.
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


def _now(reference_time=None):
    return (reference_time or datetime.now()).strftime(TIMESTAMP_FORMAT)


def _parse(datetime_text):
    return datetime.strptime(datetime_text, TIME_FORMAT)


def _overlaps(a_start, a_end, b_start, b_end):
    """D025's touching-endpoint convention: ending exactly when another
    starts is not an overlap. Mirrors `optimizer._overlaps` exactly."""
    return a_start < b_end and b_start < a_end


def _parse_review_snapshot(proposal_id, raw_text):
    """Parse and validate a stored `draft_snapshot` column back into the
    review shape the frontend renders.

    `None` (a proposal created before this column existed, or a legacy row)
    is a legitimate "no review available" answer, not corruption. Anything
    else must parse as JSON and have the shape `optimizer.generate_draft`
    always produces - a dict with a `shifts` list and a `summary` dict -
    or `ProposalSnapshotCorrupted` is raised rather than handing the caller
    a malformed or partial review silently.
    """
    if raw_text is None:
        return None
    try:
        snapshot = json.loads(raw_text)
    except (TypeError, ValueError) as error:
        raise ProposalSnapshotCorrupted(
            f"Proposal {proposal_id}'s stored review data is not valid JSON."
        ) from error
    if (
        not isinstance(snapshot, dict)
        or not isinstance(snapshot.get("shifts"), list)
        or not isinstance(snapshot.get("summary"), dict)
    ):
        raise ProposalSnapshotCorrupted(
            f"Proposal {proposal_id}'s stored review data is missing the "
            "expected 'shifts'/'summary' structure."
        )
    return snapshot


def proposal_payload(connection, proposal_id):
    """The stored proposal exactly as persisted. Read-only."""
    proposal = connection.execute(
        "SELECT id, week_start, created_at, status, decided_at, draft_snapshot"
        " FROM schedule_proposals WHERE id = ?",
        (proposal_id,),
    ).fetchone()
    if proposal is None:
        raise ProposalNotFound(f"No proposal {proposal_id}.")

    review = _parse_review_snapshot(proposal_id, proposal["draft_snapshot"])

    rows = connection.execute(
        """
        SELECT pa.shift_id, pa.employee_id, e.employee_code, e.full_name,
               s.hall, s.start_datetime, s.end_datetime
        FROM proposal_assignments pa
        JOIN employees e ON e.id = pa.employee_id
        JOIN shifts s ON s.id = pa.shift_id
        WHERE pa.proposal_id = ?
        ORDER BY s.start_datetime, s.hall, s.id, e.employee_code, e.id
        """,
        (proposal_id,),
    ).fetchall()

    return {
        "id": proposal["id"],
        "week_start": proposal["week_start"],
        "created_at": proposal["created_at"],
        "status": proposal["status"],
        "decided_at": proposal["decided_at"],
        "review": review,
        "assignments": [
            {
                "shift_id": row["shift_id"],
                "employee_id": row["employee_id"],
                "employee_code": row["employee_code"],
                "full_name": row["full_name"],
                "hall": row["hall"],
                "start_datetime": row["start_datetime"],
                "end_datetime": row["end_datetime"],
            }
            for row in rows
        ],
    }


def create_proposal(connection, week_start_text, reference_time=None):
    """Compute a draft (read-only) and persist it as a new pending proposal.

    Only the optimizer's `proposed_assignments` become `proposal_assignments`
    rows - existing assignments are never duplicated into a proposal, since
    approving it must never re-create what is already stored. Propagates
    `weeks.InvalidWeekStart`, `optimizer.WeekNotPrepared`, and
    `optimizer.DraftNotOptimal` unchanged; none of them can leave a
    half-written proposal because the draft is computed BEFORE any write
    transaction opens.
    """
    draft = generate_draft(connection, week_start_text)
    week_start = draft["week_start"]
    pairs = [
        (shift["id"], worker["employee_id"])
        for shift in draft["shifts"]
        for worker in shift["proposed_assignments"]
    ]

    def work():
        now = _now(reference_time)
        cursor = connection.execute(
            "INSERT INTO schedule_proposals"
            " (week_start, created_at, status, draft_snapshot)"
            " VALUES (?, ?, 'pending', ?)",
            (week_start, now, json.dumps(draft)),
        )
        proposal_id = cursor.lastrowid
        for shift_id, employee_id in pairs:
            connection.execute(
                "INSERT INTO proposal_assignments (proposal_id, shift_id, employee_id)"
                " VALUES (?, ?, ?)",
                (proposal_id, shift_id, employee_id),
            )
        connection.execute(
            "INSERT INTO assignment_audit (occurred_at, action, proposal_id, detail)"
            " VALUES (?, 'proposal_created', ?, ?)",
            (
                now,
                proposal_id,
                f"Proposal {proposal_id} created for week {week_start} with "
                f"{len(pairs)} proposed assignment(s).",
            ),
        )
        return proposal_payload(connection, proposal_id)

    return _in_transaction(connection, work)


def get_proposal(connection, proposal_id):
    """Read-only: the stored proposal exactly as persisted."""
    return proposal_payload(connection, proposal_id)


def list_proposals_for_week(connection, week_start_text):
    """Every stored proposal for one Monday week, newest first. Read-only.

    Ordered by id DESC (equivalently creation order, since ids are assigned
    in insertion order) so the result is deterministic and a caller that
    lost its in-memory proposal state - a refresh, a remount, navigating
    away and back - can always recover it: the most recently generated
    proposal is always first, and every earlier one for the same week
    (including already-decided ones) is still listed alongside it.
    """
    parse_week_start(week_start_text)  # raises InvalidWeekStart if malformed
    ids = [
        row["id"]
        for row in connection.execute(
            "SELECT id FROM schedule_proposals WHERE week_start = ? ORDER BY id DESC",
            (week_start_text,),
        )
    ]
    return [proposal_payload(connection, proposal_id) for proposal_id in ids]


def _parse_submitted_assignments(connection, payload):
    """Validate and resolve the caller's submitted approval payload.

    Expected shape: `{"assignments": [{"shift_id": int, "employee_code": str}, ...]}`.
    Read-only (only SELECTs, via `find_by_code`) - safe to call before a
    transaction opens.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("assignments"), list):
        raise ProposalValidationError(
            "Expected a JSON object with an 'assignments' array of "
            "{shift_id, employee_code} entries."
        )

    pairs = set()
    for entry in payload["assignments"]:
        if not isinstance(entry, dict):
            raise ProposalValidationError("Each assignment must be an object.")
        shift_id = entry.get("shift_id")
        employee_code = entry.get("employee_code")
        if not isinstance(shift_id, int) or isinstance(shift_id, bool) or not isinstance(employee_code, str):
            raise ProposalValidationError(
                "Each assignment needs an integer shift_id and a string employee_code."
            )
        employee = find_by_code(connection, employee_code)
        if employee is None:
            raise ProposalValidationError(f"No employee with code {employee_code}.")
        pair = (shift_id, employee["id"])
        if pair in pairs:
            raise ProposalValidationError(
                f"Duplicate assignment submitted for shift {shift_id} and "
                f"employee {employee_code}."
            )
        pairs.add(pair)
    return pairs


def _revalidate_proposal(connection, week_start_text, stored_pairs):
    """Re-check every proposed assignment against CURRENT stored state.

    Returns a list of structured conflict dicts (empty if everything still
    holds). Reuses `eligibility.evaluate_shift_eligibility` for every hard
    rule, then re-derives the two extra constraints `optimizer.py` needs at
    generation time - proposed-shift overlap and cumulative weekly hours -
    against what is true NOW, plus a third check `optimizer.py` does not
    need: that current staffing has not since consumed this shift's
    remaining positions.
    """
    week_start = parse_week_start(week_start_text)
    _, week_end = week_bounds(week_start)

    conflicts = []
    shifts_by_id = {}
    employees_by_id = {}
    valid_pairs = []

    for shift_id, employee_id in sorted(stored_pairs):
        shift = shifts_by_id.get(shift_id, "missing")
        if shift == "missing":
            shift = connection.execute(
                "SELECT id, hall, start_datetime, end_datetime, required_staff"
                " FROM shifts WHERE id = ?",
                (shift_id,),
            ).fetchone()
            shifts_by_id[shift_id] = shift
        if shift is None:
            conflicts.append(
                {
                    "shift_id": shift_id,
                    "employee_id": employee_id,
                    "reason_codes": ["shift_not_found"],
                    "reasons": ["This shift no longer exists."],
                }
            )
            continue

        # Ownership: every proposed shift must actually start within THIS
        # proposal's own Monday-to-Monday week (D025 - a shift belongs to the
        # week containing its start). A proposal can only ever legitimately
        # describe one week; a stored row naming a shift outside it can only
        # mean the proposal itself is malformed (hand-edited, or written by
        # code that never went through `create_proposal`), and approving it
        # would silently mix another week's shift into this week's schedule.
        shift_start = _parse(shift["start_datetime"])
        if not (week_start <= shift_start < week_end):
            conflicts.append(
                {
                    "shift_id": shift_id,
                    "employee_id": employee_id,
                    "reason_codes": ["proposal_week_mismatch"],
                    "reasons": [
                        f"Shift {shift_id} starts {shift['start_datetime']}, "
                        f"outside this proposal's week of {week_start_text}."
                    ],
                }
            )
            continue

        employee = employees_by_id.get(employee_id, "missing")
        if employee == "missing":
            employee = connection.execute(
                "SELECT id, employee_code, full_name, is_active, weekly_hour_limit"
                " FROM employees WHERE id = ?",
                (employee_id,),
            ).fetchone()
            employees_by_id[employee_id] = employee
        if employee is None:
            conflicts.append(
                {
                    "shift_id": shift_id,
                    "employee_id": employee_id,
                    "reason_codes": ["employee_not_found"],
                    "reasons": ["This worker no longer exists."],
                }
            )
            continue

        already_exists = connection.execute(
            "SELECT 1 FROM assignments WHERE shift_id = ? AND employee_id = ?",
            (shift_id, employee_id),
        ).fetchone()
        if already_exists:
            conflicts.append(
                {
                    "shift_id": shift_id,
                    "employee_id": employee_id,
                    "employee_code": employee["employee_code"],
                    "reason_codes": ["assignment_already_exists"],
                    "reasons": [
                        "This exact assignment was already created since the "
                        "proposal was generated."
                    ],
                }
            )
            continue

        result = evaluate_shift_eligibility(connection, shift, employee)
        if not result["eligible"]:
            conflicts.append(
                {
                    "shift_id": shift_id,
                    "employee_id": employee_id,
                    "employee_code": employee["employee_code"],
                    "reason_codes": result["reason_codes"],
                    "reasons": result["reasons"],
                }
            )
            continue

        valid_pairs.append((shift_id, employee_id))

    # Cross-proposal overlap and cumulative hours, per worker - only over
    # pairs that individually passed every check above.
    by_employee = {}
    for shift_id, employee_id in valid_pairs:
        by_employee.setdefault(employee_id, []).append(shift_id)

    for employee_id, shift_ids in by_employee.items():
        employee = employees_by_id[employee_id]
        for i in range(len(shift_ids)):
            shift_a = shifts_by_id[shift_ids[i]]
            a_start, a_end = _parse(shift_a["start_datetime"]), _parse(shift_a["end_datetime"])
            for j in range(i + 1, len(shift_ids)):
                shift_b = shifts_by_id[shift_ids[j]]
                b_start, b_end = _parse(shift_b["start_datetime"]), _parse(shift_b["end_datetime"])
                if _overlaps(a_start, a_end, b_start, b_end):
                    conflicts.append(
                        {
                            "shift_id": shift_a["id"],
                            "other_shift_id": shift_b["id"],
                            "employee_id": employee_id,
                            "employee_code": employee["employee_code"],
                            "reason_codes": ["proposed_assignment_overlap"],
                            "reasons": [
                                f"Shifts {shift_a['id']} and {shift_b['id']} in this "
                                "proposal overlap for the same worker."
                            ],
                        }
                    )

        existing_hours = assigned_hours_for_employee(connection, employee_id, week_start, week_end)
        proposed_hours = sum(
            shift_duration_hours(
                _parse(shifts_by_id[shift_id]["start_datetime"]),
                _parse(shifts_by_id[shift_id]["end_datetime"]),
                label=f"{shift_id}",
            )
            for shift_id in shift_ids
        )
        projected = existing_hours + proposed_hours
        if projected > employee["weekly_hour_limit"]:
            conflicts.append(
                {
                    "employee_id": employee_id,
                    "employee_code": employee["employee_code"],
                    "reason_codes": ["weekly_hour_limit_exceeded"],
                    "reasons": [
                        f"Approving this proposal would bring weekly hours to "
                        f"{projected}, exceeding the limit of "
                        f"{employee['weekly_hour_limit']}."
                    ],
                }
            )

    # Staffing: current existing count for each shift, plus however many
    # valid entries this proposal still adds to it, must not exceed
    # required_staff - state can have changed since the draft was generated.
    new_counts_by_shift = {}
    for shift_id, _employee_id in valid_pairs:
        new_counts_by_shift[shift_id] = new_counts_by_shift.get(shift_id, 0) + 1
    for shift_id, new_count in new_counts_by_shift.items():
        shift = shifts_by_id[shift_id]
        current_count = connection.execute(
            "SELECT COUNT(*) AS n FROM assignments WHERE shift_id = ?", (shift_id,)
        ).fetchone()["n"]
        if current_count + new_count > shift["required_staff"]:
            conflicts.append(
                {
                    "shift_id": shift_id,
                    "reason_codes": ["staffing_no_longer_available"],
                    "reasons": [
                        f"Shift {shift_id} now has {current_count} existing "
                        f"assignment(s); approving this proposal's {new_count} "
                        f"more would exceed required_staff={shift['required_staff']}."
                    ],
                }
            )

    return conflicts


def approve_proposal(connection, proposal_id, payload, reference_time=None):
    """Approve a proposal, creating real assignments, or refuse it whole.

    The caller must submit the proposal's exact stored assignments back
    (`{"assignments": [{"shift_id", "employee_code"}, ...]}`); a mismatch is
    `ProposalContentMismatch` (409) rather than approving unseen content.
    Every proposed assignment is then revalidated against CURRENT state
    inside the same `BEGIN IMMEDIATE` transaction that would write the
    assignments, so nothing can change between the check and the write.
    Any conflict refuses the WHOLE approval - `ProposalRevalidationFailed`,
    carrying every conflict found - and writes nothing. Approving an
    already-approved proposal with matching content is a no-op success
    (idempotent retry); approving an already-rejected one is
    `ProposalNotPending` (409).
    """
    submitted = _parse_submitted_assignments(connection, payload)

    def work():
        proposal = connection.execute(
            "SELECT id, week_start, status FROM schedule_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if proposal is None:
            raise ProposalNotFound(f"No proposal {proposal_id}.")

        stored_rows = connection.execute(
            "SELECT shift_id, employee_id FROM proposal_assignments WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchall()
        stored_pairs = {(row["shift_id"], row["employee_id"]) for row in stored_rows}

        if submitted != stored_pairs:
            raise ProposalContentMismatch(
                f"The submitted assignments do not match proposal {proposal_id}'s "
                "stored content; reload the proposal and try again."
            )

        if proposal["status"] == "rejected":
            raise ProposalNotPending(
                f"Proposal {proposal_id} was already rejected and cannot be approved."
            )
        if proposal["status"] == "approved":
            # Idempotent retry: identical content already approved earlier.
            return proposal_payload(connection, proposal_id)

        conflicts = _revalidate_proposal(connection, proposal["week_start"], stored_pairs)
        if conflicts:
            raise ProposalRevalidationFailed(conflicts)

        now = _now(reference_time)
        for shift_id, employee_id in sorted(stored_pairs):
            connection.execute(
                "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
                (employee_id, shift_id),
            )
            connection.execute(
                """
                INSERT INTO assignment_audit
                    (occurred_at, action, proposal_id, shift_id, employee_id_after, detail)
                VALUES (?, 'assignment_created', ?, ?, ?, ?)
                """,
                (
                    now, proposal_id, shift_id, employee_id,
                    f"Assignment created via approved proposal {proposal_id}.",
                ),
            )
        connection.execute(
            "UPDATE schedule_proposals SET status = 'approved', decided_at = ? WHERE id = ?",
            (now, proposal_id),
        )
        connection.execute(
            "INSERT INTO assignment_audit (occurred_at, action, proposal_id, detail)"
            " VALUES (?, 'proposal_approved', ?, ?)",
            (
                now, proposal_id,
                f"Proposal {proposal_id} approved; {len(stored_pairs)} "
                "assignment(s) created.",
            ),
        )
        return proposal_payload(connection, proposal_id)

    return _in_transaction(connection, work)


def reject_proposal(connection, proposal_id, reference_time=None):
    """Reject a pending proposal. Writes no assignment. Idempotent for a
    repeat rejection; refuses (`ProposalNotPending`) to reject an
    already-approved one."""

    def work():
        proposal = connection.execute(
            "SELECT id, status FROM schedule_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if proposal is None:
            raise ProposalNotFound(f"No proposal {proposal_id}.")
        if proposal["status"] == "rejected":
            return proposal_payload(connection, proposal_id)
        if proposal["status"] == "approved":
            raise ProposalNotPending(
                f"Proposal {proposal_id} was already approved and cannot be rejected."
            )

        now = _now(reference_time)
        connection.execute(
            "UPDATE schedule_proposals SET status = 'rejected', decided_at = ? WHERE id = ?",
            (now, proposal_id),
        )
        connection.execute(
            "INSERT INTO assignment_audit (occurred_at, action, proposal_id, detail)"
            " VALUES (?, 'proposal_rejected', ?, ?)",
            (now, proposal_id, f"Proposal {proposal_id} rejected."),
        )
        return proposal_payload(connection, proposal_id)

    return _in_transaction(connection, work)


def _replace_assignment_locked(
    connection, shift_id, outgoing_employee_code, incoming_employee_code,
    reference_time=None, agent_proposal_id=None,
):
    """The validated replacement logic itself, assuming the caller ALREADY
    holds the write lock (a `BEGIN IMMEDIATE` transaction is already open).

    Extracted from `replace_assignment()` (Phase 9 increment 2, Codex
    review) so the Phase 9 agent-approval transaction in `agent_service.py`
    can perform the exact same validated replacement, the exact same
    `assignment_audit` write, and its own `agent_proposals` status update
    all inside ONE atomic transaction - never a public function that
    commits on its own, followed by a second, separate transaction to
    record the decision. `replace_assignment()` below is now a thin wrapper
    that opens the transaction and calls this; its own behavior, signature,
    and return value are unchanged.

    `agent_proposal_id` is `None` for an ordinary Phase 7 manual
    replacement (the column exists for exactly one purpose: linking an
    audit row back to the Phase 9 proposal that caused it) and the calling
    agent proposal's id when invoked from proposal approval.
    """
    shift = connection.execute(
        "SELECT id, hall, start_datetime, end_datetime, required_staff"
        " FROM shifts WHERE id = ?",
        (shift_id,),
    ).fetchone()
    if shift is None:
        raise AssignmentNotFound(f"No shift {shift_id}.")

    outgoing = find_by_code(connection, outgoing_employee_code)
    if outgoing is None:
        raise AssignmentNotFound(f"No employee with code {outgoing_employee_code}.")
    incoming = find_by_code(connection, incoming_employee_code)
    if incoming is None:
        raise AssignmentNotFound(f"No employee with code {incoming_employee_code}.")

    existing = connection.execute(
        "SELECT id FROM assignments WHERE shift_id = ? AND employee_id = ?",
        (shift_id, outgoing["id"]),
    ).fetchone()
    if existing is None:
        raise AssignmentNotFound(
            f"{outgoing_employee_code} does not currently hold shift "
            f"{shift_id}; nothing to replace."
        )

    # Two constraint-shaped refusals, checked before eligibility and
    # before any write: a "replacement" naming the same worker twice, and
    # an incoming worker who already holds this exact shift (a second
    # assignment row for them here would violate the schema's own
    # (employee_id, shift_id) uniqueness). Both are refused as a
    # controlled 409 rather than ever reaching a raw database
    # IntegrityError, and neither writes an audit row.
    if incoming["id"] == outgoing["id"]:
        raise ReplacementInvalid(
            {
                "shift_id": shift_id,
                "employee_code": incoming_employee_code,
                "reason_codes": ["replacement_same_worker"],
                "reasons": [
                    "The incoming worker is the same as the outgoing worker; "
                    "there is nothing to replace."
                ],
            }
        )

    already_holds_shift = connection.execute(
        "SELECT 1 FROM assignments WHERE shift_id = ? AND employee_id = ?",
        (shift_id, incoming["id"]),
    ).fetchone()
    if already_holds_shift:
        raise ReplacementInvalid(
            {
                "shift_id": shift_id,
                "employee_code": incoming_employee_code,
                "reason_codes": ["replacement_already_assigned"],
                "reasons": [
                    f"{incoming_employee_code} already holds shift {shift_id}."
                ],
            }
        )

    result = evaluate_shift_eligibility(connection, shift, incoming)
    if not result["eligible"]:
        raise ReplacementInvalid(
            {
                "shift_id": shift_id,
                "employee_code": incoming_employee_code,
                "reason_codes": result["reason_codes"],
                "reasons": result["reasons"],
            }
        )

    # The outgoing row is deleted only now, inside the same transaction
    # as the incoming insert and the audit row - a failure anywhere
    # above this point leaves it completely untouched.
    connection.execute("DELETE FROM assignments WHERE id = ?", (existing["id"],))
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (incoming["id"], shift_id),
    )
    now = _now(reference_time)
    connection.execute(
        """
        INSERT INTO assignment_audit
            (occurred_at, action, shift_id, employee_id_before, employee_id_after,
             agent_proposal_id, detail)
        VALUES (?, 'assignment_replaced', ?, ?, ?, ?, ?)
        """,
        (
            now, shift_id, outgoing["id"], incoming["id"], agent_proposal_id,
            f"Shift {shift_id}: {outgoing_employee_code} replaced by "
            f"{incoming_employee_code}.",
        ),
    )
    return {
        "shift_id": shift_id,
        "outgoing_employee_code": outgoing_employee_code,
        "incoming_employee_code": incoming_employee_code,
        "occurred_at": now,
    }


def _create_assignment_locked(
    connection, shift_id, incoming_employee_code, reference_time=None, agent_proposal_id=None,
):
    """Validated creation of ONE new assignment into a genuinely uncovered
    position, assuming the caller already holds the write lock. The
    fill-an-uncovered-shift counterpart to `_replace_assignment_locked` -
    same lock-already-held contract, same validated-then-write shape, same
    single `assignment_audit` row, added for Phase 9's uncovered-shift
    proposal path (there is no outgoing worker to replace, so
    `employee_id_before` is `NULL`).

    Refuses (never writes) if the shift does not exist, the worker does
    not exist, the worker already holds this shift, the shift is already
    covered (`required_staff` positions already filled - creating another
    would overstaff it), or the worker fails any current hard eligibility
    rule.
    """
    shift = connection.execute(
        "SELECT id, hall, start_datetime, end_datetime, required_staff"
        " FROM shifts WHERE id = ?",
        (shift_id,),
    ).fetchone()
    if shift is None:
        raise AssignmentNotFound(f"No shift {shift_id}.")

    incoming = find_by_code(connection, incoming_employee_code)
    if incoming is None:
        raise AssignmentNotFound(f"No employee with code {incoming_employee_code}.")

    already_holds_shift = connection.execute(
        "SELECT 1 FROM assignments WHERE shift_id = ? AND employee_id = ?",
        (shift_id, incoming["id"]),
    ).fetchone()
    if already_holds_shift:
        raise ReplacementInvalid(
            {
                "shift_id": shift_id,
                "employee_code": incoming_employee_code,
                "reason_codes": ["replacement_already_assigned"],
                "reasons": [f"{incoming_employee_code} already holds shift {shift_id}."],
            }
        )

    assigned_count = connection.execute(
        "SELECT COUNT(*) AS n FROM assignments WHERE shift_id = ?", (shift_id,)
    ).fetchone()["n"]
    if assigned_count >= shift["required_staff"]:
        raise ReplacementInvalid(
            {
                "shift_id": shift_id,
                "reason_codes": ["shift_already_covered"],
                "reasons": [
                    f"Shift {shift_id} already has {assigned_count} of "
                    f"{shift['required_staff']} required position(s) filled; there is "
                    "no uncovered position to fill."
                ],
            }
        )

    result = evaluate_shift_eligibility(connection, shift, incoming)
    if not result["eligible"]:
        raise ReplacementInvalid(
            {
                "shift_id": shift_id,
                "employee_code": incoming_employee_code,
                "reason_codes": result["reason_codes"],
                "reasons": result["reasons"],
            }
        )

    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (incoming["id"], shift_id),
    )
    now = _now(reference_time)
    connection.execute(
        """
        INSERT INTO assignment_audit
            (occurred_at, action, shift_id, employee_id_after, agent_proposal_id, detail)
        VALUES (?, 'assignment_created', ?, ?, ?, ?)
        """,
        (
            now, shift_id, incoming["id"], agent_proposal_id,
            f"Shift {shift_id}: {incoming_employee_code} assigned to fill an "
            "uncovered position.",
        ),
    )
    return {
        "shift_id": shift_id,
        "incoming_employee_code": incoming_employee_code,
        "occurred_at": now,
    }


def create_assignment(
    connection, shift_id, incoming_employee_code, reference_time=None
):
    """Atomically fill one currently uncovered shift position.

    This is the supervisor-facing transaction wrapper around the same locked
    primitive used by agent-proposal approval. It intentionally adds no new
    validation path: existence, duplicate assignment, current staffing and
    every hard eligibility rule are checked after ``BEGIN IMMEDIATE`` is
    acquired, and the assignment plus its audit row commit together.
    """
    return _in_transaction(
        connection,
        lambda: _create_assignment_locked(
            connection,
            shift_id,
            incoming_employee_code,
            reference_time=reference_time,
        ),
    )


def replace_assignment(
    connection, shift_id, outgoing_employee_code, incoming_employee_code, reference_time=None
):
    """Atomically replace one worker's assignment to a shift with another's.

    The outgoing assignment is preserved (never deleted) unless the whole
    transaction succeeds - a failed validation leaves it exactly as it was.
    The incoming worker must pass `eligibility.evaluate_shift_eligibility`
    for this shift, evaluated as of current stored state (their own
    candidacy is unaffected by the outgoing assignment being removed, since
    they do not hold it). Records the exact before/after employee ids in one
    `assignment_audit` row.

    A thin transaction wrapper around `_replace_assignment_locked` - see
    that function for the actual validation and write logic.
    """
    return _in_transaction(
        connection,
        lambda: _replace_assignment_locked(
            connection, shift_id, outgoing_employee_code, incoming_employee_code,
            reference_time=reference_time,
        ),
    )
