"""Regression checks for supervisor entry and editing of timetables (D035).

**Read this before running it.** `main.py` calls `ensure_schema()` at import
time against whatever `database.DATABASE_PATH` points at, so this module
redirects that path to a throwaway file *before* importing `main`, and refuses
to run if `main` has already been imported. The project's own
`backend/shiftops.db` is never created, opened or migrated here.

These checks call the real endpoint functions and read what they return, so
the exception-to-status-code mapping is covered as well as the stored result.

*Limitation:* they are called as Python functions. Starlette's TestClient
needs `httpx2`, which this project does not depend on, so routing, wire
serialization and CORS are not covered here - `verify_details_http.py` covers
those over a real socket for the details route.

Checks:

 1. Creating, editing and deleting a semester schedule.
 2. Creating, editing and deleting a class block.
 3. Everything written survives closing and reopening the database file.
 4. Invalid and reversed date ranges are refused.
 5. Overlapping semesters are refused, inclusively - sharing one day counts.
 6. Invalid weekdays and times are refused.
 7. Zero-length and negative class durations are refused.
 8. Duplicate and overlapping classes are refused; touching endpoints are not.
 9. One worker cannot reach another's schedule or block.
10. Unknown employees, schedules and blocks are 404.
11. A failure part-way through rolls the whole operation back.
12. Every content change withdraws the schedule's confirmation.
13. Provisional dates survive editing and deleting migrated class blocks, and
    are cleared when a supervisor supplies the semester's dates.
14. Unrelated workers and records are left byte-for-byte unchanged.
15. The employee list's summaries and readiness follow the edits.
16. Explicit supervisor confirmation: a populated timetable, deliberate
    confirmed-no-classes, refusal without the no-classes acknowledgement,
    unknown/cross-worker ownership, malformed and stale request bodies,
    accepting provisional dates as correct, idempotent reconfirmation,
    persistence, confirmation withdrawn by a later edit, unrelated data left
    unchanged, transaction rollback, and independence from active/inactive
    status.

Run with:  python verify_timetable_editing.py
Exits non-zero if any check fails.
"""

import sqlite3
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
    DEMO_SEMESTER_CONFIRMED_AT,
    DEMO_SEMESTER_END,
    DEMO_SEMESTER_START,
    expected_demo_timetables,
    migrate_class_schedules,
)
from fastapi import HTTPException  # noqa: E402

failures = []
_counter = {"n": 0}

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


def add_worker(connection, code, name="Worker", seed_key=None):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active, seed_key)"
        " VALUES (?, ?, 'undergraduate', 20, 1, ?)",
        (code, name, seed_key),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]


def semesters_of(code):
    return main.get_employee_details(code)["semesters"]


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


def stored(connection, schedule_id):
    return connection.execute(
        "SELECT * FROM semester_schedules WHERE id = ?", (schedule_id,)
    ).fetchone()


# --------------------------------------------------------------------------
# 1 and 2. The happy paths.
# --------------------------------------------------------------------------
def check_schedule_lifecycle():
    connection = fresh_database()
    add_worker(connection, "SW-001", "Maria Alvarez")
    connection.close()

    created = main.add_semester("SW-001", {"start_date": "2026-08-24", "end_date": "2026-12-11"})
    check(
        created["start_date"] == "2026-08-24" and created["end_date"] == "2026-12-11",
        "a semester is created with the dates supplied",
    )
    check("id" in created, "and the response carries its id for later edits")

    semester = semesters_of("SW-001")[0]
    check(
        semester["confirmed_at"] is None,
        "a new semester starts unconfirmed",
    )
    check(
        semester["dates_provisional"] is False,
        "and its dates are not provisional - a supervisor supplied them",
    )
    check(
        semester["class_blocks"] == [],
        "with no classes in it yet",
    )
    check(
        main.get_employee_details("SW-001")["employee"]["timetable_status"]
        == "unconfirmed",
        "the worker's readiness for the displayed week reads unconfirmed",
    )

    schedule_id = semester["id"]
    edited = main.edit_semester(
        "SW-001", schedule_id, {"start_date": "2026-09-01", "end_date": "2026-12-18"}
    )
    check(
        edited["start_date"] == "2026-09-01" and edited["end_date"] == "2026-12-18",
        "editing a semester changes its dates",
    )
    check(
        [s["start_date"] for s in semesters_of("SW-001")] == ["2026-09-01"],
        "and the details view shows the new dates",
    )

    removed = main.remove_semester("SW-001", schedule_id)
    check(
        removed["start_date"] == "2026-09-01" and removed["class_blocks"] == 0,
        f"deleting a semester reports what went with it ({removed})",
    )
    check(semesters_of("SW-001") == [], "and it is gone from the details view")
    check(
        main.get_employee_details("SW-001")["employee"]["timetable_status"] == "missing",
        "readiness falls back to missing once the last semester is deleted",
    )


def check_block_lifecycle():
    connection = fresh_database()
    add_worker(connection, "SW-001", "Maria Alvarez")
    connection.close()

    schedule_id = main.add_semester(
        "SW-001", {"start_date": "2026-08-24", "end_date": "2026-12-11"}
    )["id"]

    block = main.add_class_block(
        "SW-001", schedule_id, {"day_of_week": 0, "start_time": "09:00", "end_time": "10:15"}
    )
    check(
        block["day_of_week"] == 0
        and block["start_time"] == "09:00"
        and block["end_time"] == "10:15",
        "a class block is created with the weekday and times supplied",
    )

    semester = semesters_of("SW-001")[0]
    check(
        [(b["day_of_week"], b["start_time"], b["end_time"]) for b in semester["class_blocks"]]
        == [(0, "09:00", "10:15")],
        "and appears in the details view",
    )
    check(
        semester["class_blocks"][0]["hours"] == 1.25,
        "with its fractional length preserved "
        f"({semester['class_blocks'][0]['hours']})",
    )

    block_id = semester["class_blocks"][0]["id"]
    main.edit_class_block(
        "SW-001",
        schedule_id,
        block_id,
        {"day_of_week": 2, "start_time": "14:00", "end_time": "16:45"},
    )
    semester = semesters_of("SW-001")[0]
    check(
        [(b["day_of_week"], b["start_time"], b["end_time"]) for b in semester["class_blocks"]]
        == [(2, "14:00", "16:45")],
        "editing a class block changes its weekday and times",
    )
    check(
        semester["class_blocks"][0]["hours"] == 2.75,
        "and its length follows "
        f"({semester['class_blocks'][0]['hours']})",
    )

    main.remove_class_block("SW-001", schedule_id, block_id)
    semester = semesters_of("SW-001")[0]
    check(semester["class_blocks"] == [], "removing a class block leaves the semester empty")
    check(
        semester["confirmed_at"] is None,
        "an emptied semester is NOT auto-confirmed as 'no classes'",
    )

    # Several classes, and the ordering the details view guarantees.
    for day, start, end in [
        (2, "13:00", "14:15"),
        (0, "09:00", "10:15"),
        (2, "09:00", "10:15"),
    ]:
        main.add_class_block(
            "SW-001", schedule_id, {"day_of_week": day, "start_time": start, "end_time": end}
        )
    semester = semesters_of("SW-001")[0]
    check(
        [(b["day_of_week"], b["start_time"]) for b in semester["class_blocks"]]
        == [(0, "09:00"), (2, "09:00"), (2, "13:00")],
        "several classes come back ordered by weekday then time",
    )

    # Deleting the semester takes its classes with it.
    removed = main.remove_semester("SW-001", schedule_id)
    check(
        removed["class_blocks"] == 3,
        f"deleting a semester removes its classes too ({removed['class_blocks']})",
    )


# --------------------------------------------------------------------------
# 3. Persistence across reopening the file.
# --------------------------------------------------------------------------
def check_persistence():
    connection = fresh_database()
    add_worker(connection, "SW-001")
    connection.close()
    path = database.DATABASE_PATH

    schedule_id = main.add_semester(
        "SW-001", {"start_date": "2026-08-24", "end_date": "2026-12-11"}
    )["id"]
    main.add_class_block(
        "SW-001", schedule_id, {"day_of_week": 3, "start_time": "16:00", "end_time": "17:15"}
    )

    # A completely new connection to the same file.
    reopened = database.get_connection(path)
    schedules = reopened.execute("SELECT * FROM semester_schedules").fetchall()
    blocks = reopened.execute("SELECT * FROM class_blocks").fetchall()
    reopened.close()

    check(
        len(schedules) == 1
        and schedules[0]["start_date"] == "2026-08-24"
        and schedules[0]["confirmed_at"] is None
        and schedules[0]["dates_provisional"] == 0,
        "the semester is still there after reopening the database file",
    )
    check(
        len(blocks) == 1
        and (blocks[0]["day_of_week"], blocks[0]["start_time"], blocks[0]["end_time"])
        == (3, "16:00", "17:15"),
        "and so is its class, with its weekday and times intact",
    )


# --------------------------------------------------------------------------
# 4 to 8. Validation.
# --------------------------------------------------------------------------
def check_date_validation():
    connection = fresh_database()
    add_worker(connection, "SW-001")
    connection.close()

    bad_dates = [
        ({"start_date": "2026-12-11", "end_date": "2026-08-24"}, "reversed range"),
        ({"start_date": "24/08/2026", "end_date": "2026-12-11"}, "non-ISO start"),
        ({"start_date": "20260824", "end_date": "2026-12-11"}, "unpadded ISO form"),
        ({"start_date": "2026-02-30", "end_date": "2026-12-11"}, "impossible day"),
        ({"start_date": "2026-13-01", "end_date": "2026-12-11"}, "impossible month"),
        ({"start_date": "", "end_date": "2026-12-11"}, "empty start"),
        ({"start_date": None, "end_date": "2026-12-11"}, "null start"),
        ({"start_date": 20260824, "end_date": "2026-12-11"}, "numeric start"),
        ({"end_date": "2026-12-11"}, "missing start"),
        ({"start_date": "2026-08-24"}, "missing end"),
        ("not an object", "a payload that is not an object"),
    ]
    for payload, description in bad_dates:
        expect_status(
            400, lambda p=payload: main.add_semester("SW-001", p), f"{description} is a 400"
        )

    check(
        semesters_of("SW-001") == [],
        "and none of those rejected payloads created a semester",
    )

    # A single-day semester is legitimate: start == end.
    same_day = main.add_semester(
        "SW-001", {"start_date": "2026-10-07", "end_date": "2026-10-07"}
    )
    check(
        same_day["start_date"] == same_day["end_date"] == "2026-10-07",
        "a one-day semester is allowed",
    )
    check(
        semesters_of("SW-001")[0]["reporting_week_coverage"] == "partial",
        "and covers part of the reporting week",
    )


def check_semester_overlap():
    connection = fresh_database()
    add_worker(connection, "SW-001")
    connection.close()

    first = main.add_semester(
        "SW-001", {"start_date": "2026-08-24", "end_date": "2026-12-11"}
    )["id"]

    overlaps = [
        ({"start_date": "2026-12-11", "end_date": "2027-01-10"}, "sharing the last day"),
        ({"start_date": "2026-07-01", "end_date": "2026-08-24"}, "sharing the first day"),
        ({"start_date": "2026-09-01", "end_date": "2026-10-01"}, "wholly inside"),
        ({"start_date": "2026-01-01", "end_date": "2027-06-01"}, "wholly containing"),
        ({"start_date": "2026-08-24", "end_date": "2026-12-11"}, "identical"),
        ({"start_date": "2026-11-01", "end_date": "2027-02-01"}, "overlapping the end"),
    ]
    for payload, description in overlaps:
        expect_status(
            409,
            lambda p=payload: main.add_semester("SW-001", p),
            f"a semester {description} is a 409",
        )

    check(
        len(semesters_of("SW-001")) == 1,
        "none of the overlapping attempts was stored",
    )

    # Abutting without sharing a day is fine: inclusive ranges that end and
    # begin on consecutive days do not overlap.
    after = main.add_semester(
        "SW-001", {"start_date": "2026-12-12", "end_date": "2027-05-07"}
    )
    check(
        after["start_date"] == "2026-12-12",
        "a semester starting the day after the previous one ends is allowed",
    )
    before = main.add_semester(
        "SW-001", {"start_date": "2026-01-12", "end_date": "2026-08-23"}
    )
    check(
        before["start_date"] == "2026-01-12",
        "and so is one ending the day before it starts",
    )
    check(
        [s["start_date"] for s in semesters_of("SW-001")]
        == ["2026-01-12", "2026-08-24", "2026-12-12"],
        "three adjacent semesters coexist, ordered by start date",
    )

    # Editing must respect the same rule, while not colliding with itself.
    expect_status(
        409,
        lambda: main.edit_semester(
            "SW-001", first, {"start_date": "2026-08-01", "end_date": "2026-12-20"}
        ),
        "editing a semester into an overlap is a 409",
    )
    same = main.edit_semester(
        "SW-001", first, {"start_date": "2026-08-24", "end_date": "2026-12-11"}
    )
    check(
        same["start_date"] == "2026-08-24",
        "but re-saving a semester's own unchanged dates is not an overlap with itself",
    )


def check_block_validation():
    connection = fresh_database()
    add_worker(connection, "SW-001")
    connection.close()
    schedule_id = main.add_semester(
        "SW-001", {"start_date": "2026-08-24", "end_date": "2026-12-11"}
    )["id"]

    bad_blocks = [
        ({"day_of_week": 7, "start_time": "09:00", "end_time": "10:15"}, "weekday 7"),
        ({"day_of_week": -1, "start_time": "09:00", "end_time": "10:15"}, "weekday -1"),
        ({"day_of_week": "1", "start_time": "09:00", "end_time": "10:15"}, "weekday as text"),
        ({"day_of_week": 1.0, "start_time": "09:00", "end_time": "10:15"}, "weekday as float"),
        ({"day_of_week": True, "start_time": "09:00", "end_time": "10:15"}, "weekday as boolean"),
        ({"start_time": "09:00", "end_time": "10:15"}, "missing weekday"),
        ({"day_of_week": 0, "start_time": "9:00", "end_time": "10:15"}, "unpadded time"),
        ({"day_of_week": 0, "start_time": "24:00", "end_time": "24:30"}, "hour 24"),
        ({"day_of_week": 0, "start_time": "09:60", "end_time": "10:15"}, "minute 60"),
        ({"day_of_week": 0, "start_time": "09:00:00", "end_time": "10:15"}, "seconds included"),
        ({"day_of_week": 0, "start_time": "morning", "end_time": "10:15"}, "words for a time"),
        ({"day_of_week": 0, "start_time": None, "end_time": "10:15"}, "null start time"),
        ({"day_of_week": 0, "end_time": "10:15"}, "missing start time"),
        ({"day_of_week": 0, "start_time": "09:00"}, "missing end time"),
        ({"day_of_week": 0, "start_time": "09:00", "end_time": "09:00"}, "zero length"),
        ({"day_of_week": 0, "start_time": "10:15", "end_time": "09:00"}, "negative length"),
        ({"day_of_week": 0, "start_time": "22:00", "end_time": "02:00"}, "running past midnight"),
    ]
    for payload, description in bad_blocks:
        expect_status(
            400,
            lambda p=payload: main.add_class_block("SW-001", schedule_id, p),
            f"a class with {description} is a 400",
        )

    check(
        semesters_of("SW-001")[0]["class_blocks"] == [],
        "and none of those rejected classes was stored",
    )

    # Every valid weekday is accepted, and a one-minute class is legitimate -
    # the demo data's 75/165-minute conventions are not imposed on entry.
    for day in range(7):
        main.add_class_block(
            "SW-001", schedule_id, {"day_of_week": day, "start_time": "08:00", "end_time": "08:01"}
        )
    check(
        len(semesters_of("SW-001")[0]["class_blocks"]) == 7,
        "all seven weekdays are accepted, and a one-minute class is allowed",
    )
    check(
        semesters_of("SW-001")[0]["class_blocks"][0]["hours"] == 0.02,
        "its length is reported without being forced to a demo duration",
    )


def check_block_clashes():
    connection = fresh_database()
    add_worker(connection, "SW-001")
    connection.close()
    schedule_id = main.add_semester(
        "SW-001", {"start_date": "2026-08-24", "end_date": "2026-12-11"}
    )["id"]

    main.add_class_block(
        "SW-001", schedule_id, {"day_of_week": 1, "start_time": "10:00", "end_time": "11:15"}
    )

    expect_status(
        409,
        lambda: main.add_class_block(
            "SW-001", schedule_id, {"day_of_week": 1, "start_time": "10:00", "end_time": "11:15"}
        ),
        "an identical class in the same semester is a 409",
    )
    for payload, description in [
        ({"day_of_week": 1, "start_time": "10:30", "end_time": "11:00"}, "wholly inside"),
        ({"day_of_week": 1, "start_time": "09:00", "end_time": "12:00"}, "wholly containing"),
        ({"day_of_week": 1, "start_time": "09:00", "end_time": "10:30"}, "overlapping the start"),
        ({"day_of_week": 1, "start_time": "11:00", "end_time": "12:00"}, "overlapping the end"),
    ]:
        expect_status(
            409,
            lambda p=payload: main.add_class_block("SW-001", schedule_id, p),
            f"a class {description} is a 409",
        )

    check(
        len(semesters_of("SW-001")[0]["class_blocks"]) == 1,
        "no clashing class was stored",
    )

    # Touching endpoints do not overlap - the same rule as everywhere else.
    before = main.add_class_block(
        "SW-001", schedule_id, {"day_of_week": 1, "start_time": "09:00", "end_time": "10:00"}
    )
    after = main.add_class_block(
        "SW-001", schedule_id, {"day_of_week": 1, "start_time": "11:15", "end_time": "12:30"}
    )
    check(
        before["end_time"] == "10:00" and after["start_time"] == "11:15",
        "a class ending exactly when another starts is allowed, and vice versa",
    )

    # The same times on a different day are a different class entirely.
    other_day = main.add_class_block(
        "SW-001", schedule_id, {"day_of_week": 2, "start_time": "10:00", "end_time": "11:15"}
    )
    check(
        other_day["day_of_week"] == 2,
        "the same times on another weekday do not clash",
    )

    # Editing obeys the same rules, without clashing with itself.
    blocks = semesters_of("SW-001")[0]["class_blocks"]
    tuesday_ten = next(
        b for b in blocks if b["day_of_week"] == 1 and b["start_time"] == "10:00"
    )
    expect_status(
        409,
        lambda: main.edit_class_block(
            "SW-001",
            schedule_id,
            tuesday_ten["id"],
            {"day_of_week": 1, "start_time": "09:30", "end_time": "11:00"},
        ),
        "editing a class into an overlap is a 409",
    )
    unchanged = main.edit_class_block(
        "SW-001",
        schedule_id,
        tuesday_ten["id"],
        {"day_of_week": 1, "start_time": "10:00", "end_time": "11:15"},
    )
    check(
        unchanged["start_time"] == "10:00",
        "but re-saving a class's own unchanged times is not a clash with itself",
    )

    # Two semesters may hold identical classes: the rule is per semester.
    second = main.add_semester(
        "SW-001", {"start_date": "2027-01-11", "end_date": "2027-05-07"}
    )["id"]
    twin = main.add_class_block(
        "SW-001", second, {"day_of_week": 1, "start_time": "10:00", "end_time": "11:15"}
    )
    check(
        twin["start_time"] == "10:00",
        "an identical class in a DIFFERENT semester is allowed",
    )


# --------------------------------------------------------------------------
# 9 and 10. Ownership and unknown identifiers.
# --------------------------------------------------------------------------
def check_ownership_and_unknowns():
    connection = fresh_database()
    add_worker(connection, "SW-001", "Owner")
    add_worker(connection, "SW-002", "Stranger")
    connection.close()

    mine = main.add_semester(
        "SW-001", {"start_date": "2026-08-24", "end_date": "2026-12-11"}
    )["id"]
    my_block = main.add_class_block(
        "SW-001", mine, {"day_of_week": 0, "start_time": "09:00", "end_time": "10:15"}
    )["id"]
    theirs = main.add_semester(
        "SW-002", {"start_date": "2026-08-24", "end_date": "2026-12-11"}
    )["id"]

    # Another worker's schedule id is NOT FOUND for this worker, even though
    # the row exists. 404 rather than 403: saying "forbidden" would confirm
    # that somebody else holds that id.
    expect_status(
        404,
        lambda: main.edit_semester(
            "SW-002", mine, {"start_date": "2027-01-01", "end_date": "2027-02-01"}
        ),
        "editing another worker's semester is a 404",
    )
    expect_status(
        404,
        lambda: main.remove_semester("SW-002", mine),
        "deleting another worker's semester is a 404",
    )
    expect_status(
        404,
        lambda: main.add_class_block(
            "SW-002", mine, {"day_of_week": 0, "start_time": "09:00", "end_time": "10:15"}
        ),
        "adding a class to another worker's semester is a 404",
    )
    expect_status(
        404,
        lambda: main.remove_class_block("SW-002", mine, my_block),
        "deleting a class from another worker's semester is a 404",
    )
    # A block that exists, addressed through a schedule that is not its own.
    expect_status(
        404,
        lambda: main.remove_class_block("SW-002", theirs, my_block),
        "a class addressed through the wrong semester is a 404",
    )

    check(
        len(semesters_of("SW-001")[0]["class_blocks"]) == 1,
        "the owner's class is untouched by all of that",
    )
    check(
        semesters_of("SW-002")[0]["class_blocks"] == [],
        "and the other worker gained nothing",
    )
    check(
        semesters_of("SW-001")[0]["start_date"] == "2026-08-24",
        "the owner's semester dates are unchanged",
    )

    # Unknown identifiers.
    expect_status(
        404,
        lambda: main.add_semester("SW-404", {"start_date": "2026-08-24", "end_date": "2026-12-11"}),
        "an unknown employee is a 404",
    )
    expect_status(
        404,
        lambda: main.edit_semester(
            "SW-001", 9999, {"start_date": "2027-01-01", "end_date": "2027-02-01"}
        ),
        "an unknown semester id is a 404",
    )
    expect_status(
        404,
        lambda: main.remove_class_block("SW-001", mine, 9999),
        "an unknown class id is a 404",
    )
    # An unknown employee is checked before the schedule, so a bad code with a
    # real schedule id still reports the employee.
    error = expect_status(
        404,
        lambda: main.remove_semester("SW-404", mine),
        "an unknown employee with a real schedule id is a 404",
    )
    check(
        error is not None and "SW-404" in str(error.detail),
        "and the message names the employee, not the schedule",
    )


# --------------------------------------------------------------------------
# 11. Rollback after an injected failure.
# --------------------------------------------------------------------------
class FailingConnection:
    """Forwards to a real connection, but fails on a chosen statement.

    `sqlite3.Connection.execute` is read-only and cannot be monkeypatched, so
    the connection is wrapped rather than modified.
    """

    def __init__(self, connection, fail_on):
        self._connection = connection
        self._fail_on = fail_on

    def execute(self, sql, parameters=()):
        if self._fail_on in " ".join(sql.split()):
            raise sqlite3.OperationalError(f"injected failure on: {self._fail_on}")
        return self._connection.execute(sql, parameters)

    def commit(self):
        return self._connection.commit()

    def rollback(self):
        return self._connection.rollback()

    def close(self):
        return self._connection.close()

    @property
    def isolation_level(self):
        return self._connection.isolation_level

    @isolation_level.setter
    def isolation_level(self, value):
        self._connection.isolation_level = value


def check_rollback():
    import timetables

    connection = fresh_database()
    employee_id = add_worker(connection, "SW-001")
    path = database.DATABASE_PATH

    # A semester with two classes, confirmed, so a partial write would be
    # visible in more than one way.
    connection.execute(
        "INSERT INTO semester_schedules (employee_id, start_date, end_date,"
        " confirmed_at, dates_provisional)"
        " VALUES (?, '2026-08-24', '2026-12-11', '2026-08-24 00:00', 0)",
        (employee_id,),
    )
    schedule_id = connection.execute("SELECT id FROM semester_schedules").fetchone()["id"]
    for day, start, end in [(0, "09:00", "10:15"), (2, "13:00", "14:15")]:
        connection.execute(
            "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time,"
            " source_note) VALUES (?, ?, ?, ?, NULL)",
            (schedule_id, day, start, end),
        )
    connection.commit()
    before = snapshot(connection)
    connection.close()

    injections = [
        ("UPDATE semester_schedules SET confirmed_at = NULL", "adding a class"),
        ("DELETE FROM semester_schedules", "deleting a semester"),
        ("UPDATE class_blocks", "editing a class"),
    ]

    for fail_on, description in injections:
        raw = database.get_connection(path)
        failing = FailingConnection(raw, fail_on)
        block_id = raw.execute(
            "SELECT id FROM class_blocks ORDER BY id LIMIT 1"
        ).fetchone()["id"]

        try:
            if description == "adding a class":
                timetables.create_block(
                    failing,
                    "SW-001",
                    schedule_id,
                    {"day_of_week": 4, "start_time": "11:00", "end_time": "12:00"},
                )
            elif description == "deleting a semester":
                timetables.delete_schedule(failing, "SW-001", schedule_id)
            else:
                timetables.update_block(
                    failing,
                    "SW-001",
                    schedule_id,
                    block_id,
                    {"day_of_week": 5, "start_time": "07:00", "end_time": "08:00"},
                )
            check(False, f"the injected failure while {description} propagated")
        except sqlite3.OperationalError:
            check(True, f"the injected failure while {description} propagated")
        raw.close()

        after_connection = database.get_connection(path)
        after = snapshot(after_connection)
        after_connection.close()
        same = all(after[table] == before[table] for table in SNAPSHOT_TABLES)
        check(same, f"and the database is byte-for-byte unchanged after {description}")


# --------------------------------------------------------------------------
# 12. Confirmation is withdrawn by every content change.
# --------------------------------------------------------------------------
def confirm(connection, schedule_id):
    connection.execute(
        "UPDATE semester_schedules SET confirmed_at = ? WHERE id = ?",
        (DEMO_SEMESTER_CONFIRMED_AT, schedule_id),
    )
    connection.commit()


def check_confirmation_invalidation():
    connection = fresh_database()
    add_worker(connection, "SW-001")
    connection.close()
    path = database.DATABASE_PATH

    schedule_id = main.add_semester(
        "SW-001", {"start_date": "2026-08-24", "end_date": "2026-12-11"}
    )["id"]
    block = main.add_class_block(
        "SW-001", schedule_id, {"day_of_week": 0, "start_time": "09:00", "end_time": "10:15"}
    )

    actions = [
        (
            "adding a class",
            lambda: main.add_class_block(
                "SW-001", schedule_id, {"day_of_week": 4, "start_time": "11:00", "end_time": "12:00"}
            ),
        ),
        (
            "editing a class",
            lambda: main.edit_class_block(
                "SW-001",
                schedule_id,
                block["id"],
                {"day_of_week": 0, "start_time": "09:30", "end_time": "10:45"},
            ),
        ),
        (
            "removing a class",
            lambda: main.remove_class_block("SW-001", schedule_id, block["id"]),
        ),
        (
            "editing the semester dates",
            lambda: main.edit_semester(
                "SW-001", schedule_id, {"start_date": "2026-08-25", "end_date": "2026-12-11"}
            ),
        ),
    ]

    for description, action in actions:
        marker = database.get_connection(path)
        confirm(marker, schedule_id)
        was_confirmed = stored(marker, schedule_id)["confirmed_at"] is not None
        marker.close()
        check(was_confirmed, f"the semester is confirmed before {description}")

        action()

        after = database.get_connection(path)
        row = stored(after, schedule_id)
        after.close()
        check(
            row["confirmed_at"] is None,
            f"{description} withdraws the confirmation",
        )

    # Re-saving identical dates still withdraws it: the supervisor asserted
    # the dates, and erring towards unconfirmed understates readiness rather
    # than overstating it.
    marker = database.get_connection(path)
    confirm(marker, schedule_id)
    marker.close()
    main.edit_semester(
        "SW-001", schedule_id, {"start_date": "2026-08-25", "end_date": "2026-12-11"}
    )
    after = database.get_connection(path)
    row = stored(after, schedule_id)
    after.close()
    check(
        row["confirmed_at"] is None,
        "re-saving the same dates also withdraws confirmation",
    )

    # A REFUSED edit changes nothing, confirmation included.
    marker = database.get_connection(path)
    confirm(marker, schedule_id)
    marker.close()
    expect_status(
        400,
        lambda: main.edit_semester(
            "SW-001", schedule_id, {"start_date": "2026-12-11", "end_date": "2026-08-24"}
        ),
        "a refused date edit is a 400",
    )
    after = database.get_connection(path)
    row = stored(after, schedule_id)
    after.close()
    check(
        row["confirmed_at"] == DEMO_SEMESTER_CONFIRMED_AT,
        "and leaves the existing confirmation standing",
    )

    # Another semester's confirmation is not disturbed.
    other = main.add_semester(
        "SW-001", {"start_date": "2027-01-11", "end_date": "2027-05-07"}
    )["id"]
    marker = database.get_connection(path)
    confirm(marker, other)
    marker.close()
    main.add_class_block(
        "SW-001", schedule_id, {"day_of_week": 6, "start_time": "10:00", "end_time": "11:00"}
    )
    after = database.get_connection(path)
    check(
        stored(after, other)["confirmed_at"] == DEMO_SEMESTER_CONFIRMED_AT,
        "editing one semester leaves another semester's confirmation alone",
    )
    after.close()


# --------------------------------------------------------------------------
# 16. Explicit supervisor confirmation of a semester timetable.
# --------------------------------------------------------------------------
def semester_row(code, schedule_id):
    return next(s for s in semesters_of(code) if s["id"] == schedule_id)


def block_snapshot(semester):
    """The exact-class-snapshot shape `confirm_schedule` now requires."""
    return [
        {
            "id": block["id"],
            "day_of_week": block["day_of_week"],
            "start_time": block["start_time"],
            "end_time": block["end_time"],
        }
        for block in semester["class_blocks"]
    ]


def check_confirmation():
    connection = fresh_database()
    add_worker(connection, "SW-001", "Maria Alvarez")
    add_worker(connection, "SW-002", "Jordan Kim")
    connection.close()
    path = database.DATABASE_PATH

    schedule_id = main.add_semester(
        "SW-001", {"start_date": "2026-08-24", "end_date": "2026-12-11"}
    )["id"]
    main.add_class_block(
        "SW-001", schedule_id, {"day_of_week": 0, "start_time": "09:00", "end_time": "10:15"}
    )

    # --- creating a semester, or adding/removing its only class, never
    # implies confirmation. ------------------------------------------------
    check(
        semester_row("SW-001", schedule_id)["confirmed_at"] is None,
        "creating a semester with a class never confirms it",
    )
    solo = main.add_class_block(
        "SW-001", schedule_id, {"day_of_week": 4, "start_time": "11:00", "end_time": "12:00"}
    )
    main.remove_class_block("SW-001", schedule_id, solo["id"])
    check(
        semester_row("SW-001", schedule_id)["confirmed_at"] is None,
        "adding then removing a class never confirms the semester either",
    )

    # --- confirming a populated timetable ----------------------------------
    snapshot_1 = block_snapshot(semester_row("SW-001", schedule_id))
    result = main.confirm_semester(
        "SW-001",
        schedule_id,
        {
            "start_date": "2026-08-24",
            "end_date": "2026-12-11",
            "class_blocks": snapshot_1,
            "acknowledge_no_classes": False,
        },
    )
    check(result["id"] == schedule_id, "confirming returns the schedule's id")
    check(result["confirmed_at"] is not None, "and a real confirmation timestamp")
    check(result["dates_provisional"] is False, "and reports dates_provisional as false")
    check(result["class_count"] == 1, "and the confirmed class count")
    check(
        semester_row("SW-001", schedule_id)["confirmed_at"] is not None,
        "the stored semester is now confirmed",
    )
    status = main.get_employee_details("SW-001")["employee"]["timetable_status"]
    check(
        status in ("confirmed", "partial"),
        f"the worker's readiness reflects the confirmation ({status})",
    )

    # --- a stale class SNAPSHOT is a conflict even when the count matches --
    # Codex-reported gap: a class moving to a different day/time while the
    # count stays the same must still be caught. Withdraw and reconfirm to
    # set up a clean precondition, then edit the block without touching the
    # count, then try to confirm with the OLD snapshot.
    main.edit_semester(
        "SW-001", schedule_id, {"start_date": "2026-08-24", "end_date": "2026-12-11"}
    )
    stale_snapshot = block_snapshot(semester_row("SW-001", schedule_id))
    moved_block_id = stale_snapshot[0]["id"]
    main.edit_class_block(
        "SW-001",
        schedule_id,
        moved_block_id,
        {"day_of_week": 2, "start_time": "15:00", "end_time": "16:00"},
    )
    check(
        len(semester_row("SW-001", schedule_id)["class_blocks"]) == len(stale_snapshot),
        "the class count is unchanged after the edit (setup for the stale-snapshot check)",
    )
    expect_status(
        409,
        lambda: main.confirm_semester(
            "SW-001",
            schedule_id,
            {
                "start_date": "2026-08-24",
                "end_date": "2026-12-11",
                "class_blocks": stale_snapshot,
                "acknowledge_no_classes": False,
            },
        ),
        "confirming a stale class snapshot is refused even though the count still matches",
    )
    check(
        semester_row("SW-001", schedule_id)["confirmed_at"] is None,
        "and nothing was confirmed - the moved-class edit's own withdrawal still stands",
    )
    # Confirming the CURRENT snapshot succeeds.
    current_snapshot = block_snapshot(semester_row("SW-001", schedule_id))
    main.confirm_semester(
        "SW-001",
        schedule_id,
        {
            "start_date": "2026-08-24",
            "end_date": "2026-12-11",
            "class_blocks": current_snapshot,
            "acknowledge_no_classes": False,
        },
    )
    check(
        semester_row("SW-001", schedule_id)["confirmed_at"] is not None,
        "confirming the classes actually shown succeeds",
    )

    # --- a second semester, to exercise the deliberate no-classes path -----
    other_id = main.add_semester(
        "SW-001", {"start_date": "2027-01-11", "end_date": "2027-05-07"}
    )["id"]

    expect_status(
        400,
        lambda: main.confirm_semester(
            "SW-001",
            other_id,
            {
                "start_date": "2027-01-11",
                "end_date": "2027-05-07",
                "class_blocks": [],
                "acknowledge_no_classes": False,
            },
        ),
        "confirming an empty semester without acknowledge_no_classes is refused",
    )
    check(
        semester_row("SW-001", other_id)["confirmed_at"] is None,
        "and it remains unconfirmed",
    )

    empty_result = main.confirm_semester(
        "SW-001",
        other_id,
        {
            "start_date": "2027-01-11",
            "end_date": "2027-05-07",
            "class_blocks": [],
            "acknowledge_no_classes": True,
        },
    )
    check(
        empty_result["confirmed_at"] is not None and empty_result["class_count"] == 0,
        "the deliberate no-classes acknowledgement confirms an empty semester",
    )

    expect_status(
        400,
        lambda: main.confirm_semester(
            "SW-001",
            schedule_id,
            {
                "start_date": "2026-08-24",
                "end_date": "2026-12-11",
                "class_blocks": current_snapshot,
                "acknowledge_no_classes": True,
            },
        ),
        "acknowledge_no_classes cannot be true when classes are actually stored",
    )

    # --- stale dates is a conflict, never a silent confirm ------------------
    expect_status(
        409,
        lambda: main.confirm_semester(
            "SW-001",
            schedule_id,
            {
                "start_date": "2026-08-25",
                "end_date": "2026-12-11",
                "class_blocks": current_snapshot,
                "acknowledge_no_classes": False,
            },
        ),
        "confirming with stale dates is a 409",
    )

    # --- malformed, missing and wrong-shaped bodies - a few representative
    # shapes, not a permutation of every field. -----------------------------
    bad_bodies = [
        ("None", None),
        ("a list", []),
        ("missing start_date", {"end_date": "2026-12-11", "class_blocks": []}),
        (
            "class_blocks not a list",
            {"start_date": "2026-08-24", "end_date": "2026-12-11", "class_blocks": "none"},
        ),
        (
            "a class_blocks entry missing id",
            {
                "start_date": "2026-08-24",
                "end_date": "2026-12-11",
                "class_blocks": [{"day_of_week": 0, "start_time": "09:00", "end_time": "10:15"}],
            },
        ),
        (
            "a non-boolean acknowledge_no_classes",
            {
                "start_date": "2026-08-24",
                "end_date": "2026-12-11",
                "class_blocks": current_snapshot,
                "acknowledge_no_classes": "yes",
            },
        ),
    ]
    for label, bad_payload in bad_bodies:
        expect_status(
            400,
            lambda bad_payload=bad_payload: main.confirm_semester(
                "SW-001", schedule_id, bad_payload
            ),
            f"a confirmation body that is {label} is refused, not confirmed",
        )
    check(
        semester_row("SW-001", schedule_id)["confirmed_at"] is not None,
        "the malformed attempts above left the earlier confirmation standing",
    )

    # --- unknown employee, unknown semester, cross-worker ownership --------
    valid_body = {
        "start_date": "2026-08-24",
        "end_date": "2026-12-11",
        "class_blocks": current_snapshot,
        "acknowledge_no_classes": False,
    }
    expect_status(
        404,
        lambda: main.confirm_semester("SW-999", schedule_id, valid_body),
        "confirming for an unknown employee is a 404",
    )
    expect_status(
        404,
        lambda: main.confirm_semester("SW-001", 999999, valid_body),
        "confirming an unknown schedule id is a 404",
    )
    expect_status(
        404,
        lambda: main.confirm_semester("SW-002", schedule_id, valid_body),
        "confirming another worker's schedule id is a 404, not a 403",
    )

    # --- editing after confirmation withdraws it, through the real routes --
    main.edit_semester(
        "SW-001", schedule_id, {"start_date": "2026-08-24", "end_date": "2026-12-18"}
    )
    check(
        semester_row("SW-001", schedule_id)["confirmed_at"] is None,
        "editing a confirmed semester's dates withdraws the confirmation",
    )
    # Re-confirm the new dates for the checks that follow.
    reconfirm_snapshot = block_snapshot(semester_row("SW-001", schedule_id))
    main.confirm_semester(
        "SW-001",
        schedule_id,
        {
            "start_date": "2026-08-24",
            "end_date": "2026-12-18",
            "class_blocks": reconfirm_snapshot,
            "acknowledge_no_classes": False,
        },
    )

    # --- repeated confirmation: idempotent success, not a refusal. Stored
    # timestamps have minute precision (D025), so a second confirmation may
    # legitimately record the SAME timestamp - only that it succeeds is
    # asserted here, not that it visibly changes. -----------------------
    reconfirmed = main.confirm_semester(
        "SW-001",
        schedule_id,
        {
            "start_date": "2026-08-24",
            "end_date": "2026-12-18",
            "class_blocks": reconfirm_snapshot,
            "acknowledge_no_classes": False,
        },
    )
    check(
        reconfirmed["confirmed_at"] is not None,
        "reconfirming identical, already-confirmed content succeeds "
        "(idempotent success, not a refusal)",
    )

    # --- persistence across a fresh connection ------------------------------
    reopened = database.get_connection(path)
    row = stored(reopened, schedule_id)
    check(
        row["confirmed_at"] is not None and row["dates_provisional"] == 0,
        "the confirmation survives reopening the database",
    )
    reopened.close()

    # --- unrelated data is left byte-for-byte unchanged by a confirmation --
    before_conn = database.get_connection(path)
    before = {table: snapshot(before_conn)[table] for table in SNAPSHOT_TABLES}
    before_conn.close()

    main.confirm_semester(
        "SW-001",
        schedule_id,
        {
            "start_date": "2026-08-24",
            "end_date": "2026-12-18",
            "class_blocks": reconfirm_snapshot,
            "acknowledge_no_classes": False,
        },
    )

    after_conn = database.get_connection(path)
    after = {table: snapshot(after_conn)[table] for table in SNAPSHOT_TABLES}
    after_conn.close()

    for table in SNAPSHOT_TABLES:
        if table == "semester_schedules":
            continue
        check(
            before[table] == after[table],
            f"confirming a semester leaves {table} byte-for-byte unchanged",
        )

    def without_confirmed_at(rows):
        # column order: id, employee_id, start_date, end_date, confirmed_at,
        # dates_provisional (see database.py's semester_schedules DDL).
        return {row[0]: row[:4] + row[5:] for row in rows}

    check(
        without_confirmed_at(before["semester_schedules"])
        == without_confirmed_at(after["semester_schedules"]),
        "and every semester row is unchanged apart from the reconfirmed row's timestamp",
    )
    check(
        len(before["employees"]) == len(after["employees"]) == 2,
        "SW-002, who owns none of this, is completely untouched",
    )

    # --- transaction rollback on failure -------------------------------
    import timetables

    raw = database.get_connection(path)
    failing = FailingConnection(raw, "UPDATE semester_schedules SET confirmed_at")
    before_rollback = stored(raw, other_id)
    try:
        timetables.confirm_schedule(
            failing,
            "SW-001",
            other_id,
            {
                "start_date": "2027-01-11",
                "end_date": "2027-05-07",
                "class_blocks": [],
                "acknowledge_no_classes": True,
            },
        )
        check(False, "the injected failure while confirming propagated")
    except sqlite3.OperationalError:
        check(True, "the injected failure while confirming propagated")
    raw.close()

    after_rollback_conn = database.get_connection(path)
    after_rollback = stored(after_rollback_conn, other_id)
    after_rollback_conn.close()
    check(
        tuple(before_rollback) == tuple(after_rollback),
        "a failure inside the confirmation transaction leaves the row unchanged",
    )

    # --- active/inactive status is independent of timetable confirmation ---
    main.deactivate_employee("SW-001")
    check(
        semester_row("SW-001", schedule_id)["confirmed_at"] is not None,
        "deactivating a worker does not disturb an existing confirmation",
    )
    main.reactivate_employee("SW-001")
    other_confirmed = main.confirm_semester(
        "SW-001", other_id, {
            "start_date": "2027-01-11",
            "end_date": "2027-05-07",
            "class_blocks": [],
            "acknowledge_no_classes": True,
        },
    )
    check(
        other_confirmed["confirmed_at"] is not None,
        "confirming an inactive-then-reactivated worker's semester still works",
    )


# --------------------------------------------------------------------------
# 13. Provisional dates survive block edits; supplying dates clears them.
# --------------------------------------------------------------------------
def migrated_fixture():
    """A database whose timetable arrived through the legacy migration."""
    connection = fresh_database()
    demo_meetings = expected_demo_timetables()["SW-001"]
    employee_id = add_worker(connection, "SW-001", "Maria Alvarez", seed_key="SW-001")
    for index, (day, start, end) in enumerate(demo_meetings):
        connection.execute(
            "INSERT INTO courses (employee_id, course_label) VALUES (?, ?)",
            (employee_id, f"course-{index}"),
        )
        course_id = connection.execute(
            "SELECT id FROM courses WHERE employee_id = ? AND course_label = ?",
            (employee_id, f"course-{index}"),
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO class_meetings (course_id, day_of_week, start_time, end_time)"
            " VALUES (?, ?, ?, ?)",
            (course_id, day, start, end),
        )
    connection.commit()
    migrate_class_schedules(connection)
    connection.close()
    return len(demo_meetings)


def check_provisional_provenance():
    total = migrated_fixture()

    semester = semesters_of("SW-001")[0]
    check(
        semester["dates_provisional"] is True,
        "a migrated semester's dates start out provisional",
    )
    check(
        semester["start_date"] == DEMO_SEMESTER_START
        and semester["end_date"] == DEMO_SEMESTER_END,
        "with the assumed demo-semester dates",
    )
    schedule_id = semester["id"]

    # Editing a migrated class must NOT change the date provenance.
    first_block = semester["class_blocks"][0]
    main.edit_class_block(
        "SW-001",
        schedule_id,
        first_block["id"],
        {"day_of_week": 5, "start_time": "07:00", "end_time": "08:00"},
    )
    check(
        semesters_of("SW-001")[0]["dates_provisional"] is True,
        "editing a migrated class leaves the dates provisional",
    )

    # Deleting every migrated class must not erase it either. This is the
    # case the old block-derived answer got wrong.
    for block in semesters_of("SW-001")[0]["class_blocks"]:
        main.remove_class_block("SW-001", schedule_id, block["id"])
    semester = semesters_of("SW-001")[0]
    check(
        semester["class_blocks"] == [],
        f"every one of the {total} migrated classes can be removed",
    )
    check(
        semester["dates_provisional"] is True,
        "and the dates are STILL provisional with no migrated block left",
    )

    # Adding a fresh class does not make them non-provisional either: the
    # dates are still nobody's decision.
    main.add_class_block(
        "SW-001", schedule_id, {"day_of_week": 1, "start_time": "09:00", "end_time": "10:15"}
    )
    check(
        semesters_of("SW-001")[0]["dates_provisional"] is True,
        "adding a new class does not make assumed dates non-provisional",
    )

    # Supplying the dates is what clears it.
    main.edit_semester(
        "SW-001", schedule_id, {"start_date": "2026-09-01", "end_date": "2026-12-18"}
    )
    check(
        semesters_of("SW-001")[0]["dates_provisional"] is False,
        "editing the semester's dates clears the provisional flag",
    )

    # And it stays cleared through later class edits.
    block_id = semesters_of("SW-001")[0]["class_blocks"][0]["id"]
    main.edit_class_block(
        "SW-001",
        schedule_id,
        block_id,
        {"day_of_week": 3, "start_time": "15:00", "end_time": "16:00"},
    )
    check(
        semesters_of("SW-001")[0]["dates_provisional"] is False,
        "and stays cleared through later class edits",
    )

    # A supervisor-created semester is never provisional.
    fresh = main.add_semester(
        "SW-001", {"start_date": "2027-01-11", "end_date": "2027-05-07"}
    )["id"]
    check(
        next(s for s in semesters_of("SW-001") if s["id"] == fresh)["dates_provisional"]
        is False,
        "a supervisor-created semester is not provisional",
    )

    # The raw migration notes never leave the backend.
    import json

    serialized = json.dumps(main.get_employee_details("SW-001"))
    check(
        "source_note" not in serialized and database.MIGRATION_NOTE_PREFIX not in serialized,
        "and no migration note text appears in the response",
    )


def check_confirmation_provisional():
    """Confirming accepts migrated (provisional) dates as correct.

    Uses its own migrated fixture, like `check_provisional_provenance`: the
    migration path only exists through `migrated_fixture()`, which builds a
    fresh database of its own.
    """
    migrated_fixture()
    semester = semesters_of("SW-001")[0]
    check(
        semester["dates_provisional"] is True,
        "the migrated fixture's dates start out provisional",
    )
    schedule_id = semester["id"]

    confirmed = main.confirm_semester(
        "SW-001",
        schedule_id,
        {
            "start_date": semester["start_date"],
            "end_date": semester["end_date"],
            "class_blocks": block_snapshot(semester),
            "acknowledge_no_classes": False,
        },
    )
    check(
        confirmed["dates_provisional"] is False,
        "confirming accepts the migrated dates as correct and clears the flag",
    )
    check(
        semesters_of("SW-001")[0]["dates_provisional"] is False,
        "which is reflected back in the details view",
    )
    check(
        semesters_of("SW-001")[0]["confirmed_at"] is not None,
        "and the semester is confirmed",
    )

    reopened = database.get_connection(database.DATABASE_PATH)
    row = stored(reopened, schedule_id)
    check(
        row["confirmed_at"] is not None and row["dates_provisional"] == 0,
        "both survive reopening the database",
    )
    reopened.close()


def check_migration_backfill():
    """The schema migration gives existing databases the right answer."""
    # A database built the old way: schedules with no dates_provisional
    # column at all, one migrated (notes on its blocks) and one seeded.
    _counter["n"] += 1
    path = _SCRATCH / f"backfill-{_counter['n']}.db"
    old = database.get_connection(path)
    old.execute(
        "CREATE TABLE employees (id INTEGER PRIMARY KEY, employee_code TEXT NOT NULL"
        " UNIQUE, full_name TEXT NOT NULL, student_type TEXT NOT NULL,"
        " weekly_hour_limit INTEGER NOT NULL DEFAULT 20, is_active INTEGER NOT NULL"
        " DEFAULT 1, seed_key TEXT)"
    )
    old.execute(
        "CREATE TABLE semester_schedules (id INTEGER PRIMARY KEY, employee_id INTEGER"
        " NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, confirmed_at TEXT,"
        " UNIQUE (employee_id, start_date, end_date))"
    )
    old.execute(
        "CREATE TABLE class_blocks (id INTEGER PRIMARY KEY, schedule_id INTEGER NOT NULL,"
        " day_of_week INTEGER NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,"
        " source_note TEXT)"
    )
    old.execute(
        "INSERT INTO employees (employee_code, full_name, student_type) VALUES"
        " ('SW-001', 'Migrated', 'undergraduate')"
    )
    old.execute(
        "INSERT INTO employees (employee_code, full_name, student_type) VALUES"
        " ('SW-002', 'Seeded', 'undergraduate')"
    )
    old.execute(
        "INSERT INTO semester_schedules (employee_id, start_date, end_date, confirmed_at)"
        " VALUES (1, '2026-08-24', '2026-12-11', NULL)"
    )
    old.execute(
        "INSERT INTO semester_schedules (employee_id, start_date, end_date, confirmed_at)"
        " VALUES (2, '2026-08-24', '2026-12-11', '2026-08-24 00:00')"
    )
    old.execute(
        "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time, source_note)"
        " VALUES (1, 0, '09:00', '10:15', ?)",
        (database.LEGACY_MIGRATION_NOTE,),
    )
    # The seeded worker's block carries no note, as seeding writes them.
    old.execute(
        "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time, source_note)"
        " VALUES (2, 1, '11:00', '12:15', NULL)"
    )
    old.commit()

    check(
        "dates_provisional" not in database.table_columns(old, "semester_schedules"),
        "the pre-migration fixture genuinely lacks the new column",
    )

    applied = database.create_schema(old)
    check(
        "semester_schedules.dates_provisional" in applied,
        f"the migration reports itself as applied ({applied})",
    )

    rows = {
        row["employee_id"]: row["dates_provisional"]
        for row in old.execute("SELECT employee_id, dates_provisional FROM semester_schedules")
    }
    check(rows.get(1) == 1, f"the migrated schedule backfills as provisional ({rows.get(1)})")
    check(
        rows.get(2) == 0,
        f"the seeded schedule backfills as NOT provisional ({rows.get(2)})",
    )
    check(
        old.execute("SELECT COUNT(*) AS n FROM class_blocks").fetchone()["n"] == 2,
        "and no class block was lost",
    )

    # Repeatable: running it again changes nothing and re-applies nothing.
    before = snapshot(old)
    applied_again = database.create_schema(old)
    after = snapshot(old)
    check(
        "semester_schedules.dates_provisional" not in applied_again,
        f"a second run does not re-apply the migration ({applied_again})",
    )
    check(
        all(after[table] == before[table] for table in SNAPSHOT_TABLES),
        "and changes nothing at all",
    )
    old.close()


# --------------------------------------------------------------------------
# 14 and 15. Isolation from everything else, and list summaries.
# --------------------------------------------------------------------------
def check_isolation_and_summaries():
    connection = fresh_database()
    add_worker(connection, "SW-001", "Edited Worker")
    other_id = add_worker(connection, "SW-002", "Bystander")

    # Give the bystander a full set of records of every kind.
    connection.execute(
        "INSERT INTO semester_schedules (employee_id, start_date, end_date,"
        " confirmed_at, dates_provisional)"
        " VALUES (?, '2026-08-24', '2026-12-11', '2026-08-24 00:00', 1)",
        (other_id,),
    )
    other_schedule = connection.execute(
        "SELECT id FROM semester_schedules WHERE employee_id = ?", (other_id,)
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time,"
        " source_note) VALUES (?, 0, '09:00', '10:15', NULL)",
        (other_schedule,),
    )
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES ('Vega', '2026-10-05 17:00', '2026-10-05 22:00', 1)"
    )
    shift_id = connection.execute("SELECT id FROM shifts").fetchone()["id"]
    connection.execute(
        "INSERT INTO shift_preferences (employee_id, shift_id, preference)"
        " VALUES (?, ?, 'preferred')",
        (other_id, shift_id),
    )
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime)"
        " VALUES (?, '2026-10-10 08:00', '2026-10-10 14:00')",
        (other_id,),
    )
    connection.commit()

    bystander_before = {
        table: [
            tuple(row)
            for row in connection.execute(f"SELECT * FROM {table}")
            if table not in ("employees",) or row["employee_code"] == "SW-002"
        ]
        for table in ("semester_schedules", "class_blocks", "shift_preferences", "approved_leave", "shifts")
    }
    connection.close()

    # Now do a full round of edits on the OTHER worker.
    schedule_id = main.add_semester(
        "SW-001", {"start_date": "2027-01-11", "end_date": "2027-05-07"}
    )["id"]
    block_id = main.add_class_block(
        "SW-001", schedule_id, {"day_of_week": 1, "start_time": "09:00", "end_time": "10:15"}
    )["id"]
    main.edit_class_block(
        "SW-001", schedule_id, block_id, {"day_of_week": 1, "start_time": "10:00", "end_time": "11:15"}
    )
    main.edit_semester(
        "SW-001", schedule_id, {"start_date": "2026-10-05", "end_date": "2026-10-11"}
    )

    after_connection = database.get_connection()
    bystander_after = {
        table: [
            tuple(row)
            for row in after_connection.execute(f"SELECT * FROM {table}")
            if table not in ("employees",) or row["employee_code"] == "SW-002"
        ]
        for table in ("semester_schedules", "class_blocks", "shift_preferences", "approved_leave", "shifts")
    }
    after_connection.close()

    # Only SW-001's own rows should differ; SW-002's rows are compared by id.
    def only_other(rows, owner_ids):
        return [row for row in rows if row[1] in owner_ids]

    check(
        only_other(bystander_after["semester_schedules"], {other_id})
        == only_other(bystander_before["semester_schedules"], {other_id}),
        "the other worker's semester row is unchanged",
    )
    check(
        bystander_after["shift_preferences"] == bystander_before["shift_preferences"],
        "their shift preferences are unchanged",
    )
    check(
        bystander_after["approved_leave"] == bystander_before["approved_leave"],
        "their approved leave is unchanged",
    )
    check(
        bystander_after["shifts"] == bystander_before["shifts"],
        "the shared shifts are unchanged",
    )
    check(
        main.get_employee_details("SW-002")["semesters"][0]["confirmed_at"]
        == "2026-08-24 00:00",
        "and their confirmation still stands",
    )

    # The list summary follows the edits: the semester now covers the whole
    # reporting week and holds one Tuesday class.
    listing = {row["employee_code"]: row for row in main.list_employees()["employees"]}
    check(
        listing["SW-001"]["class_block_count"] == 1,
        f"the list counts the new class ({listing['SW-001']['class_block_count']})",
    )
    check(
        listing["SW-001"]["weekly_class_hours"] == 1.25,
        f"and its hours ({listing['SW-001']['weekly_class_hours']})",
    )
    check(
        listing["SW-001"]["timetable_status"] == "unconfirmed",
        f"and reports the timetable as unconfirmed ({listing['SW-001']['timetable_status']})",
    )
    check(
        semesters_of("SW-001")[0]["reporting_week_coverage"] == "full",
        "the edited semester now covers the whole reporting week",
    )
    check(
        listing["SW-002"]["class_block_count"] == 1
        and listing["SW-002"]["timetable_status"] == "confirmed",
        "the other worker's summary is untouched",
    )


def run():
    check_schedule_lifecycle()
    check_block_lifecycle()
    check_persistence()
    check_date_validation()
    check_semester_overlap()
    check_block_validation()
    check_block_clashes()
    check_ownership_and_unknowns()
    check_rollback()
    check_confirmation_invalidation()
    check_confirmation()
    check_confirmation_provisional()
    check_provisional_provenance()
    check_migration_backfill()
    check_isolation_and_summaries()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("All timetable-editing checks passed.")


if __name__ == "__main__":
    run()
