"""Creating and updating employee records.

Separate from `main.py` so the rules can be tested directly against an
isolated database, without HTTP and without importing the module that
migrates the real database on import.

Scope is deliberately narrow: employee code, name and student type. The
weekly hour limit stays at its 20-hour default and new workers start active.
Creating a worker does NOT generate classes, shift preferences, approved
leave or assignments - the demo course-load conventions describe generated
demo data, not workers a supervisor enters by hand.
"""

import re
from datetime import datetime

from synthetic_data import TIME_FORMAT

VALID_STUDENT_TYPES = ("undergraduate", "masters")

# An employee code appears inside the URL of PUT /api/employees/{employee_code}.
# A code containing "/", "?", "#" or similar cannot be addressed by that route
# even when the frontend percent-encodes it, so a worker saved with one would
# be impossible to edit afterwards. Restricting codes to letters, digits,
# hyphens and underscores keeps every accepted code addressable.
#
# This is checked when saving. Codes already stored are left exactly as they
# are; nothing here rewrites or removes them.
EMPLOYEE_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
EMPLOYEE_CODE_RULE = (
    "employee_code may contain only letters, digits, hyphens and underscores"
)

EDITABLE_FIELDS = ("employee_code", "full_name", "student_type")


class EmployeeValidationError(ValueError):
    """Submitted employee details are not usable."""


class DuplicateEmployeeCode(ValueError):
    """Another employee already uses this employee code."""


class EmployeeNotFound(LookupError):
    """No employee exists with the given employee code."""


def clean_employee_input(payload):
    """Validate and normalise submitted details.

    Surrounding whitespace is stripped, so " SW-031 " and "SW-031" are the
    same code and a field containing only spaces counts as missing rather
    than as a valid value.
    """
    if not isinstance(payload, dict):
        raise EmployeeValidationError("Expected an object with employee details.")

    cleaned = {}
    for field in EDITABLE_FIELDS:
        value = payload.get(field)
        if value is None:
            raise EmployeeValidationError(f"{field} is required.")
        if not isinstance(value, str):
            raise EmployeeValidationError(f"{field} must be text.")
        stripped = value.strip()
        if stripped == "":
            raise EmployeeValidationError(f"{field} cannot be blank.")
        cleaned[field] = stripped

    if not EMPLOYEE_CODE_PATTERN.match(cleaned["employee_code"]):
        raise EmployeeValidationError(f"{EMPLOYEE_CODE_RULE}.")

    if cleaned["student_type"] not in VALID_STUDENT_TYPES:
        raise EmployeeValidationError(
            f"student_type must be one of: {', '.join(VALID_STUDENT_TYPES)}."
        )

    return cleaned


def find_by_code(connection, employee_code):
    return connection.execute(
        "SELECT * FROM employees WHERE employee_code = ?", (employee_code,)
    ).fetchone()


def _assert_code_available(connection, employee_code, existing_id=None):
    """Reject a code already used by a different employee.

    Compared case-insensitively so 'sw-031' cannot shadow 'SW-031'; the code
    is stored exactly as typed.
    """
    row = connection.execute(
        "SELECT id FROM employees WHERE employee_code = ? COLLATE NOCASE",
        (employee_code,),
    ).fetchone()
    if row is not None and row["id"] != existing_id:
        raise DuplicateEmployeeCode(
            f"Employee code {employee_code} is already in use."
        )


def create_employee(connection, payload):
    """Add a worker. Returns the stored row."""
    details = clean_employee_input(payload)
    _assert_code_available(connection, details["employee_code"])

    connection.execute(
        """
        INSERT INTO employees (employee_code, full_name, student_type)
        VALUES (?, ?, ?)
        """,
        (details["employee_code"], details["full_name"], details["student_type"]),
    )
    connection.commit()
    return find_by_code(connection, details["employee_code"])


def update_employee(connection, current_code, payload):
    """Edit a worker's details, identified by their current employee code.

    The row's internal id is never changed, so every course, class meeting,
    approved leave period, shift preference and assignment that references
    this employee keeps pointing at the same worker even when the employee
    code itself is edited. Active status, weekly hour limit and seed origin
    are left alone.
    """
    existing = find_by_code(connection, current_code)
    if existing is None:
        raise EmployeeNotFound(f"No employee with code {current_code}.")

    details = clean_employee_input(payload)
    _assert_code_available(connection, details["employee_code"], existing_id=existing["id"])

    connection.execute(
        """
        UPDATE employees
        SET employee_code = ?, full_name = ?, student_type = ?
        WHERE id = ?
        """,
        (
            details["employee_code"],
            details["full_name"],
            details["student_type"],
            existing["id"],
        ),
    )
    connection.commit()
    return find_by_code(connection, details["employee_code"])


class DeactivationBlocked(RuntimeError):
    """The worker still has assignments that have not finished."""


def unfinished_assignments(connection, employee_id, reference_time):
    """Assigned shifts that have not finished by `reference_time`.

    A shift blocks deactivation while it is still running or has not started
    yet, which is exactly `end_datetime > reference_time`. Using the END
    rather than the start is deliberate: a shift that began an hour ago is
    still being worked, and treating only future start times as unresolved
    would let a worker be deactivated mid-shift.

    Shifts that already finished are history. They stay attached to the
    worker and never block anything.
    """
    return connection.execute(
        """
        SELECT s.hall, s.start_datetime, s.end_datetime
        FROM assignments a
        JOIN shifts s ON s.id = a.shift_id
        WHERE a.employee_id = ? AND s.end_datetime > ?
        ORDER BY s.start_datetime
        """,
        (employee_id, reference_time.strftime(TIME_FORMAT)),
    ).fetchall()


def set_active(connection, employee_code, active, reference_time=None):
    """Deactivate or reactivate a worker.

    Only the `is_active` flag changes. The worker's internal id, employee
    code, name, student type, weekly limit, seed provenance, courses, class
    meetings, shift preferences, approved leave and assignments are all left
    exactly as they are - deactivating is not a soft delete.

    Deactivation is refused while the worker has assignments that have not
    finished, and the refusal lists them. Nothing is deleted, cancelled or
    reassigned automatically: resolving those shifts is a scheduling decision
    for a person to make.

    `reference_time` defaults to the current local time. The application uses
    one local simulation clock with no timezone conversion (D025), so a naive
    local `now` is the right reading of "has this shift finished?". Tests pass
    a fixed time so their results do not depend on when they run.
    """
    existing = find_by_code(connection, employee_code)
    if existing is None:
        raise EmployeeNotFound(f"No employee with code {employee_code}.")

    if not active:
        when = reference_time if reference_time is not None else datetime.now()
        blocking = unfinished_assignments(connection, existing["id"], when)
        if blocking:
            listed = "; ".join(
                f"{row['hall']} {row['start_datetime']} to {row['end_datetime']}"
                for row in blocking
            )
            raise DeactivationBlocked(
                f"{existing['full_name']} still has "
                f"{len(blocking)} unfinished assignment(s): {listed}. "
                "Reassign or remove those shifts first; deactivating will not "
                "cancel them."
            )

    connection.execute(
        "UPDATE employees SET is_active = ? WHERE id = ?",
        (1 if active else 0, existing["id"]),
    )
    connection.commit()
    return find_by_code(connection, employee_code)
