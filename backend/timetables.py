"""Supervisor entry and editing of semester schedules and class blocks (D035).

The database is authoritative. Every rule below is enforced here, not in the
browser: the frontend's validation is a convenience, and anything that reaches
these functions is checked again.

**What a timetable is made of.** A worker owns semester schedules; a schedule
owns recurring weekly class blocks. A schedule records inclusive start and end
dates. A block records a weekday (0 = Monday, D025) plus 'HH:MM' start and end
times in the project's single local clock. There are no courses here - course
names and course entities stopped being operational inputs with D035, and
nothing in this module accepts one.

**Confirmation.** `confirm_schedule` is the only function that ever sets
`confirmed_at`. Every edit that changes what the timetable says - new dates, a
new class, an edited or removed class - clears it, because a supervisor
confirmed the timetable they saw, not the one it has become. Confirming
requires the caller to submit back the semester's current dates and the exact
classes it saw (id, weekday and times, not just how many), so a stale form
cannot confirm content nobody actually looked at - including a class that
moved to a different day while the count stayed the same. Any mismatch is a
409, not a silent overwrite. Confirming a semester with zero
classes additionally requires `acknowledge_no_classes: true`, so a generic or
malformed request can never confirm "no classes" by accident. Confirming also
accepts the displayed dates as correct: `dates_provisional` is cleared, the
same as if the supervisor had retyped them (see `update_schedule`).

**Date provenance.** `dates_provisional` marks dates the migration assumed
rather than a person choosing them. Supplying dates - creating a schedule, or
editing an existing one's dates - is a person choosing them, so it clears the
flag. Editing or deleting a class block says nothing about where the dates
came from and deliberately leaves it alone.

**Ownership.** Every operation is addressed by employee code AND by the id of
the record within that worker. A schedule or block id that exists but belongs
to somebody else is reported as not found, not as forbidden: from this
worker's point of view it does not exist, and saying otherwise would confirm
that some other worker holds that id.
"""

import re
from datetime import date, datetime

from employees import EmployeeNotFound, find_by_code

# The same plain wall-clock format the rest of the project uses for a real
# (non-simulated) recorded moment - see `employees.py`'s deactivation and
# retirement timestamps. D025 fixes one local simulation clock with no
# timezone conversion, so a naive local `now` is the only reading consistent
# with every other stored datetime.
TIME_FORMAT = "%Y-%m-%d %H:%M"

# A weekday index, 0 = Monday through 6 = Sunday, matching D025 and Python's
# datetime.weekday().
WEEKDAYS = range(7)

# 'HH:MM' on a 24-hour clock. An explicit pattern rather than a parse-and-hope:
# it pins the stored format as well as the value, so '9:00' and '09:00:00' are
# rejected instead of being quietly normalised into something else.
TIME_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TimetableValidationError(ValueError):
    """The submitted values cannot describe a timetable. Maps to HTTP 400."""


class TimetableNotFound(LookupError):
    """No such schedule or block for this worker. Maps to HTTP 404."""


class TimetableConflict(RuntimeError):
    """The request is well-formed but clashes with what is stored. HTTP 409."""


# ---------------------------------------------------------------- validation


def _require_object(payload, what):
    if not isinstance(payload, dict):
        raise TimetableValidationError(f"Expected an object with {what}.")


def clean_date(value, label):
    """One ISO calendar date, as stored: 'YYYY-MM-DD'.

    `date.fromisoformat` alone is not enough on modern Python, which also
    accepts 'YYYYMMDD'. The pattern pins the stored spelling first, then the
    parse rejects impossible days such as 2026-02-30.
    """
    if not isinstance(value, str) or not DATE_PATTERN.match(value.strip()):
        raise TimetableValidationError(
            f"{label} must be a date in YYYY-MM-DD form."
        )
    text = value.strip()
    try:
        date.fromisoformat(text)
    except ValueError as error:
        raise TimetableValidationError(f"{label} is not a real date.") from error
    return text


def clean_semester_dates(payload):
    _require_object(payload, "semester dates")
    start = clean_date(payload.get("start_date"), "Semester start date")
    end = clean_date(payload.get("end_date"), "Semester end date")
    if start > end:
        # String comparison is exact for zero-padded ISO dates, which is
        # also why they are stored this way.
        raise TimetableValidationError(
            f"The semester cannot end ({end}) before it starts ({start})."
        )
    return start, end


def clean_confirmation_block(payload, index):
    """Shape-check one entry of a confirmation's submitted class snapshot.

    Deliberately mirrors `clean_block`'s field checks, plus an `id` - the
    caller is not describing a new class, it is naming an EXISTING one it
    saw, so `confirm_schedule` can compare it against what is actually
    stored. Invalid values are refused the same way `clean_block` refuses
    them; there is no reason for this snapshot to accept anything a real
    class block could not have.
    """
    if not isinstance(payload, dict):
        raise TimetableValidationError(
            f"class_blocks[{index}] must be an object with id, day_of_week, "
            "start_time and end_time."
        )

    block_id = payload.get("id")
    if isinstance(block_id, bool) or not isinstance(block_id, int):
        raise TimetableValidationError(
            f"class_blocks[{index}].id must be a whole number."
        )

    day = payload.get("day_of_week")
    if isinstance(day, bool) or not isinstance(day, int) or day not in WEEKDAYS:
        raise TimetableValidationError(
            f"class_blocks[{index}].day_of_week must be a whole number from "
            "0 (Monday) to 6 (Sunday)."
        )

    start = clean_time(payload.get("start_time"), f"class_blocks[{index}].start_time")
    end = clean_time(payload.get("end_time"), f"class_blocks[{index}].end_time")
    return block_id, day, start, end


def clean_confirmation(payload):
    """Shape-check a confirmation request. Does not touch the database.

    The submitted `class_blocks` and `acknowledge_no_classes` are compared
    against what is actually stored inside `confirm_schedule`'s transaction,
    not here - this only rejects a request that could never be valid
    regardless of what the semester currently holds.
    """
    _require_object(payload, "the semester's current dates and class blocks")
    start = clean_date(payload.get("start_date"), "Semester start date")
    end = clean_date(payload.get("end_date"), "Semester end date")

    blocks_payload = payload.get("class_blocks")
    if not isinstance(blocks_payload, list):
        raise TimetableValidationError(
            "class_blocks must be a list of the semester's currently "
            "displayed classes."
        )
    blocks = [
        clean_confirmation_block(item, index)
        for index, item in enumerate(blocks_payload)
    ]

    acknowledge_no_classes = payload.get("acknowledge_no_classes", False)
    if not isinstance(acknowledge_no_classes, bool):
        raise TimetableValidationError(
            "acknowledge_no_classes must be true or false."
        )

    return start, end, blocks, acknowledge_no_classes


def clean_time(value, label):
    if not isinstance(value, str) or not TIME_PATTERN.match(value.strip()):
        raise TimetableValidationError(
            f"{label} must be a 24-hour time in HH:MM form, such as 09:00."
        )
    return value.strip()


def clean_block(payload):
    _require_object(payload, "a weekday and start and end times")

    day = payload.get("day_of_week")
    # bool is a subclass of int in Python, so True would otherwise sail
    # through as weekday 1.
    if isinstance(day, bool) or not isinstance(day, int) or day not in WEEKDAYS:
        raise TimetableValidationError(
            "Weekday must be a whole number from 0 (Monday) to 6 (Sunday)."
        )

    start = clean_time(payload.get("start_time"), "Class start time")
    end = clean_time(payload.get("end_time"), "Class end time")
    if end <= start:
        # Same-day blocks only, for now. A class running past midnight would
        # need a different representation, and inventing one silently - by
        # letting end < start mean "tomorrow" - would make every overlap
        # check below wrong.
        raise TimetableValidationError(
            f"A class must end after it starts ({start} to {end}). "
            "Classes that run past midnight are not supported."
        )
    return day, start, end


# ------------------------------------------------------------------- lookups


def _employee_id(connection, employee_code):
    employee = find_by_code(connection, employee_code)
    if employee is None:
        raise EmployeeNotFound(f"No employee with code {employee_code}.")
    return employee["id"]


def _schedule(connection, employee_id, schedule_id):
    """One schedule, but only if this worker owns it."""
    row = connection.execute(
        "SELECT * FROM semester_schedules WHERE id = ? AND employee_id = ?",
        (schedule_id, employee_id),
    ).fetchone()
    if row is None:
        raise TimetableNotFound(f"No semester schedule {schedule_id} for this worker.")
    return row


def _block(connection, schedule_id, block_id):
    row = connection.execute(
        "SELECT * FROM class_blocks WHERE id = ? AND schedule_id = ?",
        (block_id, schedule_id),
    ).fetchone()
    if row is None:
        raise TimetableNotFound(f"No class block {block_id} in this semester.")
    return row


# ------------------------------------------------------------ clash checking


def _assert_no_semester_overlap(connection, employee_id, start, end, ignore_id=None):
    """No two of one worker's semesters may share a calendar date.

    Dates are inclusive on both ends, so a semester ending 2026-12-11 and one
    starting the same day DO overlap - they both contain that day. Two ranges
    miss each other only when one ends strictly before the other begins.
    """
    rows = connection.execute(
        "SELECT id, start_date, end_date FROM semester_schedules"
        " WHERE employee_id = ? ORDER BY start_date",
        (employee_id,),
    ).fetchall()

    for row in rows:
        if ignore_id is not None and row["id"] == ignore_id:
            continue
        if start <= row["end_date"] and row["start_date"] <= end:
            raise TimetableConflict(
                f"That period overlaps an existing semester "
                f"({row['start_date']} to {row['end_date']}). "
                "Semester dates are inclusive, so sharing a single day counts "
                "as an overlap."
            )


def _assert_block_fits(connection, schedule_id, day, start, end, ignore_id=None):
    """No duplicate and no overlapping class in the same semester.

    Touching endpoints are allowed: a class ending at 10:15 and another
    starting at 10:15 do not overlap, which is the same rule the rest of the
    project uses (D025). Two ranges on the same day overlap only when each
    starts strictly before the other ends.
    """
    rows = connection.execute(
        "SELECT id, day_of_week, start_time, end_time FROM class_blocks"
        " WHERE schedule_id = ? AND day_of_week = ?"
        " ORDER BY start_time, end_time, id",
        (schedule_id, day),
    ).fetchall()

    for row in rows:
        if ignore_id is not None and row["id"] == ignore_id:
            continue
        if row["start_time"] == start and row["end_time"] == end:
            raise TimetableConflict(
                "That class is already in this semester "
                f"({DAY_NAMES[day]} {start} to {end})."
            )
        if start < row["end_time"] and row["start_time"] < end:
            raise TimetableConflict(
                f"That class overlaps one already in this semester "
                f"({DAY_NAMES[day]} {row['start_time']} to {row['end_time']}). "
                "A class may start exactly when another ends."
            )


DAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


# -------------------------------------------------------------- transactions


def _in_transaction(connection, work):
    """Run `work` inside one BEGIN IMMEDIATE transaction.

    The same shape the employee writes use: the write lock is taken before
    anything is read, so a concurrent writer cannot invalidate a check between
    making it and acting on it, and any failure rolls the whole operation back
    rather than leaving a schedule edited but its confirmation still standing.
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


def _unconfirm(connection, schedule_id):
    """Withdraw confirmation from a schedule whose content just changed.

    A supervisor confirmed the timetable they were looking at. Once it says
    something different, that confirmation no longer describes anything, so it
    is cleared rather than left to vouch for content nobody checked.
    """
    connection.execute(
        "UPDATE semester_schedules SET confirmed_at = NULL WHERE id = ?",
        (schedule_id,),
    )


# ------------------------------------------------------------------ schedules


def create_schedule(connection, employee_code, payload):
    """Add a semester to a worker. Unconfirmed, and not provisional."""
    start, end = clean_semester_dates(payload)

    def work():
        employee_id = _employee_id(connection, employee_code)
        _assert_no_semester_overlap(connection, employee_id, start, end)
        connection.execute(
            """
            INSERT INTO semester_schedules
                (employee_id, start_date, end_date, confirmed_at, dates_provisional)
            VALUES (?, ?, ?, NULL, 0)
            """,
            (employee_id, start, end),
        )
        schedule_id = connection.execute(
            "SELECT id FROM semester_schedules WHERE employee_id = ?"
            " AND start_date = ? AND end_date = ?",
            (employee_id, start, end),
        ).fetchone()["id"]
        return {"id": schedule_id, "start_date": start, "end_date": end}

    return _in_transaction(connection, work)


def update_schedule(connection, employee_code, schedule_id, payload):
    """Change a semester's dates.

    Clears both `confirmed_at` and `dates_provisional`, unconditionally. The
    supervisor has just told us what this semester's dates are, which is
    exactly what `dates_provisional` records the absence of; and the classes
    inside it now apply over a different span, which is not what anybody
    confirmed. Both are cleared even when the submitted dates equal the stored
    ones, because the meaning of the action is "these are the dates", not
    "something differs" - and erring towards unconfirmed understates readiness
    rather than overstating it.
    """
    start, end = clean_semester_dates(payload)

    def work():
        employee_id = _employee_id(connection, employee_code)
        _schedule(connection, employee_id, schedule_id)
        _assert_no_semester_overlap(
            connection, employee_id, start, end, ignore_id=schedule_id
        )
        connection.execute(
            """
            UPDATE semester_schedules
            SET start_date = ?, end_date = ?, confirmed_at = NULL,
                dates_provisional = 0
            WHERE id = ?
            """,
            (start, end, schedule_id),
        )
        return {"id": schedule_id, "start_date": start, "end_date": end}

    return _in_transaction(connection, work)


def delete_schedule(connection, employee_code, schedule_id):
    """Remove a semester and the classes inside it.

    Its confirmation goes with it, because there is no longer anything to be
    confirmed. Nothing outside this schedule is touched: other semesters, the
    worker, their preferences, leave and assignments all stay exactly as they
    are.
    """

    def work():
        employee_id = _employee_id(connection, employee_code)
        schedule = _schedule(connection, employee_id, schedule_id)
        removed = connection.execute(
            "SELECT COUNT(*) AS n FROM class_blocks WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()["n"]
        connection.execute(
            "DELETE FROM class_blocks WHERE schedule_id = ?", (schedule_id,)
        )
        connection.execute(
            "DELETE FROM semester_schedules WHERE id = ?", (schedule_id,)
        )
        return {
            "start_date": schedule["start_date"],
            "end_date": schedule["end_date"],
            "class_blocks": removed,
        }

    return _in_transaction(connection, work)


# --------------------------------------------------------------- class blocks


def create_block(connection, employee_code, schedule_id, payload):
    """Add a recurring weekly class to a semester."""
    day, start, end = clean_block(payload)

    def work():
        employee_id = _employee_id(connection, employee_code)
        _schedule(connection, employee_id, schedule_id)
        _assert_block_fits(connection, schedule_id, day, start, end)
        cursor = connection.execute(
            """
            INSERT INTO class_blocks
                (schedule_id, day_of_week, start_time, end_time, source_note)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (schedule_id, day, start, end),
        )
        # `lastrowid` rather than a select-back by value, which is how the
        # rest of the project finds a row it just inserted. Class blocks
        # deliberately have no unique constraint - two identical ones are
        # legitimate - so there is no value to select back by.
        block_id = cursor.lastrowid
        # The class changed, so whatever was confirmed no longer describes
        # this timetable. The schedule's date provenance is NOT touched:
        # adding a class says nothing about who chose the semester dates.
        _unconfirm(connection, schedule_id)
        return {
            "id": block_id,
            "day_of_week": day,
            "start_time": start,
            "end_time": end,
        }

    return _in_transaction(connection, work)


def update_block(connection, employee_code, schedule_id, block_id, payload):
    """Change a class's weekday or times, within the same semester."""
    day, start, end = clean_block(payload)

    def work():
        employee_id = _employee_id(connection, employee_code)
        _schedule(connection, employee_id, schedule_id)
        _block(connection, schedule_id, block_id)
        _assert_block_fits(connection, schedule_id, day, start, end, ignore_id=block_id)
        connection.execute(
            """
            UPDATE class_blocks
            SET day_of_week = ?, start_time = ?, end_time = ?, source_note = NULL
            WHERE id = ?
            """,
            (day, start, end, block_id),
        )
        # The note described where the ORIGINAL times came from. Once a person
        # has changed them it is no longer true of this row, so it is cleared.
        # The schedule's own dates_provisional flag is untouched and is what
        # keeps the provisional-date fact alive.
        _unconfirm(connection, schedule_id)
        return {
            "id": block_id,
            "day_of_week": day,
            "start_time": start,
            "end_time": end,
        }

    return _in_transaction(connection, work)


def delete_block(connection, employee_code, schedule_id, block_id):
    """Remove one class from a semester.

    The semester itself stays, including its `dates_provisional` flag: losing
    the last migrated class must not erase the fact that nobody chose these
    dates. Removing a class is a change to the timetable, so confirmation is
    withdrawn - including when the last class goes, because "no classes" is a
    claim that needs confirming in its own right.
    """

    def work():
        employee_id = _employee_id(connection, employee_code)
        _schedule(connection, employee_id, schedule_id)
        block = _block(connection, schedule_id, block_id)
        connection.execute("DELETE FROM class_blocks WHERE id = ?", (block_id,))
        _unconfirm(connection, schedule_id)
        return {
            "day_of_week": block["day_of_week"],
            "start_time": block["start_time"],
            "end_time": block["end_time"],
        }

    return _in_transaction(connection, work)


# ------------------------------------------------------------- confirmation


def confirm_schedule(connection, employee_code, schedule_id, payload, reference_time=None):
    """Record that a supervisor confirmed one semester's timetable is complete.

    Confirming is a deliberate, separate action - never a side effect of
    creating a semester, adding or removing its last class, or simply viewing
    it (those all go through `create_schedule`/`create_block`/`delete_block`,
    none of which touch `confirmed_at` except to clear it). It means one of
    two distinct things, and the caller must say which by construction rather
    than by guessing from an empty body:

      - the listed classes ARE the worker's whole timetable for this
        semester, or
      - the worker genuinely has none, which is `acknowledge_no_classes: true`.

    Both require the caller to submit the semester's dates AND the exact
    classes it is confirming - not merely how many there are - and both are
    checked against what is actually stored, inside the same transaction
    that writes the confirmation:

      - dates that do not match the stored semester are a 409, not a silent
        confirmation of stale content the caller never saw refreshed.
      - the submitted `class_blocks` must name exactly the ids, weekdays and
        times currently stored for this schedule - no more, no fewer, none
        different - compared as sets so submission order cannot itself cause
        a false conflict. A matching *count* is not enough: a class that
        moved from Monday to Wednesday while another was removed can leave
        the count unchanged, and confirming that stale view would vouch for
        classes that no longer exist. Any difference is a 409, not a silent
        confirmation.
      - a stored class list of zero without `acknowledge_no_classes: true`
        is refused (400): a missing class list is not the same as a confirmed
        empty one, and a generic or malformed request body cannot confirm
        "no classes" by falling through a default.
      - `acknowledge_no_classes: true` while classes are actually stored is
        also refused (400): it would contradict what it is confirming.

    Confirming ALSO accepts the semester's current dates as correct, the same
    as if the supervisor had retyped them in `update_schedule`: migrated dates
    a supervisor has reviewed and is confirming are no longer merely assumed,
    so `dates_provisional` is cleared here too. This lets a supervisor accept
    correctly-migrated dates by confirming, without pointlessly retyping
    values that were already right.

    Confirming an already-confirmed semester with a matching snapshot is
    accepted, not refused: it does not change what the timetable says, only
    records that a supervisor looked again and it still stands, so
    `confirmed_at` is stamped with the current time. Because stored
    timestamps carry only minute precision (the same `TIME_FORMAT` as
    `employees.py`'s deactivation/retirement timestamps), two confirmations
    inside the same minute can legitimately store the same value - that is
    not a bug, and nothing here promises a visibly different timestamp on
    every call, only the current one at the application's existing
    precision.

    `reference_time` defaults to the current local time, in the plain
    wall-clock reading D025 uses for every stored datetime; tests pass a fixed
    time so results do not depend on when they run.
    """
    start, end, submitted_blocks, acknowledge_no_classes = clean_confirmation(payload)
    submitted_snapshot = sorted(submitted_blocks)

    def work():
        employee_id = _employee_id(connection, employee_code)
        schedule = _schedule(connection, employee_id, schedule_id)
        stored_rows = connection.execute(
            "SELECT id, day_of_week, start_time, end_time FROM class_blocks"
            " WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchall()
        actual_count = len(stored_rows)
        actual_snapshot = sorted(
            (row["id"], row["day_of_week"], row["start_time"], row["end_time"])
            for row in stored_rows
        )

        if start != schedule["start_date"] or end != schedule["end_date"]:
            raise TimetableConflict(
                "This semester's stored dates are "
                f"{schedule['start_date']} to {schedule['end_date']}, not "
                f"{start} to {end}. Reload this worker and confirm the dates "
                "actually shown."
            )
        if submitted_snapshot != actual_snapshot:
            raise TimetableConflict(
                "This semester's classes have changed since they were "
                "loaded. Reload this worker and confirm the classes actually "
                "shown."
            )
        if actual_count == 0 and not acknowledge_no_classes:
            raise TimetableValidationError(
                "Confirming a semester with no classes requires explicitly "
                "acknowledging that the worker has no classes "
                "(acknowledge_no_classes: true). A missing class list is not "
                "the same as a confirmed empty one."
            )
        if actual_count > 0 and acknowledge_no_classes:
            raise TimetableValidationError(
                "acknowledge_no_classes must not be true: this semester has "
                "class blocks recorded."
            )

        when = reference_time if reference_time is not None else datetime.now()
        timestamp = when.strftime(TIME_FORMAT)
        connection.execute(
            """
            UPDATE semester_schedules
            SET confirmed_at = ?, dates_provisional = 0
            WHERE id = ?
            """,
            (timestamp, schedule_id),
        )
        return {
            "id": schedule_id,
            "start_date": start,
            "end_date": end,
            "confirmed_at": timestamp,
            "dates_provisional": False,
            "class_count": actual_count,
        }

    return _in_transaction(connection, work)
