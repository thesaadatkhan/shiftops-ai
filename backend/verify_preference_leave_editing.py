"""Regression checks for supervisor editing of shift preferences and
approved leave (final Phase 5C functional batch, `backend/preferences.py`).

**Read this before running it.** `main.py` calls `ensure_schema()` at import
time against whatever `database.DATABASE_PATH` points at, so this module
redirects that path to a throwaway file *before* importing `main`, and refuses
to run if `main` has already been imported. The project's own
`backend/shiftops.db` is never created, opened or migrated here.

These checks call the real endpoint functions and read what they return, so
the exception-to-status-code mapping is covered as well as the stored result.
Real HTTP coverage for these routes lives in `verify_timetable_editing_http.py`.

Checks:

 1. A preference moves preferred -> low -> neutral, and neutral deletes the
    stored row rather than writing a third value.
 2. Unknown shift and unknown employee are 404.
 3. Approved leave: add, edit and delete.
 4. A non-positive leave interval is refused.
 5. Cross-worker leave ownership is a 404, not a 403.
 6. Everything written survives closing and reopening the database file.
 7. A duplicate leave period is refused as a conflict, and a failure inside
    the leave transaction rolls back cleanly, leaving other data untouched.

Run with:  python verify_preference_leave_editing.py
Exits non-zero if any check fails.
"""

import sys
import tempfile
from pathlib import Path

if "main" in sys.modules:  # pragma: no cover - defensive
    raise SystemExit(
        "main was imported before the database path was redirected; refusing to run."
    )

import database  # noqa: E402  - imported early on purpose, see the docstring

_TEMPORARY = tempfile.TemporaryDirectory()
_SCRATCH = Path(_TEMPORARY.name)
database.DATABASE_PATH = _SCRATCH / "bootstrap.db"

import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402

failures = []
_counter = {"n": 0}

SNAPSHOT_TABLES = [
    "employees",
    "semester_schedules",
    "class_blocks",
    "shifts",
    "shift_preferences",
    "approved_leave",
    "assignments",
]


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fresh_database():
    _counter["n"] += 1
    database.DATABASE_PATH = _SCRATCH / f"case-{_counter['n']}.db"
    connection = database.get_connection()
    database.create_schema(connection)
    return connection


def snapshot(connection):
    return {
        table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
        for table in SNAPSHOT_TABLES
    }


def add_worker(connection, code, name="Worker"):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active, seed_key)"
        " VALUES (?, ?, 'undergraduate', 20, 1, NULL)",
        (code, name),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]


def add_shift(connection, hall="Helix", start="2026-10-05 08:00", end="2026-10-05 12:00"):
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES (?, ?, ?, 1)",
        (hall, start, end),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ?", (hall, start)
    ).fetchone()["id"]


def expect_status(status, action, description):
    try:
        action()
        check(False, f"{description} (nothing was raised)")
        return None
    except HTTPException as error:
        check(
            error.status_code == status,
            f"{description} -> {error.status_code}: {error.detail}",
        )
        return error


def preferences_of(code):
    return main.get_employee_details(code)["shift_preferences"]


def leave_of(code):
    return main.get_employee_details(code)["approved_leave"]


# --------------------------------------------------------------------------
# 1 and 2. Preference lifecycle, and unknown shift/employee.
# --------------------------------------------------------------------------
def check_preference_lifecycle():
    connection = fresh_database()
    add_worker(connection, "SW-001", "Maria Alvarez")
    add_worker(connection, "SW-002", "Jordan Kim")
    shift_id = add_shift(connection)
    connection.close()

    check(preferences_of("SW-001") == [], "a shift with no preference starts absent from the list")

    result = main.set_shift_preference("SW-001", shift_id, {"preference": "preferred"})
    check(
        result == {"shift_id": shift_id, "preference": "preferred"},
        f"setting preferred returns the stable {{shift_id, preference}} shape ({result})",
    )
    check(
        [row["preference"] for row in preferences_of("SW-001")] == ["preferred"],
        "and it appears in the details view",
    )

    result = main.set_shift_preference("SW-001", shift_id, {"preference": "low"})
    check(result["preference"] == "low", "changing to low preference upserts, not duplicates")
    check(
        len(preferences_of("SW-001")) == 1 and preferences_of("SW-001")[0]["preference"] == "low",
        "so there is still exactly one row, now reading low",
    )

    result = main.set_shift_preference("SW-001", shift_id, {"preference": "neutral"})
    check(result == {"shift_id": shift_id, "preference": "neutral"}, "neutral is accepted")
    check(
        preferences_of("SW-001") == [],
        "and DELETES the row rather than storing a third value",
    )

    # Setting an already-neutral shift back to neutral is a harmless no-op.
    main.set_shift_preference("SW-001", shift_id, {"preference": "neutral"})
    check(preferences_of("SW-001") == [], "and reconfirming neutral changes nothing")

    expect_status(
        400,
        lambda: main.set_shift_preference("SW-001", shift_id, {"preference": "loved-it"}),
        "an invalid preference value is refused",
    )
    expect_status(
        400,
        lambda: main.set_shift_preference("SW-001", shift_id, {}),
        "a body missing preference is refused",
    )

    expect_status(
        404,
        lambda: main.set_shift_preference("SW-001", 999999, {"preference": "low"}),
        "an unknown shift is a 404",
    )
    expect_status(
        404,
        lambda: main.set_shift_preference("SW-999", shift_id, {"preference": "low"}),
        "an unknown employee is a 404",
    )

    # Setting SW-002's preference must never touch SW-001's.
    main.set_shift_preference("SW-001", shift_id, {"preference": "preferred"})
    main.set_shift_preference("SW-002", shift_id, {"preference": "low"})
    check(
        preferences_of("SW-001")[0]["preference"] == "preferred"
        and preferences_of("SW-002")[0]["preference"] == "low",
        "two workers hold independent preferences for the same shift",
    )


# --------------------------------------------------------------------------
# 3, 4 and 5. Approved leave: add/edit/delete, invalid interval, ownership.
# --------------------------------------------------------------------------
def check_leave_lifecycle():
    connection = fresh_database()
    add_worker(connection, "SW-001", "Maria Alvarez")
    add_worker(connection, "SW-002", "Jordan Kim")
    connection.close()
    path = database.DATABASE_PATH

    check(leave_of("SW-001") == [], "a worker starts with no approved leave")

    added = main.add_approved_leave(
        "SW-001", {"start_datetime": "2026-10-10 08:00", "end_datetime": "2026-10-10 14:00"}
    )
    check(
        added["start_datetime"] == "2026-10-10 08:00" and added["end_datetime"] == "2026-10-10 14:00",
        "adding leave returns the stored period",
    )
    check("id" in added, "and its id, for later edits")
    leave_id = added["id"]
    check(
        [row["start_datetime"] for row in leave_of("SW-001")] == ["2026-10-10 08:00"],
        "and it appears in the details view",
    )

    edited = main.edit_approved_leave(
        "SW-001", leave_id, {"start_datetime": "2026-10-10 09:00", "end_datetime": "2026-10-10 15:00"}
    )
    check(
        edited["start_datetime"] == "2026-10-10 09:00" and edited["end_datetime"] == "2026-10-10 15:00",
        "editing leave changes its stored period",
    )

    # --- invalid / non-positive intervals --------------------------------
    for label, body in [
        ("end equal to start", {"start_datetime": "2026-10-10 09:00", "end_datetime": "2026-10-10 09:00"}),
        ("end before start", {"start_datetime": "2026-10-10 09:00", "end_datetime": "2026-10-10 08:00"}),
        ("an unparseable datetime", {"start_datetime": "not a date", "end_datetime": "2026-10-10 09:00"}),
        ("a missing end_datetime", {"start_datetime": "2026-10-10 09:00"}),
    ]:
        expect_status(
            400,
            lambda body=body: main.add_approved_leave("SW-001", body),
            f"adding leave with {label} is refused",
        )

    # --- cross-worker ownership -------------------------------------------
    expect_status(
        404,
        lambda: main.edit_approved_leave(
            "SW-002", leave_id, {"start_datetime": "2026-11-01 08:00", "end_datetime": "2026-11-01 09:00"}
        ),
        "editing another worker's real leave id is a 404, not a 403",
    )
    expect_status(
        404,
        lambda: main.remove_approved_leave("SW-002", leave_id),
        "deleting another worker's real leave id is a 404",
    )
    expect_status(
        404,
        lambda: main.edit_approved_leave(
            "SW-001", 999999, {"start_datetime": "2026-11-01 08:00", "end_datetime": "2026-11-01 09:00"}
        ),
        "editing an unknown leave id is a 404",
    )

    # --- persistence across a fresh connection -----------------------------
    reopened = database.get_connection(path)
    row = reopened.execute(
        "SELECT start_datetime, end_datetime FROM approved_leave WHERE id = ?", (leave_id,)
    ).fetchone()
    check(
        row["start_datetime"] == "2026-10-10 09:00",
        "the edited leave survives reopening the database",
    )
    reopened.close()

    removed = main.remove_approved_leave("SW-001", leave_id)
    check(
        removed == {"start_datetime": "2026-10-10 09:00", "end_datetime": "2026-10-10 15:00"},
        f"deleting leave reports what was removed ({removed})",
    )
    check(leave_of("SW-001") == [], "and it is gone from the details view")


# --------------------------------------------------------------------------
# 7. Duplicate conflict and transaction rollback.
# --------------------------------------------------------------------------
def check_leave_conflict_and_rollback():
    import sqlite3

    connection = fresh_database()
    employee_id = add_worker(connection, "SW-001", "Maria Alvarez")
    other_id = add_worker(connection, "SW-002", "Jordan Kim")
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime)"
        " VALUES (?, '2026-10-10 08:00', '2026-10-10 14:00')",
        (other_id,),
    )
    connection.commit()
    path = database.DATABASE_PATH
    connection.close()

    main.add_approved_leave(
        "SW-001", {"start_datetime": "2026-10-10 08:00", "end_datetime": "2026-10-10 14:00"}
    )
    before_conn = database.get_connection(path)
    before = snapshot(before_conn)
    before_conn.close()

    expect_status(
        409,
        lambda: main.add_approved_leave(
            "SW-001", {"start_datetime": "2026-10-10 08:00", "end_datetime": "2026-10-10 14:00"}
        ),
        "adding an exact-duplicate leave period is refused as a conflict",
    )
    after_conn = database.get_connection(path)
    after = snapshot(after_conn)
    after_conn.close()
    check(
        before == after,
        "the rejected duplicate changed nothing, including the other worker's leave",
    )

    # A failure part-way through `update_leave`'s transaction rolls back.
    import preferences

    raw = database.get_connection(path)

    class FailingConnection:
        def __init__(self, real, fail_on):
            self._real = real
            self._fail_on = fail_on

        def execute(self, sql, parameters=()):
            if self._fail_on in " ".join(sql.split()):
                raise sqlite3.OperationalError(f"injected failure on: {self._fail_on}")
            return self._real.execute(sql, parameters)

        def commit(self):
            return self._real.commit()

        @property
        def isolation_level(self):
            return self._real.isolation_level

        @isolation_level.setter
        def isolation_level(self, value):
            self._real.isolation_level = value

    leave_id = raw.execute(
        "SELECT id FROM approved_leave WHERE employee_id = ?", (employee_id,)
    ).fetchone()["id"]
    failing = FailingConnection(raw, "UPDATE approved_leave")
    before_rollback = snapshot(raw)
    try:
        preferences.update_leave(
            failing,
            "SW-001",
            leave_id,
            {"start_datetime": "2026-11-01 08:00", "end_datetime": "2026-11-01 09:00"},
        )
        check(False, "the injected failure while editing leave propagated")
    except sqlite3.OperationalError:
        check(True, "the injected failure while editing leave propagated")
    raw.close()

    after_rollback_conn = database.get_connection(path)
    after_rollback = snapshot(after_rollback_conn)
    after_rollback_conn.close()
    check(
        before_rollback == after_rollback,
        "a failure inside the leave-edit transaction leaves every table unchanged",
    )


def run():
    check_preference_lifecycle()
    check_leave_lifecycle()
    check_leave_conflict_and_rollback()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("All preference/leave editing checks passed.")


if __name__ == "__main__":
    run()
