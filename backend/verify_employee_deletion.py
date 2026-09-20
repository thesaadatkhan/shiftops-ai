"""Regression checks for permanently deleting an employee.

Everything here runs against throwaway in-memory or temporary databases.
The project's own `backend/shiftops.db` is never opened, read or modified.

Checks:

1. A worker with courses, class meetings, preferences and leave is deleted
   along with exactly those records.
2. Shared shifts survive, and so does every other worker and their records.
3. Deletion is refused when the worker has an assignment, whether it is
   historical, in progress, or still ahead. Nothing is removed.
4. An unknown employee code reports not-found, and deleting twice reports
   not-found the second time rather than corrupting anything.
5. A failure partway through rolls the whole thing back - the worker and
   every one of their records are still there.
6. The assignment check runs inside the write transaction, so it cannot be
   invalidated by a concurrent insert.
7. Deletion persists across closing and reopening the database file.
8. The retired employee code survives the deletion and the restart, and
   demo initialization refuses on a database that holds one.

Run with:  python verify_employee_deletion.py
Exits non-zero if any check fails.
"""

import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from database import create_schema, get_connection
from employees import (
    DeletionBlocked,
    EmployeeNotFound,
    create_employee,
    delete_employee,
    find_by_code,
    retired_employee_codes,
)
from seed import DatabaseNotEmpty, initialize_demo_data

RETIRED_AT = datetime(2026, 10, 7, 12, 0)

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fixture():
    connection = get_connection(":memory:")
    create_schema(connection)
    return connection


def add_worker(connection, code, name):
    return create_employee(
        connection,
        {"employee_code": code, "full_name": name, "student_type": "undergraduate"},
    )


def add_shift(connection, hall, start, end):
    connection.execute(
        "INSERT OR IGNORE INTO shifts (hall, start_datetime, end_datetime) VALUES (?, ?, ?)",
        (hall, start, end),
    )
    return connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ?", (hall, start)
    ).fetchone()["id"]


def give_records(connection, employee_id, label="Writing A"):
    """One course with two meetings, one leave period, one preference."""
    connection.execute(
        "INSERT INTO courses (employee_id, course_label) VALUES (?, ?)",
        (employee_id, label),
    )
    course_id = connection.execute(
        "SELECT id FROM courses WHERE employee_id = ? AND course_label = ?",
        (employee_id, label),
    ).fetchone()["id"]
    for day in (0, 2):
        connection.execute(
            "INSERT INTO class_meetings (course_id, day_of_week, start_time, end_time)"
            " VALUES (?, ?, ?, ?)",
            (course_id, day, "09:00", "10:15"),
        )
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime)"
        " VALUES (?, ?, ?)",
        (employee_id, "2026-10-10 08:00", "2026-10-10 14:00"),
    )
    shift_id = add_shift(connection, "Vega", "2026-10-05 17:00", "2026-10-05 22:00")
    connection.execute(
        "INSERT INTO shift_preferences (employee_id, shift_id, preference)"
        " VALUES (?, ?, ?)",
        (employee_id, shift_id, "preferred"),
    )
    connection.commit()
    return shift_id


def counts(connection):
    tables = (
        "employees",
        "courses",
        "class_meetings",
        "shifts",
        "shift_preferences",
        "approved_leave",
        "assignments",
    )
    return {
        table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in tables
    }


def assign(connection, employee_id, shift_id):
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (employee_id, shift_id),
    )
    connection.commit()


class FailingConnection:
    """A real connection that raises on the Nth DELETE it is asked to run.

    Used to prove the rollback works. `sqlite3.Connection.execute` is
    read-only, so the failure cannot be patched onto the connection itself;
    this forwards every call to the real one instead.
    """

    def __init__(self, real, fail_on_delete):
        self._real = real
        self._fail_on_delete = fail_on_delete
        self.deletes = 0

    def execute(self, sql, *args):
        if sql.strip().upper().startswith("DELETE"):
            self.deletes += 1
            if self.deletes == self._fail_on_delete:
                raise sqlite3.OperationalError("injected failure")
        return self._real.execute(sql, *args)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()


def blocked_case(label, start, end):
    """Deletion must be refused whatever the shift's timing."""
    connection = fixture()
    worker = add_worker(connection, "SW-001", "Alice Adams")
    give_records(connection, worker["id"])
    shift_id = add_shift(connection, "Capella", start, end)
    assign(connection, worker["id"], shift_id)
    before = counts(connection)

    try:
        delete_employee(connection, "SW-001", retired_at=RETIRED_AT)
        check(False, f"deletion should be refused for a {label} assignment")
    except DeletionBlocked as error:
        message = str(error)
        check("Deactivate them instead" in message, f"a {label} assignment blocks deletion")
        check(
            "history must be preserved" in message.lower(),
            f"the {label} refusal explains that history is preserved",
        )

    check(counts(connection) == before, f"nothing was removed after the {label} refusal")
    check(
        find_by_code(connection, "SW-001") is not None,
        f"the worker still exists after the {label} refusal",
    )
    check(
        retired_employee_codes(connection) == [],
        f"a refused deletion does not retire the code ({label})",
    )
    connection.close()


def main():
    # 1-2. A successful deletion removes the worker's own records only.
    connection = fixture()
    alice = add_worker(connection, "SW-001", "Alice Adams")
    bob = add_worker(connection, "SW-002", "Bob Brown")
    shared_shift = give_records(connection, alice["id"])
    give_records(connection, bob["id"], label="Chemistry B")
    # Bob prefers the same shift Alice does - deleting her must not touch it.
    connection.execute(
        "INSERT OR IGNORE INTO shift_preferences (employee_id, shift_id, preference)"
        " VALUES (?, ?, ?)",
        (bob["id"], shared_shift, "low"),
    )
    connection.commit()
    before = counts(connection)

    result = delete_employee(connection, "SW-001", retired_at=RETIRED_AT)
    after = counts(connection)

    check(find_by_code(connection, "SW-001") is None, "the worker is gone")
    check(result["full_name"] == "Alice Adams", "the result names the deleted worker")
    check(
        result["removed"] == {
            "courses": 1,
            "class_meetings": 2,
            "shift_preferences": 1,
            "approved_leave": 1,
        },
        f"the result reports what was removed {result['removed']}",
    )
    check(after["employees"] == before["employees"] - 1, "exactly one employee was removed")
    check(after["courses"] == before["courses"] - 1, "only their course was removed")
    check(
        after["class_meetings"] == before["class_meetings"] - 2,
        "only their class meetings were removed",
    )
    check(
        after["approved_leave"] == before["approved_leave"] - 1,
        "only their approved leave was removed",
    )
    check(
        after["shift_preferences"] == before["shift_preferences"] - 1,
        "only their shift preference was removed",
    )
    check(after["shifts"] == before["shifts"], "shared shifts are untouched")

    survivor = find_by_code(connection, "SW-002")
    check(survivor is not None and survivor["id"] == bob["id"], "the other worker is untouched")
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM shift_preferences"
            " WHERE employee_id = ? AND shift_id = ?",
            (bob["id"], shared_shift),
        ).fetchone()["n"] == 1,
        "the other worker keeps their preference on the shift they shared",
    )
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM courses WHERE employee_id = ?", (bob["id"],)
        ).fetchone()["n"] == 1,
        "the other worker keeps their course",
    )

    # 8a. The code is retired.
    check(
        retired_employee_codes(connection) == ["SW-001"],
        f"the deleted code is retired {retired_employee_codes(connection)}",
    )
    retired_row = connection.execute(
        "SELECT retired_at FROM retired_employee_codes WHERE employee_code = 'SW-001'"
    ).fetchone()
    check(retired_row["retired_at"] == "2026-10-07 12:00", "the retirement time is recorded")
    check(
        {row[1] for row in connection.execute("PRAGMA table_info(retired_employee_codes)")}
        == {"employee_code", "retired_at"},
        "the ledger stores only the code and the time, no personal details",
    )

    # 4. Deleting again reports not-found rather than misbehaving.
    try:
        delete_employee(connection, "SW-001", retired_at=RETIRED_AT)
        check(False, "a repeated delete should report not-found")
    except EmployeeNotFound as error:
        check("SW-001" in str(error), "a repeated delete reports not-found")
    try:
        delete_employee(connection, "SW-NOPE", retired_at=RETIRED_AT)
        check(False, "an unknown code should report not-found")
    except EmployeeNotFound as error:
        check("SW-NOPE" in str(error), "an unknown code reports not-found")
    check(counts(connection) == after, "the failed deletes changed nothing")
    connection.close()

    # 3. Assignments block deletion whatever their timing.
    blocked_case("historical", "2026-10-06 17:00", "2026-10-06 22:00")
    blocked_case("in-progress", "2026-10-07 09:00", "2026-10-07 15:00")
    blocked_case("future", "2026-10-09 17:00", "2026-10-09 22:00")

    # 5. A failure partway through rolls everything back.
    connection = fixture()
    worker = add_worker(connection, "SW-001", "Alice Adams")
    give_records(connection, worker["id"])
    before = counts(connection)

    # `sqlite3.Connection.execute` cannot be reassigned, so the failure is
    # injected through a thin stand-in that forwards everything else.
    failing = FailingConnection(connection, fail_on_delete=2)
    try:
        delete_employee(failing, "SW-001", retired_at=RETIRED_AT)
        check(False, "the injected failure should have propagated")
    except sqlite3.OperationalError as error:
        check("injected failure" in str(error), "the injected failure propagates")
    check(
        failing.deletes == 2,
        f"the failure landed after a real delete had run ({failing.deletes})",
    )

    check(counts(connection) == before, f"the failed deletion rolled back completely {counts(connection)}")
    check(find_by_code(connection, "SW-001") is not None, "the worker survives a rolled-back deletion")
    check(
        retired_employee_codes(connection) == [],
        "a rolled-back deletion does not retire the code",
    )
    connection.close()

    # 6. The assignment check happens inside the write transaction. A second
    #    connection trying to insert an assignment mid-deletion is locked out,
    #    so it cannot invalidate the check after it has run.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "locking.db"
        first = get_connection(path)
        create_schema(first)
        worker = add_worker(first, "SW-001", "Alice Adams")
        shift_id = add_shift(first, "Capella", "2026-10-09 17:00", "2026-10-09 22:00")
        first.commit()
        employee_id = worker["id"]

        # Hold the deletion's transaction open at the point after the check.
        first.execute("BEGIN IMMEDIATE")
        blocking = first.execute(
            "SELECT COUNT(*) AS n FROM assignments WHERE employee_id = ?", (employee_id,)
        ).fetchone()["n"]
        check(blocking == 0, "no assignment exists when the check runs")

        second = get_connection(path)
        second.execute("PRAGMA busy_timeout = 250")
        try:
            second.execute(
                "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
                (employee_id, shift_id),
            )
            second.commit()
            check(False, "a concurrent assignment insert should be locked out")
        except sqlite3.OperationalError as error:
            check(
                "locked" in str(error).lower(),
                f"BEGIN IMMEDIATE locks out a concurrent assignment insert -> {error}",
            )
        second.close()
        first.rollback()
        first.close()

    # 7 + 8b. Deletion and the retired code both survive a restart.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "deleted.db"
        first = get_connection(path)
        create_schema(first)
        add_worker(first, "SW-031", "Jack Ma")
        add_worker(first, "SW-032", "Kept Worker")
        give_records(first, find_by_code(first, "SW-031")["id"])
        delete_employee(first, "SW-031", retired_at=RETIRED_AT)
        first.close()

        reopened = get_connection(path)
        check(find_by_code(reopened, "SW-031") is None, "the deletion survives reopening")
        check(
            find_by_code(reopened, "SW-032") is not None,
            "the remaining worker survives reopening",
        )
        check(
            retired_employee_codes(reopened) == ["SW-031"],
            "the retired code survives reopening",
        )
        check(
            reopened.execute("SELECT COUNT(*) AS n FROM courses").fetchone()["n"] == 0,
            "the deleted worker's courses are still gone after reopening",
        )
        # The number stays spent: a future allocator reading employees alone
        # would wrongly conclude SW-031 is free.
        highest_live = reopened.execute(
            "SELECT MAX(employee_code) AS c FROM employees"
        ).fetchone()["c"]
        check(
            highest_live == "SW-032" and "SW-031" in retired_employee_codes(reopened),
            "SW-031 is absent from employees but still recorded as spent",
        )
        reopened.close()

    # 8c. Demo initialization refuses on a database holding a retired code.
    connection = fixture()
    connection.execute(
        "INSERT INTO retired_employee_codes (employee_code, retired_at) VALUES (?, ?)",
        ("SW-005", "2026-10-07 12:00"),
    )
    connection.commit()
    try:
        initialize_demo_data(connection)
        check(False, "initialization should refuse when a code has been retired")
    except DatabaseNotEmpty as error:
        check(
            "retired_employee_codes" in str(error),
            f"initialization refuses and names the retired-code ledger -> {error}",
        )
    check(
        connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 0,
        "the refused initialization wrote nothing",
    )
    connection.close()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll employee deletion checks passed (isolated databases only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
