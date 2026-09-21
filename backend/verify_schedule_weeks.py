"""Focused checks for Phase 7 increment 1: the week-generation, week-
preparation, and read-only weekly-schedule foundation (`weeks.py`,
`scheduling.py`, and the generalized `synthetic_data.generate_required_shifts`).

Runs entirely against isolated in-memory/temporary databases and pure
functions. The project's own `backend/shiftops.db` is never opened.

Checks:

1. `generate_required_shifts()` with no argument is exactly unchanged: 99
   shifts, 489 hours, same identities/dates/halls/ordering as before.
2. `generate_required_shifts(other_monday)` produces the identical pattern
   (halls, relative offsets, required_staff, cross-midnight end dates)
   shifted onto that week.
3. `weeks.parse_week_start` rejects malformed dates, non-calendar dates and
   non-Mondays, and accepts a real Monday.
4. `scheduling.get_week_schedule` on an unprepared week returns an empty,
   valid response and performs no writes.
5. `scheduling.prepare_week` inserts the expected shifts for a week; a
   repeat is a no-op (zero inserted); the sample week and unrelated rows
   are untouched; no shift preferences are copied.
6. `scheduling.get_week_schedule` distinguishes assigned/uncovered shifts
   and reports correct cross-midnight start-week ownership.
7. `main.list_employees` uses the requested week and preserves the
   no-parameter default.

Run with:  python verify_schedule_weeks.py
Exits non-zero if any check fails.
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

if "main" in sys.modules:  # pragma: no cover - defensive
    raise SystemExit(
        "main was imported before the database path was redirected; refusing to run."
    )

import database  # noqa: E402  - imported early on purpose, see the docstring

_TEMPORARY = tempfile.TemporaryDirectory()
_SCRATCH = Path(_TEMPORARY.name)
database.DATABASE_PATH = _SCRATCH / "bootstrap.db"

if database.DATABASE_PATH.name == "shiftops.db":  # pragma: no cover - defensive
    raise SystemExit("refusing to run against the project database")

import main  # noqa: E402
import scheduling  # noqa: E402
import weeks  # noqa: E402
from demo_fixture import demo_fixture_connection  # noqa: E402
from synthetic_data import TIME_FORMAT, generate_required_shifts  # noqa: E402

failures = []
_total = {"n": 0}


def check(condition, description):
    _total["n"] += 1
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fresh_connection():
    connection = database.get_connection(":memory:")
    database.create_schema(connection)
    return connection


def table_snapshot(connection, table):
    return set(connection.execute(f"SELECT * FROM {table}").fetchall())


def full_snapshot(connection):
    return {
        table: tuple(sorted(map(tuple, connection.execute(f"SELECT * FROM {table}"))))
        for table in (
            "employees",
            "semester_schedules",
            "class_blocks",
            "shift_preferences",
            "approved_leave",
            "assignments",
            "code_allocation",
            "retired_employee_codes",
        )
    }


# --------------------------------------------------------------------------
# 1-2. Generator behavior: default output unchanged, other weeks shifted.
# --------------------------------------------------------------------------


def check_default_generator_unchanged():
    demo = demo_fixture_connection()
    try:
        expected_shifts = demo.execute(
            "SELECT hall, start_datetime, end_datetime, required_staff FROM shifts"
            " ORDER BY start_datetime, hall"
        ).fetchall()
    finally:
        demo.close()

    default_shifts = generate_required_shifts()
    check(len(default_shifts) == 99, f"default generator still produces 99 shifts ({len(default_shifts)})")

    total_hours = sum(
        (
            datetime.strptime(s["end_datetime"], TIME_FORMAT)
            - datetime.strptime(s["start_datetime"], TIME_FORMAT)
        ).total_seconds()
        / 3600
        for s in default_shifts
    )
    check(total_hours == 489, f"default generator still totals 489 hours ({total_hours})")

    sorted_default = sorted(
        default_shifts, key=lambda s: (s["start_datetime"], s["hall"])
    )
    identical = len(sorted_default) == len(expected_shifts) and all(
        a["hall"] == b["hall"]
        and a["start_datetime"] == b["start_datetime"]
        and a["end_datetime"] == b["end_datetime"]
        and a["required_staff"] == b["required_staff"]
        for a, b in zip(sorted_default, expected_shifts)
    )
    check(identical, "default generator's shift identities/dates/halls/ordering are byte-identical to the demo fixture")


def check_other_monday_shifted():
    default_shifts = sorted(
        generate_required_shifts(), key=lambda s: (s["start_datetime"], s["hall"])
    )
    other_monday = datetime(2026, 11, 2)  # an arbitrary later Monday
    other_shifts = sorted(
        generate_required_shifts(other_monday), key=lambda s: (s["start_datetime"], s["hall"])
    )

    check(len(other_shifts) == 99, f"another Monday still produces 99 shifts ({len(other_shifts)})")
    total_hours = sum(
        (
            datetime.strptime(s["end_datetime"], TIME_FORMAT)
            - datetime.strptime(s["start_datetime"], TIME_FORMAT)
        ).total_seconds()
        / 3600
        for s in other_shifts
    )
    check(total_hours == 489, f"another Monday still totals 489 hours ({total_hours})")

    same_pattern = True
    cross_midnight_preserved = True
    for a, b in zip(default_shifts, other_shifts):
        a_start = datetime.strptime(a["start_datetime"], TIME_FORMAT)
        b_start = datetime.strptime(b["start_datetime"], TIME_FORMAT)
        a_end = datetime.strptime(a["end_datetime"], TIME_FORMAT)
        b_end = datetime.strptime(b["end_datetime"], TIME_FORMAT)
        if a["hall"] != b["hall"] or a["required_staff"] != b["required_staff"]:
            same_pattern = False
        if (b_start - a_start).days != 28 or a_start.time() != b_start.time():
            same_pattern = False
        if (a_end.date() != a_start.date()) != (b_end.date() != b_start.date()):
            cross_midnight_preserved = False
    check(same_pattern, "shifting to another Monday preserves hall/time-of-day/required_staff pattern exactly")
    check(cross_midnight_preserved, "cross-midnight shifts remain cross-midnight after shifting to another Monday")


def check_generator_rejects_invalid_week_start():
    """`generate_required_shifts` enforces its own week_start invariant.

    An explicitly supplied value must be a naive (no timezone) `datetime`
    that is exactly midnight on a real Monday - the generator does not trust
    a caller to have validated it first (defense in depth alongside
    `weeks.parse_week_start`, which validates a STRING before it ever
    becomes a datetime).
    """
    from datetime import timezone

    try:
        generate_required_shifts(datetime(2026, 11, 3))  # a real Tuesday
        check(False, "a non-Monday datetime should have been rejected")
    except weeks.InvalidWeekStart:
        check(True, "a non-Monday datetime is rejected")

    try:
        generate_required_shifts(datetime(2026, 11, 2, 6, 0))  # Monday, not midnight
        check(False, "a Monday that is not exactly midnight should have been rejected")
    except weeks.InvalidWeekStart:
        check(True, "a Monday that is not exactly midnight is rejected")

    try:
        generate_required_shifts(datetime(2026, 11, 2, tzinfo=timezone.utc))
        check(False, "a timezone-aware datetime should have been rejected")
    except weeks.InvalidWeekStart:
        check(True, "a timezone-aware datetime is rejected")

    # None is the reserved sentinel for "use the default sample week" and
    # must NOT be rejected; every other non-datetime type must be.
    for bad in ["2026-11-02", 20261102, 1762060800.0, object()]:
        try:
            generate_required_shifts(bad)
            check(False, f"a non-datetime week_start {bad!r} should have been rejected")
        except weeks.InvalidWeekStart:
            check(True, f"a non-datetime week_start {bad!r} is rejected")

    try:
        result = generate_required_shifts(None)
        check(len(result) == 99, "week_start=None is still the valid default-week sentinel, not rejected")
    except weeks.InvalidWeekStart:
        check(False, "week_start=None should never be rejected - it means 'use the default sample week'")


# --------------------------------------------------------------------------
# 3. weeks.parse_week_start validation.
# --------------------------------------------------------------------------


def check_parse_week_start():
    monday = weeks.parse_week_start("2026-10-12")
    check(monday == datetime(2026, 10, 12), "a real Monday parses to the correct datetime")

    for bad in ["2026-1-5", "not-a-date", "2026-02-30", "2026/10/12", "", None, 20261012]:
        try:
            weeks.parse_week_start(bad)
            check(False, f"malformed week_start {bad!r} should have been rejected")
        except weeks.InvalidWeekStart:
            check(True, f"malformed week_start {bad!r} is rejected")

    try:
        weeks.parse_week_start("2026-10-13")  # a Tuesday
        check(False, "a non-Monday date should have been rejected")
    except weeks.InvalidWeekStart:
        check(True, "a non-Monday real calendar date is rejected")


# --------------------------------------------------------------------------
# 4-6. scheduling.prepare_week / get_week_schedule.
# --------------------------------------------------------------------------


def check_unprepared_week_is_empty_and_read_only():
    connection = fresh_connection()
    try:
        before = full_snapshot(connection)
        before_shift_count = connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"]

        result = scheduling.get_week_schedule(connection, "2026-11-02")
        check(result["shifts"] == [], "reading an unprepared week returns an empty shifts list")
        check(result["week_start"] == "2026-11-02", "the empty response echoes the requested week_start")
        check(result["week_end"] == "2026-11-09", "the empty response's week_end is the exclusive following Monday")

        after = full_snapshot(connection)
        after_shift_count = connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"]
        check(before == after, "reading an unprepared week writes nothing to any other table")
        check(before_shift_count == after_shift_count == 0, "reading an unprepared week inserts no shifts")
    finally:
        connection.close()


def check_prepare_week_inserts_and_is_idempotent():
    connection = fresh_connection()
    try:
        untouched_before = full_snapshot(connection)

        result = scheduling.prepare_week(connection, "2026-11-02")
        check(result["shifts_required"] == 99, "prepare_week reports 99 shifts required")
        check(result["shifts_inserted"] == 99, "prepare_week inserts all 99 shifts on first run")
        check(result["shifts_already_present"] == 0, "nothing was already present on first run")
        check(result["week_start"] == "2026-11-02" and result["week_end"] == "2026-11-09", "prepare_week echoes the correct week bounds")

        stored_count = connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"]
        check(stored_count == 99, f"99 shifts are actually stored ({stored_count})")

        repeat = scheduling.prepare_week(connection, "2026-11-02")
        check(repeat["shifts_inserted"] == 0, "preparing the same week again inserts zero additional shifts")
        check(repeat["shifts_already_present"] == 99, "the repeat reports all 99 as already present")

        stored_count_after_repeat = connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"]
        check(stored_count_after_repeat == 99, "the shift count is unchanged after the repeat")

        untouched_after = full_snapshot(connection)
        check(untouched_before == untouched_after, "preparing a week never touches employees, timetables, preferences, leave, assignments or allocator state")
    finally:
        connection.close()


def check_prepare_week_never_deletes_or_overwrites():
    connection = fresh_connection()
    try:
        # A manually inserted shift for the target week, with an unusual
        # required_staff value that would be overwritten if prepare_week
        # ever deleted-and-reinserted rather than only adding what is
        # missing.
        connection.execute(
            "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
            " VALUES ('Andromeda', '2026-11-02 17:00', '2026-11-02 22:00', 1)"
        )
        before_id = connection.execute(
            "SELECT id FROM shifts WHERE hall = 'Andromeda' AND start_datetime = '2026-11-02 17:00'"
        ).fetchone()["id"]

        result = scheduling.prepare_week(connection, "2026-11-02")
        check(result["shifts_inserted"] == 98, "the one pre-existing shift is not re-inserted (98 of 99 inserted)")
        check(result["shifts_already_present"] == 1, "the one pre-existing shift is counted as already present")

        after_id = connection.execute(
            "SELECT id FROM shifts WHERE hall = 'Andromeda' AND start_datetime = '2026-11-02 17:00'"
        ).fetchone()["id"]
        check(before_id == after_id, "the pre-existing shift's own row/id is untouched, not deleted and reinserted")

        stored_count = connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"]
        check(stored_count == 99, "exactly 99 shifts exist afterward, no duplicates")
    finally:
        connection.close()


def check_sample_week_and_unrelated_rows_untouched():
    connection = demo_fixture_connection()
    try:
        sample_week_shifts_before = table_snapshot(connection, "shifts")
        preferences_before = table_snapshot(connection, "shift_preferences")
        employees_before = table_snapshot(connection, "employees")

        scheduling.prepare_week(connection, "2026-11-02")

        sample_week_shifts_after = {
            row for row in connection.execute(
                "SELECT * FROM shifts WHERE start_datetime < '2026-10-12'"
            )
        }
        sample_week_only_before = {
            row for row in sample_week_shifts_before if row["start_datetime"] < "2026-10-12"
        }
        check(sample_week_only_before == sample_week_shifts_after, "the demo sample week's own 99 shifts are unchanged after preparing a different week")

        preferences_after = table_snapshot(connection, "shift_preferences")
        check(preferences_before == preferences_after, "no shift_preferences rows are copied onto the newly prepared week")

        employees_after = table_snapshot(connection, "employees")
        check(employees_before == employees_after, "no employees are created by preparing a week")

        new_week = scheduling.get_week_schedule(connection, "2026-11-02")
        check(len(new_week["shifts"]) == 99, "the newly prepared week now reads back 99 shifts")
        check(
            all(
                shift["assigned_employees"] == [] and shift["assigned_count"] == 0 and not shift["covered"]
                for shift in new_week["shifts"]
            ),
            "every shift in the newly prepared week is uncovered (no assignments exist)",
        )
    finally:
        connection.close()


def check_assigned_vs_uncovered_and_cross_midnight_ownership():
    """Zero, one, and multiple assignments per shift, including a shift whose
    `required_staff` is 2 - proving the response never silently discards an
    assignment when a shift has more than one, and that `covered` reflects
    `assigned_count >= required_staff`, not merely "at least one worker".
    """
    connection = fresh_connection()
    try:
        for code, name in (("SW-001", "Amara Okonkwo"), ("SW-002", "Bennett Salazar")):
            connection.execute(
                "INSERT INTO employees (employee_code, full_name, student_type)"
                " VALUES (?, ?, 'undergraduate')",
                (code, name),
            )
        employee_ids = {
            row["employee_code"]: row["id"]
            for row in connection.execute("SELECT employee_code, id FROM employees")
        }

        scheduling.prepare_week(connection, "2026-11-02")

        # A cross-midnight shift genuinely produced for this week: Capella
        # (24/7) has a shift starting Sunday night 2026-11-08 that ends past
        # midnight on 2026-11-09. It must belong to the 11-02 week (D025:
        # charged to the week containing its START) and NOT appear when
        # querying the following week.
        cross_midnight = connection.execute(
            "SELECT id, start_datetime, end_datetime FROM shifts"
            " WHERE start_datetime LIKE '2026-11-08%'"
            " AND substr(end_datetime, 1, 10) != substr(start_datetime, 1, 10)"
            " ORDER BY start_datetime LIMIT 1"
        ).fetchone()
        check(cross_midnight is not None, "a Sunday-night-crossing-into-Monday shift exists in the prepared week")

        zero_shift, one_shift, full_shift, partial_shift = connection.execute(
            "SELECT id FROM shifts WHERE hall = 'Andromeda'"
            " ORDER BY start_datetime, id LIMIT 4"
        ).fetchall()

        # one_shift: a single assignment against the default required_staff=1.
        connection.execute(
            "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
            (employee_ids["SW-001"], one_shift["id"]),
        )

        # full_shift: required_staff raised to 2, both workers assigned -
        # fully covered, and BOTH must appear, not just one of them.
        connection.execute(
            "UPDATE shifts SET required_staff = 2 WHERE id = ?", (full_shift["id"],)
        )
        connection.execute(
            "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?), (?, ?)",
            (
                employee_ids["SW-002"],
                full_shift["id"],
                employee_ids["SW-001"],
                full_shift["id"],
            ),
        )

        # partial_shift: required_staff raised to 2, only one worker assigned
        # - a real assignment exists, but it is not yet enough to be covered.
        connection.execute(
            "UPDATE shifts SET required_staff = 2 WHERE id = ?", (partial_shift["id"],)
        )
        connection.execute(
            "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
            (employee_ids["SW-001"], partial_shift["id"]),
        )

        week = scheduling.get_week_schedule(connection, "2026-11-02")
        by_id = {shift["id"]: shift for shift in week["shifts"]}

        check(
            by_id[zero_shift["id"]]["assigned_employees"] == []
            and by_id[zero_shift["id"]]["assigned_count"] == 0
            and by_id[zero_shift["id"]]["covered"] is False,
            "a shift with zero assignments reports an empty list, zero count, and uncovered",
        )

        check(
            by_id[one_shift["id"]]["assigned_employees"]
            == [{"employee_id": employee_ids["SW-001"], "employee_code": "SW-001", "full_name": "Amara Okonkwo"}]
            and by_id[one_shift["id"]]["assigned_count"] == 1
            and by_id[one_shift["id"]]["covered"] is True,
            "a shift with one assignment against required_staff=1 is fully covered and names the worker",
        )

        check(
            by_id[full_shift["id"]]["assigned_count"] == 2
            and by_id[full_shift["id"]]["covered"] is True,
            "a required_staff=2 shift with two assignments is covered, not just the first worker found",
        )
        check(
            by_id[full_shift["id"]]["assigned_employees"] == [
                {"employee_id": employee_ids["SW-001"], "employee_code": "SW-001", "full_name": "Amara Okonkwo"},
                {"employee_id": employee_ids["SW-002"], "employee_code": "SW-002", "full_name": "Bennett Salazar"},
            ],
            "both assignments are present, in deterministic employee-code order, even though SW-002 was inserted first",
        )

        check(
            by_id[partial_shift["id"]]["assigned_count"] == 1
            and by_id[partial_shift["id"]]["covered"] is False,
            "a required_staff=2 shift with only one assignment is NOT covered, but the one real assignment is still reported",
        )
        check(
            by_id[partial_shift["id"]]["assigned_employees"]
            == [{"employee_id": employee_ids["SW-001"], "employee_code": "SW-001", "full_name": "Amara Okonkwo"}],
            "the partial shift's one real assignment is not silently discarded",
        )

        untouched = [
            s for s in week["shifts"]
            if s["id"] not in (zero_shift["id"], one_shift["id"], full_shift["id"], partial_shift["id"])
        ]
        check(
            all(s["assigned_employees"] == [] and s["assigned_count"] == 0 and not s["covered"] for s in untouched),
            "every other shift is reported as uncovered with no assignments",
        )

        if cross_midnight is not None:
            check(cross_midnight["id"] in by_id, "the cross-midnight shift is included in the week containing its START")
            next_week = scheduling.get_week_schedule(connection, "2026-11-09")
            check(cross_midnight["id"] not in {s["id"] for s in next_week["shifts"]}, "the cross-midnight shift does NOT appear in the following week's schedule")

        ids = [shift["id"] for shift in week["shifts"]]
        starts = [(shift["start_datetime"], shift["hall"], shift["id"]) for shift in week["shifts"]]
        check(starts == sorted(starts), "shifts are sorted by start_datetime, then hall, then id")
        check(len(ids) == len(set(ids)), "no duplicate shift ids in the response")
    finally:
        connection.close()


# --------------------------------------------------------------------------
# 7. main.list_employees uses the requested week; default is preserved.
# --------------------------------------------------------------------------


def check_list_employees_week_parameter():
    default_payload = main.list_employees()
    check(default_payload["week_start"] == "2026-10-05", "list_employees() with no argument keeps the default sample week_start")
    check(default_payload["week_end"] == "2026-10-11", "list_employees() with no argument keeps the default sample week_end")

    other_payload = main.list_employees(week_start="2026-11-02")
    check(other_payload["week_start"] == "2026-11-02", "list_employees(week_start=...) echoes the requested week")
    check(other_payload["week_end"] == "2026-11-08", "list_employees(week_start=...) computes the correct Sunday week_end")

    same_worker_default = {e["employee_code"]: e for e in default_payload["employees"]}
    same_worker_other = {e["employee_code"]: e for e in other_payload["employees"]}
    check(set(same_worker_default) == set(same_worker_other), "the same set of employees is reported regardless of the requested week")
    check(
        all(
            same_worker_default[code]["is_active"] == same_worker_other[code]["is_active"]
            and same_worker_default[code]["full_name"] == same_worker_other[code]["full_name"]
            for code in same_worker_default
        ),
        "employee identity and active status are unaffected by the requested week",
    )

    try:
        main.list_employees(week_start="not-a-date")
        check(False, "a malformed week_start on /api/employees should have been rejected")
    except Exception as error:
        from fastapi import HTTPException
        is_400 = isinstance(error, HTTPException) and error.status_code == 400 and isinstance(error.detail, str)
        check(is_400, "a malformed week_start on /api/employees is a 400 with a string detail")

    probe = database.get_connection()
    try:
        before = full_snapshot(probe)
    finally:
        probe.close()
    main.list_employees(week_start="2026-12-07")  # an empty, never-prepared week
    probe = database.get_connection()
    try:
        after = full_snapshot(probe)
    finally:
        probe.close()
    check(before == after, "querying an empty/unprepared week through /api/employees mutates nothing")


def check_list_employees_week_dependent_values_actually_differ():
    """Proves `week_start` actually changes week-dependent figures, not just
    that two different (both-empty) weeks happen to report the same zeros.

    One worker gets a semester confirmed for exactly 2026-11-02..2026-11-08
    (one Monday week), one class block inside it, and one assignment to a
    real shift inside that same week. Every week-dependent figure is then
    compared against a different, uncovered week (2026-11-16) where none of
    that applies - each must actually differ, not merely be independently
    plausible.
    """
    connection = database.get_connection()
    try:
        connection.execute(
            "INSERT INTO employees (employee_code, full_name, student_type,"
            " weekly_hour_limit) VALUES ('SW-100', 'Week Test Worker',"
            " 'undergraduate', 20)"
        )
        employee_id = connection.execute(
            "SELECT id FROM employees WHERE employee_code = 'SW-100'"
        ).fetchone()["id"]

        # Confirmed and non-provisional, covering exactly the Monday week of
        # 2026-11-02 - all seven of its days and no others.
        connection.execute(
            "INSERT INTO semester_schedules"
            " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
            " VALUES (?, '2026-11-02', '2026-11-08', '2020-01-01 00:00', 0)",
            (employee_id,),
        )
        schedule_id = connection.execute(
            "SELECT id FROM semester_schedules WHERE employee_id = ?",
            (employee_id,),
        ).fetchone()["id"]
        # A 60-minute Monday class - falls inside 2026-11-02's week, and
        # inside no other week this semester covers (there is no other week).
        connection.execute(
            "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time)"
            " VALUES (?, 0, '09:00', '10:00')",
            (schedule_id,),
        )
        connection.commit()
    finally:
        connection.close()

    connection = database.get_connection()
    try:
        scheduling.prepare_week(connection, "2026-11-02")
        shift = connection.execute(
            "SELECT id, start_datetime, end_datetime FROM shifts"
            " WHERE hall = 'Andromeda' AND start_datetime = '2026-11-02 17:00'"
        ).fetchone()
        connection.execute(
            "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
            (employee_id, shift["id"]),
        )
        connection.commit()
        expected_hours = int(
            (
                datetime.strptime(shift["end_datetime"], TIME_FORMAT)
                - datetime.strptime(shift["start_datetime"], TIME_FORMAT)
            ).total_seconds()
            // 3600
        )
    finally:
        connection.close()

    week_a = main.list_employees(week_start="2026-11-02")  # covered and assigned
    week_b = main.list_employees(week_start="2026-11-16")  # neither

    worker_a = next(e for e in week_a["employees"] if e["employee_code"] == "SW-100")
    worker_b = next(e for e in week_b["employees"] if e["employee_code"] == "SW-100")

    check(
        worker_a["assigned_hours"] == expected_hours and expected_hours > 0,
        f"week A reports the real assigned hours from the shift ({worker_a['assigned_hours']})",
    )
    check(worker_b["assigned_hours"] == 0, f"week B has no assignment, so zero assigned hours ({worker_b['assigned_hours']})")
    check(worker_a["assigned_hours"] != worker_b["assigned_hours"], "assigned_hours actually differs between the two weeks")

    check(
        worker_a["remaining_capacity_hours"] == 20 - worker_a["assigned_hours"],
        "remaining_capacity_hours reflects week A's assigned hours",
    )
    check(
        worker_a["remaining_capacity_hours"] != worker_b["remaining_capacity_hours"],
        "remaining_capacity_hours actually differs between the two weeks",
    )

    check(worker_a["class_block_count"] == 1, f"week A counts the one class block whose semester covers it ({worker_a['class_block_count']})")
    check(worker_b["class_block_count"] == 0, f"week B has no covering semester, so zero class blocks count ({worker_b['class_block_count']})")
    check(worker_a["class_block_count"] != worker_b["class_block_count"], "class_block_count actually differs between the two weeks")

    check(worker_a["weekly_class_hours"] == 1.0, f"week A's class hours reflect the 60-minute block ({worker_a['weekly_class_hours']})")
    check(worker_b["weekly_class_hours"] == 0, f"week B's class hours are zero ({worker_b['weekly_class_hours']})")
    check(worker_a["weekly_class_hours"] != worker_b["weekly_class_hours"], "weekly_class_hours actually differs between the two weeks")

    check(worker_a["timetable_status"] == "confirmed", f"week A is fully confirmed for all seven of its days ({worker_a['timetable_status']})")
    check(worker_b["timetable_status"] == "outside_period", f"week B has a schedule but it covers none of week B's days ({worker_b['timetable_status']})")
    check(worker_a["timetable_status"] != worker_b["timetable_status"], "timetable_status actually differs between the two weeks")

    check(
        worker_a["is_active"] is True and worker_b["is_active"] is True,
        "active status is unaffected by the requested week",
    )
    check(
        worker_a["full_name"] == worker_b["full_name"] == "Week Test Worker",
        "employee identity (full_name) is unaffected by the requested week",
    )
    check(
        worker_a["employee_code"] == worker_b["employee_code"] == "SW-100",
        "employee identity (employee_code) is unaffected by the requested week",
    )

    probe = database.get_connection()
    try:
        before = full_snapshot(probe)
    finally:
        probe.close()
    main.list_employees(week_start="2026-11-02")
    main.list_employees(week_start="2026-11-16")
    probe = database.get_connection()
    try:
        after = full_snapshot(probe)
    finally:
        probe.close()
    check(before == after, "reading either week through /api/employees again performs no writes")


def main_entry():
    check_default_generator_unchanged()
    check_other_monday_shifted()
    check_generator_rejects_invalid_week_start()
    check_parse_week_start()
    check_unprepared_week_is_empty_and_read_only()
    check_prepare_week_inserts_and_is_idempotent()
    check_prepare_week_never_deletes_or_overwrites()
    check_sample_week_and_unrelated_rows_untouched()
    check_assigned_vs_uncovered_and_cross_midnight_ownership()
    check_list_employees_week_parameter()
    check_list_employees_week_dependent_values_actually_differ()

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"\nAll {_total['n']} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
