"""Deterministic tool registry for the Phase 9 scheduling agent (increment 1).

Every tool here calls an EXISTING application/domain function - `employees`,
`eligibility`, `reporting`, `weeks` - rather than reproducing any scheduling
rule of its own. This module adds lookup/aggregation convenience around
those functions (resolving a name to a worker, a hall/date/time to a shift,
tallying ineligibility reasons) and exactly one write: `propose_replacement`,
which creates a PENDING `agent_proposals` row and nothing else. No tool here
ever writes to `assignments`, `approved_leave`, or `employees` - a call-out
statement alone can therefore never remove an assignment, create leave, or
change a worker, no matter what the model asks for, because no such tool
exists in this registry at all.

**Ambiguity is a first-class, explicit result**, never guessed away: a name
or a hall/date/time that matches more than one record returns
`{"status": "ambiguous", "matches": [...]}` rather than silently picking
one. The bounded loop (`agent_service.py`) surfaces this to the supervisor
as a clarification request.

**Ranking is one documented, deterministic rule** (`rank_candidates`, an
internal helper - NOT exposed to the model as its own tool, since a
standalone tool would require trusting arbitrary nested candidate data the
model constructed itself rather than data this module already produced):
preferred before neutral before low, then lower projected weekly hours,
then employee_code as the final tie-breaker. `get_eligible_candidates`
returns its candidates pre-ranked by this same function, and
`propose_replacement` requires the proposed incoming worker to be that
ranked list's FIRST entry for the ordinary replacement workflow - there is
only one ranking implementation, used everywhere ranking happens, and the
model cannot choose to propose anyone else.

**Every tool's arguments and return values are plain JSON-serializable
data** (str/int/float/bool/list/dict/None) - suitable for both the OpenAI
tool-calling wire format and a scripted fake model in tests.
"""

from datetime import datetime

from eligibility import (
    ShiftNotFound as CoverageShiftNotFound,
    UnknownExcludedWorker,
    shift_coverage,
)
from employees import find_by_code
from reporting import assigned_hours_for_employee, remaining_capacity_hours
from synthetic_data import TIME_FORMAT
from weeks import InvalidWeekStart, parse_week_start, week_bounds


class ToolError(ValueError):
    """A tool call could not be completed as asked - bad arguments, or a
    named record that turned out not to exist. Carries a `code` the loop
    feeds back to the model as a structured, factual `tool_result`, never a
    raw Python exception message."""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


PREFERENCE_RANK = {"preferred": 0, "neutral": 1, "low": 2}


def _employee_summary(row):
    return {
        "employee_code": row["employee_code"],
        "full_name": row["full_name"],
        "is_active": bool(row["is_active"]),
        "weekly_hour_limit": row["weekly_hour_limit"],
    }


def _shift_summary(row):
    return {
        "shift_id": row["id"],
        "hall": row["hall"],
        "start_datetime": row["start_datetime"],
        "end_datetime": row["end_datetime"],
        "required_staff": row["required_staff"],
    }


def find_employee(connection, query):
    """Resolve a worker by exact employee code (case-insensitive) or by a
    case-insensitive substring of their full name.

    An exact code match wins outright even if the same text would also
    substring-match a name, since a code is a stable, unambiguous identity
    and a name is not. Otherwise every worker whose name contains `query`
    (case-insensitive) is a candidate; zero is `not_found`, more than one is
    `ambiguous` with every match listed, and exactly one is `found`.
    """
    if not isinstance(query, str) or not query.strip():
        raise ToolError("invalid_arguments", "query must be a non-empty string.")
    query = query.strip()

    exact = connection.execute(
        "SELECT * FROM employees WHERE employee_code = ? COLLATE NOCASE", (query,)
    ).fetchone()
    if exact is not None:
        return {"status": "found", "employee": _employee_summary(exact)}

    rows = connection.execute(
        "SELECT * FROM employees WHERE full_name LIKE ? COLLATE NOCASE ORDER BY employee_code",
        (f"%{query}%",),
    ).fetchall()
    if not rows:
        return {"status": "not_found", "query": query}
    if len(rows) > 1:
        return {
            "status": "ambiguous",
            "query": query,
            "matches": [_employee_summary(row) for row in rows],
        }
    return {"status": "found", "employee": _employee_summary(rows[0])}


def find_shift(connection, shift_id=None, hall=None, date=None, time=None):
    """Resolve a dated shift by its stable id, or by hall/date/optional time.

    `shift_id` alone is authoritative and skips every other filter. Without
    it, `hall` (case-insensitive exact) and `date` ('YYYY-MM-DD') are
    required, and `time` ('HH:MM', the shift's exact start time) further
    narrows the match. Zero matches is `not_found`; more than one is
    `ambiguous` (typically: `time` was not given and the hall had more than
    one shift that day); exactly one is `found`.
    """
    if shift_id is not None:
        if isinstance(shift_id, bool) or not isinstance(shift_id, int):
            raise ToolError("invalid_arguments", "shift_id must be an integer.")
        row = connection.execute(
            "SELECT id, hall, start_datetime, end_datetime, required_staff"
            " FROM shifts WHERE id = ?",
            (shift_id,),
        ).fetchone()
        if row is None:
            return {"status": "not_found", "shift_id": shift_id}
        return {"status": "found", "shift": _shift_summary(row)}

    if not hall or not date:
        raise ToolError(
            "invalid_arguments", "Provide shift_id, or both hall and date."
        )
    if not isinstance(date, str) or len(date) != 10 or date[4] != "-" or date[7] != "-":
        raise ToolError("invalid_arguments", "date must be 'YYYY-MM-DD'.")

    if time is not None:
        if not isinstance(time, str) or len(time) != 5 or time[2] != ":":
            raise ToolError("invalid_arguments", "time must be 'HH:MM'.")
        rows = connection.execute(
            "SELECT id, hall, start_datetime, end_datetime, required_staff"
            " FROM shifts WHERE hall = ? COLLATE NOCASE AND start_datetime = ?"
            " ORDER BY id",
            (hall, f"{date} {time}"),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT id, hall, start_datetime, end_datetime, required_staff"
            " FROM shifts WHERE hall = ? COLLATE NOCASE AND start_datetime LIKE ?"
            " ORDER BY start_datetime, id",
            (hall, f"{date}%"),
        ).fetchall()

    if not rows:
        return {"status": "not_found", "hall": hall, "date": date, "time": time}
    if len(rows) > 1:
        return {
            "status": "ambiguous",
            "hall": hall,
            "date": date,
            "time": time,
            "matches": [_shift_summary(row) for row in rows],
        }
    return {"status": "found", "shift": _shift_summary(rows[0])}


def get_shift_details(connection, shift_id):
    """The shift's own record plus its current assignments and coverage.

    A plain read of stored `shifts`/`assignments` rows - no eligibility rule
    is evaluated here at all (see `get_eligible_candidates` for that); this
    only reports who already holds the shift and whether that is enough.
    """
    shift = connection.execute(
        "SELECT id, hall, start_datetime, end_datetime, required_staff"
        " FROM shifts WHERE id = ?",
        (shift_id,),
    ).fetchone()
    if shift is None:
        raise ToolError("shift_not_found", f"No shift {shift_id}.")

    assigned = connection.execute(
        """
        SELECT e.employee_code, e.full_name, e.is_active
        FROM assignments a JOIN employees e ON e.id = a.employee_id
        WHERE a.shift_id = ? ORDER BY e.employee_code
        """,
        (shift_id,),
    ).fetchall()

    return {
        "shift": _shift_summary(shift),
        "assigned_employees": [
            {
                "employee_code": row["employee_code"],
                "full_name": row["full_name"],
                "is_active": bool(row["is_active"]),
            }
            for row in assigned
        ],
        "assigned_count": len(assigned),
        "covered": len(assigned) >= shift["required_staff"],
    }


def rank_candidates(candidates):
    """Sort eligible candidates by the one documented deterministic rule:

    preferred before neutral before low, then lower projected weekly hours,
    then employee_code as the final tie-breaker.

    `candidates` is a list of dicts each carrying at least `preference`,
    `projected_weekly_hours`, and `employee_code` - the same shape
    `eligibility.shift_coverage`'s `eligible_candidates` entries already
    have. Returns a NEW list; the input is not mutated.
    """
    return sorted(
        candidates,
        key=lambda c: (
            PREFERENCE_RANK.get(c.get("preference"), 1),
            c.get("projected_weekly_hours", 0),
            c.get("employee_code", ""),
        ),
    )


def get_eligible_candidates(connection, shift_id, exclude_employee_code=None):
    """Who is currently eligible to cover this shift, ranked deterministically.

    A thin wrapper around `eligibility.shift_coverage` - the exact same
    hard-rule evaluation Coverage, the optimizer, and proposal revalidation
    already use - plus this module's one ranking rule applied to its
    `eligible_candidates`. `exclude_employee_code` is the outgoing worker on
    a call-out; they are removed from the eligible list even though they may
    otherwise pass every hard rule (shift_coverage already does this).
    """
    try:
        coverage = shift_coverage(connection, shift_id, exclude_employee_code)
    except CoverageShiftNotFound as error:
        raise ToolError("shift_not_found", str(error)) from error
    except UnknownExcludedWorker as error:
        raise ToolError("employee_not_found", str(error)) from error

    ranked = rank_candidates(coverage["eligible_candidates"])
    return {
        "shift": coverage["shift"],
        "excluded_employee_code": coverage["excluded_employee_code"],
        "eligible_candidates": [
            {
                "employee_code": c["employee_code"],
                "full_name": c["full_name"],
                "preference": c["preference"],
                "assigned_hours_this_week": c["assigned_hours_this_week"],
                "projected_weekly_hours": c["projected_weekly_hours"],
                "weekly_hour_limit": c["weekly_hour_limit"],
            }
            for c in ranked
        ],
        "eligible_count": len(ranked),
    }


def inspect_uncovered_shift(connection, shift_id):
    """Why a shift is (or is not) uncovered, and who could fill it.

    Combines `get_shift_details` (the plain facts: how many are assigned,
    how many are required) with `get_eligible_candidates` (who could take
    it) and a tally of WHY every currently-ineligible worker is ineligible,
    derived by counting `reason_codes` already returned by
    `eligibility.evaluate_shift_eligibility` for every worker - not a second
    eligibility implementation, just an aggregation of the same per-worker
    results `shift_coverage` already computed.
    """
    details = get_shift_details(connection, shift_id)
    try:
        coverage = shift_coverage(connection, shift_id)
    except CoverageShiftNotFound as error:  # pragma: no cover - get_shift_details already raised
        raise ToolError("shift_not_found", str(error)) from error

    reason_tally = {}
    for result in coverage["results"]:
        if result["eligible"]:
            continue
        for code in result["reason_codes"]:
            reason_tally[code] = reason_tally.get(code, 0) + 1

    ranked = rank_candidates(coverage["eligible_candidates"])
    return {
        "shift": details["shift"],
        "assigned_count": details["assigned_count"],
        "covered": details["covered"],
        "eligible_candidates": [
            {
                "employee_code": c["employee_code"],
                "full_name": c["full_name"],
                "preference": c["preference"],
                "projected_weekly_hours": c["projected_weekly_hours"],
            }
            for c in ranked
        ],
        "ineligibility_reason_tally": reason_tally,
    }


def get_employee_hours(connection, employee_code, week_start):
    """One worker's assigned/remaining hours for a requested reporting week.

    Reuses `reporting.assigned_hours_for_employee` and
    `reporting.remaining_capacity_hours` - the exact figures `/api/employees`
    already reports - and `weeks.parse_week_start` for the same Monday
    validation every other week-scoped read uses. Theoretical capacity only:
    never eligibility, and class/leave hours are not subtracted (see
    `reporting.remaining_capacity_hours`'s own docstring).
    """
    employee = find_by_code(connection, employee_code)
    if employee is None:
        raise ToolError("employee_not_found", f"No employee with code {employee_code}.")
    try:
        week_start_dt = parse_week_start(week_start)
    except InvalidWeekStart as error:
        raise ToolError("invalid_week", str(error)) from error
    _, week_end_dt = week_bounds(week_start_dt)

    assigned = assigned_hours_for_employee(
        connection, employee["id"], week_start_dt, week_end_dt
    )
    remaining = remaining_capacity_hours(employee["weekly_hour_limit"], assigned)
    return {
        "employee": _employee_summary(employee),
        "week_start": week_start,
        "assigned_hours": assigned,
        "weekly_hour_limit": employee["weekly_hour_limit"],
        "remaining_capacity_hours": remaining,
    }


def _in_transaction(connection, work):
    """Run `work` inside one BEGIN IMMEDIATE transaction - the same shape
    every other write path in this project uses (see `proposals.py`,
    `scheduling.py`, `timetables.py`, `employees.delete_employee`). The
    write lock is taken FIRST, so the "at most one proposal per task" check,
    the staffing check, the outgoing-assignment check, the eligibility/
    ranking check, and the insert all see one consistent, locked snapshot -
    a concurrent second attempt for the SAME task either serializes behind
    this one (and then finds the proposal this call just created) or fails
    outright; it can never observe a half-decided state.
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


def propose_replacement(connection, task_id, shift_id, incoming_employee_code, outgoing_employee_code=None):
    """Create exactly one PENDING `agent_proposals` row for this task. The
    only tool in this registry that writes anything, and it never writes to
    `assignments`/`approved_leave`/`employees` - only to this Phase 9 table.

    **No model-authored rationale.** This function takes no `rationale`
    argument at all (Codex review: an earlier version accepted one from the
    model and stored it verbatim, which could assert any fact regardless of
    whether it was true). The stored rationale is built entirely here, from
    the SAME authoritative, ranked candidate result `get_eligible_candidates`
    returns - preference, projected hours, weekly limit, and rank position -
    never from anything the model said.

    **The incoming worker must be the authoritative TOP-RANKED eligible
    candidate.** The eligible-candidate list is recomputed here (via
    `get_eligible_candidates`, the same function and the same ranking rule
    used everywhere else), and `incoming_employee_code` must match its
    first entry. A candidate who is eligible but NOT top-ranked, or who is
    not in the list at all (not currently eligible, or fabricated), is a
    `ToolError` - never silently accepted and never re-ranked around. A
    later feature may let a supervisor deliberately choose a different
    eligible candidate; that must be an explicit supervisor action, not
    something an untrusted model argument can request here.

    **At most one proposal per task**, checked first (cheapest check) and
    enforced again by the `agent_proposals.UNIQUE(task_id)` constraint - a
    second `propose_replacement` call for a task that already has one, from
    the same model turn or a later one, is a controlled
    `proposal_already_exists` `ToolError`, never a second row and never an
    uncontrolled `IntegrityError`.

    **Filling an uncovered shift** (`outgoing_employee_code` is `None`)
    requires the shift's current assigned count to be strictly below
    `required_staff` - proposing a "fill" for an already-covered or
    overstaffed shift is `shift_already_covered`, not a proposal.

    The entire check-and-insert sequence runs inside one `BEGIN IMMEDIATE`
    transaction (see `_in_transaction`); any failure writes nothing.
    """

    def work():
        existing_proposal = connection.execute(
            "SELECT id FROM agent_proposals WHERE task_id = ?", (task_id,)
        ).fetchone()
        if existing_proposal is not None:
            raise ToolError(
                "proposal_already_exists",
                f"Task {task_id} already has proposal {existing_proposal['id']}; at "
                "most one proposal is allowed per task. It must be approved or "
                "rejected before anything else can be proposed.",
            )

        shift = connection.execute(
            "SELECT id, hall, start_datetime, end_datetime, required_staff"
            " FROM shifts WHERE id = ?",
            (shift_id,),
        ).fetchone()
        if shift is None:
            raise ToolError("shift_not_found", f"No shift {shift_id}.")

        incoming = find_by_code(connection, incoming_employee_code)
        if incoming is None:
            raise ToolError(
                "employee_not_found", f"No employee with code {incoming_employee_code}."
            )

        outgoing_id = None
        if outgoing_employee_code is not None:
            outgoing = find_by_code(connection, outgoing_employee_code)
            if outgoing is None:
                raise ToolError(
                    "employee_not_found", f"No employee with code {outgoing_employee_code}."
                )
            holds_it = connection.execute(
                "SELECT 1 FROM assignments WHERE shift_id = ? AND employee_id = ?",
                (shift_id, outgoing["id"]),
            ).fetchone()
            if holds_it is None:
                raise ToolError(
                    "not_currently_assigned",
                    f"{outgoing_employee_code} does not currently hold shift {shift_id}; "
                    "nothing to replace.",
                )
            outgoing_id = outgoing["id"]
            if outgoing["id"] == incoming["id"]:
                raise ToolError(
                    "same_worker",
                    "The incoming worker is the same as the outgoing worker.",
                )
        else:
            assigned_count = connection.execute(
                "SELECT COUNT(*) AS n FROM assignments WHERE shift_id = ?", (shift_id,)
            ).fetchone()["n"]
            if assigned_count >= shift["required_staff"]:
                raise ToolError(
                    "shift_already_covered",
                    f"Shift {shift_id} already has {assigned_count} of "
                    f"{shift['required_staff']} required position(s) filled; there is "
                    "no uncovered position to fill.",
                )

        eligible = get_eligible_candidates(
            connection, shift_id, exclude_employee_code=outgoing_employee_code
        )
        ranked = eligible["eligible_candidates"]
        match_index = next(
            (i for i, c in enumerate(ranked) if c["employee_code"] == incoming["employee_code"]),
            None,
        )
        if match_index is None:
            raise ToolError(
                "candidate_ineligible",
                f"{incoming_employee_code} is not currently an eligible candidate for "
                f"shift {shift_id}.",
            )
        if match_index != 0:
            top = ranked[0]
            raise ToolError(
                "candidate_not_top_ranked",
                f"{incoming_employee_code} is eligible but ranks "
                f"{match_index + 1} of {len(ranked)} by the documented rule; the "
                f"top-ranked eligible candidate is {top['employee_code']} "
                f"({top['full_name']}). Propose the top-ranked candidate for an "
                "ordinary replacement.",
            )

        winner = ranked[0]
        rationale = (
            f"{winner['employee_code']} ({winner['full_name']}) is eligible and ranks "
            f"1st of {len(ranked)} eligible candidate(s): {winner['preference']} "
            f"preference, {winner['projected_weekly_hours']} projected weekly hours "
            f"(weekly limit {winner['weekly_hour_limit']})."
        )

        now = datetime.now().strftime(TIME_FORMAT)
        cursor = connection.execute(
            """
            INSERT INTO agent_proposals
                (task_id, created_at, status, action_type, shift_id,
                 outgoing_employee_id, incoming_employee_id, rationale)
            VALUES (?, ?, 'pending', 'replace_assignment', ?, ?, ?, ?)
            """,
            (task_id, now, shift_id, outgoing_id, incoming["id"], rationale),
        )
        return proposal_payload(connection, cursor.lastrowid)

    return _in_transaction(connection, work)


def proposal_payload(connection, proposal_id):
    """The stored agent proposal exactly as persisted. Read-only."""
    row = connection.execute(
        """
        SELECT ap.id, ap.task_id, ap.created_at, ap.status, ap.decided_at,
               ap.action_type, ap.shift_id, ap.rationale,
               ap.executed_at, ap.execution_outcome,
               ap.verified_at, ap.verification_outcome,
               s.hall, s.start_datetime, s.end_datetime,
               out_e.employee_code AS outgoing_employee_code,
               out_e.full_name AS outgoing_full_name,
               in_e.employee_code AS incoming_employee_code,
               in_e.full_name AS incoming_full_name
        FROM agent_proposals ap
        JOIN shifts s ON s.id = ap.shift_id
        LEFT JOIN employees out_e ON out_e.id = ap.outgoing_employee_id
        JOIN employees in_e ON in_e.id = ap.incoming_employee_id
        WHERE ap.id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise ToolError("proposal_not_found", f"No agent proposal {proposal_id}.")
    return _proposal_row_to_dict(row)


def _proposal_row_to_dict(row):
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "created_at": row["created_at"],
        "status": row["status"],
        "decided_at": row["decided_at"],
        "action_type": row["action_type"],
        "shift_id": row["shift_id"],
        "hall": row["hall"],
        "start_datetime": row["start_datetime"],
        "end_datetime": row["end_datetime"],
        "outgoing_employee_code": row["outgoing_employee_code"],
        "outgoing_full_name": row["outgoing_full_name"],
        "incoming_employee_code": row["incoming_employee_code"],
        "incoming_full_name": row["incoming_full_name"],
        "rationale": row["rationale"],
        "executed_at": row["executed_at"],
        "execution_outcome": row["execution_outcome"],
        "verified_at": row["verified_at"],
        "verification_outcome": row["verification_outcome"],
    }


# --------------------------------------------------------------- registry
#
# The OpenAI tool-calling wire format (`chat.completions.create(tools=...)`)
# for every tool above, plus a lightweight argument validator and dispatcher
# `agent_service.py`'s bounded loop uses for BOTH the real model and a
# scripted fake one in tests - the same validation runs either way, so a
# test never exercises a looser path than production does.

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "find_employee",
            "description": (
                "Resolve a worker by exact employee code or a substring of "
                "their name. Returns status 'found', 'not_found', or "
                "'ambiguous' (with every match listed) - never guesses."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Employee code or (partial) name."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_shift",
            "description": (
                "Resolve a dated shift by its stable shift_id, or by hall + "
                "date (and optionally its exact start time). Returns "
                "status 'found', 'not_found', or 'ambiguous'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "shift_id": {"type": "integer"},
                    "hall": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time": {"type": "string", "description": "HH:MM, the shift's start time"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_shift_details",
            "description": "Read a shift's current assignments and whether it is covered.",
            "parameters": {
                "type": "object",
                "properties": {"shift_id": {"type": "integer"}},
                "required": ["shift_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_eligible_candidates",
            "description": (
                "List workers currently eligible to cover a shift, ranked "
                "deterministically (preferred > neutral > low, then lower "
                "projected weekly hours, then employee_code). "
                "exclude_employee_code removes the outgoing worker on a call-out."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "shift_id": {"type": "integer"},
                    "exclude_employee_code": {"type": "string"},
                },
                "required": ["shift_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_uncovered_shift",
            "description": (
                "Explain why a shift is or is not fully covered: current "
                "assignment count vs. required, eligible candidates, and a "
                "tally of why other workers are ineligible."
            ),
            "parameters": {
                "type": "object",
                "properties": {"shift_id": {"type": "integer"}},
                "required": ["shift_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_employee_hours",
            "description": (
                "One worker's assigned and remaining theoretical capacity "
                "hours for a requested Monday reporting week."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_code": {"type": "string"},
                    "week_start": {"type": "string", "description": "YYYY-MM-DD, a Monday"},
                },
                "required": ["employee_code", "week_start"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_replacement",
            "description": (
                "Create a PENDING proposal to assign incoming_employee_code "
                "to shift_id, replacing outgoing_employee_code if given (omit "
                "it to propose filling a currently-uncovered position - the "
                "shift must currently have fewer assigned workers than its "
                "required_staff, or this is refused). incoming_employee_code "
                "MUST be the first-ranked candidate returned by "
                "get_eligible_candidates for this exact shift (and exact "
                "exclude_employee_code, if replacing someone) - proposing "
                "anyone else is refused. There is no rationale argument: the "
                "explanation is computed server-side from the same "
                "deterministic ranking. Does NOT create a real assignment and "
                "cannot be approved or executed by this tool - a supervisor "
                "must approve it separately. At most one proposal is allowed "
                "per task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "shift_id": {"type": "integer"},
                    "outgoing_employee_code": {"type": "string"},
                    "incoming_employee_code": {"type": "string"},
                },
                "required": ["shift_id", "incoming_employee_code"],
            },
        },
    },
]

# name -> {field: (python_type, required)}. `int` deliberately excludes
# `bool` (Python's bool is an int subclass) via the explicit isinstance
# check in `_validate_arguments` below, the same guard `analytics.py`'s
# `workforce_scenario` uses for the same reason.
_ARG_SCHEMAS = {
    "find_employee": {"query": (str, True)},
    "find_shift": {
        "shift_id": (int, False),
        "hall": (str, False),
        "date": (str, False),
        "time": (str, False),
    },
    "get_shift_details": {"shift_id": (int, True)},
    "get_eligible_candidates": {
        "shift_id": (int, True),
        "exclude_employee_code": (str, False),
    },
    "inspect_uncovered_shift": {"shift_id": (int, True)},
    "get_employee_hours": {"employee_code": (str, True), "week_start": (str, True)},
    "propose_replacement": {
        "shift_id": (int, True),
        "outgoing_employee_code": (str, False),
        "incoming_employee_code": (str, True),
    },
}

# Tools whose handler needs context the MODEL must never supply itself
# (here: which task a proposal belongs to) - the loop injects these, and any
# same-named key the model puts in its own arguments is discarded before
# validation, never merged in, so a crafted tool-call argument can never
# attribute a proposal to a different task.
_CONTEXT_INJECTED_ARGS = {"propose_replacement": ("task_id",)}

_HANDLERS = {
    "find_employee": find_employee,
    "find_shift": find_shift,
    "get_shift_details": get_shift_details,
    "get_eligible_candidates": get_eligible_candidates,
    "inspect_uncovered_shift": inspect_uncovered_shift,
    "get_employee_hours": get_employee_hours,
    "propose_replacement": propose_replacement,
}


def _validate_arguments(name, arguments):
    if not isinstance(arguments, dict):
        raise ToolError("invalid_arguments", f"Arguments for '{name}' must be a JSON object.")
    schema = _ARG_SCHEMAS[name]
    allowed = set(schema) | set(_CONTEXT_INJECTED_ARGS.get(name, ()))
    unexpected = set(arguments) - allowed
    if unexpected:
        raise ToolError(
            "invalid_arguments",
            f"Unexpected argument(s) for '{name}': {', '.join(sorted(unexpected))}.",
        )
    cleaned = {}
    for field, (expected_type, required) in schema.items():
        if field not in arguments or arguments[field] is None:
            if required:
                raise ToolError("invalid_arguments", f"'{name}' requires '{field}'.")
            continue
        value = arguments[field]
        if expected_type is int and (isinstance(value, bool) or not isinstance(value, int)):
            raise ToolError("invalid_arguments", f"'{field}' must be an integer.")
        elif expected_type is not int and not isinstance(value, expected_type):
            raise ToolError(
                "invalid_arguments",
                f"'{field}' must be a {expected_type.__name__}.",
            )
        cleaned[field] = value
    return cleaned


def call_tool(connection, name, arguments, *, task_id=None):
    """Validate and dispatch one tool call. Never raises anything other
    than `ToolError` - every failure mode (unknown tool, malformed
    arguments, a record that does not exist, a candidate that turns out
    ineligible, or any OTHER unexpected failure inside a handler) is a
    controlled, structured result the caller can feed back to the model or
    surface to the supervisor, never a raw exception surfaced as an HTTP
    500. The final `except Exception` is deliberate defense in depth. It
    should never trigger - every known failure mode already raises a
    specific `ToolError` above it - but it is what makes the dispatcher's
    OWN stated contract actually true regardless of what a future tool
    handler might do, rather than true only for the failure modes anyone
    thought to check.
    """
    if name not in _ARG_SCHEMAS:
        raise ToolError("unknown_tool", f"Unknown tool '{name}'.")
    cleaned = _validate_arguments(name, arguments)
    for context_field in _CONTEXT_INJECTED_ARGS.get(name, ()):
        if context_field == "task_id":
            cleaned["task_id"] = task_id
    try:
        return _HANDLERS[name](connection, **cleaned)
    except ToolError:
        raise
    except Exception as error:  # noqa: BLE001 - deliberate final backstop, see docstring
        raise ToolError(
            "tool_execution_failed", f"'{name}' failed unexpectedly: {error}"
        ) from error
