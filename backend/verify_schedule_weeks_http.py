"""Real-HTTP checks for Phase 7 increment 1's new/changed contracts:

  POST /api/schedule/weeks/{week_start}/prepare
  GET  /api/schedule/weeks/{week_start}
  GET  /api/employees?week_start=...

`verify_schedule_weeks.py` covers the underlying functions directly; this
script closes the same gap `verify_eligibility_http.py` closes for the
Coverage routes - real FastAPI routing, path/query parameter parsing, JSON
serialization and status codes over a real Uvicorn server on a free port.

**It never touches the project's database.** `database.DATABASE_PATH` is
redirected to a temporary file before `main` is imported, and the
redirection is asserted rather than assumed. The server listens on a free
port chosen by the operating system, never the project's 8000.

Run with:  python verify_schedule_weeks_http.py
Exits non-zero if any check fails.
"""

import json
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

if "main" in sys.modules:  # pragma: no cover - defensive
    raise SystemExit(
        "main was imported before the database path was redirected; refusing to run."
    )

import database  # noqa: E402  - imported early on purpose, see the docstring

_TEMPORARY = tempfile.TemporaryDirectory()
database.DATABASE_PATH = Path(_TEMPORARY.name) / "schedule-weeks-http-checks.db"

if database.DATABASE_PATH.name == "shiftops.db":  # pragma: no cover - defensive
    raise SystemExit("refusing to run against the project database")

import main  # noqa: E402
import uvicorn  # noqa: E402

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def database_snapshot():
    connection = database.get_connection()
    try:
        return tuple(connection.iterdump())
    finally:
        connection.close()


def build_fixture():
    """Two active workers, no shifts yet."""
    connection = database.get_connection()
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active) VALUES ('SW-001', 'Maria Alvarez',"
        " 'undergraduate', 20, 1)"
    )
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active) VALUES ('SW-002', 'Jordan Kim',"
        " 'masters', 20, 1)"
    )
    connection.commit()
    connection.close()


def run():
    build_fixture()

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    check(server.started, f"a real uvicorn server started on port {port}")
    if not server.started:  # pragma: no cover - nothing else can be checked
        return

    def request(method, path):
        req = urllib.request.Request(f"{base}{path}", method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    try:
        # ---------------------------------------- reading an unprepared week
        status, headers, body = request("GET", "/api/schedule/weeks/2026-11-02")
        check(status == 200, f"GET an unprepared week is 200 ({status})")
        check(
            headers.get("content-type", "").startswith("application/json"),
            "and is served as JSON",
        )
        payload = json.loads(body)
        check(payload["shifts"] == [], "an unprepared week's shifts list is empty")
        check(
            payload["week_start"] == "2026-11-02" and payload["week_end"] == "2026-11-09",
            f"and echoes the correct week bounds ({payload['week_start']}, {payload['week_end']})",
        )

        before_prepare = database_snapshot()
        check(
            "2026-11-02" not in str(before_prepare),
            "reading the unprepared week inserted nothing referencing that date",
        )

        # -------------------------------------------------------- prepare it
        status, headers, body = request(
            "POST", "/api/schedule/weeks/2026-11-02/prepare"
        )
        check(status == 200, f"POST prepare over HTTP is 200 ({status})")
        payload = json.loads(body)
        check(payload["shifts_required"] == 99, f"prepare reports 99 shifts required ({payload['shifts_required']})")
        check(payload["shifts_inserted"] == 99, f"prepare inserts all 99 on first run ({payload['shifts_inserted']})")
        check(payload["shifts_already_present"] == 0, "nothing was already present")

        # ---------------------------------------------------- idempotent repeat
        status, _, body = request("POST", "/api/schedule/weeks/2026-11-02/prepare")
        check(status == 200, f"repeating prepare over HTTP is still 200 ({status})")
        payload = json.loads(body)
        check(payload["shifts_inserted"] == 0, "the repeat over HTTP inserts zero additional shifts")
        check(payload["shifts_already_present"] == 99, "the repeat reports all 99 as already present")

        # ----------------------------------------------------- read it back
        status, _, body = request("GET", "/api/schedule/weeks/2026-11-02")
        check(status == 200, f"GET the now-prepared week is 200 ({status})")
        payload = json.loads(body)
        check(len(payload["shifts"]) == 99, f"the prepared week now reads back 99 shifts ({len(payload['shifts'])})")
        check(
            all(
                "covered" in shift and "assigned_employees" in shift and "assigned_count" in shift
                for shift in payload["shifts"]
            ),
            "every shift carries covered/assigned_employees/assigned_count fields",
        )
        check(
            "assigned_employee" not in payload["shifts"][0],
            "the old ambiguous singular assigned_employee field is gone",
        )
        check(
            all(
                shift["covered"] is False and shift["assigned_employees"] == [] and shift["assigned_count"] == 0
                for shift in payload["shifts"]
            ),
            "every shift is reported uncovered with an empty assignment list, since no assignment exists",
        )
        starts = [(shift["start_datetime"], shift["hall"], shift["id"]) for shift in payload["shifts"]]
        check(starts == sorted(starts), "shifts are sorted by start_datetime, hall, then id")

        # ------------------------------- multiple assignments on one shift
        # No assignment-creation endpoint exists yet (Phase 7 increments 2+),
        # so this inserts directly, the same way this script's own fixture
        # workers are inserted, to prove the HTTP response actually reflects
        # more than one stored assignment rather than picking an arbitrary
        # one.
        multi_shift_id = payload["shifts"][0]["id"]
        connection = database.get_connection()
        connection.execute(
            "UPDATE shifts SET required_staff = 2 WHERE id = ?", (multi_shift_id,)
        )
        connection.execute(
            "INSERT INTO assignments (employee_id, shift_id)"
            " SELECT id, ? FROM employees WHERE employee_code IN ('SW-002', 'SW-001')",
            (multi_shift_id,),
        )
        connection.commit()
        connection.close()

        status, _, body = request("GET", "/api/schedule/weeks/2026-11-02")
        check(status == 200, f"GET after inserting two assignments is still 200 ({status})")
        payload = json.loads(body)
        multi_shift = next(s for s in payload["shifts"] if s["id"] == multi_shift_id)
        check(
            multi_shift["assigned_count"] == 2 and multi_shift["covered"] is True,
            f"a required_staff=2 shift with two assignments reports count 2 and covered=true ({multi_shift['assigned_count']}, {multi_shift['covered']})",
        )
        check(
            [worker["employee_code"] for worker in multi_shift["assigned_employees"]] == ["SW-001", "SW-002"],
            f"both assignments are listed in deterministic employee-code order, not just one ({multi_shift['assigned_employees']})",
        )

        # ------------------------------------------- the sample week is intact
        status, _, body = request("GET", "/api/schedule/weeks/2026-10-05")
        check(status == 200, f"GET the sample week is still 200 ({status})")
        payload = json.loads(body)
        check(len(payload["shifts"]) == 0, "the sample week has no prepared shifts either, since nothing prepared it in this isolated fixture")

        # ------------------------------------------------- malformed week_start
        for bad_path, what in (
            ("/api/schedule/weeks/2026-13-40/prepare", "prepare with an invalid calendar date"),
            ("/api/schedule/weeks/2026-10-06/prepare", "prepare with a real non-Monday date"),
            ("/api/schedule/weeks/not-a-date/prepare", "prepare with a non-date string"),
        ):
            status, _, body = request("POST", bad_path)
            check(status == 400, f"{what} is 400 ({status})")
            detail = json.loads(body).get("detail")
            check(isinstance(detail, str), f"{what} has a string detail")

        status, _, body = request("GET", "/api/schedule/weeks/2026-10-06")
        check(status == 400, f"GET with a real non-Monday date is 400 ({status})")
        detail = json.loads(body).get("detail")
        check(isinstance(detail, str), "with a string detail")

        # ---------------------------------------- /api/employees, no parameter
        status, _, body = request("GET", "/api/employees")
        check(status == 200, f"GET /api/employees with no parameter is still 200 ({status})")
        payload = json.loads(body)
        check(
            payload["week_start"] == "2026-09-21" and payload["week_end"] == "2026-09-27",
            f"and keeps the default sample week ({payload['week_start']}, {payload['week_end']})",
        )
        default_codes = {row["employee_code"] for row in payload["employees"]}
        check("SW-001" in default_codes, "the fixture worker is listed")

        # ------------------------------------------- /api/employees?week_start=
        status, _, body = request("GET", "/api/employees?week_start=2026-11-02")
        check(status == 200, f"GET /api/employees?week_start=... is 200 ({status})")
        payload = json.loads(body)
        check(
            payload["week_start"] == "2026-11-02" and payload["week_end"] == "2026-11-08",
            f"and reports the requested week's bounds ({payload['week_start']}, {payload['week_end']})",
        )
        other_codes = {row["employee_code"] for row in payload["employees"]}
        check(other_codes == default_codes, "the same worker is listed regardless of the requested week")

        status, _, body = request("GET", "/api/employees?week_start=2026-10-06")
        check(status == 400, f"GET /api/employees with a non-Monday week_start is 400 ({status})")
        detail = json.loads(body).get("detail")
        check(isinstance(detail, str), "with a string detail")

        # -------------------------------------------------------------- CORS
        req = urllib.request.Request(
            f"{base}/api/schedule/weeks/2026-11-02",
            method="GET",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            check(
                response.headers.get("access-control-allow-origin")
                == "http://localhost:5173",
                "the weekly schedule route allows the configured frontend origin",
            )

        req = urllib.request.Request(
            f"{base}/api/schedule/weeks/2026-11-09/prepare",
            method="POST",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            check(
                response.headers.get("access-control-allow-origin")
                == "http://localhost:5173",
                "the prepare route allows the configured frontend origin",
            )

        # ------------------------------------- rejected requests changed nothing
        # A snapshot right before the bad-input checks would have to account
        # for the legitimate 2026-11-02 and 2026-11-09 preparations above, so
        # instead confirm directly that no shift exists for any of the
        # rejected malformed dates.
        final = database_snapshot()
        check(
            "2026-13-40" not in str(final) and "not-a-date" not in str(final),
            "no rejected malformed date was ever written to storage",
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    if failures:
        print(f"\n{len(failures)} check(s) failed:")
        for description in failures:
            print(f" - {description}")
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    run()
