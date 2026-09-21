"""Read-only coverage/workforce analytics for one reporting week (Phase 8).

Pure aggregation over already-stored records, reusing the same rules the
rest of the project already established rather than re-deriving them:

  - `weeks.parse_week_start` for the exact same Monday validation every
    other week-scoped endpoint uses (D025's single week-boundary rule).
  - `reporting.shift_duration_hours` / `reporting.assigned_hours_by_employee`
    for the same whole-hour, start-week accounting `scheduling.py` and the
    `/api/employees` endpoint already use.
  - `reporting.reporting_period_coverage` for the exact same "timetable
    ready for THIS displayed week" rule `eligibility.py`'s hard rules and
    `/api/employees` already require - never a second, looser readiness
    definition invented for a dashboard.

Nothing here writes anything. In particular, reading analytics for a week
nobody has prepared yet must never call `scheduling.prepare_week` - an
unprepared week is a completely valid, honest answer (zero stored shifts,
zero required positions, zero everything), not an error and not an implicit
side effect of looking at the dashboard.
"""

import math
from datetime import datetime, timedelta

from reporting import (
    assigned_hours_by_employee,
    remaining_capacity_hours,
    reporting_period_coverage,
    shift_duration_hours,
)
from synthetic_data import TIME_FORMAT
from weeks import DATE_FORMAT, parse_week_start, week_bounds, week_dates


def week_coverage_metrics(connection, week_start_text):
    """Coverage metrics for the stored shifts of one reporting week.

    Every figure here is capped/derived the same way the Phase 7 proposal
    review summary (`optimizer.py`) already computes overstaffing and
    uncovered positions, so a supervisor never sees the dashboard and the
    proposal review disagree about what "filled" or "excess" means.

    `filled_positions` is each shift's assigned count capped at its own
    `required_staff` - an overstaffed shift can never inflate the aggregate
    beyond what was actually required. The overflow is reported separately
    as `excess_assignments`, never silently dropped.

    `required_coverage_hours` = sum(duration x required_staff) over every
    stored shift. `scheduled_coverage_hours` = sum(duration x
    min(assigned_count, required_staff)) - the hours actually usable towards
    the requirement. `recorded_assignment_hours` = sum(duration x
    assigned_count), uncapped, so a supervisor can also see how many hours
    are recorded in total, excess included.

    No join against `employees.is_active` happens anywhere in this
    function - an assignment already recorded here counts exactly the same
    whether the worker holding it is active or not today. Deactivating a
    worker changes their eligibility for FUTURE assignments; it does not,
    and must not, erase what already happened this week from a historical
    coverage report.

    `coverage_percentage` is `scheduled_coverage_hours / required_coverage_hours
    * 100`, rounded to one decimal place. Zero required hours (nothing
    prepared, or a prepared week whose shifts all need zero staff, which
    cannot happen through `synthetic_data.generate_required_shifts` but is
    not assumed impossible here) defines the percentage as exactly 100.0 -
    there is nothing to cover and nothing is uncovered, which is the honest
    reading, not an undefined division or a misleading 0.
    """
    week_start = parse_week_start(week_start_text)
    _, week_end = week_bounds(week_start)
    display_end = week_end - timedelta(days=1)  # inclusive Sunday, not the exclusive next Monday

    shifts = connection.execute(
        "SELECT id, hall, start_datetime, end_datetime, required_staff FROM shifts"
        " WHERE start_datetime >= ? AND start_datetime < ?"
        " ORDER BY start_datetime, hall, id",
        (week_start.strftime(TIME_FORMAT), week_end.strftime(TIME_FORMAT)),
    ).fetchall()

    assigned_counts = {}
    shift_ids = [row["id"] for row in shifts]
    if shift_ids:
        placeholders = ",".join("?" for _ in shift_ids)
        for row in connection.execute(
            f"SELECT shift_id, COUNT(*) AS assigned_count FROM assignments"
            f" WHERE shift_id IN ({placeholders}) GROUP BY shift_id",
            shift_ids,
        ):
            assigned_counts[row["shift_id"]] = row["assigned_count"]

    required_positions = 0
    filled_positions = 0
    excess_assignments = 0
    unfilled_shift_count = 0
    required_coverage_hours = 0
    scheduled_coverage_hours = 0
    recorded_assignment_hours = 0

    for row in shifts:
        start = datetime.strptime(row["start_datetime"], TIME_FORMAT)
        end = datetime.strptime(row["end_datetime"], TIME_FORMAT)
        duration = shift_duration_hours(start, end, label=f"{row['id']} at {row['hall']}")
        required_staff = row["required_staff"]
        assigned_count = assigned_counts.get(row["id"], 0)
        filled = min(assigned_count, required_staff)
        excess = max(0, assigned_count - required_staff)

        required_positions += required_staff
        filled_positions += filled
        excess_assignments += excess
        if filled < required_staff:
            unfilled_shift_count += 1

        required_coverage_hours += duration * required_staff
        scheduled_coverage_hours += duration * filled
        recorded_assignment_hours += duration * assigned_count

    uncovered_positions = max(0, required_positions - filled_positions)
    coverage_percentage = (
        100.0
        if required_coverage_hours == 0
        else round(100 * scheduled_coverage_hours / required_coverage_hours, 1)
    )

    return {
        "week_start": week_start.strftime(DATE_FORMAT),
        "week_end": display_end.strftime(DATE_FORMAT),
        "shift_count": len(shifts),
        "required_positions": required_positions,
        "filled_positions": filled_positions,
        "uncovered_positions": uncovered_positions,
        "unfilled_shift_count": unfilled_shift_count,
        "excess_assignments": excess_assignments,
        "required_coverage_hours": required_coverage_hours,
        "scheduled_coverage_hours": scheduled_coverage_hours,
        "recorded_assignment_hours": recorded_assignment_hours,
        "coverage_percentage": coverage_percentage,
    }


def workforce_capacity_metrics(connection, week_start_text):
    """Current workforce counts, theoretical capacity, and per-worker rows.

    `timetable_ready` here is exactly `reporting.reporting_period_coverage`'s
    `scheduling_ready` flag for the displayed week - accepted (confirmed AND
    non-provisional) semester schedules covering all seven days - the same
    stricter fact `eligibility.py`'s `timetable_not_confirmed` rule actually
    requires. It is never equated with eligibility for a specific shift (a
    class or approved-leave conflict can still make a ready, active worker
    ineligible for one particular shift) and it is counted independently of
    active status, so both "timetable-ready" and "active AND timetable-ready"
    can be reported without one silently standing in for the other.

    Theoretical active capacity is `sum(weekly_hour_limit)` over active
    workers only - inactive workers contribute no theoretical capacity,
    since they are not schedulable regardless of their stored limit. Class
    hours and approved leave are deliberately NOT subtracted from anyone's
    capacity (`reporting.remaining_capacity_hours` already established this;
    reused verbatim here, not re-derived).

    Recorded assigned hours - both the active-only figure used for the
    active remaining-capacity figure, and the separate all-workers figure -
    include a currently INACTIVE worker's stored assignments for this week.
    Deactivating someone does not erase what they already worked.
    """
    week_start = parse_week_start(week_start_text)
    _, week_end = week_bounds(week_start)
    dates = week_dates(week_start)

    employees = connection.execute(
        "SELECT id, employee_code, full_name, is_active, weekly_hour_limit"
        " FROM employees ORDER BY employee_code"
    ).fetchall()
    schedules = connection.execute(
        "SELECT employee_id, start_date, end_date, confirmed_at, dates_provisional"
        " FROM semester_schedules"
    ).fetchall()
    readiness = reporting_period_coverage(schedules, dates)
    assigned_hours = assigned_hours_by_employee(
        connection, week_start=week_start, week_end=week_end
    )

    total_workers = 0
    active_workers = 0
    timetable_ready_workers = 0
    active_and_ready_workers = 0
    active_theoretical_capacity_hours = 0
    active_assigned_hours = 0
    recorded_assigned_hours_all_workers = 0
    worker_rows = []

    for employee in employees:
        total_workers += 1
        is_active = bool(employee["is_active"])
        ready = readiness.get(
            employee["id"], {"scheduling_ready": False}
        )["scheduling_ready"]
        assigned = assigned_hours.get(employee["id"], 0)
        remaining = remaining_capacity_hours(employee["weekly_hour_limit"], assigned)
        utilization_percentage = (
            round(100 * assigned / employee["weekly_hour_limit"], 1)
            if employee["weekly_hour_limit"] > 0
            else 0.0
        )

        if is_active:
            active_workers += 1
            active_theoretical_capacity_hours += employee["weekly_hour_limit"]
            active_assigned_hours += assigned
        if ready:
            timetable_ready_workers += 1
        if is_active and ready:
            active_and_ready_workers += 1
        recorded_assigned_hours_all_workers += assigned

        worker_rows.append(
            {
                "employee_code": employee["employee_code"],
                "full_name": employee["full_name"],
                "is_active": is_active,
                "scheduling_ready": ready,
                "weekly_hour_limit": employee["weekly_hour_limit"],
                "assigned_hours": assigned,
                "remaining_capacity_hours": remaining,
                "utilization_percentage": utilization_percentage,
            }
        )

    return {
        "total_workers": total_workers,
        "active_workers": active_workers,
        "timetable_ready_workers": timetable_ready_workers,
        "active_and_timetable_ready_workers": active_and_ready_workers,
        "active_theoretical_capacity_hours": active_theoretical_capacity_hours,
        "active_assigned_hours": active_assigned_hours,
        "theoretical_remaining_active_capacity_hours": max(
            0, active_theoretical_capacity_hours - active_assigned_hours
        ),
        "recorded_assigned_hours_all_workers": recorded_assigned_hours_all_workers,
        "workers": worker_rows,
    }


def week_analytics(connection, week_start_text):
    """The full analytics payload for one reporting week: coverage + workforce.

    The single function `main.py`'s new `/api/analytics/weeks/{week_start}`
    route calls. Both halves validate the same `week_start` through the same
    `weeks.parse_week_start` call other week-scoped endpoints already use,
    so a malformed date or non-Monday is rejected identically everywhere -
    including here, where it is validated once and reused, rather than
    twice with a chance of disagreeing.
    """
    parse_week_start(week_start_text)  # raise before doing any work, once
    return {
        "coverage": week_coverage_metrics(connection, week_start_text),
        "workforce": workforce_capacity_metrics(connection, week_start_text),
    }


def workforce_scenario(required_coverage_hours, hypothetical_worker_count, weekly_hours_per_worker):
    """Aggregate-only workforce-size scenario math (Workforce Planning).

    Explicit supervisor inputs only - `hypothetical_worker_count` and
    `weekly_hours_per_worker` are never inferred, generated, or defaulted
    from current staffing. This is deliberately the ONLY thing this function
    computes: an aggregate hour comparison. It does not, and cannot, prove a
    feasible schedule exists - specific workers' classes, approved leave,
    overlapping shifts, and per-shift availability are not modeled at all.
    Callers must present `theoretical_minimum_workers` and
    `capacity_sufficient` as a lower bound only, never as a feasible
    workforce size or a hiring/firing recommendation (see
    `docs/PROJECT_SPEC.md`'s Workforce Planning section and D051).

    `theoretical_minimum_workers` is `ceil(required_coverage_hours /
    weekly_hours_per_worker)` when both inputs make that meaningful:
      - `required_coverage_hours <= 0` needs no workers at all: 0.
      - `weekly_hours_per_worker <= 0` can never accumulate any positive
        required hours no matter how many such workers exist: `None`
        (mathematically undefined, not zero and not infinite - reported to
        the caller as "cannot be computed", never silently coerced to 0).

    Raises `ValueError` for a negative worker count or negative weekly
    hours - both are nonsensical supervisor inputs, not simply small ones -
    and for a fractional worker count (2.5 hypothetical workers is not a
    meaningful input, unlike weekly hours per worker, which may legitimately
    be fractional).
    """
    if isinstance(hypothetical_worker_count, bool) or not isinstance(
        hypothetical_worker_count, (int, float)
    ):
        raise ValueError("hypothetical_worker_count must be a whole number")
    if hypothetical_worker_count != int(hypothetical_worker_count):
        raise ValueError("hypothetical_worker_count must be a whole number")
    if hypothetical_worker_count < 0:
        raise ValueError("hypothetical_worker_count must not be negative")
    if weekly_hours_per_worker < 0:
        raise ValueError("weekly_hours_per_worker must not be negative")

    hypothetical_capacity_hours = hypothetical_worker_count * weekly_hours_per_worker
    capacity_gap_hours = hypothetical_capacity_hours - required_coverage_hours

    if required_coverage_hours <= 0:
        theoretical_minimum_workers = 0
    elif weekly_hours_per_worker <= 0:
        theoretical_minimum_workers = None
    else:
        theoretical_minimum_workers = math.ceil(required_coverage_hours / weekly_hours_per_worker)

    return {
        "required_coverage_hours": required_coverage_hours,
        "hypothetical_worker_count": hypothetical_worker_count,
        "weekly_hours_per_hypothetical_worker": weekly_hours_per_worker,
        "hypothetical_theoretical_capacity_hours": hypothetical_capacity_hours,
        "capacity_gap_hours": capacity_gap_hours,
        "theoretical_minimum_workers": theoretical_minimum_workers,
        "capacity_sufficient": hypothetical_capacity_hours >= required_coverage_hours,
    }
