"""Creating and updating employee records.

Separate from `main.py` so the rules can be tested directly against an
isolated database, without HTTP and without importing the module that
migrates the real database on import.

Scope is deliberately narrow: employee code, name and student type. The code
is set when the worker is created and fixed from then on (D042), so editing
covers the name and student type. The weekly hour limit stays at its 20-hour
default and new workers start active.
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
# Checked when creating, which is the only time a code is entered (D036).
# Codes already stored are left exactly as they are; nothing here rewrites or
# removes them.
EMPLOYEE_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
EMPLOYEE_CODE_RULE = (
    "employee_code may contain only letters, digits, hyphens and underscores"
)

# Every field a create or update request must supply. Not all of them are
# editable: `employee_code` is required in the payload so an update can be
# checked against the stored code, but it is fixed once the worker exists
# (D042). Only `full_name` and `student_type` can actually change.
EMPLOYEE_INPUT_FIELDS = ("employee_code", "full_name", "student_type")

# The fields a create request supplies. The employee code is NOT among them:
# the backend issues it (D034). A request that sends one is rejected rather
# than ignored, so nobody is left believing they chose the code.
NEW_EMPLOYEE_FIELDS = ("full_name", "student_type")

# The automatic sequence: "SW-" and at least three digits, zero-padded.
CODE_PREFIX = "SW-"
CODE_DIGITS = 3

# Which stored codes count as part of that sequence, and therefore reserve
# their number. Deliberately strict and deliberately generous in one way each:
#
#   - Case-insensitive, so `sw-031` reserves 31 just as `SW-031` does.
#   - [0-9] rather than \d, because \d also matches other scripts' digits in
#     Python; only ASCII digits are the sequence.
#   - The suffix must be ENTIRELY digits, so `SW-12A` and `SW-3-B` are not
#     sequence codes and reserve nothing.
#   - Any amount of zero padding, so `SW-31`, `SW-031` and `SW-0000031` all
#     reserve 31.
#
# Codes that do not match - legacy or hand-made ones like `TEMP-5` - are left
# completely alone. They are never interpreted as sequence values, never
# renumbered, and never rewritten.
# Matched with `fullmatch`, deliberately: with `match` a trailing "$" still
# allows a trailing newline, so a code ending in one would otherwise be read
# as a sequence code.
SEQUENCE_CODE_PATTERN = re.compile(rf"{CODE_PREFIX}([0-9]+)", re.IGNORECASE)

# SQLite stores INTEGER as signed 64-bit. A number above this cannot be held
# in the counter, so it is reported rather than silently wrapped or skipped.
MAX_EMPLOYEE_NUMBER = 9223372036854775807
_MAX_NUMBER_DIGITS = str(MAX_EMPLOYEE_NUMBER)


class EmployeeValidationError(ValueError):
    """Submitted employee details are not usable."""


class DuplicateEmployeeCode(ValueError):
    """Another employee already uses this employee code."""


class EmployeeNotFound(LookupError):
    """No employee exists with the given employee code."""


class CodeAllocationError(RuntimeError):
    """An employee code could not be issued."""


def format_employee_code(number):
    """Render a sequence number as a code: 1 -> 'SW-001', 1000 -> 'SW-1000'.

    Padded to at least three digits and never truncated, so the sequence
    simply grows a digit at SW-999 -> SW-1000.
    """
    return f"{CODE_PREFIX}{number:0{CODE_DIGITS}d}"


def sequence_number(employee_code):
    """The number a stored code reserves, or None if it reserves nothing.

    Raises CodeAllocationError if the code is a sequence code whose number is
    too large for the counter to hold.

    The leading zeros are stripped and the remaining digits are measured
    BEFORE any integer conversion. That order matters: Python refuses to
    convert a string of more than 4300 digits at all, so a hand-made code
    like `SW-` followed by five thousand digits would raise a bare ValueError
    from inside int() rather than the controlled error this promises. Length
    is compared first, then - only for a number of exactly the maximum length
    - the digits themselves, which for equal-length digit strings compare the
    same way lexicographically as numerically.

    Stripping zeros first is also what makes padding irrelevant: `SW-31`,
    `SW-031` and `SW-` plus five thousand zeros then `31` all reserve 31.
    """
    match = SEQUENCE_CODE_PATTERN.fullmatch(employee_code or "")
    if match is None:
        return None

    digits = match.group(1).lstrip("0")
    if digits == "":
        # `SW-0`, `SW-000`: a sequence code reserving nothing, since the
        # sequence itself starts at 1.
        return 0

    too_long = len(digits) > len(_MAX_NUMBER_DIGITS)
    too_large = (
        len(digits) == len(_MAX_NUMBER_DIGITS) and digits > _MAX_NUMBER_DIGITS
    )
    if too_long or too_large:
        raise CodeAllocationError(
            f"Employee code {employee_code} holds a number larger "
            f"than this application can track ({MAX_EMPLOYEE_NUMBER}). "
            "Automatic codes cannot continue above it. Remove or correct "
            "that code before creating more workers."
        )

    return int(digits)


def highest_reserved_number(connection):
    """The largest number reserved by any code the database has ever issued.

    Both live employees and retired codes are consulted. Retired codes matter
    most: once the employee row is gone, its number would otherwise look free,
    and the next create would hand a deleted worker's identifier to somebody
    new (D040).

    Returns 0 when nothing is reserved, so a genuinely unused database starts
    at SW-001.
    """
    highest = 0
    for table in ("employees", "retired_employee_codes"):
        for row in connection.execute(f"SELECT employee_code FROM {table}"):
            # sequence_number() raises CodeAllocationError for a number the
            # counter cannot hold, so the bound is enforced in one place.
            number = sequence_number(row["employee_code"])
            if number is None:
                continue
            highest = max(highest, number)
    return highest


def allocation_progress(connection):
    """How far the sequence has been issued, or None if it has never run."""
    row = connection.execute(
        "SELECT highest_issued FROM code_allocation WHERE id = 1"
    ).fetchone()
    return None if row is None else row["highest_issued"]


def reserve_up_to(connection, number):
    """Move the counter to `number` if it is not already at least that high.

    Used by demo initialization so the codes it writes are reserved for the
    sequence. The counter never moves backwards.
    """
    current = allocation_progress(connection)
    if current is None:
        connection.execute(
            "INSERT INTO code_allocation (id, highest_issued) VALUES (1, ?)",
            (number,),
        )
    elif number > current:
        connection.execute(
            "UPDATE code_allocation SET highest_issued = ? WHERE id = 1", (number,)
        )


CODE_IMMUTABLE_RULE = (
    "employee_code cannot be changed once a worker has been created"
)


def clean_employee_input(payload, require_url_safe_code=True):
    """Validate and normalise submitted details.

    Surrounding whitespace is stripped, so " SW-031 " and "SW-031" are the
    same code and a field containing only spaces counts as missing rather
    than as a valid value.

    `require_url_safe_code` is off when editing, because an edit refuses to
    change the code at all and so has nothing to validate. Applying the rule
    there would make a worker whose code predates it permanently uneditable -
    exactly the problem D036 was introduced to fix.
    """
    if not isinstance(payload, dict):
        raise EmployeeValidationError("Expected an object with employee details.")

    cleaned = {}
    for field in EMPLOYEE_INPUT_FIELDS:
        value = payload.get(field)
        if value is None:
            raise EmployeeValidationError(f"{field} is required.")
        if not isinstance(value, str):
            raise EmployeeValidationError(f"{field} must be text.")
        stripped = value.strip()
        if stripped == "":
            raise EmployeeValidationError(f"{field} cannot be blank.")
        cleaned[field] = stripped

    if require_url_safe_code and not EMPLOYEE_CODE_PATTERN.match(
        cleaned["employee_code"]
    ):
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


def _assert_code_available(connection, employee_code):
    """Reject a code already used by an employee.

    Compared case-insensitively so 'sw-031' cannot shadow 'SW-031'; the code
    is stored exactly as typed.

    Only creation calls this. It used to take the editing worker's id so a
    rename could be screened without matching the worker against themselves;
    codes are immutable now (D042), so there is no such case left.
    """
    row = connection.execute(
        "SELECT id FROM employees WHERE employee_code = ? COLLATE NOCASE",
        (employee_code,),
    ).fetchone()
    if row is not None:
        raise DuplicateEmployeeCode(
            f"Employee code {employee_code} is already in use."
        )


CODE_IS_AUTOMATIC_RULE = (
    "employee_code is assigned automatically and cannot be supplied"
)


def clean_new_employee_input(payload):
    """Validate the details a create request may send: name and student type.

    A supplied `employee_code` is refused rather than ignored. Silently
    dropping it would let somebody submit SW-500, get SW-031 back, and have no
    idea their choice was discarded.
    """
    if not isinstance(payload, dict):
        raise EmployeeValidationError("Expected an object with employee details.")

    if "employee_code" in payload:
        raise EmployeeValidationError(
            f"{CODE_IS_AUTOMATIC_RULE}. Send only "
            f"{' and '.join(NEW_EMPLOYEE_FIELDS)}; the new worker's ID is "
            "issued when they are saved."
        )

    cleaned = {}
    for field in NEW_EMPLOYEE_FIELDS:
        value = payload.get(field)
        if value is None:
            raise EmployeeValidationError(f"{field} is required.")
        if not isinstance(value, str):
            raise EmployeeValidationError(f"{field} must be text.")
        stripped = value.strip()
        if stripped == "":
            raise EmployeeValidationError(f"{field} cannot be blank.")
        cleaned[field] = stripped

    if cleaned["student_type"] not in VALID_STUDENT_TYPES:
        raise EmployeeValidationError(
            f"student_type must be one of: {', '.join(VALID_STUDENT_TYPES)}."
        )

    return cleaned


def create_employee(connection, payload):
    """Add a worker under the next automatic employee code. Returns the row.

    The whole thing is one `BEGIN IMMEDIATE` transaction: read the counter,
    work out the next number, insert the worker, advance the counter, commit.

    `BEGIN IMMEDIATE` takes SQLite's write lock at the start rather than on
    the first write, so a second connection doing the same thing waits (up to
    sqlite3's connect timeout) instead of interleaving. Two creates therefore
    cannot read the same counter value and both claim it. This is SQLite's own
    write serialization - not a lock inside one Python process, which would do
    nothing about a second process or a second worker.

    If anything fails, the whole transaction rolls back: no worker is left
    created without the counter advancing, and the counter never advances
    without the worker existing.
    """
    details = clean_new_employee_input(payload)

    # Explicit transaction control. sqlite3's implicit handling commits at
    # points we do not choose and will not hold a write lock across the read
    # of the counter and the write that depends on it.
    previous_isolation = connection.isolation_level
    connection.isolation_level = None

    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            progress = allocation_progress(connection)
            if progress is None:
                # First ever allocation in this database. Start from whatever
                # existing live and retired codes already reserve, so an
                # established database continues above its highest number
                # instead of colliding with SW-001.
                progress = highest_reserved_number(connection)

            number = progress + 1
            if number > MAX_EMPLOYEE_NUMBER:
                raise CodeAllocationError(
                    "The automatic employee-code sequence has reached its "
                    f"maximum ({MAX_EMPLOYEE_NUMBER}). No further codes can be "
                    "issued."
                )

            code = format_employee_code(number)

            # Belt and braces. The counter should make this impossible, but a
            # collision here would silently give two workers one identifier,
            # so it is checked rather than assumed. A database hand-edited to
            # contain SW-031 without reserving it lands here.
            clash = connection.execute(
                "SELECT id FROM employees WHERE employee_code = ? COLLATE NOCASE",
                (code,),
            ).fetchone()
            if clash is not None:
                raise CodeAllocationError(
                    f"Automatic allocation produced {code}, which an existing "
                    "worker already uses. The stored codes and the allocation "
                    "counter disagree; no worker was created."
                )

            connection.execute(
                """
                INSERT INTO employees (employee_code, full_name, student_type)
                VALUES (?, ?, ?)
                """,
                (code, details["full_name"], details["student_type"]),
            )
            reserve_up_to(connection, number)

            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.isolation_level = previous_isolation

    return find_by_code(connection, code)


def update_employee(connection, current_code, payload):
    """Edit a worker's name and student type.

    The employee code is immutable once the worker exists (D042). It is a
    visible operational identifier that ends up in schedules, reports and
    history, so letting it be edited would make one worker appear under two
    identifiers and could free the old one for accidental reuse. Only the
    name and student type are written here.

    Nothing else moves either: the row's internal id, active status, weekly
    hour limit and seed origin are untouched, so every course, class meeting,
    approved leave period, shift preference and assignment stays attached.

    There is no duplicate-code check any more. It existed only so a rename
    could be screened against other workers; an edit that cannot change the
    code cannot collide with one.
    """
    existing = find_by_code(connection, current_code)
    if existing is None:
        # Checked before anything else, so an unknown worker is reported as
        # missing rather than as an immutability problem.
        raise EmployeeNotFound(f"No employee with code {current_code}.")

    details = clean_employee_input(payload, require_url_safe_code=False)

    # Compared exactly, so "sw-001" counts as a change from "SW-001" and is
    # refused too. Accepting a case-only edit would silently rewrite the
    # identifier people read, which is the thing this rule prevents.
    if details["employee_code"] != existing["employee_code"]:
        raise EmployeeValidationError(
            f"{CODE_IMMUTABLE_RULE}. This worker is "
            f"{existing['employee_code']}; the request asked for "
            f"{details['employee_code']}. Edit the name or student type "
            "instead."
        )

    connection.execute(
        "UPDATE employees SET full_name = ?, student_type = ? WHERE id = ?",
        (details["full_name"], details["student_type"], existing["id"]),
    )
    connection.commit()
    return find_by_code(connection, existing["employee_code"])


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


class DeletionBlocked(RuntimeError):
    """The worker has assignments, so their history must be preserved."""


# Records that belong to one worker and mean nothing without them. Shifts are
# deliberately absent: a shift is a staffing requirement of a residence hall,
# shared by everyone, and deleting a worker must never remove one.
OWNED_RECORD_COUNTS = {
    "semester_schedules": (
        "SELECT COUNT(*) AS n FROM semester_schedules WHERE employee_id = ?"
    ),
    "class_blocks": (
        "SELECT COUNT(*) AS n FROM class_blocks b "
        "JOIN semester_schedules s ON s.id = b.schedule_id WHERE s.employee_id = ?"
    ),
    # Legacy timetable rows. A database migrated from the course-based model
    # still holds them, and they belong to this worker just as much as the
    # blocks do, so deleting the worker must take them too.
    "courses": "SELECT COUNT(*) AS n FROM courses WHERE employee_id = ?",
    "class_meetings": (
        "SELECT COUNT(*) AS n FROM class_meetings m "
        "JOIN courses c ON c.id = m.course_id WHERE c.employee_id = ?"
    ),
    "shift_preferences": (
        "SELECT COUNT(*) AS n FROM shift_preferences WHERE employee_id = ?"
    ),
    "approved_leave": "SELECT COUNT(*) AS n FROM approved_leave WHERE employee_id = ?",
}


def delete_employee(connection, employee_code, retired_at=None):
    """Permanently delete a worker and the records that belong only to them.

    Their semester schedules and class blocks go with them, and so do any
    legacy course rows a migrated database still holds. Shifts are untouched:
    a shift is a hall's staffing requirement shared by everyone.

    Allowed only when the worker has NO assignments at all - not just none
    ahead of them. An assignment is a record that this person worked, or is
    down to work, a particular shift; deleting the worker would leave that
    history referring to nobody. A worker who has ever been assigned is
    deactivated instead, which keeps everything and is reversible.

    The check and the deletes share one `BEGIN IMMEDIATE` transaction. That
    matters: `BEGIN IMMEDIATE` takes the write lock up front, so no other
    connection can insert an assignment between the moment we find none and
    the moment the row is gone. Checking first and deleting afterwards in
    separate transactions would leave exactly that gap.

    Any failure rolls the whole thing back, so a worker is never left with
    half their records removed.

    Shifts are untouched, and so is every other worker's data.

    The employee code is written to `retired_employee_codes` in the same
    transaction. Phase 5C's automatic allocator (D034) must never reissue a
    number that has been used, and once the employee row is gone the code
    would otherwise be indistinguishable from one that was never used. Only
    the code and the time are kept - no copy of the deleted worker's details.

    Returns a summary of what was removed, for the confirmation message.
    """
    when = retired_at if retired_at is not None else datetime.now()

    try:
        connection.execute("BEGIN IMMEDIATE")

        existing = find_by_code(connection, employee_code)
        if existing is None:
            raise EmployeeNotFound(f"No employee with code {employee_code}.")

        employee_id = existing["id"]

        assignments = connection.execute(
            "SELECT COUNT(*) AS n FROM assignments WHERE employee_id = ?",
            (employee_id,),
        ).fetchone()["n"]

        # A worker can also be scheduling history without a live assignment:
        # named in a schedule proposal (Phase 7 increment 3), or named as the
        # before/after worker in an assignment-change audit record. Both
        # tables have foreign keys to employees, so deleting past this point
        # without checking them would otherwise reach an unhandled SQLite
        # foreign-key error instead of the controlled refusal below.
        proposal_history = connection.execute(
            "SELECT COUNT(*) AS n FROM proposal_assignments WHERE employee_id = ?",
            (employee_id,),
        ).fetchone()["n"]
        audit_history = connection.execute(
            "SELECT COUNT(*) AS n FROM assignment_audit"
            " WHERE employee_id_before = ? OR employee_id_after = ?",
            (employee_id, employee_id),
        ).fetchone()["n"]

        if assignments or proposal_history or audit_history:
            reasons = []
            if assignments:
                reasons.append(f"{assignments} assignment(s)")
            if proposal_history:
                reasons.append(f"{proposal_history} schedule proposal reference(s)")
            if audit_history:
                reasons.append(f"{audit_history} scheduling audit record(s)")
            raise DeletionBlocked(
                f"{existing['full_name']} ({existing['employee_code']}) has "
                f"{', '.join(reasons)} on record and cannot be deleted. "
                "Scheduling history must be preserved. Deactivate them "
                "instead - that keeps every record and can be undone."
            )

        removed = {
            name: connection.execute(query, (employee_id,)).fetchone()["n"]
            for name, query in OWNED_RECORD_COUNTS.items()
        }

        # Blocks hang off schedules, and legacy meetings hang off courses, so
        # the owned rows go before their owners in both cases.
        connection.execute(
            "DELETE FROM class_blocks WHERE schedule_id IN "
            "(SELECT id FROM semester_schedules WHERE employee_id = ?)",
            (employee_id,),
        )
        connection.execute(
            "DELETE FROM semester_schedules WHERE employee_id = ?", (employee_id,)
        )
        connection.execute(
            "DELETE FROM class_meetings WHERE course_id IN "
            "(SELECT id FROM courses WHERE employee_id = ?)",
            (employee_id,),
        )
        connection.execute("DELETE FROM courses WHERE employee_id = ?", (employee_id,))
        # `migrated_employees` is internal migration bookkeeping, not
        # scheduling history a supervisor needs preserved (unlike
        # `proposal_assignments`/`assignment_audit`, checked above) - a
        # deleted employee_id is never reissued (D040's numbering is
        # permanent), so there is no future worker this row could wrongly
        # protect from re-migration. It has a foreign key to `employees`
        # like the tables just above it, so it must go before the employee
        # row too.
        connection.execute(
            "DELETE FROM migrated_employees WHERE employee_id = ?", (employee_id,)
        )
        connection.execute(
            "DELETE FROM shift_preferences WHERE employee_id = ?", (employee_id,)
        )
        connection.execute(
            "DELETE FROM approved_leave WHERE employee_id = ?", (employee_id,)
        )
        connection.execute("DELETE FROM employees WHERE id = ?", (employee_id,))

        # REPLACE rather than IGNORE: if this code was created and deleted
        # again, the ledger should show the most recent retirement. Either way
        # the number stays spent.
        connection.execute(
            "INSERT OR REPLACE INTO retired_employee_codes "
            "(employee_code, retired_at) VALUES (?, ?)",
            (existing["employee_code"], when.strftime(TIME_FORMAT)),
        )

        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return {
        "employee_code": existing["employee_code"],
        "full_name": existing["full_name"],
        "removed": removed,
    }


def retired_employee_codes(connection):
    """Every employee code that has been permanently deleted."""
    return [
        row["employee_code"]
        for row in connection.execute(
            "SELECT employee_code FROM retired_employee_codes ORDER BY employee_code"
        )
    ]
