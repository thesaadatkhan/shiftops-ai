"""HTTP checks for the employee-details route, over a real socket.

Every other endpoint check in this project calls the endpoint as a plain
Python function, because Starlette's TestClient needs `httpx2` and this
project does not depend on it. That covers data handling and payload shape
but proves nothing about routing, status codes on the wire, JSON
serialization or CORS. This script covers exactly that gap by running a real
uvicorn server in a thread and making real requests to it.

**It never touches the project's database.** `database.DATABASE_PATH` is
redirected to a temporary file before `main` is imported - `main` calls
`ensure_schema()` at import time - and the redirection is asserted rather
than assumed. The server listens on a free port chosen by the operating
system, never the project's 8000, so it cannot collide with a development
server the developer is running.

Checks:

1. A real server starts against a throwaway database.
2. GET /api/employees/{code} returns 200 and JSON over the wire.
3. The body identifies the right worker and survives serialization,
   including fractional class hours and overnight second dates.
4. Provisional dates cross the wire as a flag, with no internal migration
   note text anywhere in the served body.
5. Individual-semester coverage of the displayed week crosses the wire.
6. Assigned hours are the requested worker's own: a second worker's invalid
   shift does not affect them, while that second worker's own page is still
   a controlled 500 and the whole-workforce list still fails on it too.
7. An unknown code is a genuine HTTP 404 in the single-detail shape (D033).
8. The allowed frontend origin receives a CORS header on this route and a
   disallowed origin does not.

Run with:  python verify_details_http.py
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
database.DATABASE_PATH = Path(_TEMPORARY.name) / "http-checks.db"

# Belt and braces: if the redirection above ever stopped working, this stops
# the script rather than letting it start a server on the real database.
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
    """A port the operating system says is free, so never the project's."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def build_fixture():
    """One worker with something stored in every section."""
    connection = database.get_connection()
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active, seed_key)"
        " VALUES ('SW-001', 'Maria Alvarez', 'undergraduate', 20, 1, NULL)"
    )
    employee_id = connection.execute("SELECT id FROM employees").fetchone()["id"]

    # A schedule as the migration writes them: assumed dates, so
    # dates_provisional is set on the SCHEDULE. The note on the block below is
    # internal provenance and is no longer what the flag is read from.
    connection.execute(
        "INSERT INTO semester_schedules (employee_id, start_date, end_date,"
        " confirmed_at, dates_provisional)"
        " VALUES (?, '2026-08-24', '2026-12-11', '2026-08-24 00:00', 1)",
        (employee_id,),
    )
    schedule_id = connection.execute("SELECT id FROM semester_schedules").fetchone()["id"]
    connection.execute(
        "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time,"
        " source_note) VALUES (?, 0, '09:00', '10:15', ?)",
        (schedule_id, database.DEMO_MIGRATION_NOTE),
    )

    # An overnight shift, so the response has to carry two different dates.
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES ('Capella', '2026-10-11 22:00', '2026-10-12 03:00', 1)"
    )
    shift_id = connection.execute("SELECT id FROM shifts").fetchone()["id"]
    connection.execute(
        "INSERT INTO shift_preferences (employee_id, shift_id, preference)"
        " VALUES (?, ?, 'low')",
        (employee_id, shift_id),
    )
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime)"
        " VALUES (?, '2026-10-10 08:00', '2026-10-10 14:00')",
        (employee_id,),
    )

    # A valid assignment for this worker: five whole hours inside the week.
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES ('Vega', '2026-10-05 17:00', '2026-10-05 22:00', 1)"
    )
    good_shift = connection.execute(
        "SELECT id FROM shifts WHERE hall = 'Vega'"
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (employee_id, good_shift),
    )

    # A SECOND worker holding a shift that breaks the whole-hour rule. The
    # first worker's page must be unaffected by it; this worker's own page
    # must still fail in the controlled way.
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active, seed_key)"
        " VALUES ('SW-002', 'Broken Shift Owner', 'masters', 20, 1, NULL)"
    )
    other_id = connection.execute(
        "SELECT id FROM employees WHERE employee_code = 'SW-002'"
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES ('Helix', '2026-10-06 17:00', '2026-10-06 22:30', 1)"
    )
    bad_shift = connection.execute(
        "SELECT id FROM shifts WHERE hall = 'Helix'"
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (other_id, bad_shift),
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

    def get(path, headers=None):
        request = urllib.request.Request(f"{base}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    try:
        status, headers, body = get("/api/employees/SW-001")
        check(status == 200, f"GET /api/employees/SW-001 over HTTP is 200 ({status})")
        check(
            headers.get("content-type", "").startswith("application/json"),
            f"and is served as JSON ({headers.get('content-type')})",
        )

        payload = json.loads(body)
        check(
            payload["employee"]["employee_code"] == "SW-001"
            and payload["employee"]["full_name"] == "Maria Alvarez",
            "the body identifies the worker that was asked for",
        )
        check(
            payload["semesters"][0]["class_blocks"][0]["hours"] == 1.25,
            "fractional class hours survive JSON serialization",
        )
        check(
            payload["semesters"][0]["dates_provisional"] is True,
            "the migrated schedule crosses the wire marked provisional",
        )
        check(
            database.MIGRATION_NOTE_PREFIX not in body.decode("utf-8")
            and "source_note" not in body.decode("utf-8"),
            "and the internal provenance note is nowhere in the served body",
        )
        check(
            payload["semesters"][0]["reporting_week_coverage"] == "full",
            "a semester spanning the whole week reports full coverage "
            f"({payload['semesters'][0]['reporting_week_coverage']})",
        )
        check(
            payload["shift_preferences"][0]["start_datetime"] == "2026-10-11 22:00"
            and payload["shift_preferences"][0]["end_datetime"] == "2026-10-12 03:00",
            "an overnight preference keeps both of its dates",
        )
        check(
            payload["approved_leave"][0]["end_datetime"] == "2026-10-10 14:00",
            "approved leave keeps its actual datetimes",
        )

        check(
            payload["employee"]["assigned_hours"] == 5
            and payload["employee"]["remaining_capacity_hours"] == 15,
            "assigned hours are this worker's own, unaffected by the other "
            f"worker's invalid shift ({payload['employee']['assigned_hours']})",
        )

        # The owner of the invalid shift still gets the controlled error.
        status, _, body = get("/api/employees/SW-002")
        check(
            status == 500,
            f"the owner of the invalid shift still gets a real HTTP 500 ({status})",
        )
        check(
            "Stored shift data is invalid" in json.loads(body)["detail"],
            f"with the existing stored-data message ({body!r})",
        )

        status, _, body = get("/api/employees/SW-404")
        check(status == 404, f"an unknown code is a genuine HTTP 404 ({status})")
        check(
            json.loads(body) == {"detail": "No employee with code SW-404."},
            f"in the one {{'detail': ...}} error shape every route uses ({body!r})",
        )

        _, headers, _ = get(
            "/api/employees/SW-001", headers={"Origin": "http://localhost:5173"}
        )
        check(
            headers.get("access-control-allow-origin") == "http://localhost:5173",
            "the allowed frontend origin receives a CORS header on this route",
        )

        _, headers, _ = get(
            "/api/employees/SW-001", headers={"Origin": "http://evil.example"}
        )
        check(
            "access-control-allow-origin" not in {key.lower() for key in headers},
            "a disallowed origin does not",
        )

        # The list route reports on every worker, so it still validates every
        # assignment and still fails on the invalid one. That behaviour was
        # deliberately not weakened when the details route was narrowed.
        status, _, body = get("/api/employees")
        check(
            status == 500,
            f"the list route still validates the whole workforce ({status})",
        )
        check(
            "Stored shift data is invalid" in json.loads(body)["detail"],
            f"and reports it in the same shape ({body!r})",
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("All employee-details HTTP checks passed.")


if __name__ == "__main__":
    run()
