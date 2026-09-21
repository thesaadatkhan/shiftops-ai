from datetime import timedelta
from typing import Annotated, Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from database import ensure_schema, get_connection
from employees import (
    CodeAllocationError,
    DeactivationBlocked,
    DeletionBlocked,
    DuplicateEmployeeCode,
    EmployeeNotFound,
    EmployeeValidationError,
    create_employee,
    delete_employee,
    find_by_code,
    set_active,
    update_employee,
)
from timetables import (
    TimetableConflict,
    TimetableNotFound,
    TimetableValidationError,
    confirm_schedule,
    create_block,
    create_schedule,
    delete_block,
    delete_schedule,
    update_block,
    update_schedule,
)
from preferences import (
    LeaveConflict,
    LeaveNotFound,
    LeaveValidationError,
    PreferenceValidationError,
    ShiftNotFound,
    add_leave,
    delete_leave,
    set_preference,
    update_leave,
)
from reporting import (
    InvalidWorkDuration,
    assigned_hours_by_employee,
    assigned_hours_for_employee,
    minutes_between,
    remaining_capacity_hours,
    reporting_period_coverage,
    schedule_covered_days,
)
from eligibility import (
    ShiftNotFound as CoverageShiftNotFound,
    UnknownExcludedWorker,
    shift_coverage,
)
from optimizer import DraftNotOptimal, WeekNotPrepared, generate_draft
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
from analytics import week_analytics
from scheduling import get_week_schedule, prepare_week
from synthetic_data import WEEK_START
import weeks

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    # POST/PUT for creating and editing, DELETE for permanent removal. The
    # JSON bodies POST/PUT carry make Content-Type a non-simple header, so it
    # must be allowed too.
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)

ensure_schema()


@app.get("/api/health")
def health_check():
    return {"status": "ok"}


@app.get("/api/employees")
def list_employees(week_start: str | None = None):
    """Workforce summary for one reporting week.

    No `week_start` keeps today's exact default behavior: the fixed sample
    week (2026-10-05). Supplying one reports assigned hours and semester-
    aware class summaries for a different Monday week instead; it must be a
    real, strictly-formatted Monday, or this is the usual 400/string-detail
    shape (D033). Selecting or reading a week never writes anything -
    preparing required shifts for it is the separate, explicit
    `POST /api/schedule/weeks/{week_start}/prepare`. Employee identity,
    active status and stored records are unaffected either way.
    """
    if week_start is None:
        active_week_start = WEEK_START
    else:
        try:
            active_week_start = weeks.parse_week_start(week_start)
        except weeks.InvalidWeekStart as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    active_week_end = active_week_start + timedelta(days=7)
    active_week_dates = weeks.week_dates(active_week_start)

    connection = get_connection()
    try:
        employees = connection.execute(
            """
            SELECT
                e.id,
                e.employee_code,
                e.full_name,
                e.student_type,
                e.weekly_hour_limit,
                e.is_active,
                (SELECT COUNT(*) FROM approved_leave l
                  WHERE l.employee_id = e.id) AS approved_leave_count
            FROM employees e
            ORDER BY e.employee_code
            """
        ).fetchall()

        # Every block, with the semester it belongs to. Which of them
        # actually happen in the reporting week is decided per block below,
        # from the date that block falls on - not from whether the semester
        # merely overlaps the week somewhere.
        blocks = connection.execute(
            """
            SELECT s.employee_id, s.start_date, s.end_date,
                   b.day_of_week, b.start_time, b.end_time
            FROM class_blocks b
            JOIN semester_schedules s ON s.id = b.schedule_id
            """
        ).fetchall()

        schedules = connection.execute(
            "SELECT employee_id, start_date, end_date, confirmed_at, dates_provisional"
            " FROM semester_schedules"
        ).fetchall()

        try:
            assigned_hours = assigned_hours_by_employee(
                connection, week_start=active_week_start, week_end=active_week_end
            )
        except InvalidWorkDuration as error:
            # Stored shift data violates the whole-hour work rule. Report it
            # instead of returning a capacity figure derived from bad data.
            raise HTTPException(
                status_code=500,
                detail=f"Stored shift data is invalid: {error}",
            ) from error
    finally:
        connection.close()

    meeting_counts = {}
    class_minutes = {}
    for block in blocks:
        # The date this recurring class actually falls on inside the
        # reporting week. A Monday class in a semester that starts on the
        # Wednesday of that week does NOT happen this week, even though the
        # semester overlaps the week - which is exactly what comparing whole
        # ranges got wrong.
        occurrence = active_week_dates[block["day_of_week"]]
        if not (block["start_date"] <= occurrence <= block["end_date"]):
            continue

        employee_id = block["employee_id"]
        meeting_counts[employee_id] = meeting_counts.get(employee_id, 0) + 1
        # Class blocks keep their real lengths (75 and 165 minutes in the
        # demo data), so class hours stay fractional. The whole-hour rule
        # applies to work shifts only.
        class_minutes[employee_id] = class_minutes.get(employee_id, 0) + minutes_between(
            block["start_time"], block["end_time"]
        )

    coverage = reporting_period_coverage(schedules, active_week_dates)

    def employee_payload(employee):
        assigned = assigned_hours.get(employee["id"], 0)
        # Theoretical unused work capacity only. Class hours and approved
        # leave are NOT subtracted: this is not an eligibility calculation.
        remaining = remaining_capacity_hours(employee["weekly_hour_limit"], assigned)
        return {
            "employee_code": employee["employee_code"],
            "full_name": employee["full_name"],
            "student_type": employee["student_type"],
            "is_active": bool(employee["is_active"]),
            "weekly_hour_limit": employee["weekly_hour_limit"],
            "class_block_count": meeting_counts.get(employee["id"], 0),
            "weekly_class_hours": round(class_minutes.get(employee["id"], 0) / 60, 2),
            # Readiness for the DISPLAYED reporting week, in five states -
            # missing, outside_period, unconfirmed, partial, confirmed. See
            # reporting_period_coverage() for what each one means and why
            # they are deliberately not collapsed. Independent of active
            # status.
            "timetable_status": coverage.get(
                employee["id"], {"status": "missing", "scheduling_ready": False}
            )["status"],
            # The stricter fact eligibility.py actually requires - confirmed
            # AND non-provisional dates covering every day of this week, not
            # merely "confirmed" - so a provisional-but-confirmed worker (see
            # reporting_period_coverage()) is not mistaken for one who is
            # actually available. Phase 8 readiness counts must use this
            # field, not `timetable_status == "confirmed"` alone.
            "scheduling_ready": coverage.get(
                employee["id"], {"status": "missing", "scheduling_ready": False}
            )["scheduling_ready"],
            "approved_leave_count": employee["approved_leave_count"],
            # Whole hours: work shifts are whole-hour blocks, so these
            # totals never need rounding.
            "assigned_hours": assigned,
            "remaining_capacity_hours": remaining,
        }

    return {
        # The reporting week the capacity figures describe: the sample week
        # by default, or the requested `week_start`.
        "week_start": active_week_start.strftime("%Y-%m-%d"),
        # active_week_end is the exclusive following-Monday boundary; show
        # the Sunday instead.
        "week_end": (active_week_end - timedelta(days=1)).strftime("%Y-%m-%d"),
        "employees": [employee_payload(employee) for employee in employees],
    }


def week_coverage_state(covered_days, week_dates):
    """How much of the displayed reporting week a set of days accounts for.

    Three states, because "covers the week" and "touches the week" are not
    the same claim and review found the earlier boolean blurring them:

      outside - none of the seven days.
      partial - some of them, but not all seven. A semester that starts on
                the Wednesday overlaps this week without covering it.
      full    - all seven days.

    This describes DATES ONLY. It says nothing about whether anybody
    confirmed the timetable, which is a separate field, and nothing about the
    employee's combined readiness across all their semesters.
    """
    if not covered_days:
        return "outside"
    if len(covered_days) < len(week_dates):
        return "partial"
    return "full"


def semester_payload(schedule, blocks_by_schedule, week_dates):
    """One stored semester schedule, with the class blocks it owns.

    Three independent facts, deliberately not merged:

      `confirmed_at`      - whether anybody confirmed THIS semester, and when.
      `reporting_week_coverage` - how much of the displayed week its DATES
                            reach: outside, partial or full. Nothing to do
                            with confirmation.
      the employee's `timetable_status` (elsewhere in this payload) - their
                            combined readiness across every semester they have.

    A semester confirmed for the spring is genuinely confirmed and still lies
    wholly outside October; two unconfirmed half-semesters can cover the whole
    week between them without either covering it alone. Collapsing any pair of
    these into one field loses a distinction somebody needs.

    `dates_provisional` is derived from stored provenance, never guessed. The
    migration is the only thing that writes a `source_note`, and it assigns
    every schedule it creates the same assumed demo-semester dates because the
    legacy model recorded none. A block carrying a migration note is therefore
    the only evidence the database holds that these dates were assumed rather
    than entered by a supervisor.

    The notes themselves are NOT returned. They are internal provenance
    strings, and a supervisor needs to know that the dates want checking, not
    to read the sentence the migration happened to write. Keeping them out
    also stops product wording being coupled to their exact stored text.
    """
    blocks = blocks_by_schedule.get(schedule["id"], [])
    return {
        # Ids are exposed because the editing routes address records by them.
        # They are internal database keys, not a second public identifier for
        # a worker - that is still the employee code.
        "id": schedule["id"],
        "start_date": schedule["start_date"],
        "end_date": schedule["end_date"],
        "confirmed_at": schedule["confirmed_at"],
        "reporting_week_coverage": week_coverage_state(
            schedule_covered_days(schedule, week_dates), week_dates
        ),
        "dates_provisional": bool(schedule["dates_provisional"]),
        "class_blocks": [
            {
                "id": block["id"],
                "day_of_week": block["day_of_week"],
                "start_time": block["start_time"],
                "end_time": block["end_time"],
                # Classes keep their real lengths, so these stay fractional.
                # The whole-hour rule is about work shifts only.
                "hours": round(
                    minutes_between(block["start_time"], block["end_time"]) / 60, 2
                ),
            }
            for block in blocks
        ],
    }


def employee_detail_payload(connection, employee_code, week_start_text=None):
    """Everything stored about one worker, read-only.

    Every query filters on this worker's own internal id - the class blocks
    reach it through their semester schedule - so no other worker's records
    can appear here. Nothing is created, updated or deleted: reading a
    worker's details must never be a write.

    Legacy `courses` and `class_meetings` are deliberately NOT returned. They
    are retained provenance for the migration, not a timetable; exposing them
    here would present a second, contradictory set of class times.

    `week_start_text` is the same shared reporting week `GET /api/employees`
    already accepts (Codex review finding 4: this used to always compute
    assigned hours, remaining capacity, semester coverage and timetable
    status for the fixed sample week, disagreeing with whatever week
    Employees/Schedule had selected). Omitting it keeps the exact former
    default - the fixed sample week - unchanged.
    """
    employee = find_by_code(connection, employee_code)
    if employee is None:
        raise EmployeeNotFound(f"No employee with code {employee_code}.")

    employee_id = employee["id"]

    if week_start_text is None:
        active_week_start = WEEK_START
    else:
        active_week_start = weeks.parse_week_start(week_start_text)
    active_week_end = active_week_start + timedelta(days=7)
    active_week_dates = weeks.week_dates(active_week_start)

    schedules = connection.execute(
        "SELECT id, employee_id, start_date, end_date, confirmed_at,"
        " dates_provisional"
        " FROM semester_schedules WHERE employee_id = ?"
        " ORDER BY start_date, end_date, id",
        (employee_id,),
    ).fetchall()

    # Ordered here rather than in the frontend so the sequence is the same for
    # every caller: by weekday, then by time of day, then by insertion order
    # so two identical blocks keep a stable relative position.
    blocks = connection.execute(
        """
        SELECT b.id, b.schedule_id, b.day_of_week, b.start_time, b.end_time
        FROM class_blocks b
        JOIN semester_schedules s ON s.id = b.schedule_id
        WHERE s.employee_id = ?
        ORDER BY b.day_of_week, b.start_time, b.end_time, b.id
        """,
        (employee_id,),
    ).fetchall()

    blocks_by_schedule = {}
    for block in blocks:
        blocks_by_schedule.setdefault(block["schedule_id"], []).append(block)

    # Only the shifts this worker actually expressed a preference about.
    # Neutral is the absence of a row (D025), so listing every neutral shift
    # would mean inventing 99 rows a supervisor never recorded.
    preferences = connection.execute(
        """
        SELECT p.shift_id, p.preference, s.hall, s.start_datetime, s.end_datetime
        FROM shift_preferences p
        JOIN shifts s ON s.id = p.shift_id
        WHERE p.employee_id = ?
        ORDER BY s.start_datetime, s.end_datetime, s.hall
        """,
        (employee_id,),
    ).fetchall()

    # Every existing shift, so a supervisor can set a preference on one that
    # is currently neutral - which, being the absence of a row, would not
    # otherwise appear anywhere in this payload. Small (99 rows) and reused
    # as-is; nothing here creates a shift.
    all_shifts = connection.execute(
        "SELECT id, hall, start_datetime, end_datetime FROM shifts"
        " ORDER BY start_datetime, end_datetime, hall"
    ).fetchall()

    leave = connection.execute(
        "SELECT id, start_datetime, end_datetime FROM approved_leave"
        " WHERE employee_id = ? ORDER BY start_datetime, end_datetime",
        (employee_id,),
    ).fetchall()

    try:
        # This worker's own assignments only. Reading the whole workforce
        # here meant a corrupt shift belonging to somebody else made THIS
        # worker's details return 500 - a page failing because of a record it
        # has no connection to. The list endpoint still validates everything,
        # because it reports on everyone. Uses the SAME selected week
        # Employees/Schedule are showing, not always the fixed sample week.
        assigned = assigned_hours_for_employee(
            connection, employee_id, week_start=active_week_start, week_end=active_week_end
        )
    except InvalidWorkDuration as error:
        # This worker's own assigned shift is unusable. Still a controlled
        # 500 rather than a guessed number.
        raise HTTPException(
            status_code=500,
            detail=f"Stored shift data is invalid: {error}",
        ) from error

    # The same readiness the list shows, computed by the same function over
    # the same schedules and the same selected week, so the two views can
    # never disagree about the displayed week.
    readiness = reporting_period_coverage(schedules, active_week_dates).get(
        employee_id, {"status": "missing", "scheduling_ready": False}
    )

    return {
        "week_start": active_week_start.strftime("%Y-%m-%d"),
        "week_end": (active_week_end - timedelta(days=1)).strftime("%Y-%m-%d"),
        "employee": {
            "employee_code": employee["employee_code"],
            "full_name": employee["full_name"],
            "student_type": employee["student_type"],
            "is_active": bool(employee["is_active"]),
            "weekly_hour_limit": employee["weekly_hour_limit"],
            "assigned_hours": assigned,
            "remaining_capacity_hours": remaining_capacity_hours(
                employee["weekly_hour_limit"], assigned
            ),
            "timetable_status": readiness["status"],
            # See reporting_period_coverage(): the stricter accepted
            # (confirmed AND non-provisional) fact eligibility.py actually
            # requires, not merely `timetable_status == "confirmed"`.
            "scheduling_ready": readiness["scheduling_ready"],
        },
        "semesters": [
            semester_payload(schedule, blocks_by_schedule, active_week_dates)
            for schedule in schedules
        ],
        "shift_preferences": [
            {
                "shift_id": row["shift_id"],
                "preference": row["preference"],
                "hall": row["hall"],
                # Full dated start and end, so an overnight shift shows both
                # of its dates rather than looking like it ends before it
                # starts.
                "start_datetime": row["start_datetime"],
                "end_datetime": row["end_datetime"],
            }
            for row in preferences
        ],
        # Every existing shift, for setting a preference on one that is
        # currently neutral. Not itself a preference record.
        "shifts": [
            {
                "id": row["id"],
                "hall": row["hall"],
                "start_datetime": row["start_datetime"],
                "end_datetime": row["end_datetime"],
            }
            for row in all_shifts
        ],
        "approved_leave": [
            {
                "id": row["id"],
                "start_datetime": row["start_datetime"],
                "end_datetime": row["end_datetime"],
            }
            for row in leave
        ],
    }


@app.get("/api/employees/{employee_code}")
def get_employee_details(employee_code: str, week_start: str | None = None):
    """One worker's stored details, classes, preferences and approved leave.

    Read-only. It shares `run_employee_action`'s error mapping so an unknown
    code is a 404 in the same `{"detail": ...}` shape as every other employee
    route (D033). `week_start` is the same optional shared reporting week
    `GET /api/employees` accepts; omitting it keeps the exact former default
    (the fixed sample week) unchanged.
    """
    return run_employee_action(
        lambda connection: employee_detail_payload(connection, employee_code, week_start)
    )


@app.get("/api/shifts")
def list_shifts():
    """Every stored shift, for the Coverage screen's shift picker.

    Read-only, and deliberately the smallest possible shape: stable id, hall
    and dated start/end, sorted by start datetime then hall then id so the
    picker's order is deterministic. Nothing here is worker-specific - the
    employee-details payload already lists every shift for its own picker,
    but a shift-centric screen like Coverage has no employee code to address,
    so it needs its own top-level read.
    """
    connection = get_connection()
    try:
        shifts = connection.execute(
            "SELECT id, hall, start_datetime, end_datetime FROM shifts"
            " ORDER BY start_datetime, hall, id"
        ).fetchall()
        return {
            "shifts": [
                {
                    "id": row["id"],
                    "hall": row["hall"],
                    "start_datetime": row["start_datetime"],
                    "end_datetime": row["end_datetime"],
                }
                for row in shifts
            ]
        }
    finally:
        connection.close()


@app.get("/api/shifts/{shift_id}/coverage")
def get_shift_coverage(shift_id: int, exclude_employee_code: str | None = None):
    """"Who can cover this shift?" - deterministic eligibility, read-only.

    Addressed by the shift's stable database id. `exclude_employee_code` is
    an optional query parameter naming a worker (typically the one calling
    out) to remove from `eligible_candidates`, though they still appear in
    `results`. See `eligibility.shift_coverage` for the full response shape.
    This performs no assignment, cancellation or other mutation - Phase 7
    owns optimization and Phase 9 will consume these facts to propose one.
    """
    connection = get_connection()
    try:
        return shift_coverage(connection, shift_id, exclude_employee_code)
    except CoverageShiftNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except UnknownExcludedWorker as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except InvalidWorkDuration as error:
        raise HTTPException(
            status_code=500,
            detail=f"Stored shift data is invalid: {error}",
        ) from error
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Week-level scheduling (Phase 7 increment 1): preparing required shifts for
# an explicitly chosen Monday week, and reading back what is currently
# stored for it. Neither route generates assignments or touches any other
# week; see `scheduling.py`.
# --------------------------------------------------------------------------


@app.post("/api/schedule/weeks/{week_start}/prepare")
def prepare_schedule_week(week_start: str):
    """Insert any required shifts missing for one Monday week. Idempotent.

    `week_start` must be a real, strictly-formatted 'YYYY-MM-DD' Monday, or
    this is a 400 with a string `detail` (D033). Never deletes an existing
    shift and never touches assignments, employees, timetables, preferences
    or leave. This is the only way another week's required shifts come to
    exist - reading or selecting a week never prepares it.
    """
    connection = get_connection()
    try:
        return prepare_week(connection, week_start)
    except weeks.InvalidWeekStart as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        connection.close()


@app.get("/api/schedule/weeks/{week_start}")
def get_schedule_week(week_start: str):
    """The stored schedule for one Monday week. Read-only.

    `week_start` must be a real, strictly-formatted 'YYYY-MM-DD' Monday, or
    this is a 400 with a string `detail` (D033). An unprepared week returns
    an empty but valid `shifts` list - this never generates or inserts a
    shift.
    """
    connection = get_connection()
    try:
        return get_week_schedule(connection, week_start)
    except weeks.InvalidWeekStart as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except InvalidWorkDuration as error:
        raise HTTPException(
            status_code=500,
            detail=f"Stored shift data is invalid: {error}",
        ) from error
    finally:
        connection.close()


@app.get("/api/analytics/weeks/{week_start}")
def get_week_analytics(week_start: str):
    """Coverage and workforce-capacity analytics for one Monday week. Read-only.

    `week_start` is validated exactly like every other week-scoped endpoint
    (`weeks.parse_week_start`) - a 400 with a string `detail` for anything
    that is not a real, strictly-formatted Monday (D033). An unprepared week
    is a completely valid answer (zero stored shifts, zero required
    positions), never an error and never a reason to call
    `scheduling.prepare_week` on the caller's behalf - reading a dashboard
    must never have the side effect of preparing a week. See `analytics.py`
    for the exact formulas.
    """
    connection = get_connection()
    try:
        return week_analytics(connection, week_start)
    except weeks.InvalidWeekStart as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except InvalidWorkDuration as error:
        raise HTTPException(
            status_code=500,
            detail=f"Stored shift data is invalid: {error}",
        ) from error
    finally:
        connection.close()


@app.post("/api/schedule/weeks/{week_start}/draft")
def draft_schedule_week(week_start: str):
    """Compute (never store) a coverage-maximizing draft for one Monday week.

    Read-only and computational: every existing assignment is preserved
    exactly, and only currently unfilled required-staff positions receive a
    proposal, using Phase 6's own `eligibility.evaluate_shift_eligibility`
    for every hard rule (see `optimizer.py`). `week_start` must be a real,
    strictly-formatted 'YYYY-MM-DD' Monday, or this is a 400 with a string
    `detail` (D033). A week with no prepared shifts is a 409 - this never
    prepares one on its own. Every optimization tier must solve to a proven
    OPTIMAL status; anything else (a timeout, `FEASIBLE`, `INFEASIBLE`,
    `UNKNOWN`) is a 503 rather than an unproven draft presented as an
    optimized one. See `optimizer.generate_draft` for the full response
    shape. Nothing here is bound, approved or persisted; that is Phase 7
    increment 3's scope.
    """
    connection = get_connection()
    try:
        return generate_draft(connection, week_start)
    except weeks.InvalidWeekStart as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except WeekNotPrepared as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except DraftNotOptimal as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except InvalidWorkDuration as error:
        raise HTTPException(
            status_code=500,
            detail=f"Stored shift data is invalid: {error}",
        ) from error
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Schedule proposals, approval and explicit replacement (Phase 7 increment
# 3). A proposal is a stored, immutable snapshot of a computed draft; only
# `approve_proposal` ever creates real `assignments` rows, and only after
# revalidating the exact stored content against current state inside one
# transaction. See `proposals.py`.
# --------------------------------------------------------------------------


@app.post("/api/schedule/weeks/{week_start}/proposals", status_code=201)
def create_schedule_proposal(week_start: str):
    """Compute a draft and persist it as a new pending proposal.

    `week_start` must be a real, strictly-formatted 'YYYY-MM-DD' Monday
    (400), the week must already be prepared (409 otherwise), and every
    optimization tier must solve to a proven OPTIMAL status (503 otherwise)
    - the same contract `POST .../draft` has, since this calls the same
    `optimizer.generate_draft` before persisting anything.
    """
    connection = get_connection()
    try:
        return create_proposal(connection, week_start)
    except weeks.InvalidWeekStart as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except WeekNotPrepared as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except DraftNotOptimal as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except InvalidWorkDuration as error:
        raise HTTPException(
            status_code=500,
            detail=f"Stored shift data is invalid: {error}",
        ) from error
    except ProposalSnapshotCorrupted as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        connection.close()


@app.get("/api/schedule/weeks/{week_start}/proposals")
def list_schedule_proposals(week_start: str):
    """Every stored proposal for one Monday week, newest first. Read-only.

    Lets the Schedule UI recover a pending or recently decided proposal
    after a refresh or remount, rather than only ever knowing about the one
    it generated in the current browser session. `week_start` must be a
    real, strictly-formatted 'YYYY-MM-DD' Monday, or this is a 400.
    """
    connection = get_connection()
    try:
        return list_proposals_for_week(connection, week_start)
    except weeks.InvalidWeekStart as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ProposalSnapshotCorrupted as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        connection.close()


@app.get("/api/schedule/proposals/{proposal_id}")
def get_schedule_proposal(proposal_id: int):
    """The stored proposal exactly as persisted. Read-only."""
    connection = get_connection()
    try:
        return get_proposal(connection, proposal_id)
    except ProposalNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ProposalSnapshotCorrupted as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        connection.close()


@app.post("/api/schedule/proposals/{proposal_id}/approve")
def approve_schedule_proposal(
    proposal_id: int, payload: Annotated[Any, Body()] = None
):
    """Approve a proposal, creating real assignments, or refuse it whole.

    The request body must echo the proposal's exact stored assignments back
    (`{"assignments": [{"shift_id", "employee_code"}, ...]}`) - a mismatch is
    409, not a silent approval of unseen content. Every assignment is then
    revalidated against current state; any conflict is 409 with a structured
    `conflicts` list, and nothing is written. Approving an already-approved
    proposal with matching content is a 200 no-op (idempotent retry);
    approving an already-rejected one is 409.
    """
    connection = get_connection()
    try:
        return approve_proposal(connection, proposal_id, payload)
    except ProposalValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ProposalNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ProposalContentMismatch as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ProposalNotPending as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ProposalRevalidationFailed as error:
        raise HTTPException(
            status_code=409,
            detail={"message": str(error), "conflicts": error.conflicts},
        ) from error
    except ProposalSnapshotCorrupted as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        connection.close()


@app.post("/api/schedule/proposals/{proposal_id}/reject")
def reject_schedule_proposal(proposal_id: int):
    """Reject a pending proposal. Writes no assignment."""
    connection = get_connection()
    try:
        return reject_proposal(connection, proposal_id)
    except ProposalNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ProposalNotPending as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ProposalSnapshotCorrupted as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        connection.close()


@app.post("/api/schedule/assignments/replace")
def replace_schedule_assignment(payload: Annotated[Any, Body()] = None):
    """Explicitly replace one worker's assignment to a shift with another's.

    Body: `{"shift_id": int, "outgoing_employee_code": str,
    "incoming_employee_code": str}`. The outgoing assignment is preserved
    unless the whole operation succeeds; the incoming worker must pass every
    hard eligibility rule (409 with structured detail otherwise). Neither
    worker existing, or the outgoing worker not currently holding the named
    shift, is 404.
    """
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="Expected a JSON object with shift_id, outgoing_employee_code, "
            "and incoming_employee_code.",
        )
    shift_id = payload.get("shift_id")
    outgoing_code = payload.get("outgoing_employee_code")
    incoming_code = payload.get("incoming_employee_code")
    if (
        not isinstance(shift_id, int)
        or isinstance(shift_id, bool)
        or not isinstance(outgoing_code, str)
        or not isinstance(incoming_code, str)
    ):
        raise HTTPException(
            status_code=400,
            detail="shift_id must be an integer; outgoing_employee_code and "
            "incoming_employee_code must be strings.",
        )

    connection = get_connection()
    try:
        return replace_assignment(connection, shift_id, outgoing_code, incoming_code)
    except AssignmentNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ReplacementInvalid as error:
        raise HTTPException(status_code=409, detail=error.detail) from error
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Semester schedules and class blocks (D035).
#
# Addressed by employee code first, then by the id of the record within that
# worker, so ownership is part of the route rather than something the handler
# has to remember to check. All seven share `run_employee_action`'s error
# mapping, so they answer in the same `{"detail": ...}` shape as every other
# employee route (D033).
#
# Six of these change what a timetable says, and withdraw any confirmation it
# had. The seventh, `confirm_schedule`, is the only one that ever grants
# confirmation, and only when the caller submits the semester's current dates
# and exact class blocks back for the backend to check against what is
# actually stored.
# --------------------------------------------------------------------------


# Accept parsed JSON here so timetable validation owns the object check and
# its 400/string-detail error. None also sends null or a missing body through
# that validator, rather than FastAPI's required-body 422 response.
@app.post("/api/employees/{employee_code}/semesters", status_code=201)
def add_semester(employee_code: str, payload: Annotated[Any, Body()] = None):
    return run_employee_action(
        lambda connection: create_schedule(connection, employee_code, payload)
    )


@app.put("/api/employees/{employee_code}/semesters/{schedule_id}")
def edit_semester(
    employee_code: str, schedule_id: int, payload: Annotated[Any, Body()] = None
):
    return run_employee_action(
        lambda connection: update_schedule(
            connection, employee_code, schedule_id, payload
        )
    )


@app.delete("/api/employees/{employee_code}/semesters/{schedule_id}")
def remove_semester(employee_code: str, schedule_id: int):
    return run_employee_action(
        lambda connection: delete_schedule(connection, employee_code, schedule_id)
    )


@app.post(
    "/api/employees/{employee_code}/semesters/{schedule_id}/blocks", status_code=201
)
def add_class_block(
    employee_code: str, schedule_id: int, payload: Annotated[Any, Body()] = None
):
    return run_employee_action(
        lambda connection: create_block(
            connection, employee_code, schedule_id, payload
        )
    )


@app.put("/api/employees/{employee_code}/semesters/{schedule_id}/blocks/{block_id}")
def edit_class_block(
    employee_code: str, schedule_id: int, block_id: int,
    payload: Annotated[Any, Body()] = None,
):
    return run_employee_action(
        lambda connection: update_block(
            connection, employee_code, schedule_id, block_id, payload
        )
    )


@app.delete("/api/employees/{employee_code}/semesters/{schedule_id}/blocks/{block_id}")
def remove_class_block(employee_code: str, schedule_id: int, block_id: int):
    return run_employee_action(
        lambda connection: delete_block(
            connection, employee_code, schedule_id, block_id
        )
    )


@app.post("/api/employees/{employee_code}/semesters/{schedule_id}/confirm")
def confirm_semester(
    employee_code: str, schedule_id: int, payload: Annotated[Any, Body()] = None
):
    """Confirm that one semester's timetable, as currently stored, is complete.

    The caller must submit the semester's current dates and exact class blocks back;
    `confirm_schedule` checks them against what is actually stored before
    granting confirmation, so a stale or malformed request cannot confirm
    content nobody has actually seen. See `timetables.confirm_schedule` for
    the full contract, including the deliberate no-classes acknowledgement.
    """
    return run_employee_action(
        lambda connection: confirm_schedule(
            connection, employee_code, schedule_id, payload
        )
    )


# --------------------------------------------------------------------------
# Shift preferences and approved leave (final Phase 5C functional batch).
#
# Preferences are addressed by employee code and shift id - there is no
# separate preference id, because a worker can hold at most one preference
# per shift (the table's own unique constraint). Leave is addressed by
# employee code and its own id, the same shape semesters use.
# --------------------------------------------------------------------------


@app.put("/api/employees/{employee_code}/preferences/{shift_id}")
def set_shift_preference(
    employee_code: str, shift_id: int, payload: Annotated[Any, Body()] = None
):
    return run_employee_action(
        lambda connection: set_preference(connection, employee_code, shift_id, payload)
    )


@app.post("/api/employees/{employee_code}/leave", status_code=201)
def add_approved_leave(
    employee_code: str, payload: Annotated[Any, Body()] = None
):
    return run_employee_action(
        lambda connection: add_leave(connection, employee_code, payload)
    )


@app.put("/api/employees/{employee_code}/leave/{leave_id}")
def edit_approved_leave(
    employee_code: str, leave_id: int, payload: Annotated[Any, Body()] = None
):
    return run_employee_action(
        lambda connection: update_leave(connection, employee_code, leave_id, payload)
    )


@app.delete("/api/employees/{employee_code}/leave/{leave_id}")
def remove_approved_leave(employee_code: str, leave_id: int):
    return run_employee_action(
        lambda connection: delete_leave(connection, employee_code, leave_id)
    )


def employee_response(row):
    """The stored row as the frontend sees it after a save."""
    return {
        "employee_code": row["employee_code"],
        "full_name": row["full_name"],
        "student_type": row["student_type"],
        "is_active": bool(row["is_active"]),
        "weekly_hour_limit": row["weekly_hour_limit"],
    }


def run_employee_action(action):
    """Run an employee operation and turn its errors into HTTP responses.

    Every employee route shares this mapping, so the frontend only ever has
    to read one `{"detail": ...}` shape (D033). The read-only details route
    uses it too, for exactly one of these cases: an unknown employee code is
    a 404 there for the same reason it is everywhere else.
    """
    connection = get_connection()
    try:
        return action(connection)
    except EmployeeValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except weeks.InvalidWeekStart as error:
        # The details route's optional `week_start` query parameter (Codex
        # review finding 4) is validated the same way every other week
        # parameter in this project is - a malformed or non-Monday value is
        # 400 with a string detail (D033), never a 500.
        raise HTTPException(status_code=400, detail=str(error)) from error
    except DuplicateEmployeeCode as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except EmployeeNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DeactivationBlocked as error:
        # 409: the request is valid, but the worker's current state does not
        # allow it. Nothing was changed.
        raise HTTPException(status_code=409, detail=str(error)) from error
    except DeletionBlocked as error:
        # 409 for the same reason: the worker has shift history, so deleting
        # them is refused. The transaction rolled back; nothing was removed.
        raise HTTPException(status_code=409, detail=str(error)) from error
    except CodeAllocationError as error:
        # 500: the request was fine, but the server cannot issue a code. The
        # transaction rolled back, so no worker was created.
        raise HTTPException(status_code=500, detail=str(error)) from error
    except TimetableValidationError as error:
        # 400, same as any other unusable input.
        raise HTTPException(status_code=400, detail=str(error)) from error
    except TimetableNotFound as error:
        # 404. A schedule or block id that belongs to a different worker lands
        # here too: from this worker's point of view it does not exist, and
        # answering 403 would confirm that somebody else holds that id.
        raise HTTPException(status_code=404, detail=str(error)) from error
    except TimetableConflict as error:
        # 409: well-formed, but it clashes with what is already stored - an
        # overlapping semester, or a duplicate or overlapping class. Nothing
        # was written; the transaction rolled back.
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PreferenceValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ShiftNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except LeaveValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except LeaveNotFound as error:
        # 404. Another worker's real leave id lands here too, for the same
        # not-found-not-forbidden reason as timetable records.
        raise HTTPException(status_code=404, detail=str(error)) from error
    except LeaveConflict as error:
        # 409: the exact period is already recorded for this worker. Nothing
        # was written; the transaction rolled back.
        raise HTTPException(status_code=409, detail=str(error)) from error
    finally:
        connection.close()


def write_employee(action):
    """Run a write whose result is a stored employee row."""
    return run_employee_action(
        lambda connection: employee_response(action(connection))
    )


@app.post("/api/employees", status_code=201)
def add_employee(payload: dict):
    """Create a worker. The employee code is issued by the backend (D034).

    The payload carries `full_name` and `student_type` only; sending an
    `employee_code` is a 400. The response is the usual employee shape, so the
    caller reads the code that was actually issued from it.
    """
    return write_employee(lambda connection: create_employee(connection, payload))


@app.put("/api/employees/{employee_code}")
def edit_employee(employee_code: str, payload: dict):
    return write_employee(
        lambda connection: update_employee(connection, employee_code, payload)
    )


@app.post("/api/employees/{employee_code}/deactivate")
def deactivate_employee(employee_code: str):
    return write_employee(
        lambda connection: set_active(connection, employee_code, active=False)
    )


@app.post("/api/employees/{employee_code}/reactivate")
def reactivate_employee(employee_code: str):
    return write_employee(
        lambda connection: set_active(connection, employee_code, active=True)
    )


@app.delete("/api/employees/{employee_code}")
def remove_employee(employee_code: str):
    """Permanently delete a worker who has no assignments.

    Returns what was removed rather than an employee row, because by the time
    this responds the employee no longer exists.
    """
    return run_employee_action(
        lambda connection: delete_employee(connection, employee_code)
    )
