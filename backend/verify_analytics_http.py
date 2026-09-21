"""Real-HTTP checks for Phase 8's new contract:

  GET /api/analytics/weeks/{week_start}

`verify_analytics.py` covers the underlying `analytics.py` functions
directly; this closes the same gap the other `verify_*_http.py` scripts
close - real FastAPI routing, JSON serialization and status codes over a
real Uvicorn server on a free port.

**It never touches the project's database.** `database.DATABASE_PATH` is
redirected to a temporary file before `main` is imported, and the
redirection is asserted rather than assumed. The server listens on a free
port chosen by the operating system, never the project's 8000.

Run with:  python verify_analytics_http.py
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
database.DATABASE_PATH = Path(_TEMPORARY.name) / "analytics-http-checks.db"

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
    """One active worker with an accepted timetable, no shifts yet."""
    connection = database.get_connection()
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active) VALUES ('SW-001', 'Maria Alvarez',"
        " 'undergraduate', 20, 1)"
    )
    employee_id = connection.execute(
        "SELECT id FROM employees WHERE employee_code = 'SW-001'"
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
        # ---------------------------------- reading analytics for an unprepared week
        before = database_snapshot()
        status, headers, body = request("GET", "/api/analytics/weeks/2026-11-02")
        check(status == 200, f"GET analytics for an unprepared week is 200 ({status})")
        check(
            headers.get("content-type", "").startswith("application/json"),
            "and is served as JSON",
        )
        payload = json.loads(body)
        check(
            payload["coverage"]["week_start"] == "2026-11-02"
            and payload["coverage"]["week_end"] == "2026-11-08",
            f"coverage echoes the correct inclusive week bounds ({payload['coverage']['week_start']}, {payload['coverage']['week_end']})",
        )
        check(payload["coverage"]["shift_count"] == 0, "an unprepared week reports zero stored shifts")
        check(payload["coverage"]["coverage_percentage"] == 100.0, "zero required hours reports 100.0% coverage")
        check(payload["workforce"]["total_workers"] == 1, "the fixture worker is counted")
        check(payload["workforce"]["active_and_timetable_ready_workers"] == 1, "the fixture worker is active and timetable-ready")
        check(
            payload["workforce"]["workers"][0]["employee_code"] == "SW-001",
            "the per-worker utilization row names the fixture worker",
        )
        after = database_snapshot()
        check(before == after, "reading analytics for an unprepared week wrote nothing at all")

        # ---------------------------------------------------------- prepare, then reread
        status, _, body = request("POST", "/api/schedule/weeks/2026-11-02/prepare")
        check(status == 200, f"preparing the week over HTTP is 200 ({status})")

        status, _, body = request("GET", "/api/analytics/weeks/2026-11-02")
        check(status == 200, f"GET analytics for the now-prepared week is 200 ({status})")
        payload = json.loads(body)
        check(payload["coverage"]["shift_count"] == 99, f"the prepared week reports 99 stored shifts ({payload['coverage']['shift_count']})")
        check(payload["coverage"]["required_positions"] > 0, "the prepared week reports required positions")
        check(payload["coverage"]["filled_positions"] == 0, "nothing has been assigned yet")
        check(payload["coverage"]["coverage_percentage"] == 0.0, "an entirely unstaffed prepared week reports 0% coverage")

        # --------------------------------------------------------- malformed week_start
        for bad_path, what in (
            ("/api/analytics/weeks/2026-13-40", "an invalid calendar date"),
            ("/api/analytics/weeks/2026-10-06", "a real non-Monday date"),
            ("/api/analytics/weeks/not-a-date", "a non-date string"),
        ):
            status, _, body = request("GET", bad_path)
            check(status == 400, f"analytics with {what} is 400 ({status})")
            detail = json.loads(body).get("detail")
            check(isinstance(detail, str), f"{what} has a string detail (D033)")

        # -------------------------------------------------------------------- CORS
        req = urllib.request.Request(
            f"{base}/api/analytics/weeks/2026-11-02",
            method="GET",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            check(
                response.headers.get("access-control-allow-origin") == "http://localhost:5173",
                "the analytics route allows the configured frontend origin",
            )

        # ------------------------------------------------ rejected requests changed nothing
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
    print("\nAll analytics HTTP checks passed.")


if __name__ == "__main__":
    run()
