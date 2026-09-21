"""Read-only draft schedule optimization (Phase 7 increment 2).

Deterministically proposes workers for currently UNFILLED required-staff
positions in one already-prepared Monday week, using Google OR-Tools CP-SAT.

**This module performs no database writes and stores nothing.** Only
`SELECT` statements are ever issued here; a future increment owns binding a
draft into a real proposal, approval, and persisted assignments. Every
existing assignment is preserved exactly as stored - this module only ever
adds proposals for positions that are still open (`required_staff` not yet
met), and never removes, replaces or second-guesses one that already exists.

**Hard eligibility is never re-implemented here.** Every hard rule (active
status, confirmed/non-provisional timetable coverage, class conflict, leave
conflict, conflict with an EXISTING assignment, and the weekly-hour limit
against currently stored hours) is evaluated by reusing
`eligibility.evaluate_shift_eligibility` directly - the same function Phase
6's Coverage screen uses - so this module cannot silently drift from or
weaken that contract. Two further hard constraints exist only because a
draft reasons about many shifts AT ONCE, which one `evaluate_shift_eligibility`
call cannot see on its own:

1. Two shifts proposed to the SAME worker in this draft must not overlap in
   time (an existing-vs-existing or existing-vs-proposed overlap is already
   caught by `assignment_conflict` inside eligibility itself).
2. The sum of every shift's hours proposed to one worker in this draft, on
   top of their already-stored hours for the week, must not exceed their
   `weekly_hour_limit` (eligibility only proves that any ONE candidate shift
   alone would not exceed it against currently stored hours; several
   candidate shifts proposed together could still jointly exceed it).

**Objective, solved as three SEQUENTIAL CP-SAT passes over the same model,
each one's proven optimum fixed as a constraint before the next runs - not a
single pass with fixed weights, which could only ever approximate strict
priority rather than guarantee it:**

1. Maximize the number of newly filled positions.
2. With that maximum fixed exactly, maximize total preference
   (`preferred` > `neutral` > `low`). A low-preference worker is still
   proposed when they are the only way to reach tier 1's maximum, because
   tier 1 is fixed before tier 2 ever runs.
3. With both of those fixed exactly, minimize the highest total projected
   weekly hours any one worker ends up with (a standard, simple "balance the
   load" proxy - it can only choose among the already-optimal-coverage,
   already-optimal-preference solutions; it can never trade either away for
   a flatter spread).

**Every tier must solve to a proven `OPTIMAL` status.** If any tier returns
anything else - `FEASIBLE` (a time or search limit was hit before optimality
was proven), `INFEASIBLE`, `UNKNOWN`, or any other non-optimal outcome -
`DraftNotOptimal` is raised rather than returning a draft built from an
unproven or partial solution. A caller must never be handed something that
looks like an optimized draft but is not proven to be one.

**Deterministic by construction.** Employees are read in `employee_code`
order and shifts in `(start_datetime, hall, id)` order when the CP-SAT model
is built, and the solver is configured to run single-threaded with a fixed
seed for every one of the three passes, so identical stored data always
produces an identical draft.
"""

from datetime import datetime

from ortools.sat.python import cp_model

from eligibility import evaluate_shift_eligibility
from reporting import assigned_hours_by_employee, shift_duration_hours
from synthetic_data import TIME_FORMAT
from weeks import DATE_FORMAT, parse_week_start, week_bounds

# Preference is a fact, never a hard rule (D022) - this is purely the
# tier-2 objective's per-assignment score.
PREFERENCE_WEIGHT = {"preferred": 2, "neutral": 1, "low": 0}


class WeekNotPrepared(LookupError):
    """No shifts are stored for the requested week. Maps to HTTP 409."""


class DraftNotOptimal(RuntimeError):
    """A required optimization tier did not solve to a proven OPTIMAL
    status. Maps to HTTP 503 - a controlled refusal, never a draft built from
    an unproven or partial solution."""


def _solve_tier(model, solver, sense, expression):
    """Solve one objective tier and require a proven OPTIMAL status.

    Adding the objective and solving are combined here so every call site
    looks the same, and so a non-OPTIMAL result is caught at the moment it
    happens rather than discovered later while trying to read a value.
    """
    if sense == "max":
        model.Maximize(expression)
    else:
        model.Minimize(expression)
    status = solver.Solve(model)
    if status != cp_model.OPTIMAL:
        raise DraftNotOptimal(
            "The optimizer could not prove an optimal solution for this "
            f"week (solver status: {solver.StatusName(status)}). No draft "
            "was produced rather than returning an unproven or partial one."
        )
    return solver.Value(expression)


def _parse(datetime_text):
    return datetime.strptime(datetime_text, TIME_FORMAT)


def _overlaps(a_start, a_end, b_start, b_end):
    """D025's touching-endpoint convention: ending exactly when another
    starts is not an overlap."""
    return a_start < b_end and b_start < a_end


def _week_shifts(connection, week_start, week_end):
    return connection.execute(
        "SELECT id, hall, start_datetime, end_datetime, required_staff FROM shifts"
        " WHERE start_datetime >= ? AND start_datetime < ?"
        " ORDER BY start_datetime, hall, id",
        (week_start.strftime(TIME_FORMAT), week_end.strftime(TIME_FORMAT)),
    ).fetchall()


def _existing_assignments_by_shift(connection, shift_ids):
    """Every existing assignment for the given shifts, grouped by shift id.

    Ordered by shift, then employee_code, then employee id, so the same
    stored data always comes back in the same order regardless of insertion
    order or how SQLite happens to walk the join - the same determinism
    guarantee `scheduling.get_week_schedule`'s `assigned_employees` already
    makes.
    """
    if not shift_ids:
        return {}
    placeholders = ",".join("?" for _ in shift_ids)
    by_shift = {}
    for row in connection.execute(
        f"""
        SELECT a.shift_id, e.id AS employee_id, e.employee_code, e.full_name
        FROM assignments a
        JOIN employees e ON e.id = a.employee_id
        WHERE a.shift_id IN ({placeholders})
        ORDER BY a.shift_id, e.employee_code, e.id
        """,
        shift_ids,
    ):
        by_shift.setdefault(row["shift_id"], []).append(
            {
                "employee_id": row["employee_id"],
                "employee_code": row["employee_code"],
                "full_name": row["full_name"],
            }
        )
    return by_shift


def _uncovered_reason(
    remaining_needed, eligible_ids, proposed_ids, reason_tally, total_employees
):
    """A useful, structured, and TRUTHFUL reason a position stayed uncovered.

    Four distinct answers, checked in this order, because they are different
    claims and conflating them would mislead a supervisor reading this:

    1. `capacity_allocated_elsewhere` - an eligible worker existed and was
       NOT proposed here, because the optimizer used them on a different
       shift to maximize total coverage. A normal, expected outcome, not a
       data problem.
    2. `insufficient_eligible_workers` - at least one worker is eligible
       (and every eligible one WAS already proposed, here or elsewhere), but
       there simply are not enough distinct eligible people to fill every
       remaining position this shift still needs (`required_staff > 1`).
       Never confused with "nobody is eligible": somebody is, just not
       enough of them.
    3. `no_eligible_workers` with a reason-code tally - other workers exist
       and were evaluated, but every one of them failed at least one hard
       rule.
    4. `no_eligible_workers` with a plain detail - the degenerate case where
       there was nobody left to evaluate at all (every other worker in the
       system is already assigned to this very shift), worded to never
       falsely claim "no workers exist" when the system, or this shift's own
       `existing_assignments`, plainly has some.
    """
    remaining_eligible = eligible_ids - proposed_ids
    if remaining_eligible:
        return {
            "reason_code": "capacity_allocated_elsewhere",
            "detail": (
                f"{len(remaining_eligible)} eligible worker(s) were available "
                "but assigned to other shifts to maximize total coverage."
            ),
        }
    if eligible_ids:
        return {
            "reason_code": "insufficient_eligible_workers",
            "detail": (
                f"This shift still needs {remaining_needed} more distinct "
                f"worker(s), but only {len(eligible_ids)} eligible worker(s) "
                "exist for it."
            ),
        }
    if reason_tally:
        return {
            "reason_code": "no_eligible_workers",
            "detail": "No remaining worker satisfies every hard eligibility rule for this shift.",
            "reason_counts": dict(sorted(reason_tally.items())),
        }
    if total_employees:
        return {
            "reason_code": "no_eligible_workers",
            "detail": "No other worker is available to evaluate for the remaining position(s).",
        }
    return {
        "reason_code": "no_eligible_workers",
        "detail": "No workers are registered in the system.",
    }


def generate_draft(connection, week_start_text):
    """Compute (never store) a coverage-maximizing draft for one Monday week.

    Raises `weeks.InvalidWeekStart` for a malformed or non-Monday value, and
    `WeekNotPrepared` if the week has no stored shifts at all - callers must
    prepare the week first (`scheduling.prepare_week`), which this function
    never does on its own. Every query here is a `SELECT`; nothing is
    inserted, updated or deleted.
    """
    week_start = parse_week_start(week_start_text)
    _, week_end = week_bounds(week_start)

    shifts = _week_shifts(connection, week_start, week_end)
    if not shifts:
        raise WeekNotPrepared(
            f"No shifts are prepared for the week of {week_start.strftime(DATE_FORMAT)}. "
            "Prepare the week first with POST /api/schedule/weeks/{week_start}/prepare."
        )

    employees = connection.execute(
        "SELECT id, employee_code, full_name, is_active, weekly_hour_limit"
        " FROM employees ORDER BY employee_code"
    ).fetchall()
    employee_by_id = {row["id"]: row for row in employees}

    shift_ids = [row["id"] for row in shifts]
    existing_by_shift = _existing_assignments_by_shift(connection, shift_ids)
    existing_hours = assigned_hours_by_employee(connection, week_start, week_end)

    duration_by_shift = {}
    for shift in shifts:
        start = _parse(shift["start_datetime"])
        end = _parse(shift["end_datetime"])
        duration_by_shift[shift["id"]] = shift_duration_hours(
            start, end, label=f"{shift['id']} at {shift['hall']}"
        )

    open_shifts = [
        row
        for row in shifts
        if len(existing_by_shift.get(row["id"], [])) < row["required_staff"]
    ]

    # For every still-open shift, the base hard-eligible candidates (already-
    # assigned workers are excluded - they cannot fill their own shift's
    # remaining position again) and a tally of why everyone else was excluded.
    eligible_by_shift = {}
    reason_tally_by_shift = {}
    for shift in open_shifts:
        already_assigned_ids = {
            worker["employee_id"] for worker in existing_by_shift.get(shift["id"], [])
        }
        candidates = {}
        tally = {}
        for employee in employees:
            if employee["id"] in already_assigned_ids:
                continue
            result = evaluate_shift_eligibility(connection, shift, employee)
            if result["eligible"]:
                candidates[employee["id"]] = result
            else:
                for code in result["reason_codes"]:
                    tally[code] = tally.get(code, 0) + 1
        eligible_by_shift[shift["id"]] = candidates
        reason_tally_by_shift[shift["id"]] = tally

    # ---------------------------------------------------------- CP-SAT model
    model = cp_model.CpModel()

    # (employee_id, shift_id) -> BoolVar, created only for pairs that passed
    # base eligibility, in stable employee_code/shift order.
    x = {}
    for shift in open_shifts:
        candidates = eligible_by_shift[shift["id"]]
        for employee in employees:
            if employee["id"] not in candidates:
                continue
            x[(employee["id"], shift["id"])] = model.NewBoolVar(
                f"x_{employee['id']}_{shift['id']}"
            )

    # A shift's open position count may never be exceeded by its proposals.
    for shift in open_shifts:
        remaining = shift["required_staff"] - len(existing_by_shift.get(shift["id"], []))
        variables = [
            x[(employee["id"], shift["id"])]
            for employee in employees
            if (employee["id"], shift["id"]) in x
        ]
        if variables:
            model.Add(sum(variables) <= remaining)

    # Per-employee: no two proposed shifts may overlap, and the cumulative
    # hours of every shift proposed to them, plus their already-stored
    # hours, may not exceed their weekly limit.
    for employee in employees:
        own_pairs = [(shift_id, var) for (eid, shift_id), var in x.items() if eid == employee["id"]]
        if not own_pairs:
            continue

        for i in range(len(own_pairs)):
            shift_id_a, var_a = own_pairs[i]
            shift_a = next(s for s in shifts if s["id"] == shift_id_a)
            for j in range(i + 1, len(own_pairs)):
                shift_id_b, var_b = own_pairs[j]
                shift_b = next(s for s in shifts if s["id"] == shift_id_b)
                if _overlaps(
                    _parse(shift_a["start_datetime"]),
                    _parse(shift_a["end_datetime"]),
                    _parse(shift_b["start_datetime"]),
                    _parse(shift_b["end_datetime"]),
                ):
                    model.Add(var_a + var_b <= 1)

        load_terms = [duration_by_shift[shift_id] * var for shift_id, var in own_pairs]
        model.Add(
            existing_hours.get(employee["id"], 0) + sum(load_terms)
            <= employee["weekly_hour_limit"]
        )

    # Tier 3's auxiliary variable: the highest total projected weekly hours
    # any single candidate worker ends up with, bounded by the largest
    # weekly_hour_limit in the workforce so it can never be forced above
    # what any worker could legally reach.
    max_possible_hours = max((row["weekly_hour_limit"] for row in employees), default=0)
    max_load = model.NewIntVar(0, max(max_possible_hours, 0), "max_load")
    any_candidate = bool(x)
    for employee in employees:
        own_pairs = [(shift_id, var) for (eid, shift_id), var in x.items() if eid == employee["id"]]
        if not own_pairs:
            continue
        load_terms = [duration_by_shift[shift_id] * var for shift_id, var in own_pairs]
        model.Add(existing_hours.get(employee["id"], 0) + sum(load_terms) <= max_load)

    proposed_pairs = set()
    if any_candidate:
        solver = cp_model.CpSolver()
        # Single-threaded with a fixed seed for every one of the three
        # passes: identical input must always produce an identical draft,
        # never a different-but-equally-optimal one.
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        solver.parameters.max_time_in_seconds = 30

        # Tier 1: maximize filled positions. Fix that exact value before
        # tier 2 ever runs, so tier 2 can only choose AMONG the
        # already-maximal-coverage solutions, never trade coverage away.
        filled_expression = sum(x.values())
        filled_optimum = _solve_tier(model, solver, "max", filled_expression)
        model.Add(filled_expression == filled_optimum)

        # Tier 2: maximize total preference, then fix it the same way before
        # tier 3 runs.
        preference_expression = sum(
            PREFERENCE_WEIGHT[eligible_by_shift[shift_id][employee_id]["preference"]] * var
            for (employee_id, shift_id), var in x.items()
        )
        preference_optimum = _solve_tier(model, solver, "max", preference_expression)
        model.Add(preference_expression == preference_optimum)

        # Tier 3: minimize the highest projected load, among only the
        # solutions that already achieve both fixed optima above.
        _solve_tier(model, solver, "min", max_load)

        for key, var in x.items():
            if solver.Value(var) == 1:
                proposed_pairs.add(key)

    proposed_hours_by_employee = {}
    for employee_id, shift_id in proposed_pairs:
        proposed_hours_by_employee[employee_id] = (
            proposed_hours_by_employee.get(employee_id, 0) + duration_by_shift[shift_id]
        )

    # ------------------------------------------------------------- response
    #
    # A shift's filled count is capped at its own required_staff: an
    # overstaffed shift (more existing assignments than required, which the
    # optimizer never causes but stored data can already contain) must never
    # inflate `filled_count`, `covered`, or the summary's totals beyond what
    # the shift actually needs. Every existing assignment is still returned
    # in full in `existing_assignments`; the excess is reported explicitly
    # in `excess_assignments` instead of being silently absorbed or lost.
    shift_payloads = []
    total_required = 0
    total_existing_filled = 0
    total_proposed = 0
    total_excess = 0

    for shift in shifts:
        existing = existing_by_shift.get(shift["id"], [])
        required_staff = shift["required_staff"]
        proposed_ids = [eid for (eid, sid) in proposed_pairs if sid == shift["id"]]
        proposed = []
        for employee_id in sorted(
            proposed_ids, key=lambda eid: employee_by_id[eid]["employee_code"]
        ):
            result = eligible_by_shift[shift["id"]][employee_id]
            employee = employee_by_id[employee_id]
            proposed.append(
                {
                    "employee_id": employee_id,
                    "employee_code": employee["employee_code"],
                    "full_name": employee["full_name"],
                    "preference": result["preference"],
                    "assigned_hours_before": existing_hours.get(employee_id, 0),
                    "projected_hours_after": existing_hours.get(employee_id, 0)
                    + proposed_hours_by_employee.get(employee_id, 0),
                }
            )

        existing_filled = min(len(existing), required_staff)
        raw_filled = len(existing) + len(proposed)
        filled_count = min(required_staff, raw_filled)
        excess_assignments = max(0, len(existing) - required_staff)
        uncovered_positions = max(0, required_staff - filled_count)

        payload = {
            "id": shift["id"],
            "hall": shift["hall"],
            "start_datetime": shift["start_datetime"],
            "end_datetime": shift["end_datetime"],
            "duration_hours": duration_by_shift[shift["id"]],
            "required_staff": required_staff,
            "existing_assignments": existing,
            "proposed_assignments": proposed,
            "filled_count": filled_count,
            "excess_assignments": excess_assignments,
            "covered": uncovered_positions == 0,
            "uncovered_positions": uncovered_positions,
        }
        if uncovered_positions > 0:
            payload["uncovered_reasons"] = [
                _uncovered_reason(
                    uncovered_positions,
                    set(eligible_by_shift.get(shift["id"], {})),
                    set(proposed_ids),
                    reason_tally_by_shift.get(shift["id"], {}),
                    len(employees),
                )
            ]

        shift_payloads.append(payload)
        total_required += required_staff
        total_existing_filled += existing_filled
        total_proposed += len(proposed)
        total_excess += excess_assignments

    total_filled = total_existing_filled + total_proposed
    return {
        "week_start": week_start.strftime(DATE_FORMAT),
        "week_end": week_end.strftime(DATE_FORMAT),
        "status": "complete" if total_filled >= total_required else "partial",
        "shifts": shift_payloads,
        "summary": {
            "required_positions": total_required,
            "existing_filled_positions": total_existing_filled,
            "proposed_filled_positions": total_proposed,
            "total_filled_positions": total_filled,
            "uncovered_positions": max(0, total_required - total_filled),
            "excess_assignments": total_excess,
        },
    }
