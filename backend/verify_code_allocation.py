"""Regression checks for automatic employee-code allocation (D034).

Everything here runs against throwaway in-memory or temporary databases. The
project's own `backend/shiftops.db` is never opened, read or modified.

Checks:

1. A genuinely unused database issues SW-001, then SW-002, SW-003.
2. Existing live codes are reserved: the sequence continues above the highest
   one and never fills a historical gap.
3. Retired codes are reserved too, so a deleted worker's number is not reissued.
4. Mixed case and any zero padding reserve the same number.
5. Codes that are not `SW-` plus digits reserve nothing and are left alone.
6. A code held by a live worker AND the retired ledger is preserved once and
   its number reserved once.
7. Progress survives closing and reopening the database file.
8. Deleting the highest-numbered worker does not free their number.
9. Re-running `create_schema()` never disturbs stored progress.
10. Invalid input and a supplied employee_code are rejected, creating nothing.
11. An injected failure rolls back both the worker and the counter.
12. Concurrent creates on one file, through separate connections, issue
    distinct codes.
13. Demo initialization reserves the demo numbers; Add then continues above
    them. Add first makes initialization refuse. Concurrent initialization
    and creation cannot interleave.
14. SW-999 rolls forward to SW-1000.
15. Existing workers keep their identity, provenance, status and records.
16. A number larger than the counter can hold is reported, not wrapped.

Run with:  python verify_code_allocation.py
Exits non-zero if any check fails.
"""

import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

from database import create_schema, get_connection
from employees import (
    CodeAllocationError,
    EmployeeValidationError,
    allocation_progress,
    create_employee,
    delete_employee,
    find_by_code,
    format_employee_code,
    highest_reserved_number,
    sequence_number,
    update_employee,
)
from seed import DatabaseNotEmpty, initialize_demo_data

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fixture():
    connection = get_connection(":memory:")
    create_schema(connection)
    return connection


def add(connection, name="Fixture Worker", student_type="undergraduate"):
    return create_employee(
        connection, {"full_name": name, "student_type": student_type}
    )


def employee_rows(connection):
    """Every employee row in full, so preservation is checked by value."""
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT id, employee_code, full_name, student_type, weekly_hour_limit,"
            " is_active, seed_key FROM employees ORDER BY id"
        )
    ]


def put_employee(connection, code, seed_key=None):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type, seed_key)"
        " VALUES (?, ?, 'undergraduate', ?)",
        (code, f"Legacy {code}", seed_key),
    )
    connection.commit()


def retire(connection, code, when="2026-10-07 12:00"):
    connection.execute(
        "INSERT OR REPLACE INTO retired_employee_codes (employee_code, retired_at)"
        " VALUES (?, ?)",
        (code, when),
    )
    connection.commit()


def expect_validation_error(action, description):
    try:
        action()
        check(False, f"{description} (no error raised)")
    except EmployeeValidationError as error:
        check(True, f"{description} -> {error}")


class FailingConnection:
    """Forwards to a real connection but fails on the Nth matching statement.

    `keyword` picks which statements count - "INSERT" or "UPDATE" - so a
    failure can be placed before the employee row is written, after it but
    during the counter's first insert, or during a later counter update.
    """

    def __init__(self, real, keyword="INSERT", fail_on=1):
        self._real = real
        self._keyword = keyword.upper()
        self._fail_on = fail_on
        self.matched = 0

    def execute(self, sql, *args):
        if sql.strip().upper().startswith(self._keyword):
            self.matched += 1
            if self.matched == self._fail_on:
                raise sqlite3.OperationalError("injected failure")
        return self._real.execute(sql, *args)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    @property
    def isolation_level(self):
        return self._real.isolation_level

    @isolation_level.setter
    def isolation_level(self, value):
        self._real.isolation_level = value


def main():
    # 1. A genuinely unused database starts at SW-001.
    connection = fixture()
    check(allocation_progress(connection) is None, "an unused database has no counter row")
    check(highest_reserved_number(connection) == 0, "nothing is reserved yet")

    first = add(connection, "Alice Adams")
    check(first["employee_code"] == "SW-001", f"first worker is SW-001 ({first['employee_code']})")
    second = add(connection, "Bob Brown")
    third = add(connection, "Cara Cruz")
    check(
        (second["employee_code"], third["employee_code"]) == ("SW-002", "SW-003"),
        f"the sequence continues ({second['employee_code']}, {third['employee_code']})",
    )
    check(allocation_progress(connection) == 3, "the counter records three issued")
    check(first["is_active"] == 1 and first["weekly_hour_limit"] == 20,
          "new workers still start active with the 20-hour limit")
    check(first["seed_key"] is None, "manually created workers carry no seed provenance")
    connection.close()

    # 2. Existing live codes are reserved; gaps are never filled.
    connection = fixture()
    for code in ("SW-001", "SW-002", "SW-007"):
        put_employee(connection, code)
    issued = add(connection)
    check(issued["employee_code"] == "SW-008", f"continues above the highest live code ({issued['employee_code']})")
    check(find_by_code(connection, "SW-003") is None, "the gap at SW-003 is not filled")
    check(
        [r["employee_code"] for r in connection.execute(
            "SELECT employee_code FROM employees ORDER BY id")] ==
        ["SW-001", "SW-002", "SW-007", "SW-008"],
        "existing codes are untouched",
    )
    connection.close()

    # 3. Retired codes are reserved.
    connection = fixture()
    put_employee(connection, "SW-001")
    retire(connection, "SW-031")
    issued = add(connection)
    check(issued["employee_code"] == "SW-032", f"continues above the highest RETIRED code ({issued['employee_code']})")
    connection.close()

    # 4. Case and padding.
    for code, expected in [("sw-031", "SW-032"), ("SW-0000031", "SW-032"), ("Sw-31", "SW-032")]:
        connection = fixture()
        put_employee(connection, code)
        issued = add(connection)
        check(issued["employee_code"] == expected, f"{code!r} reserves 31 -> next is {issued['employee_code']}")
        check(
            find_by_code(connection, code)["employee_code"] == code,
            f"{code!r} is preserved exactly as stored",
        )
        connection.close()

    # 5. Non-sequence codes reserve nothing.
    for code in ("TEMP-5", "SW-12A", "SW-", "SW-3-B", "XSW-004", "SW_004"):
        check(sequence_number(code) is None, f"{code!r} is not a sequence code")
    check(sequence_number("SW-004") == 4, "SW-004 parses to 4")

    connection = fixture()
    for code in ("TEMP-5", "SW-12A", "LEGACY"):
        put_employee(connection, code)
    issued = add(connection)
    check(issued["employee_code"] == "SW-001", f"unrelated legacy codes do not move the sequence ({issued['employee_code']})")
    check(
        {r["employee_code"] for r in connection.execute("SELECT employee_code FROM employees")}
        == {"TEMP-5", "SW-12A", "LEGACY", "SW-001"},
        "legacy codes are preserved untouched alongside the issued one",
    )
    connection.close()

    # 6. A code that is both live and retired.
    connection = fixture()
    put_employee(connection, "SW-031")
    retire(connection, "SW-031")
    issued = add(connection)
    check(issued["employee_code"] == "SW-032", f"a live+retired duplicate reserves 31 once ({issued['employee_code']})")
    check(
        connection.execute(
            "SELECT COUNT(*) AS n FROM employees WHERE employee_code = 'SW-031'"
        ).fetchone()["n"] == 1,
        "the existing SW-031 worker is preserved and not duplicated",
    )
    connection.close()

    # 7-8. Persistence across restart, and deleting the highest worker.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "alloc.db"
        first_conn = get_connection(path)
        create_schema(first_conn)
        add(first_conn, "One")
        add(first_conn, "Two")
        top = add(first_conn, "Three")
        check(top["employee_code"] == "SW-003", "third worker is SW-003")
        first_conn.close()

        reopened = get_connection(path)
        check(allocation_progress(reopened) == 3, "progress survives reopening the file")
        fourth = add(reopened, "Four")
        check(fourth["employee_code"] == "SW-004", f"allocation resumes after a restart ({fourth['employee_code']})")

        delete_employee(reopened, "SW-004")
        check(allocation_progress(reopened) == 4, "deleting the highest worker does not lower the counter")
        after_delete = add(reopened, "Five")
        check(after_delete["employee_code"] == "SW-005",
              f"a deleted worker's number is never reissued ({after_delete['employee_code']})")
        reopened.close()

        # 9. Repeated schema initialization preserves progress.
        again = get_connection(path)
        create_schema(again)
        create_schema(again)
        check(allocation_progress(again) == 5, "repeated create_schema() leaves progress alone")
        check(
            again.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 4,
            "repeated create_schema() leaves the workers alone",
        )
        again.close()

    # 10. Invalid input, and a supplied employee_code.
    connection = fixture()
    expect_validation_error(
        lambda: create_employee(connection, {"employee_code": "SW-500", "full_name": "X", "student_type": "masters"}),
        "a supplied employee_code is rejected",
    )
    expect_validation_error(
        lambda: create_employee(connection, {"employee_code": "", "full_name": "X", "student_type": "masters"}),
        "even a blank employee_code is rejected rather than ignored",
    )
    for payload, label in [
        ({"student_type": "masters"}, "missing full_name"),
        ({"full_name": "  ", "student_type": "masters"}, "whitespace-only full_name"),
        ({"full_name": "X"}, "missing student_type"),
        ({"full_name": "X", "student_type": "phd"}, "unsupported student_type"),
        ({"full_name": 7, "student_type": "masters"}, "non-text full_name"),
    ]:
        expect_validation_error(lambda p=payload: create_employee(connection, p), f"{label} is rejected")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 0,
        "no rejected attempt created a worker",
    )
    check(allocation_progress(connection) is None, "no rejected attempt advanced the counter")
    connection.close()

    # 11. Injected failure rolls back worker and counter together.
    connection = fixture()
    add(connection, "Existing")
    before_progress = allocation_progress(connection)
    failing = FailingConnection(connection, keyword="INSERT", fail_on=1)
    try:
        create_employee(failing, {"full_name": "Doomed", "student_type": "masters"})
        check(False, "the injected failure should have propagated")
    except sqlite3.OperationalError as error:
        check("injected failure" in str(error), "the injected failure propagates")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 1,
        "the failed create left no partial worker",
    )
    check(allocation_progress(connection) == before_progress,
          f"the failed create left the counter at {before_progress}")
    recovered = add(connection, "After Failure")
    check(recovered["employee_code"] == "SW-002",
          f"the next create reuses the number the rollback released ({recovered['employee_code']})")
    connection.close()

    # 11b. Failure AFTER the employee row is written, while the counter row is
    #      being created for the first time. The employee insert really did
    #      execute, so this proves the rollback undoes it rather than merely
    #      never reaching it.
    connection = fixture()
    before_rows = employee_rows(connection)
    check(before_rows == [] and allocation_progress(connection) is None,
          "the fixture starts with no workers and no counter")

    failing = FailingConnection(connection, keyword="INSERT", fail_on=2)
    try:
        create_employee(failing, {"full_name": "Doomed", "student_type": "masters"})
        check(False, "the failure during the counter insert should have propagated")
    except sqlite3.OperationalError as error:
        check("injected failure" in str(error), "the counter-insert failure propagates")
    check(failing.matched == 2, f"the failure landed on the second INSERT ({failing.matched})")

    check(employee_rows(connection) == before_rows,
          f"the employee insert was rolled back too {employee_rows(connection)}")
    check(allocation_progress(connection) is None,
          "no counter row survives the rolled-back counter insert")
    after_first = add(connection, "After Counter Insert Failure")
    check(after_first["employee_code"] == "SW-001",
          f"the next successful create still issues SW-001 ({after_first['employee_code']})")
    connection.close()

    # 11c. Failure AFTER the employee row is written, while an EXISTING
    #      counter row is being updated.
    connection = fixture()
    add(connection, "Established")
    before_rows = employee_rows(connection)
    before_progress = allocation_progress(connection)
    check(before_progress == 1, "the counter row exists at 1 before the injected failure")

    failing = FailingConnection(connection, keyword="UPDATE", fail_on=1)
    try:
        create_employee(failing, {"full_name": "Doomed Too", "student_type": "masters"})
        check(False, "the failure during the counter update should have propagated")
    except sqlite3.OperationalError as error:
        check("injected failure" in str(error), "the counter-update failure propagates")
    check(failing.matched == 1, f"the failure landed on the first UPDATE ({failing.matched})")

    check(employee_rows(connection) == before_rows,
          f"the employee rows are exactly as they were {employee_rows(connection)}")
    check(allocation_progress(connection) == before_progress,
          f"the counter is exactly as it was ({allocation_progress(connection)})")
    after_update = add(connection, "After Counter Update Failure")
    check(after_update["employee_code"] == "SW-002",
          f"the next successful create issues the correct next code ({after_update['employee_code']})")
    check(allocation_progress(connection) == 2, "and the counter advances to 2")
    connection.close()

    # 12. Real concurrent creates, separate connections, one file.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "concurrent.db"
        setup = get_connection(path)
        create_schema(setup)
        setup.close()

        results = []
        errors = []
        barrier = threading.Barrier(6)

        def worker(index):
            own = get_connection(path)
            try:
                barrier.wait()
                row = create_employee(
                    own, {"full_name": f"Racer {index}", "student_type": "undergraduate"}
                )
                results.append(row["employee_code"])
            except Exception as error:  # noqa: BLE001 - recorded, not swallowed
                errors.append(f"{type(error).__name__}: {error}")
            finally:
                own.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        check(errors == [], f"six concurrent creates all succeeded ({errors})")
        check(len(set(results)) == len(results) == 6,
              f"every concurrent create got a distinct code ({sorted(results)})")
        check(sorted(results) == [format_employee_code(n) for n in range(1, 7)],
              f"concurrent creates issued SW-001..SW-006 with no gaps ({sorted(results)})")

        verify = get_connection(path)
        check(verify.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 6,
              "exactly six workers exist")
        check(allocation_progress(verify) == 6, "the counter matches the six issued")
        verify.close()

    # 13. Demo initialization and allocation together.
    connection = fixture()
    initialize_demo_data(connection)
    check(allocation_progress(connection) == 30, f"initialization reserves the 30 demo numbers ({allocation_progress(connection)})")
    after_demo = add(connection, "Jack Ma", "masters")
    check(after_demo["employee_code"] == "SW-031",
          f"Add after initialization continues at SW-031 ({after_demo['employee_code']})")
    check(find_by_code(connection, "SW-001")["seed_key"] == "SW-001", "demo provenance is preserved")
    connection.close()

    # Add first, then initialization must refuse.
    connection = fixture()
    made = add(connection, "Manual First")
    check(made["employee_code"] == "SW-001", "manual create in an empty database is SW-001")
    try:
        initialize_demo_data(connection)
        check(False, "initialization should refuse after a manual create")
    except DatabaseNotEmpty as error:
        check("employees" in str(error), f"initialization refuses after a manual create -> {error}")
    connection.close()

    # Allocation history alone blocks initialization, even with no rows left.
    connection = fixture()
    add(connection, "Temporary")
    delete_employee(connection, "SW-001")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 0,
        "the worker really is gone",
    )
    try:
        initialize_demo_data(connection)
        check(False, "initialization should refuse when codes have been issued")
    except DatabaseNotEmpty as error:
        check("issued employee codes" in str(error),
              f"issued-code history alone blocks initialization -> {error}")
    connection.close()

    # A counter row still at zero must not block initialization.
    connection = fixture()
    connection.execute("INSERT INTO code_allocation (id, highest_issued) VALUES (1, 0)")
    connection.commit()
    try:
        initialize_demo_data(connection)
        check(True, "an unused counter row does not block initialization")
    except DatabaseNotEmpty as error:
        check(False, f"an unused counter row wrongly blocked initialization -> {error}")
    check(allocation_progress(connection) == 30, "initialization then reserves the demo numbers")
    connection.close()

    # Concurrent initialization and creation on one file. Exactly one of two
    # outcomes is acceptable, and anything else - including either operation
    # failing unexpectedly - is a failure, not a tolerated variation.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "race.db"
        setup = get_connection(path)
        create_schema(setup)
        setup.close()

        outcomes = {}
        unexpected = []
        start_together = threading.Barrier(2)

        def seeder():
            own = get_connection(path)
            try:
                start_together.wait()
                initialize_demo_data(own)
                outcomes["seed"] = "seeded"
            except DatabaseNotEmpty:
                outcomes["seed"] = "refused"
            except Exception as error:  # noqa: BLE001 - recorded, not swallowed
                outcomes["seed"] = "error"
                unexpected.append(f"seeder raised {type(error).__name__}: {error}")
            finally:
                own.close()

        def creator():
            own = get_connection(path)
            try:
                start_together.wait()
                row = create_employee(own, {"full_name": "Racer", "student_type": "masters"})
                outcomes["create"] = row["employee_code"]
            except Exception as error:  # noqa: BLE001 - recorded, not swallowed
                outcomes["create"] = "error"
                unexpected.append(f"creator raised {type(error).__name__}: {error}")
            finally:
                own.close()

        threads = [threading.Thread(target=seeder), threading.Thread(target=creator)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        check(unexpected == [], f"neither operation raised an unexpected error ({unexpected})")

        after = get_connection(path)
        employees = after.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"]
        progress = allocation_progress(after)
        demo_rows = {
            table: after.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("semester_schedules", "class_blocks", "shifts",
                          "shift_preferences", "approved_leave")
        }

        if outcomes.get("seed") == "seeded":
            # The seed won the write lock. The create then ran against a
            # seeded database and must have been issued the number after the
            # demo block.
            check(outcomes.get("create") == "SW-031",
                  f"after a winning seed, the create is issued SW-031 ({outcomes.get('create')})")
            check(employees == 31, f"31 employees exist: 30 demo plus the created one ({employees})")
            check(progress == 31, f"the counter is 31 ({progress})")
            check(
                demo_rows == {"semester_schedules": 30, "class_blocks": 148,
                              "shifts": 99, "shift_preferences": 168,
                              "approved_leave": 5},
                f"the complete demo dataset is present {demo_rows}",
            )
        elif outcomes.get("seed") == "refused":
            # The create won. Seeding must then have refused outright and
            # written nothing at all.
            check(outcomes.get("create") == "SW-001",
                  f"after a winning create, it is issued SW-001 ({outcomes.get('create')})")
            check(employees == 1, f"exactly one employee exists ({employees})")
            check(progress == 1, f"the counter is 1 ({progress})")
            check(
                all(count == 0 for count in demo_rows.values()),
                f"the refused seed inserted no demo records at all {demo_rows}",
            )
        else:
            check(False, f"neither valid outcome occurred: {outcomes}")

        codes = [r["employee_code"] for r in after.execute("SELECT employee_code FROM employees")]
        check(len(set(codes)) == len(codes), f"no duplicate codes after the race ({len(codes)} codes)")
        check(progress is not None and progress >= highest_reserved_number(after),
              f"the counter is never behind the codes on disk (counter={progress})")
        after.close()

    # 14. SW-999 -> SW-1000.
    connection = fixture()
    put_employee(connection, "SW-999")
    rolled = add(connection)
    check(rolled["employee_code"] == "SW-1000", f"SW-999 rolls forward to SW-1000 ({rolled['employee_code']})")
    next_one = add(connection)
    check(next_one["employee_code"] == "SW-1001", f"and continues to SW-1001 ({next_one['employee_code']})")
    check(format_employee_code(7) == "SW-007" and format_employee_code(12345) == "SW-12345",
          "codes keep at least three digits and grow beyond them")
    connection.close()

    # 15. Existing workers and their records are untouched by allocation.
    connection = fixture()
    initialize_demo_data(connection)
    before = connection.execute(
        "SELECT (SELECT COUNT(*) FROM employees) AS e, (SELECT COUNT(*) FROM courses) AS c,"
        " (SELECT COUNT(*) FROM class_meetings) AS m, (SELECT COUNT(*) FROM shift_preferences) AS p,"
        " (SELECT COUNT(*) FROM approved_leave) AS l, (SELECT COUNT(*) FROM shifts) AS s"
    ).fetchone()
    sw001_before = dict(find_by_code(connection, "SW-001"))

    add(connection, "New Person", "masters")

    after = connection.execute(
        "SELECT (SELECT COUNT(*) FROM employees) AS e, (SELECT COUNT(*) FROM courses) AS c,"
        " (SELECT COUNT(*) FROM class_meetings) AS m, (SELECT COUNT(*) FROM shift_preferences) AS p,"
        " (SELECT COUNT(*) FROM approved_leave) AS l, (SELECT COUNT(*) FROM shifts) AS s"
    ).fetchone()
    check(tuple(after) == (before["e"] + 1, before["c"], before["m"], before["p"], before["l"], before["s"]),
          f"creating a worker adds exactly one employee row and nothing else {tuple(after)}")
    check(dict(find_by_code(connection, "SW-001")) == sw001_before,
          "an existing worker's row is byte-identical after an allocation")

    # Edit immutability still holds, including case-only.
    edited = update_employee(connection, "SW-031", {
        "employee_code": "SW-031", "full_name": "Renamed Person", "student_type": "undergraduate"})
    check(edited["employee_code"] == "SW-031" and edited["full_name"] == "Renamed Person",
          "a name edit still works and keeps the issued code")
    expect_validation_error(
        lambda: update_employee(connection, "SW-031", {
            "employee_code": "sw-031", "full_name": "X", "student_type": "masters"}),
        "a case-only edit of an issued code is still rejected",
    )
    expect_validation_error(
        lambda: update_employee(connection, "SW-031", {
            "employee_code": "SW-999", "full_name": "X", "student_type": "masters"}),
        "changing an issued code is still rejected",
    )
    connection.close()

    # 15b. Very long legacy suffixes. Python refuses to convert a string of
    #      more than 4300 digits to int at all, so these would raise a bare
    #      ValueError if the digits were not measured before conversion.
    huge_padding = "SW-" + ("0" * 5000) + "31"
    check(sequence_number(huge_padding) == 31,
          "SW- plus 5,000 zeros then 31 reserves 31, not an int() failure")

    connection = fixture()
    put_employee(connection, huge_padding)
    issued = add(connection)
    check(issued["employee_code"] == "SW-032",
          f"a 5,000-zero padded code reserves 31, so the next code is SW-032 ({issued['employee_code']})")
    check(
        find_by_code(connection, huge_padding) is not None,
        "the 5,000-zero padded code is preserved exactly as stored",
    )
    connection.close()

    huge_number = "SW-" + ("9" * 5000)
    try:
        sequence_number(huge_number)
        check(False, "SW- plus 5,000 nines should raise a controlled error")
    except CodeAllocationError as error:
        check("larger than this application can track" in str(error),
              "SW- plus 5,000 nines raises a controlled allocation error")
    except ValueError as error:
        check(False, f"SW- plus 5,000 nines raised a bare ValueError: {error}")

    connection = fixture()
    put_employee(connection, huge_number)
    before_rows = connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"]
    try:
        add(connection)
        check(False, "creating should refuse while a 5,000-digit code exists")
    except CodeAllocationError:
        check(True, "creating refuses rather than crashing on a 5,000-digit code")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == before_rows,
        "the refused create added nobody",
    )
    check(allocation_progress(connection) is None, "the refused create left no counter row")
    connection.close()

    # Boundary and shape cases around the supported maximum.
    check(sequence_number("SW-9223372036854775807") == 9223372036854775807,
          "the largest supported number parses")
    try:
        sequence_number("SW-9223372036854775808")
        check(False, "one above the maximum should raise")
    except CodeAllocationError:
        check(True, "one above the maximum raises a controlled error")
    check(sequence_number("SW-000") == 0, "SW-000 reserves nothing (0)")
    check(sequence_number("SW-0") == 0, "SW-0 reserves nothing (0)")
    check(sequence_number("SW-031" + chr(10)) is None,
          "a code with a trailing newline is not a sequence code")
    check(sequence_number("xSW-031") is None, "a prefixed code is not a sequence code")
    check(sequence_number("SW-031x") is None, "a suffixed code is not a sequence code")

    # 16. A number beyond what the counter can hold is reported, not wrapped.
    connection = fixture()
    put_employee(connection, "SW-99999999999999999999999")
    try:
        highest_reserved_number(connection)
        check(False, "an out-of-range code should be reported")
    except CodeAllocationError as error:
        check("larger than this application can track" in str(error),
              f"an out-of-range code is reported clearly -> {error}")
    try:
        add(connection)
        check(False, "creating should fail while an out-of-range code exists")
    except CodeAllocationError:
        check(True, "creating refuses rather than wrapping or reusing a number")
    check(
        connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"] == 1,
        "the refused create added nobody",
    )
    connection.close()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll code-allocation checks passed (isolated databases only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
