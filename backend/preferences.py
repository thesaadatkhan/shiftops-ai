"""Supervisor editing of shift preferences and approved leave (Phase 5C).

The database is authoritative; the frontend's validation is a convenience
and anything that reaches these functions is checked again.

**Shift preferences.** A worker's preference for one existing, dated shift is
either 'preferred', 'low', or absent. Absence IS neutral (D025) - there is no
third stored value for it. Setting a shift to neutral therefore deletes the
row rather than writing one, and setting it to 'preferred' or 'low' upserts
one row addressed by (employee, shift) - the same shift can only ever hold
one preference for one worker. Nothing here creates a shift or a recurring
template; every shift already exists before a preference can be set on it.

**Approved leave.** A leave period is a worker-owned row with its own id, in
the same style as a semester schedule: create, edit and delete are addressed
by employee code and, for edit/delete, the id within that worker. A period
must have a real end after its start, in the project's plain wall-clock
'YYYY-MM-DD HH:MM' form (D025). This module knows nothing about assignments
or eligibility - reconciling leave against assignments is Phase 6 work.
"""

import re
from datetime import datetime

from employees import EmployeeNotFound, find_by_code
from timetables import _in_transaction

DATETIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

VALID_PREFERENCES = {"preferred", "low", "neutral"}


class PreferenceValidationError(ValueError):
    """The submitted preference cannot be stored. Maps to HTTP 400."""


class ShiftNotFound(LookupError):
    """No such shift exists. Maps to HTTP 404."""


class LeaveValidationError(ValueError):
    """The submitted leave period cannot be stored. Maps to HTTP 400."""


class LeaveNotFound(LookupError):
    """No such approved-leave record for this worker. Maps to HTTP 404."""


class LeaveConflict(RuntimeError):
    """The request is well-formed but duplicates a stored period. HTTP 409."""


# ---------------------------------------------------------------- validation


def clean_preference(payload):
    if not isinstance(payload, dict):
        raise PreferenceValidationError("Expected an object with preference.")
    value = payload.get("preference")
    if value not in VALID_PREFERENCES:
        raise PreferenceValidationError(
            "preference must be one of 'preferred', 'low' or 'neutral'."
        )
    return value


def clean_datetime(value, label):
    """One 'YYYY-MM-DD HH:MM' point in the project's single local clock."""
    if not isinstance(value, str) or not DATETIME_PATTERN.match(value.strip()):
        raise LeaveValidationError(
            f"{label} must be a date and time in 'YYYY-MM-DD HH:MM' form."
        )
    text = value.strip()
    try:
        datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError as error:
        raise LeaveValidationError(f"{label} is not a real date and time.") from error
    return text


def clean_leave(payload):
    if not isinstance(payload, dict):
        raise LeaveValidationError(
            "Expected an object with start_datetime and end_datetime."
        )
    start = clean_datetime(payload.get("start_datetime"), "Leave start")
    end = clean_datetime(payload.get("end_datetime"), "Leave end")
    if end <= start:
        # String comparison is exact for this fixed-width, zero-padded form.
        raise LeaveValidationError(
            f"Leave must end ({end}) after it starts ({start})."
        )
    return start, end


# ------------------------------------------------------------------- lookups


def _employee_id(connection, employee_code):
    employee = find_by_code(connection, employee_code)
    if employee is None:
        raise EmployeeNotFound(f"No employee with code {employee_code}.")
    return employee["id"]


def _shift(connection, shift_id):
    row = connection.execute(
        "SELECT id FROM shifts WHERE id = ?", (shift_id,)
    ).fetchone()
    if row is None:
        raise ShiftNotFound(f"No shift {shift_id}.")
    return row


def _leave(connection, employee_id, leave_id):
    row = connection.execute(
        "SELECT * FROM approved_leave WHERE id = ? AND employee_id = ?",
        (leave_id, employee_id),
    ).fetchone()
    if row is None:
        raise LeaveNotFound(f"No approved leave {leave_id} for this worker.")
    return row


# -------------------------------------------------------------- preferences


def set_preference(connection, employee_code, shift_id, payload):
    """Set, change or clear one worker's preference for one existing shift.

    'neutral' deletes the row if one exists (a no-op if it does not, since
    "already neutral" and "made neutral" reach the same state). 'preferred'
    or 'low' upserts a single row addressed by (employee_id, shift_id), so
    changing a preference never leaves a stale second row behind.
    """
    preference = clean_preference(payload)

    def work():
        employee_id = _employee_id(connection, employee_code)
        _shift(connection, shift_id)
        if preference == "neutral":
            connection.execute(
                "DELETE FROM shift_preferences"
                " WHERE employee_id = ? AND shift_id = ?",
                (employee_id, shift_id),
            )
        else:
            connection.execute(
                """
                INSERT INTO shift_preferences (employee_id, shift_id, preference)
                VALUES (?, ?, ?)
                ON CONFLICT (employee_id, shift_id)
                DO UPDATE SET preference = excluded.preference
                """,
                (employee_id, shift_id, preference),
            )
        return {"shift_id": shift_id, "preference": preference}

    return _in_transaction(connection, work)


# -------------------------------------------------------------- approved leave


def add_leave(connection, employee_code, payload):
    start, end = clean_leave(payload)

    def work():
        employee_id = _employee_id(connection, employee_code)
        clash = connection.execute(
            "SELECT id FROM approved_leave"
            " WHERE employee_id = ? AND start_datetime = ? AND end_datetime = ?",
            (employee_id, start, end),
        ).fetchone()
        if clash is not None:
            raise LeaveConflict(
                f"That leave period ({start} to {end}) is already recorded "
                "for this worker."
            )
        connection.execute(
            "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime)"
            " VALUES (?, ?, ?)",
            (employee_id, start, end),
        )
        leave_id = connection.execute(
            "SELECT id FROM approved_leave"
            " WHERE employee_id = ? AND start_datetime = ? AND end_datetime = ?",
            (employee_id, start, end),
        ).fetchone()["id"]
        return {"id": leave_id, "start_datetime": start, "end_datetime": end}

    return _in_transaction(connection, work)


def update_leave(connection, employee_code, leave_id, payload):
    start, end = clean_leave(payload)

    def work():
        employee_id = _employee_id(connection, employee_code)
        _leave(connection, employee_id, leave_id)
        clash = connection.execute(
            "SELECT id FROM approved_leave"
            " WHERE employee_id = ? AND start_datetime = ? AND end_datetime = ?"
            " AND id != ?",
            (employee_id, start, end, leave_id),
        ).fetchone()
        if clash is not None:
            raise LeaveConflict(
                f"That leave period ({start} to {end}) is already recorded "
                "for this worker."
            )
        connection.execute(
            "UPDATE approved_leave SET start_datetime = ?, end_datetime = ?"
            " WHERE id = ?",
            (start, end, leave_id),
        )
        return {"id": leave_id, "start_datetime": start, "end_datetime": end}

    return _in_transaction(connection, work)


def delete_leave(connection, employee_code, leave_id):
    def work():
        employee_id = _employee_id(connection, employee_code)
        leave = _leave(connection, employee_id, leave_id)
        connection.execute("DELETE FROM approved_leave WHERE id = ?", (leave_id,))
        return {
            "start_datetime": leave["start_datetime"],
            "end_datetime": leave["end_datetime"],
        }

    return _in_transaction(connection, work)
