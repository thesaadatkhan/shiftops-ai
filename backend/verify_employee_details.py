"""Regression checks for the read-only employee-details endpoint.

**Read this before running it.** `main.py` calls `ensure_schema()` at import
time against whatever `database.DATABASE_PATH` points at, so this module
redirects that path to a throwaway file *before* importing `main`, and
refuses to run if `main` has already been imported. The project's own
`backend/shiftops.db` is never created, opened or migrated here.

These checks call `main.get_employee_details()` - the real endpoint function -
and read the payload it returns, rather than re-implementing its SQL. A test
that duplicates the query it is checking agrees with the code by construction
and proves nothing.

*Limitation:* the endpoint is called as a Python function. Starlette's
TestClient needs `httpx2`, which this project does not depend on. So these
exercise the endpoint's data handling and payload shape. They are NOT HTTP
transport tests: routing, JSON serialization over the wire, status codes on
the socket and CORS are not covered here.

Checks:

 1. Identity and ownership - one worker's payload carries their own records
    and none of another worker's, across every section.
 2. An unknown employee code is a 404, not an empty payload.
 3. Inactive workers have details like anyone else.
 4. Multiple semesters, including an expired one and a future one, are all
    returned in a deterministic order, each with its own coverage of the
    displayed week: outside, partial or full, tested at exact boundaries and
    kept separate from confirmation and from combined readiness.
 5. Missing, unconfirmed-empty and confirmed-no-classes timetables stay
    three distinguishable things.
 6. Fractional class durations survive, and blocks are ordered
    deterministically regardless of insertion order.
 7. Preferred and low preferences on concrete dated shifts; the empty state.
 8. Overnight shifts and approved leave keep both of their dates.
 9. Provisional migrated dates are reported only where stored provenance
    supports it; the internal note text never leaves the backend, and legacy
    course rows are never exposed as a timetable.
10. Assigned hours are read for the requested worker only, so another
    worker's invalid shift cannot break this page - while the worker's own
    invalid shift, and the whole-workforce list, still fail loudly.
11. Reading details changes nothing in the database.

Run with:  python verify_employee_details.py
Exits non-zero if any check fails.
"""

import json
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
from database import (  # noqa: E402
    DEMO_MIGRATION_NOTE,
    DEMO_SEMESTER_CONFIRMED_AT,
    DEMO_SEMESTER_END,
    DEMO_SEMESTER_START,
    LEGACY_MIGRATION_NOTE,
    MIGRATION_NOTE_PREFIX,
    expected_demo_timetables,
    migrate_class_schedules,
)
from fastapi import HTTPException  # noqa: E402

failures = []
_counter = {"n": 0}

# Tables whose contents must be identical before and after a details read.
SNAPSHOT_TABLES = [
    "employees",
    "semester_schedules",
    "class_blocks",
    "courses",
    "class_meetings",
    "shifts",
    "shift_preferences",
    "approved_leave",
    "assignments",
    "retired_employee_codes",
    "code_allocation",
]


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fresh_database():
    """Point the application at a brand-new throwaway file and return it."""
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


def add_worker(connection, code, name="Worker", active=1, seed_key=None):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active, seed_key)"
        " VALUES (?, ?, 'undergraduate', 20, ?, ?)",
        (code, name, active, seed_key),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]


def add_schedule(connection, employee_id, start, end, confirmed=None):
    connection.execute(
        "INSERT INTO semester_schedules (employee_id, start_date, end_date, confirmed_at)"
        " VALUES (?, ?, ?, ?)",
        (employee_id, start, end, confirmed),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM semester_schedules WHERE employee_id = ?"
        " AND start_date = ? AND end_date = ?",
        (employee_id, start, end),
    ).fetchone()["id"]


def add_block(connection, schedule_id, day, start, end, note=None):
    connection.execute(
        "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time,"
        " source_note) VALUES (?, ?, ?, ?, ?)",
        (schedule_id, day, start, end, note),
    )
    connection.commit()


def add_shift(connection, hall, start, end):
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES (?, ?, ?, 1)",
        (hall, start, end),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ? AND end_datetime = ?",
        (hall, start, end),
    ).fetchone()["id"]


def add_preference(connection, employee_id, shift_id, preference):
    connection.execute(
        "INSERT INTO shift_preferences (employee_id, shift_id, preference)"
        " VALUES (?, ?, ?)",
        (employee_id, shift_id, preference),
    )
    connection.commit()


def add_leave(connection, employee_id, start, end):
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime)"
        " VALUES (?, ?, ?)",
        (employee_id, start, end),
    )
    connection.commit()


def block_times(semester):
    return [
        (block["day_of_week"], block["start_time"], block["end_time"])
        for block in semester["class_blocks"]
    ]


# --------------------------------------------------------------------------
# 1. Identity and ownership.
# --------------------------------------------------------------------------
def check_identity_and_ownership():
    connection = fresh_database()

    mine = add_worker(connection, "SW-001", "Maria Alvarez")
    theirs = add_worker(connection, "SW-002", "Jordan Kim")

    my_schedule = add_schedule(connection, mine, "2026-08-24", "2026-12-11", None)
    add_block(connection, my_schedule, 0, "09:00", "10:15")

    their_schedule = add_schedule(connection, theirs, "2026-08-24", "2026-12-11", None)
    add_block(connection, their_schedule, 4, "18:00", "19:00")

    my_shift = add_shift(connection, "Vega", "2026-10-05 17:00", "2026-10-05 22:00")
    their_shift = add_shift(connection, "Helix", "2026-10-06 17:00", "2026-10-06 22:00")
    add_preference(connection, mine, my_shift, "preferred")
    add_preference(connection, theirs, their_shift, "low")

    add_leave(connection, mine, "2026-10-10 08:00", "2026-10-10 14:00")
    add_leave(connection, theirs, "2026-11-01 08:00", "2026-11-01 14:00")
    connection.close()

    payload = main.get_employee_details("SW-001")

    check(
        payload["employee"]["employee_code"] == "SW-001"
        and payload["employee"]["full_name"] == "Maria Alvarez",
        "the payload identifies the worker that was asked for",
    )
    check(
        len(payload["semesters"]) == 1
        and block_times(payload["semesters"][0]) == [(0, "09:00", "10:15")],
        f"only this worker's class blocks appear ({block_times(payload['semesters'][0])})",
    )
    check(
        [(row["hall"], row["preference"]) for row in payload["shift_preferences"]]
        == [("Vega", "preferred")],
        "only this worker's shift preferences appear",
    )
    check(
        [row["start_datetime"] for row in payload["approved_leave"]]
        == ["2026-10-10 08:00"],
        "only this worker's approved leave appears",
    )

    other = main.get_employee_details("SW-002")
    check(
        block_times(other["semesters"][0]) == [(4, "18:00", "19:00")]
        and [row["hall"] for row in other["shift_preferences"]] == ["Helix"]
        and [row["start_datetime"] for row in other["approved_leave"]]
        == ["2026-11-01 08:00"],
        "the other worker gets their own records, not the first worker's",
    )

    # The reporting week is named, so capacity figures are not floating free.
    check(
        payload["week_start"] == "2026-10-05" and payload["week_end"] == "2026-10-11",
        f"the reporting week is identified ({payload['week_start']} to {payload['week_end']})",
    )
    check(
        payload["employee"]["weekly_hour_limit"] == 20
        and payload["employee"]["assigned_hours"] == 0
        and payload["employee"]["remaining_capacity_hours"] == 20,
        "weekly limit, assigned hours and remaining capacity are reported",
    )


# --------------------------------------------------------------------------
# 2. Unknown worker.
# --------------------------------------------------------------------------
def check_unknown_worker():
    connection = fresh_database()
    add_worker(connection, "SW-001")
    connection.close()

    try:
        main.get_employee_details("SW-999")
        check(False, "an unknown employee code raises (it did not)")
    except HTTPException as error:
        check(
            error.status_code == 404,
            f"an unknown employee code is 404, not an empty payload ({error.status_code})",
        )
        check(
            "SW-999" in str(error.detail),
            f"the 404 detail names the code that was not found ({error.detail})",
        )

    # A code differing only in case is a different code here, exactly as it is
    # for every other route that addresses a worker by code.
    try:
        main.get_employee_details("sw-001")
        check(False, "a case-mismatched code raises (it did not)")
    except HTTPException as error:
        check(
            error.status_code == 404,
            f"a case-mismatched code is 404, matching the other routes ({error.status_code})",
        )


# --------------------------------------------------------------------------
# 3. Inactive workers.
# --------------------------------------------------------------------------
def check_inactive_worker():
    connection = fresh_database()
    employee_id = add_worker(connection, "SW-007", "Dormant Worker", active=0)
    schedule = add_schedule(
        connection, employee_id, "2026-08-24", "2026-12-11", DEMO_SEMESTER_CONFIRMED_AT
    )
    add_block(connection, schedule, 2, "13:00", "14:15")
    connection.close()

    payload = main.get_employee_details("SW-007")
    check(
        payload["employee"]["is_active"] is False,
        "an inactive worker is reported as inactive",
    )
    check(
        block_times(payload["semesters"][0]) == [(2, "13:00", "14:15")],
        "an inactive worker's stored classes are still shown",
    )
    check(
        payload["employee"]["timetable_status"] == "confirmed",
        "timetable readiness is independent of active status "
        f"({payload['employee']['timetable_status']})",
    )


# --------------------------------------------------------------------------
# 4. Several semesters, including expired and future ones.
# --------------------------------------------------------------------------
def check_multiple_semesters():
    connection = fresh_database()
    employee_id = add_worker(connection, "SW-010", "Three Semesters")

    # Inserted deliberately out of chronological order.
    future = add_schedule(connection, employee_id, "2027-01-11", "2027-05-07")
    expired = add_schedule(
        connection, employee_id, "2026-01-12", "2026-05-08", "2026-01-12 00:00"
    )
    current = add_schedule(
        connection, employee_id, "2026-08-24", "2026-12-11", DEMO_SEMESTER_CONFIRMED_AT
    )
    add_block(connection, expired, 1, "08:00", "09:15")
    add_block(connection, current, 3, "16:00", "17:15")
    add_block(connection, future, 0, "10:00", "11:15")
    connection.close()

    payload = main.get_employee_details("SW-010")
    semesters = payload["semesters"]

    check(len(semesters) == 3, f"every stored semester is returned ({len(semesters)})")
    check(
        [s["start_date"] for s in semesters]
        == ["2026-01-12", "2026-08-24", "2027-01-11"],
        f"semesters are ordered by start date ({[s['start_date'] for s in semesters]})",
    )
    check(
        [s["reporting_week_coverage"] for s in semesters]
        == ["outside", "full", "outside"],
        "each semester reports how much of the week its dates reach "
        f"({[s['reporting_week_coverage'] for s in semesters]})",
    )
    check(
        block_times(semesters[0]) == [(1, "08:00", "09:15")]
        and block_times(semesters[2]) == [(0, "10:00", "11:15")],
        "classes stored outside the reporting week are still shown",
    )
    check(
        semesters[0]["confirmed_at"] == "2026-01-12 00:00"
        and semesters[2]["confirmed_at"] is None,
        "each semester carries its own confirmation state and timestamp",
    )
    # The expired semester is confirmed, yet the week on screen is October.
    # Its own confirmation and the week's readiness are separate figures.
    check(
        payload["employee"]["timetable_status"] == "confirmed",
        "the week's readiness comes from the semester covering that week",
    )


def check_only_expired_semester():
    connection = fresh_database()
    employee_id = add_worker(connection, "SW-011", "Last Spring Only")
    schedule = add_schedule(
        connection, employee_id, "2026-01-12", "2026-05-08", "2026-01-12 00:00"
    )
    add_block(connection, schedule, 1, "08:00", "09:15")
    connection.close()

    payload = main.get_employee_details("SW-011")
    check(
        payload["employee"]["timetable_status"] == "outside_period",
        "a confirmed semester elsewhere does not make the week ready "
        f"({payload['employee']['timetable_status']})",
    )
    check(
        payload["semesters"][0]["confirmed_at"] == "2026-01-12 00:00"
        and payload["semesters"][0]["reporting_week_coverage"] == "outside",
        "that semester is still shown as confirmed, for its own period",
    )


# --------------------------------------------------------------------------
# 4b. Individual-semester coverage of the displayed week.
#
#     Review found the earlier boolean claiming a semester "includes the
#     reporting week" when it overlapped a single day of it. Coverage is now
#     three states, and it is about DATES ONLY - not confirmation, and not
#     the employee's combined readiness across all their semesters.
#
#     The displayed week is Monday 2026-10-05 to Sunday 2026-10-11.
# --------------------------------------------------------------------------
def coverage_of(connection, code, start, end, confirmed=None):
    employee_id = add_worker(connection, code, f"Boundary {code}")
    add_schedule(connection, employee_id, start, end, confirmed)
    return main.get_employee_details(code)["semesters"][0]["reporting_week_coverage"]


def check_semester_week_coverage():
    connection = fresh_database()

    cases = [
        # (code, start, end, expected, description)
        ("SW-060", "2026-10-05", "2026-10-11", "full", "exactly the week"),
        ("SW-061", "2026-08-24", "2026-12-11", "full", "a semester containing it"),
        ("SW-062", "2026-10-07", "2026-12-11", "partial", "starting midweek"),
        ("SW-063", "2026-08-24", "2026-10-07", "partial", "ending midweek"),
        ("SW-064", "2026-10-11", "2026-12-11", "partial", "starting on the last day"),
        ("SW-065", "2026-08-24", "2026-10-05", "partial", "ending on the first day"),
        ("SW-066", "2026-10-06", "2026-10-11", "partial", "missing only the Monday"),
        ("SW-067", "2026-10-05", "2026-10-10", "partial", "missing only the Sunday"),
        ("SW-068", "2026-10-12", "2026-12-11", "outside", "starting the day after"),
        ("SW-069", "2026-08-24", "2026-10-04", "outside", "ending the day before"),
        ("SW-070", "2026-01-12", "2026-05-08", "outside", "a different semester"),
    ]
    for code, start, end, expected, description in cases:
        actual = coverage_of(connection, code, start, end)
        check(actual == expected, f"{description}: {start} to {end} -> {actual}")

    # Two schedules that each cover part of the week and together cover all of
    # it. Neither is individually 'full'; the employee's combined readiness is
    # 'confirmed'. That is exactly the distinction the per-semester state must
    # not collapse.
    split = add_worker(connection, "SW-071", "Two Half Semesters")
    add_schedule(connection, split, "2026-08-24", "2026-10-07", "2026-08-24 00:00")
    add_schedule(connection, split, "2026-10-08", "2026-12-11", "2026-10-08 00:00")
    connection.close()

    payload = main.get_employee_details("SW-071")
    states = [s["reporting_week_coverage"] for s in payload["semesters"]]
    check(
        states == ["partial", "partial"],
        f"two half-semesters are each partial on their own ({states})",
    )
    check(
        payload["employee"]["timetable_status"] == "confirmed",
        "yet together they make the employee's week fully confirmed "
        f"({payload['employee']['timetable_status']})",
    )

    # Confirmation is independent of coverage in both directions.
    connection = fresh_database()
    unconfirmed_full = coverage_of(
        connection, "SW-072", "2026-08-24", "2026-12-11", None
    )
    confirmed_outside = coverage_of(
        connection, "SW-073", "2026-01-12", "2026-05-08", "2026-01-12 00:00"
    )
    connection.close()
    check(
        unconfirmed_full == "full",
        f"an unconfirmed semester can still cover the whole week ({unconfirmed_full})",
    )
    check(
        confirmed_outside == "outside",
        f"and a confirmed one can lie entirely outside it ({confirmed_outside})",
    )


# --------------------------------------------------------------------------
# 5. Missing / unconfirmed-empty / confirmed-no-classes.
# --------------------------------------------------------------------------
def check_three_empty_states():
    connection = fresh_database()
    add_worker(connection, "SW-020", "Nothing Entered")

    unconfirmed = add_worker(connection, "SW-021", "Entered, Unchecked")
    add_schedule(connection, unconfirmed, "2026-08-24", "2026-12-11", None)

    declared = add_worker(connection, "SW-022", "Deliberately No Classes")
    add_schedule(
        connection, declared, "2026-08-24", "2026-12-11", DEMO_SEMESTER_CONFIRMED_AT
    )
    connection.close()

    missing = main.get_employee_details("SW-020")
    check(
        missing["semesters"] == []
        and missing["employee"]["timetable_status"] == "missing",
        "no schedule at all reads as missing, with no semesters listed",
    )

    empty = main.get_employee_details("SW-021")
    check(
        len(empty["semesters"]) == 1
        and empty["semesters"][0]["class_blocks"] == []
        and empty["semesters"][0]["confirmed_at"] is None
        and empty["employee"]["timetable_status"] == "unconfirmed",
        "an unconfirmed empty schedule is a semester with no blocks and no "
        "confirmation",
    )

    confirmed = main.get_employee_details("SW-022")
    check(
        len(confirmed["semesters"]) == 1
        and confirmed["semesters"][0]["class_blocks"] == []
        and confirmed["semesters"][0]["confirmed_at"] == DEMO_SEMESTER_CONFIRMED_AT
        and confirmed["employee"]["timetable_status"] == "confirmed",
        "a confirmed schedule with no blocks is a deliberate 'no classes'",
    )

    check(
        missing["semesters"] != empty["semesters"]
        and empty["semesters"][0]["confirmed_at"]
        != confirmed["semesters"][0]["confirmed_at"],
        "all three states stay distinguishable from one another",
    )


# --------------------------------------------------------------------------
# 6. Fractional durations and deterministic ordering.
# --------------------------------------------------------------------------
def check_times_and_ordering():
    connection = fresh_database()
    employee_id = add_worker(connection, "SW-030", "Mixed Timetable")
    schedule = add_schedule(connection, employee_id, "2026-08-24", "2026-12-11")

    # Inserted in a deliberately scrambled order, including two blocks that
    # are identical to each other - the migration can legitimately produce
    # those, so the view must not drop one.
    add_block(connection, schedule, 3, "16:00", "17:15")
    add_block(connection, schedule, 0, "13:00", "14:15")
    add_block(connection, schedule, 3, "08:00", "10:45")
    add_block(connection, schedule, 0, "09:00", "10:15")
    add_block(connection, schedule, 3, "16:00", "17:15")
    connection.close()

    semester = main.get_employee_details("SW-030")["semesters"][0]

    check(
        block_times(semester)
        == [
            (0, "09:00", "10:15"),
            (0, "13:00", "14:15"),
            (3, "08:00", "10:45"),
            (3, "16:00", "17:15"),
            (3, "16:00", "17:15"),
        ],
        f"blocks come back ordered by weekday then time ({block_times(semester)})",
    )
    check(
        len(semester["class_blocks"]) == 5,
        f"two identical blocks are both kept ({len(semester['class_blocks'])})",
    )
    check(
        [block["hours"] for block in semester["class_blocks"]]
        == [1.25, 1.25, 2.75, 1.25, 1.25],
        "class durations stay fractional "
        f"({[block['hours'] for block in semester['class_blocks']]})",
    )

    # Repeating the read returns exactly the same thing.
    again = main.get_employee_details("SW-030")["semesters"][0]
    check(again == semester, "repeating the read returns an identical payload")


# --------------------------------------------------------------------------
# 7 and 8. Preferences, leave, overnight dates and empty states.
# --------------------------------------------------------------------------
def check_preferences_and_leave():
    connection = fresh_database()
    employee_id = add_worker(connection, "SW-040", "Has Preferences")
    add_worker(connection, "SW-041", "Has Nothing")

    evening = add_shift(connection, "Vega", "2026-10-05 17:00", "2026-10-05 22:00")
    overnight = add_shift(connection, "Capella", "2026-10-11 22:00", "2026-10-12 03:00")
    early = add_shift(connection, "Andromeda", "2026-10-06 03:00", "2026-10-06 08:00")

    add_preference(connection, employee_id, overnight, "low")
    add_preference(connection, employee_id, evening, "preferred")
    add_preference(connection, employee_id, early, "low")

    add_leave(connection, employee_id, "2026-10-10 08:00", "2026-10-10 14:00")
    add_leave(connection, employee_id, "2026-12-24 00:00", "2026-12-26 23:00")
    connection.close()

    payload = main.get_employee_details("SW-040")
    preferences = payload["shift_preferences"]

    check(
        [row["start_datetime"] for row in preferences]
        == ["2026-10-05 17:00", "2026-10-06 03:00", "2026-10-11 22:00"],
        "preferences are ordered by when the shift starts",
    )
    check(
        [row["preference"] for row in preferences] == ["preferred", "low", "low"],
        "preferred and low are both reported, as stored",
    )
    check(
        [row["hall"] for row in preferences] == ["Vega", "Andromeda", "Capella"],
        "each preference names the hall its shift is at",
    )
    check(
        preferences[2]["start_datetime"] == "2026-10-11 22:00"
        and preferences[2]["end_datetime"] == "2026-10-12 03:00",
        "an overnight shift keeps both of its dates "
        f"({preferences[2]['start_datetime']} to {preferences[2]['end_datetime']})",
    )
    check(
        len(preferences) == 3,
        f"only stored preferences are listed, not every neutral shift ({len(preferences)})",
    )

    leave = payload["approved_leave"]
    check(
        [(row["start_datetime"], row["end_datetime"]) for row in leave]
        == [
            ("2026-10-10 08:00", "2026-10-10 14:00"),
            ("2026-12-24 00:00", "2026-12-26 23:00"),
        ],
        "approved leave keeps its actual start and end datetimes, in order",
    )

    bare = main.get_employee_details("SW-041")
    check(
        bare["shift_preferences"] == [] and bare["approved_leave"] == [],
        "a worker with none of either gets empty lists, not invented rows",
    )


# --------------------------------------------------------------------------
# 9. Provisional migrated dates, from stored provenance only.
# --------------------------------------------------------------------------
def legacy_worker(connection, code, seed_key, meetings):
    employee_id = add_worker(connection, code, f"Legacy {code}", seed_key=seed_key)
    for index, (day, start, end) in enumerate(meetings):
        connection.execute(
            "INSERT INTO courses (employee_id, course_label) VALUES (?, ?)",
            (employee_id, f"{code}-course-{index}"),
        )
        course_id = connection.execute(
            "SELECT id FROM courses WHERE employee_id = ? AND course_label = ?",
            (employee_id, f"{code}-course-{index}"),
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO class_meetings (course_id, day_of_week, start_time, end_time)"
            " VALUES (?, ?, ?, ?)",
            (course_id, day, start, end),
        )
    connection.commit()
    return employee_id


def check_migrated_provenance():
    connection = fresh_database()

    # A hand-made worker the generator does not produce: migrates unconfirmed.
    legacy_worker(connection, "SW-900", None, [(1, "11:00", "12:15")])
    # A genuine demo worker whose stored meetings are the generated ones.
    demo_meetings = expected_demo_timetables()["SW-001"]
    legacy_worker(connection, "SW-001", "SW-001", demo_meetings)

    migrated = migrate_class_schedules(connection)
    check(migrated == 2, f"the fixture's two legacy workers migrated ({migrated})")

    # And a worker whose schedule was entered directly, with no migration.
    typed = add_worker(connection, "SW-901", "Typed In")
    typed_schedule = add_schedule(connection, typed, "2027-01-11", "2027-05-07")
    add_block(connection, typed_schedule, 2, "10:00", "11:15")
    connection.close()

    legacy = main.get_employee_details("SW-900")["semesters"][0]
    check(
        legacy["dates_provisional"] is True,
        "a migrated schedule's dates are reported as provisional",
    )
    check(
        "source_notes" not in legacy,
        f"without publishing the internal provenance note ({sorted(legacy)})",
    )
    check(
        legacy["start_date"] == DEMO_SEMESTER_START
        and legacy["end_date"] == DEMO_SEMESTER_END,
        "and those provisional dates are the documented demo semester",
    )
    check(
        legacy["confirmed_at"] is None,
        "a migrated legacy timetable stays unconfirmed",
    )

    demo = main.get_employee_details("SW-001")["semesters"][0]
    check(
        demo["dates_provisional"] is True,
        "a migrated demo timetable is provisional too",
    )
    check(
        demo["confirmed_at"] == DEMO_SEMESTER_CONFIRMED_AT,
        "and it is confirmed, because its meetings matched the generated ones",
    )
    check(
        len(demo["class_blocks"]) == len(demo_meetings),
        f"every legacy meeting became a block ({len(demo['class_blocks'])} of "
        f"{len(demo_meetings)})",
    )

    entered = main.get_employee_details("SW-901")["semesters"][0]
    check(
        entered["dates_provisional"] is False,
        "a schedule with no stored provenance is not called provisional",
    )

    # The notes are still stored - they are the evidence dates_provisional is
    # derived from - and must simply not leave the backend.
    inspect = database.get_connection()
    try:
        stored_notes = inspect.execute(
            "SELECT DISTINCT source_note FROM class_blocks WHERE source_note IS NOT NULL"
        ).fetchall()
    finally:
        inspect.close()
    check(
        sorted(row["source_note"] for row in stored_notes)
        == sorted([DEMO_MIGRATION_NOTE, LEGACY_MIGRATION_NOTE]),
        "both migration notes are still recorded in the database",
    )

    # The legacy course rows still exist, and neither they nor the internal
    # note text are exposed as a second timetable.
    payload = main.get_employee_details("SW-001")
    serialized = json.dumps(payload)
    check(
        "courses" not in payload and "class_meetings" not in payload,
        "legacy courses and class meetings are not part of the payload",
    )
    check(
        all("course" not in key for key in payload["employee"]),
        "and no course field survives in the worker's own details",
    )
    check(
        DEMO_MIGRATION_NOTE not in serialized
        and LEGACY_MIGRATION_NOTE not in serialized
        and MIGRATION_NOTE_PREFIX not in serialized,
        "no migration note text appears anywhere in the response",
    )
    check(
        "source_note" not in serialized,
        "and no source_note field appears at any depth",
    )


# --------------------------------------------------------------------------
# 9b. Assigned hours are read for the requested worker only.
#
#     Review found the endpoint calling the whole-workforce reporting
#     function, so a corrupt shift belonging to ANOTHER employee made this
#     worker's page return 500 - a failure caused by a record they have
#     nothing to do with. The worker's own bad data must still fail loudly.
# --------------------------------------------------------------------------
def assign(connection, employee_id, hall, start, end):
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES (?, ?, ?, 1)",
        (hall, start, end),
    )
    shift_id = connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ? AND end_datetime = ?",
        (hall, start, end),
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (employee_id, shift_id),
    )
    connection.commit()


def check_assigned_hours_scoping():
    connection = fresh_database()
    target = add_worker(connection, "SW-080", "Target Worker")
    other = add_worker(connection, "SW-081", "Unrelated Worker")

    # The target's own assignments are all valid: a weekday evening shift and
    # a Sunday-night shift crossing into Monday, which D025 charges entirely
    # to the week containing its start.
    assign(connection, target, "Vega", "2026-10-05 17:00", "2026-10-05 22:00")
    assign(connection, target, "Capella", "2026-10-11 22:00", "2026-10-12 03:00")

    # The other worker holds a shift that breaks the whole-hour rule.
    assign(connection, other, "Helix", "2026-10-06 17:00", "2026-10-06 22:30")
    connection.close()

    payload = main.get_employee_details("SW-080")
    check(
        payload["employee"]["assigned_hours"] == 10,
        "the target's own hours are 5 + 5, cross-midnight charged to the "
        f"starting week ({payload['employee']['assigned_hours']})",
    )
    check(
        payload["employee"]["remaining_capacity_hours"] == 10,
        f"remaining capacity follows ({payload['employee']['remaining_capacity_hours']})",
    )
    check(
        True,
        "another worker's invalid assigned shift did not break this page",
    )

    # The same broken row still makes the OTHER worker's own page fail.
    try:
        main.get_employee_details("SW-081")
        check(False, "the owner of the invalid shift still gets an error (it did not)")
    except HTTPException as error:
        check(
            error.status_code == 500,
            f"the owner of the invalid shift still gets a controlled 500 ({error.status_code})",
        )
        check(
            "Stored shift data is invalid" in str(error.detail),
            f"with the existing stored-data message ({error.detail})",
        )

    # And the workforce list, which reports on everyone, still validates
    # everything - that behaviour must not be weakened.
    try:
        main.list_employees()
        check(False, "the list endpoint still validates every assignment (it did not)")
    except HTTPException as error:
        check(
            error.status_code == 500,
            f"the list endpoint still validates every assignment ({error.status_code})",
        )

    # A worker with no assignments at all is simply zero.
    connection = fresh_database()
    add_worker(connection, "SW-082", "Never Assigned")
    connection.close()
    bare = main.get_employee_details("SW-082")
    check(
        bare["employee"]["assigned_hours"] == 0
        and bare["employee"]["remaining_capacity_hours"] == 20,
        "a worker with no assignments reports zero assigned hours",
    )

    # An assignment outside the reporting week is not counted, and its
    # validity is irrelevant because it is never read.
    connection = fresh_database()
    later = add_worker(connection, "SW-083", "Next Week Only")
    assign(connection, later, "Vega", "2026-10-12 17:00", "2026-10-12 22:00")
    connection.close()
    outside = main.get_employee_details("SW-083")
    check(
        outside["employee"]["assigned_hours"] == 0,
        "an assignment in another week is not charged to this one "
        f"({outside['employee']['assigned_hours']})",
    )


# --------------------------------------------------------------------------
# Codex review finding 4: the details route must use the SAME shared
# reporting week Employees/Schedule select, not always the fixed sample week.
# --------------------------------------------------------------------------
def check_selected_week_applies_to_details():
    connection = database.get_connection()
    employee_id = add_worker(connection, "SW-090", "Week Threaded")
    # Confirmed, non-provisional, and long enough to cover both weeks -
    # so any DIFFERENCE the two calls report comes from the assignment and
    # class-block data below, not from timetable coverage itself.
    schedule = add_schedule(connection, employee_id, "2026-08-24", "2026-12-11", "2026-01-01 00:00")
    add_block(connection, schedule, 0, "09:00", "10:00")  # every Monday

    # Sample week (2026-10-05): one assignment.
    assign(connection, employee_id, "Andromeda", "2026-10-05 08:00", "2026-10-05 13:00")
    # A genuinely different week (2026-11-02): two assignments, so the total
    # hours are actually different, not coincidentally the same number.
    assign(connection, employee_id, "Vega", "2026-11-02 08:00", "2026-11-02 13:00")
    assign(connection, employee_id, "Vega", "2026-11-02 17:00", "2026-11-02 22:00")
    connection.close()

    default_payload = main.get_employee_details("SW-090")
    other_payload = main.get_employee_details("SW-090", "2026-11-02")

    check(default_payload["week_start"] == "2026-10-05", "omitting week_start keeps the fixed sample week default")
    check(other_payload["week_start"] == "2026-11-02", "supplying week_start changes the reported week")
    check(
        default_payload["employee"]["assigned_hours"] == 5,
        f"the default week reports its own assigned hours ({default_payload['employee']['assigned_hours']})",
    )
    check(
        other_payload["employee"]["assigned_hours"] == 10,
        f"the other week reports its own, genuinely different assigned hours ({other_payload['employee']['assigned_hours']})",
    )
    check(
        default_payload["employee"]["assigned_hours"] != other_payload["employee"]["assigned_hours"],
        "assigned hours actually differ between the two weeks, not coincidentally equal",
    )
    check(
        default_payload["employee"]["remaining_capacity_hours"]
        != other_payload["employee"]["remaining_capacity_hours"],
        "remaining capacity actually differs between the two weeks too",
    )
    # Both weeks are fully covered by the same long confirmed semester, so
    # identity and readiness must NOT differ just because the week changed.
    check(
        default_payload["employee"]["employee_code"] == other_payload["employee"]["employee_code"]
        and default_payload["employee"]["timetable_status"] == other_payload["employee"]["timetable_status"]
        == "confirmed",
        "worker identity and timetable readiness are unaffected by which week was requested",
    )

    try:
        main.get_employee_details("SW-090", "not-a-date")
        check(False, "a malformed week_start on the details route is rejected")
    except HTTPException as error:
        check(error.status_code == 400, f"a malformed week_start on the details route is 400 ({error.status_code})")


# --------------------------------------------------------------------------
# Codex review finding 6: `scheduling_ready` distinguishes accepted
# (confirmed AND non-provisional) dates from a merely-confirmed status.
# --------------------------------------------------------------------------
def check_scheduling_ready_distinguishes_provisional():
    connection = database.get_connection()

    provisional_id = add_worker(connection, "SW-091", "Provisional Confirmed")
    schedule = add_schedule(connection, provisional_id, "2026-10-05", "2026-10-11", "2026-01-01 00:00")
    connection.execute(
        "UPDATE semester_schedules SET dates_provisional = 1 WHERE id = ?", (schedule,)
    )

    accepted_id = add_worker(connection, "SW-092", "Accepted Confirmed")
    add_schedule(connection, accepted_id, "2026-10-05", "2026-10-11", "2026-01-01 00:00")

    unconfirmed_id = add_worker(connection, "SW-093", "Unconfirmed")
    add_schedule(connection, unconfirmed_id, "2026-10-05", "2026-10-11", None)

    missing_id = add_worker(connection, "SW-094", "Missing")
    connection.commit()
    connection.close()

    provisional = main.get_employee_details("SW-091")["employee"]
    accepted = main.get_employee_details("SW-092")["employee"]
    unconfirmed = main.get_employee_details("SW-093")["employee"]
    missing = main.get_employee_details("SW-094")["employee"]

    check(
        provisional["timetable_status"] == "confirmed",
        f"a provisional-but-confirmed semester still reports timetable_status=confirmed, preserving that history ({provisional['timetable_status']})",
    )
    check(
        provisional["scheduling_ready"] is False,
        "but scheduling_ready is False for it - provisional dates are not accepted availability",
    )
    check(
        accepted["timetable_status"] == "confirmed" and accepted["scheduling_ready"] is True,
        "an accepted (confirmed, non-provisional) semester is both confirmed and scheduling_ready",
    )
    check(
        unconfirmed["scheduling_ready"] is False and missing["scheduling_ready"] is False,
        "unconfirmed and missing timetables are both not scheduling_ready",
    )

    # The list endpoint must agree exactly - one readiness definition, reused.
    listed = {
        row["employee_code"]: row
        for row in main.list_employees()["employees"]
        if row["employee_code"] in ("SW-091", "SW-092", "SW-093", "SW-094")
    }
    for code, detail in (
        ("SW-091", provisional),
        ("SW-092", accepted),
        ("SW-093", unconfirmed),
        ("SW-094", missing),
    ):
        check(
            listed[code]["scheduling_ready"] == detail["scheduling_ready"]
            and listed[code]["timetable_status"] == detail["timetable_status"],
            f"{code}: the list and details views agree on readiness and status",
        )


# --------------------------------------------------------------------------
# 10. Reading changes nothing.
# --------------------------------------------------------------------------
def check_read_is_read_only():
    connection = fresh_database()
    employee_id = add_worker(connection, "SW-050", "Untouched")
    schedule = add_schedule(connection, employee_id, "2026-08-24", "2026-12-11")
    add_block(connection, schedule, 0, "09:00", "10:15")
    shift = add_shift(connection, "Vega", "2026-10-05 17:00", "2026-10-05 22:00")
    add_preference(connection, employee_id, shift, "preferred")
    add_leave(connection, employee_id, "2026-10-10 08:00", "2026-10-10 14:00")

    before = snapshot(connection)
    connection.close()

    for _ in range(3):
        main.get_employee_details("SW-050")
    try:
        main.get_employee_details("SW-404")
    except HTTPException:
        pass

    connection = database.get_connection()
    after = snapshot(connection)
    connection.close()

    for table in SNAPSHOT_TABLES:
        check(
            after[table] == before[table],
            f"{table} is unchanged by reading details",
        )


def run():
    check_identity_and_ownership()
    check_unknown_worker()
    check_inactive_worker()
    check_multiple_semesters()
    check_only_expired_semester()
    check_semester_week_coverage()
    check_three_empty_states()
    check_times_and_ordering()
    check_preferences_and_leave()
    check_migrated_provenance()
    check_assigned_hours_scoping()
    check_selected_week_applies_to_details()
    check_scheduling_ready_distinguishes_provisional()
    check_read_is_read_only()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("All employee-details checks passed.")


if __name__ == "__main__":
    run()
