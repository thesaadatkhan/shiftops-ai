"""Read-only calculations over stored workforce data.

Pure functions that take a connection and return numbers. Importing this
module has no side effects - in particular it does not open, create or
migrate the project's database, so tests can import it and work entirely
against their own fixtures. (`main.py` migrates on import because a running
API needs its schema; verification scripts must not inherit that.)
"""

from datetime import datetime

from synthetic_data import TIME_FORMAT, WEEK_END, WEEK_START


def minutes_between(start_time, end_time):
    """Minutes between two 'HH:MM' times on the same day."""
    start_hour, start_minute = (int(part) for part in start_time.split(":"))
    end_hour, end_minute = (int(part) for part in end_time.split(":"))
    return (end_hour * 60 + end_minute) - (start_hour * 60 + start_minute)


SECONDS_PER_HOUR = 3600


class InvalidWorkDuration(ValueError):
    """Stored shift data that cannot produce a valid work duration.

    Raised rather than silently rounded: a shift recorded as 90 minutes or as
    ending before it starts is a data problem, and quietly turning it into
    1 or 2 hours would hide it and report a capacity figure that is simply
    wrong.
    """


def shift_duration_hours(start, end, label=""):
    """Whole hours between two shift date-times.

    Work shifts must be a positive whole number of hours (see
    docs/PROJECT_SPEC.md). Zero-length, negative and fractional durations are
    rejected instead of being rounded or truncated into valid-looking ones.

    Class meetings are NOT work and are not subject to this rule - they are
    legitimately 75 or 165 minutes long and are measured elsewhere.
    """
    seconds = (end - start).total_seconds()

    if seconds <= 0:
        raise InvalidWorkDuration(
            f"shift {label} ends at or before it starts "
            f"({start} to {end}); work shifts must be a positive whole number of hours"
        )
    if seconds % SECONDS_PER_HOUR != 0:
        raise InvalidWorkDuration(
            f"shift {label} lasts {seconds / SECONDS_PER_HOUR} hours "
            f"({start} to {end}); work shifts must be a whole number of hours"
        )

    return int(seconds // SECONDS_PER_HOUR)


def assigned_hours_by_employee(
    connection, week_start=WEEK_START, week_end=WEEK_END, employee_id=None
):
    """Whole hours each employee is assigned during one Monday-to-Sunday week.

    Follows D025's start-week convention: a shift counts entirely towards the
    week containing its START. A Sunday 10 PM to Monday 3 AM shift therefore
    puts all five hours in the week that Sunday belongs to, and none in the
    next one. Hours are never split across two weeks.

    Only stored assignments count. Class meetings and approved leave are not
    work and are deliberately not included here.

    Raises InvalidWorkDuration if any assigned shift **that this call reads**
    is not a positive whole number of hours, so a bad row can never quietly
    corrupt a capacity figure.

    `employee_id` narrows the query to one worker's own assignments. That
    matters for more than speed: which rows are read decides which rows can
    raise. A report about the whole workforce has to validate every
    assignment, because it reports on every worker. A report about one worker
    must not, or a corrupt shift belonging to somebody else would make that
    worker's page fail - a fault in a record they have nothing to do with.
    """
    # Every fragment below is a fixed literal written here; the only thing
    # that varies is how many of them there are. Values never go into the SQL
    # text - they are all bound parameters, as everywhere else (D024).
    filters = ["s.start_datetime >= ?", "s.start_datetime < ?"]
    parameters = [week_start.strftime(TIME_FORMAT), week_end.strftime(TIME_FORMAT)]

    if employee_id is not None:
        filters.append("a.employee_id = ?")
        parameters.append(employee_id)

    rows = connection.execute(
        f"""
        SELECT a.employee_id, s.hall, s.start_datetime, s.end_datetime
        FROM assignments a
        JOIN shifts s ON s.id = a.shift_id
        WHERE {' AND '.join(filters)}
        """,
        tuple(parameters),
    ).fetchall()

    hours = {}
    for row in rows:
        start = datetime.strptime(row["start_datetime"], TIME_FORMAT)
        end = datetime.strptime(row["end_datetime"], TIME_FORMAT)
        duration = shift_duration_hours(start, end, label=f"at {row['hall']}")
        hours[row["employee_id"]] = hours.get(row["employee_id"], 0) + duration
    return hours


def assigned_hours_for_employee(
    connection, employee_id, week_start=WEEK_START, week_end=WEEK_END
):
    """One employee's assigned whole hours for the week, as a number.

    Everything `assigned_hours_by_employee` documents applies - the same
    start-week accounting, the same whole-hour rule, the same exception for a
    shift that breaks it - restricted to this worker's own assignments. A
    worker with none is 0, which is the same answer as a worker who exists but
    has not been assigned anything.
    """
    return assigned_hours_by_employee(
        connection, week_start, week_end, employee_id=employee_id
    ).get(employee_id, 0)


def remaining_capacity_hours(weekly_hour_limit, assigned):
    """Theoretical unused work capacity for the week, in whole hours.

    Deliberately NOT an eligibility or availability figure: class hours and
    approved leave are not subtracted, because neither consumes any of the
    worker's weekly hour limit.
    """
    return max(0, weekly_hour_limit - assigned)


def schedule_covered_days(schedule, week_dates):
    """Which days of the displayed reporting week one schedule covers.

    The single definition of "this semester applies on that day", used both
    for an individual semester's coverage state and for the employee's
    combined readiness below, so the two can never disagree about which days
    a schedule reaches. Dates are inclusive on both ends (D035). `week_dates`
    is the displayed week's seven ISO dates (`weeks.week_dates`).

    Moved here from `main.py` for Phase 8 (D051): analytics needs the exact
    same readiness rule employee reporting already uses, and this module -
    unlike `main.py` - has no import-time side effects, so both the API
    layer and read-only analytics/tests can share one definition without
    either depending on the FastAPI app module.
    """
    return {
        date
        for date in week_dates
        if schedule["start_date"] <= date <= schedule["end_date"]
    }


def reporting_period_coverage(schedules, week_dates):
    """Timetable readiness for the DISPLAYED reporting week, per employee.

    Readiness is about the week on screen, not about whether a worker ever
    had a confirmed timetable. A spring semester confirmed months ago says
    nothing about October, and reporting it as "confirmed" while October is
    displayed would be actively misleading.

    Returns `{employee_id: {"status": ..., "scheduling_ready": bool}}`.

    `status` is one of five states, each answering a different question:

      missing        - no semester schedule at all. Nobody has said anything
                       about this worker's classes.
      outside_period - schedules exist, but none of them covers any day of
                       the reporting week. Their information is about some
                       other period: an expired semester, or one still ahead.
      unconfirmed    - a schedule covers part of the week, but no confirmed
                       schedule covers any of it. Something has been entered
                       and nobody has checked it is complete.
      partial        - confirmed schedules cover some days of the week but
                       not all seven. The week is only partly accounted for.
      confirmed      - confirmed schedules cover all seven days. Combined
                       with a class-block count of zero, that is a deliberate
                       "this worker has no classes this week", which stays
                       distinguishable from `missing`. **`confirmed` alone
                       does not mean scheduling-ready** - a schedule can be
                       confirmed while its dates are still `dates_provisional`
                       (the Phase 5C migration can leave one that way; see
                       `database.migrate_class_schedules`), meaning nobody
                       has actually accepted those assumed dates.

    `scheduling_ready` is the separate, stricter fact Phase 6 eligibility
    (`eligibility.py`) actually requires and Phase 8 reuses: whether
    ACCEPTED schedules - `confirmed_at IS NOT NULL AND dates_provisional = 0`
    - cover all seven days of the displayed week. Codex review found this
    project reporting a provisional-but-confirmed worker as plain
    `"confirmed"` everywhere, which reads as available when
    `evaluate_shift_eligibility` would still correctly refuse them
    (`timetable_not_confirmed`) until a supervisor actually confirms the
    real dates. `status` keeps recording confirmation history honestly (a
    provisional confirmation IS a real, historical fact - it is not
    weakened or hidden here); `scheduling_ready` is the field that must
    never treat provisional dates as accepted availability.
    """
    any_days = {}
    confirmed_days = {}
    accepted_days = {}
    seen = set()

    for schedule in schedules:
        employee_id = schedule["employee_id"]
        seen.add(employee_id)
        covered = schedule_covered_days(schedule, week_dates)
        if not covered:
            continue
        any_days.setdefault(employee_id, set()).update(covered)
        if schedule["confirmed_at"] is not None:
            confirmed_days.setdefault(employee_id, set()).update(covered)
            if not schedule["dates_provisional"]:
                accepted_days.setdefault(employee_id, set()).update(covered)

    result = {}
    for employee_id in seen:
        covered = any_days.get(employee_id, set())
        confirmed = confirmed_days.get(employee_id, set())
        accepted = accepted_days.get(employee_id, set())
        if not covered:
            status = "outside_period"
        elif not confirmed:
            status = "unconfirmed"
        elif len(confirmed) < len(week_dates):
            status = "partial"
        else:
            status = "confirmed"
        result[employee_id] = {
            "status": status,
            "scheduling_ready": len(accepted) == len(week_dates),
        }
    return result
