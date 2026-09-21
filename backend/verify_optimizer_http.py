"""Real-HTTP checks for Phase 7 increment 2's new contract:

  POST /api/schedule/weeks/{week_start}/draft

`verify_optimizer.py` covers `optimizer.generate_draft` directly; this
script closes the same gap `verify_eligibility_http.py` and
`verify_schedule_weeks_http.py` close for their own routes - real FastAPI
routing, path parameter parsing, JSON serialization and status codes over a
real Uvicorn server on a free port.

**It never touches the project's database.** `database.DATABASE_PATH` is
redirected to a temporary file before `main` is imported, and the
redirection is asserted rather than assumed. The server listens on a free
port chosen by the operating system, never the project's 8000.

Run with:  python verify_optimizer_http.py
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
from unittest.mock import patch

if "main" in sys.modules:  # pragma: no cover - defensive
    raise SystemExit(
        "main was imported before the database path was redirected; refusing to run."
    )

import database  # noqa: E402  - imported early on purpose, see the docstring

_TEMPORARY = tempfile.TemporaryDirectory()
database.DATABASE_PATH = Path(_TEMPORARY.name) / "optimizer-http-checks.db"

if database.DATABASE_PATH.name == "shiftops.db":  # pragma: no cover - defensive
    raise SystemExit("refusing to run against the project database")

import main  # noqa: E402
import uvicorn  # noqa: E402
from ortools.sat.python import cp_model  # noqa: E402

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
    """Two active workers, both confirmed and non-provisional for the whole
    target week, no classes, no leave - both hard-eligible for anything."""
    connection = database.get_connection()
    for code, name in (("SW-001", "Maria Alvarez"), ("SW-002", "Jordan Kim")):
        connection.execute(
            "INSERT INTO employees (employee_code, full_name, student_type,"
            " weekly_hour_limit, is_active) VALUES (?, ?, 'undergraduate', 20, 1)",
            (code, name),
        )
        employee_id = connection.execute(
            "SELECT id FROM employees WHERE employee_code = ?", (code,)
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO semester_schedules"
            " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
            " VALUES (?, '2026-11-02', '2026-11-08', '2026-01-01 00:00', 0)",
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
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    try:
        # -------------------------------------------- unprepared week is 409
        status, headers, body = request("POST", "/api/schedule/weeks/2026-11-02/draft")
        check(status == 409, f"drafting an unprepared week is 409 ({status})")
        detail = json.loads(body).get("detail")
        check(isinstance(detail, str), "with a string detail")

        before_prepare = database_snapshot()

        # --------------------------------------------------------- 400s
        for bad_path, what in (
            ("/api/schedule/weeks/not-a-date/draft", "a non-date string"),
            ("/api/schedule/weeks/2026-11-03/draft", "a real non-Monday date"),
            ("/api/schedule/weeks/2026-13-40/draft", "an invalid calendar date"),
        ):
            status, _, body = request("POST", bad_path)
            check(status == 400, f"drafting with {what} is 400 ({status})")
            detail = json.loads(body).get("detail")
            check(isinstance(detail, str), f"and {what} has a string detail")

        after_bad_requests = database_snapshot()
        check(
            before_prepare == after_bad_requests,
            "the unprepared-week and malformed-input requests wrote nothing",
        )

        # ------------------------------------------------- prepare the week
        status, _, body = request("POST", "/api/schedule/weeks/2026-11-02/prepare")
        check(status == 200, f"preparing the target week is 200 ({status})")

        # ----------------------------------------------------- draft it
        status, headers, body = request("POST", "/api/schedule/weeks/2026-11-02/draft")
        check(status == 200, f"drafting a prepared week over HTTP is 200 ({status})")
        check(
            headers.get("content-type", "").startswith("application/json"),
            "and is served as JSON",
        )
        payload = json.loads(body)
        check(
            payload["week_start"] == "2026-11-02" and payload["week_end"] == "2026-11-09",
            f"the draft echoes the correct week bounds ({payload['week_start']}, {payload['week_end']})",
        )
        check(payload["status"] in ("complete", "partial"), f"status is one of the documented values ({payload['status']})")
        check(len(payload["shifts"]) == 99, f"every one of the week's 99 shifts is represented ({len(payload['shifts'])})")
        check(
            all(
                "existing_assignments" in shift and "proposed_assignments" in shift
                and "filled_count" in shift and "covered" in shift and "uncovered_positions" in shift
                for shift in payload["shifts"]
            ),
            "every shift carries the documented structured fields",
        )
        summary = payload["summary"]
        check(
            all(
                key in summary
                for key in (
                    "required_positions", "existing_filled_positions",
                    "proposed_filled_positions", "total_filled_positions", "uncovered_positions",
                )
            ),
            f"the summary carries every documented count ({summary})",
        )
        check(
            summary["required_positions"] == 99
            and summary["total_filled_positions"] == summary["existing_filled_positions"] + summary["proposed_filled_positions"],
            f"the summary's counts are internally consistent ({summary})",
        )

        # ------------------------------------------- draft again is unchanged
        status, _, second_body = request("POST", "/api/schedule/weeks/2026-11-02/draft")
        check(status == 200, f"drafting the same unchanged week again is still 200 ({status})")
        check(json.loads(second_body) == payload, "repeating the draft over HTTP returns an identical result")

        # ------------------------------------------- draft never wrote anything
        after_draft = database_snapshot()
        # A prepare call happened between the two snapshots, on purpose - so
        # compare drafting alone by re-snapshotting immediately around it.
        before_draft_only = database_snapshot()
        request("POST", "/api/schedule/weeks/2026-11-02/draft")
        after_draft_only = database_snapshot()
        check(before_draft_only == after_draft_only, "drafting over HTTP writes nothing to storage")

        # ---------------------------------------- non-optimal status is 503
        before_non_optimal = database_snapshot()
        with patch.object(cp_model.CpSolver, "Solve", return_value=cp_model.UNKNOWN):
            status, _, body = request("POST", "/api/schedule/weeks/2026-11-02/draft")
        check(status == 503, f"a non-OPTIMAL solver status over HTTP is 503, not an ordinary draft ({status})")
        detail = json.loads(body).get("detail")
        check(isinstance(detail, str), "with a string detail")
        after_non_optimal = database_snapshot()
        check(before_non_optimal == after_non_optimal, "the 503 path writes nothing to storage either")

        # --------------------------------------------------------------- CORS
        req = urllib.request.Request(
            f"{base}/api/schedule/weeks/2026-11-02/draft",
            method="POST",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            check(
                response.headers.get("access-control-allow-origin")
                == "http://localhost:5173",
                "the draft route allows the configured frontend origin",
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
