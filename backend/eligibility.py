"""Deterministic shift eligibility and the read-only Coverage query (Phase 6,
increment 1: "who can cover this shift?").

This module answers one question - given a stored shift and a stored worker,
is that worker eligible to cover it, and why or why not - using only current
database records. It performs no writes and makes no assignment; Phase 7 owns
optimization and Phase 9 will consume these facts to propose a change.

**Hard rules (D022), evaluated independently so every applicable reason is
returned, not just the first one found:**

1. `worker_inactive` - the worker's `is_active` flag is false.
2. `timetable_not_confirmed` - some calendar date containing a non-zero part
   of the shift is not covered by any of this worker's CONFIRMED, NON-
   PROVISIONAL semester schedules - `confirmed_at IS NOT NULL AND
   dates_provisional = 0`. Both conditions are required: the Phase 5C
   migration can leave a schedule with `confirmed_at` set (because its
   stored classes happened to match the generated demo timetable exactly)
   while its dates are still `dates_provisional = 1`, because the legacy
   schema recorded no semester dates at all and the migration had to assume
   the demo semester's. Nobody has accepted those assumed dates, so they
   must not establish coverage; only `confirm_schedule()` clears
   `dates_provisional`, in the same write that grants or reaffirms
   confirmation (D035). Missing, unconfirmed, still-provisional or expired
   timetable information is never treated as unrestricted availability
   (D035's scheduling handoff). A confirmed, non-provisional schedule with
   zero class blocks counts as covering its dates - "confirmed no classes"
   is a real answer, not missing information.
3. `class_conflict` - a recurring class block, expanded only onto its own
   weekday and only for dates inside its OWN semester's inclusive dates,
   overlaps the shift. This is checked against every stored schedule
   regardless of confirmation: a class a supervisor actually entered is a
   real hard restriction whether or not the timetable as a whole has been
   confirmed complete (rule 2 already reports the confirmation gap
   separately).
4. `leave_conflict` - an approved leave period overlaps the shift.
5. `assignment_conflict` - another existing assignment for this worker
   overlaps the shift. The shift's own current assignment, if this worker
   already holds it, is excluded from this check - it is not a conflict for
   the shift to overlap itself.
6. `weekly_hour_limit_exceeded` - adding the shift's whole duration to this
   worker's other assigned hours in the week containing the shift's START
   (D025) would exceed `weekly_hour_limit`.

Every overlap check uses the project's endpoint-touching convention: a period
ending exactly when another starts does not overlap (D025).

Preferred, low and neutral preference are facts returned alongside these
rules, never a reason for ineligibility (D022): a low-preference shift with
every hard rule passing is eligible.
"""

from datetime import datetime, timedelta

from employees import find_by_code
from reporting import InvalidWorkDuration, shift_duration_hours

DATETIME_FORMAT = "%Y-%m-%d %H:%M"
DATE_FORMAT = "%Y-%m-%d"


class ShiftNotFound(LookupError):
    """No such shift exists. Maps to HTTP 404."""


class UnknownExcludedWorker(ValueError):
    """The employee code named to exclude does not exist. Maps to HTTP 400."""


def _parse(datetime_text):
    return datetime.strptime(datetime_text, DATETIME_FORMAT)


def _week_bounds(start):
    """Monday 00:00 through the following Monday 00:00 containing `start`.

    D025: a shift's whole duration is charged to the week containing its
    START, never split across two weeks. Weeks run Monday through Sunday.
    """
    monday = (start - timedelta(days=start.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday, monday + timedelta(days=7)


def _dates_touched(start, end):
    """Calendar dates containing a non-zero part of [start, end).

    A shift ending exactly at midnight touches nothing on that later date -
    the same touching-endpoint convention used everywhere else in this
    project (D025).
    """
    dates = []
    current = start.date()
    last = end.date()
    while current <= last:
        day_start = datetime.combine(current, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        overlap = min(end, day_end) - max(start, day_start)
        if overlap.total_seconds() > 0:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def _shift(connection, shift_id):
    row = connection.execute(
        "SELECT id, hall, start_datetime, end_datetime FROM shifts WHERE id = ?",
        (shift_id,),
    ).fetchone()
    if row is None:
        raise ShiftNotFound(f"No shift {shift_id}.")
    return row


def evaluate_shift_eligibility(connection, shift, employee):
    """Whether one worker is eligible for one stored shift, and why or why not.

    `shift` and `employee` are the rows already looked up by the caller (see
    `_shift` and `employees.find_by_code`), so this function itself performs
    no existence checks and can be reused for every worker in a Coverage
    query without repeating them.
    """
    shift_id = shift["id"]
    employee_id = employee["id"]

    start = _parse(shift["start_datetime"])
    end = _parse(shift["end_datetime"])

    # A corrupt shift is a data problem, not an eligibility answer - let it
    # propagate the same way reporting.py's capacity figures do, so the
    # caller can report a controlled 500 instead of a plausible-looking
    # eligibility result derived from bad data.
    duration = shift_duration_hours(start, end, label=f"{shift_id} at {shift['hall']}")

    reason_codes = []
    reasons = []
    conflicts = {
        "missing_timetable_dates": [],
        "class_blocks": [],
        "leave": [],
        "assignments": [],
    }

    touched_dates = _dates_touched(start, end)

    # 1. Active.
    if not employee["is_active"]:
        reason_codes.append("worker_inactive")
        reasons.append("This worker is not active.")

    # 2. Confirmed timetable coverage for every date the shift touches.
    # Multiple confirmed, non-overlapping schedules may collectively cover
    # the relevant dates.
    #
    # `confirmed_at IS NOT NULL` alone is not enough. The Phase 5C migration
    # can produce a schedule whose `confirmed_at` is set because its class
    # blocks happened to match the generated demo timetable exactly, while
    # `dates_provisional = 1` because the legacy schema recorded no semester
    # dates at all and the migration had to assume the fictional demo
    # semester's dates. Nobody - no supervisor - has looked at those assumed
    # dates and confirmed them; `confirm_schedule()` is what turns provisional
    # dates into accepted ones (it clears `dates_provisional` in the same
    # write that grants confirmation, D035). Coverage must therefore require
    # BOTH conditions: confirmed AND not provisional. A row that is confirmed
    # but still provisional counts here exactly like an unconfirmed one - its
    # dates have not actually been accepted.
    confirmed_schedules = connection.execute(
        "SELECT start_date, end_date FROM semester_schedules"
        " WHERE employee_id = ? AND confirmed_at IS NOT NULL"
        " AND dates_provisional = 0",
        (employee_id,),
    ).fetchall()
    missing_dates = []
    for day in touched_dates:
        day_text = day.strftime(DATE_FORMAT)
        covered = any(
            row["start_date"] <= day_text <= row["end_date"]
            for row in confirmed_schedules
        )
        if not covered:
            missing_dates.append(day_text)
    if missing_dates:
        conflicts["missing_timetable_dates"] = missing_dates
        reason_codes.append("timetable_not_confirmed")
        reasons.append(
            "No confirmed and accepted semester timetable covers "
            + ", ".join(missing_dates)
            + " of this shift. Missing, unconfirmed, provisional/unaccepted "
            "or expired timetable information does not establish "
            "availability."
        )

    # 3. Recurring class occurrences, expanded only onto their own weekday and
    # only within their own semester's inclusive dates. Checked against every
    # stored schedule regardless of confirmation - see the module docstring.
    schedules = connection.execute(
        "SELECT id, start_date, end_date FROM semester_schedules WHERE employee_id = ?",
        (employee_id,),
    ).fetchall()
    schedules_by_id = {row["id"]: row for row in schedules}
    blocks = connection.execute(
        """
        SELECT b.id, b.schedule_id, b.day_of_week, b.start_time, b.end_time
        FROM class_blocks b
        JOIN semester_schedules s ON s.id = b.schedule_id
        WHERE s.employee_id = ?
        """,
        (employee_id,),
    ).fetchall()
    class_hits = []
    for block in blocks:
        schedule = schedules_by_id.get(block["schedule_id"])
        if schedule is None:
            continue
        for day in touched_dates:
            if day.weekday() != block["day_of_week"]:
                continue
            day_text = day.strftime(DATE_FORMAT)
            if not (schedule["start_date"] <= day_text <= schedule["end_date"]):
                continue
            occurrence_start = datetime.combine(
                day, datetime.strptime(block["start_time"], "%H:%M").time()
            )
            occurrence_end = datetime.combine(
                day, datetime.strptime(block["end_time"], "%H:%M").time()
            )
            if occurrence_start < end and start < occurrence_end:
                class_hits.append(
                    {
                        "schedule_id": schedule["id"],
                        "block_id": block["id"],
                        "date": day_text,
                        "day_of_week": block["day_of_week"],
                        "start_time": block["start_time"],
                        "end_time": block["end_time"],
                    }
                )
    if class_hits:
        conflicts["class_blocks"] = class_hits
        reason_codes.append("class_conflict")
        plural = "occurrences" if len(class_hits) != 1 else "occurrence"
        reasons.append(
            f"A recurring class overlaps this shift ({len(class_hits)} {plural})."
        )

    # 4. Approved leave.
    leave_rows = connection.execute(
        "SELECT id, start_datetime, end_datetime FROM approved_leave"
        " WHERE employee_id = ?",
        (employee_id,),
    ).fetchall()
    leave_hits = []
    for row in leave_rows:
        leave_start = _parse(row["start_datetime"])
        leave_end = _parse(row["end_datetime"])
        if leave_start < end and start < leave_end:
            leave_hits.append(
                {
                    "id": row["id"],
                    "start_datetime": row["start_datetime"],
                    "end_datetime": row["end_datetime"],
                }
            )
    if leave_hits:
        conflicts["leave"] = leave_hits
        reason_codes.append("leave_conflict")
        reasons.append("Approved leave overlaps this shift.")

    # 5. Existing assignment overlap. The shift's OWN current assignment (if
    # this worker already holds it) is excluded - it does not conflict with
    # itself.
    assignment_rows = connection.execute(
        """
        SELECT s.id AS shift_id, s.hall, s.start_datetime, s.end_datetime
        FROM assignments a
        JOIN shifts s ON s.id = a.shift_id
        WHERE a.employee_id = ? AND a.shift_id != ?
        """,
        (employee_id, shift_id),
    ).fetchall()
    assignment_hits = []
    for row in assignment_rows:
        a_start = _parse(row["start_datetime"])
        a_end = _parse(row["end_datetime"])
        if a_start < end and start < a_end:
            assignment_hits.append(
                {
                    "shift_id": row["shift_id"],
                    "hall": row["hall"],
                    "start_datetime": row["start_datetime"],
                    "end_datetime": row["end_datetime"],
                }
            )
    if assignment_hits:
        conflicts["assignments"] = assignment_hits
        reason_codes.append("assignment_conflict")
        reasons.append("An existing assignment overlaps this shift.")

    # 6. Weekly hour limit, charged to the week containing the shift's start
    # (D025). The shift's own current assignment (if any) is excluded from
    # the existing total the same way rule 5 excludes it, so evaluating the
    # worker who already holds this shift does not double-count it.
    week_start, week_end = _week_bounds(start)
    week_rows = connection.execute(
        """
        SELECT s.start_datetime, s.end_datetime, s.hall
        FROM assignments a
        JOIN shifts s ON s.id = a.shift_id
        WHERE a.employee_id = ? AND a.shift_id != ?
          AND s.start_datetime >= ? AND s.start_datetime < ?
        """,
        (
            employee_id,
            shift_id,
            week_start.strftime(DATETIME_FORMAT),
            week_end.strftime(DATETIME_FORMAT),
        ),
    ).fetchall()
    assigned_hours = 0
    for row in week_rows:
        a_start = _parse(row["start_datetime"])
        a_end = _parse(row["end_datetime"])
        assigned_hours += shift_duration_hours(a_start, a_end, label=f"at {row['hall']}")
    projected_hours = assigned_hours + duration
    if projected_hours > employee["weekly_hour_limit"]:
        reason_codes.append("weekly_hour_limit_exceeded")
        reasons.append(
            f"Assigning this shift would bring weekly hours to "
            f"{projected_hours}, exceeding the limit of "
            f"{employee['weekly_hour_limit']}."
        )

    # Preference is a fact, never a hard restriction (D022): a low-preference
    # shift with every rule above passing is still eligible.
    preference_row = connection.execute(
        "SELECT preference FROM shift_preferences"
        " WHERE employee_id = ? AND shift_id = ?",
        (employee_id, shift_id),
    ).fetchone()
    preference = preference_row["preference"] if preference_row else "neutral"

    return {
        "employee_id": employee_id,
        "employee_code": employee["employee_code"],
        "full_name": employee["full_name"],
        "shift_id": shift_id,
        "eligible": not reason_codes,
        "reason_codes": reason_codes,
        "reasons": reasons,
        "shift_duration_hours": duration,
        "assigned_hours_this_week": assigned_hours,
        "projected_weekly_hours": projected_hours,
        "weekly_hour_limit": employee["weekly_hour_limit"],
        "preference": preference,
        "conflicts": conflicts,
    }


def shift_coverage(connection, shift_id, exclude_employee_code=None):
    """"Who can cover this shift?" - one shift, every current worker.

    Response shape, chosen deliberately as the one clear contract for this
    increment:

        {
          "shift": {id, hall, start_datetime, end_datetime, duration_hours},
          "excluded_employee_code": <code or null>,
          "results": [ one evaluate_shift_eligibility() dict per worker,
                        ordered by employee_code ],
          "eligible_candidates": [ the subset of `results` that is eligible,
                                    excluding the named worker if any ],
        }

    `results` covers every worker so the future Coverage UI can show useful
    ineligibility reasons for everyone, not just those who qualify.
    `eligible_candidates` is the explicit, ready-to-use replacement list -
    the excluded worker (typically the one calling out) is removed from it
    but still appears in `results` for reference. No assignment, cancellation
    or other mutation happens here; this is read-only.
    """
    shift = _shift(connection, shift_id)
    start = _parse(shift["start_datetime"])
    end = _parse(shift["end_datetime"])
    duration = shift_duration_hours(start, end, label=f"{shift_id} at {shift['hall']}")

    excluded_id = None
    if exclude_employee_code is not None:
        excluded = find_by_code(connection, exclude_employee_code)
        if excluded is None:
            raise UnknownExcludedWorker(
                f"No employee with code {exclude_employee_code} to exclude."
            )
        excluded_id = excluded["id"]

    employees = connection.execute(
        "SELECT id, employee_code, full_name, is_active, weekly_hour_limit"
        " FROM employees ORDER BY employee_code"
    ).fetchall()

    results = [
        evaluate_shift_eligibility(connection, shift, employee)
        for employee in employees
    ]
    eligible_candidates = [
        result
        for result in results
        if result["eligible"] and result["employee_id"] != excluded_id
    ]

    return {
        "shift": {
            "id": shift["id"],
            "hall": shift["hall"],
            "start_datetime": shift["start_datetime"],
            "end_datetime": shift["end_datetime"],
            "duration_hours": duration,
        },
        "excluded_employee_code": exclude_employee_code,
        "results": results,
        "eligible_candidates": eligible_candidates,
    }
