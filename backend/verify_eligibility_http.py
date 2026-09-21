"""HTTP checks for the Coverage API, over a real socket (Phase 6 increment 1).

`verify_eligibility.py` covers the eligibility rules exhaustively by calling
`eligibility.py`'s functions directly. This script closes the same gap
`verify_timetable_editing_http.py` and `verify_details_http.py` close for
their routes: real FastAPI routing, path/query parameter parsing, JSON
serialization and CORS, over a real Uvicorn server on a free port.

Covers:

  GET /api/shifts                                              -> 200, sorted
  GET /api/shifts/{shift_id}/coverage                         -> 200
  GET /api/shifts/{shift_id}/coverage?exclude_employee_code=.. -> 200, excluded
  GET /api/shifts/{unknown_id}/coverage                        -> 404
  GET /api/shifts/{shift_id}/coverage?exclude_employee_code=<nonexistent> -> 400
  CORS on the coverage route
  Persistence: repeated calls make no stored changes.

**It never touches the project's database.** `database.DATABASE_PATH` is
redirected to a temporary file before `main` is imported, and the redirection
is asserted rather than assumed. The server listens on a free port chosen by
the operating system, never the project's 8000.

Run with:  python verify_eligibility_http.py
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
database.DATABASE_PATH = Path(_TEMPORARY.name) / "eligibility-http-checks.db"

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
    """Two workers, both confirmed and clear of conflicts, and one shift."""
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
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES ('Helix', '2026-10-05 08:00', '2026-10-05 12:00', 1)"
    )
    # A second shift, deliberately inserted out of chronological order, so
    # GET /api/shifts's ordering can be checked for real rather than merely
    # matching insertion order by coincidence.
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES ('Andromeda', '2026-10-04 08:00', '2026-10-04 12:00', 1)"
    )
    for code in ("SW-001", "SW-002"):
        employee_id = connection.execute(
            "SELECT id FROM employees WHERE employee_code = ?", (code,)
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO semester_schedules"
            " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
            " VALUES (?, '2026-08-24', '2026-12-11', '2026-01-01 00:00', 0)",
            (employee_id,),
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
        before = database_snapshot()

        # --------------------------------------------------- GET /api/shifts
        status, headers, body = request("GET", "/api/shifts")
        check(status == 200, f"GET /api/shifts over HTTP is 200 ({status})")
        check(
            headers.get("content-type", "").startswith("application/json"),
            "and is served as JSON",
        )
        payload = json.loads(body)
        shift_list = payload["shifts"]
        check(
            len(shift_list) == 2 and {row["hall"] for row in shift_list} == {"Helix", "Andromeda"},
            f"lists every stored shift ({shift_list})",
        )
        check(
            [row["hall"] for row in shift_list] == ["Andromeda", "Helix"],
            f"sorted by start_datetime, so the earlier shift (Andromeda) comes first ({[row['hall'] for row in shift_list]})",
        )
        check(
            all(isinstance(row["id"], int) for row in shift_list),
            "every shift carries a real integer id",
        )

        # ------------------------------------------------------- 200, shape
        status, headers, body = request("GET", "/api/shifts/1/coverage")
        check(status == 200, f"GET coverage over HTTP is 200 ({status})")
        check(
            headers.get("content-type", "").startswith("application/json"),
            "and is served as JSON",
        )
        payload = json.loads(body)
        check(payload["shift"]["id"] == 1, "the shift facts name the requested shift")
        check(
            payload["shift"]["duration_hours"] == 4,
            f"and report the correct duration ({payload['shift']['duration_hours']})",
        )
        codes = {row["employee_code"] for row in payload["results"]}
        check(
            codes == {"SW-001", "SW-002"},
            f"results cover every current worker ({codes})",
        )
        eligible_codes = {row["employee_code"] for row in payload["eligible_candidates"]}
        check(
            eligible_codes == {"SW-001", "SW-002"},
            f"both clear workers are eligible candidates ({eligible_codes})",
        )
        check(
            payload["excluded_employee_code"] is None,
            "no exclusion was requested",
        )

        # --------------------------------------------------- exclude a worker
        status, _, body = request(
            "GET", "/api/shifts/1/coverage?exclude_employee_code=SW-001"
        )
        check(status == 200, f"GET coverage with exclusion is 200 ({status})")
        payload = json.loads(body)
        eligible_codes = {row["employee_code"] for row in payload["eligible_candidates"]}
        result_codes = {row["employee_code"] for row in payload["results"]}
        check(
            "SW-001" not in eligible_codes and "SW-002" in eligible_codes,
            f"the excluded worker is removed from eligible_candidates ({eligible_codes})",
        )
        check(
            "SW-001" in result_codes,
            "but still appears in results",
        )
        check(
            payload["excluded_employee_code"] == "SW-001",
            "the response names who was excluded",
        )

        # ------------------------------------------------------- unknown shift
        status, _, body = request("GET", "/api/shifts/999999/coverage")
        check(status == 404, f"an unknown shift id is 404 ({status})")
        detail = json.loads(body).get("detail")
        check(isinstance(detail, str), "with a string detail")

        # --------------------------------------------------- invalid excluded
        status, _, body = request(
            "GET", "/api/shifts/1/coverage?exclude_employee_code=SW-999"
        )
        check(status == 400, f"a nonexistent excluded worker is 400 ({status})")
        detail = json.loads(body).get("detail")
        check(isinstance(detail, str), "with a string detail")

        # --------------------------------------------------------------- CORS
        req = urllib.request.Request(
            f"{base}/api/shifts/1/coverage",
            method="GET",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            check(
                response.headers.get("access-control-allow-origin")
                == "http://localhost:5173",
                "the coverage route allows the configured frontend origin",
            )

        after = database_snapshot()
        check(before == after, "no request changed any stored data")
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    if failures:
        print(f"\n{len(failures)} check(s) failed:")
        for description in failures:
            print(f" - {description}")
        sys.exit(1)
    print(f"\nAll checks passed.")


if __name__ == "__main__":
    run()
