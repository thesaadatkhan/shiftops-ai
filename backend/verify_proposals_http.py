"""Real-HTTP checks for Phase 7 increment 3's new contracts:

  POST /api/schedule/weeks/{week_start}/proposals
  GET  /api/schedule/proposals/{proposal_id}
  POST /api/schedule/proposals/{proposal_id}/approve
  POST /api/schedule/proposals/{proposal_id}/reject
  POST /api/schedule/assignments/replace

`verify_proposals.py` covers `proposals.py` directly; this script closes the
same gap `verify_optimizer_http.py` and `verify_schedule_weeks_http.py`
close for their own routes - real FastAPI routing, JSON serialization and
status codes over a real Uvicorn server on a free port.

**It never touches the project's database.** `database.DATABASE_PATH` is
redirected to a temporary file before `main` is imported, and the
redirection is asserted rather than assumed. The server listens on a free
port chosen by the operating system, never the project's 8000.

Run with:  python verify_proposals_http.py
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
database.DATABASE_PATH = Path(_TEMPORARY.name) / "proposals-http-checks.db"

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

    def request(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(f"{base}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    try:
        # ---------------------------------------------------- prepare a week
        status, _, _ = request("POST", "/api/schedule/weeks/2026-11-02/prepare")
        check(status == 200, f"preparing the target week is 200 ({status})")

        # ---------------------------------------------- malformed/unprepared
        status, _, body = request("POST", "/api/schedule/weeks/not-a-date/proposals")
        check(status == 400, f"creating a proposal for a malformed week is 400 ({status})")
        check(isinstance(json.loads(body).get("detail"), str), "with a string detail")

        status, _, body = request("POST", "/api/schedule/weeks/2026-11-09/proposals")
        check(status == 409, f"creating a proposal for an unprepared week is 409 ({status})")
        check(isinstance(json.loads(body).get("detail"), str), "with a string detail")

        # ------------------------------------------------------ create (201)
        status, headers, body = request("POST", "/api/schedule/weeks/2026-11-02/proposals")
        check(status == 201, f"creating a proposal over HTTP is 201 ({status})")
        check(headers.get("content-type", "").startswith("application/json"), "and is served as JSON")
        proposal = json.loads(body)
        check(proposal["status"] == "pending", f"the new proposal is pending ({proposal['status']})")
        check(len(proposal["assignments"]) > 0, "the fixture proposed at least one assignment")

        conn = database.get_connection()
        assignment_count = conn.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"]
        conn.close()
        check(assignment_count == 0, "creating a proposal over HTTP writes no row to assignments")

        # ------------------------------------------------------------- read
        status, _, body = request("GET", f"/api/schedule/proposals/{proposal['id']}")
        check(status == 200, f"reading the proposal back is 200 ({status})")
        check(json.loads(body) == proposal, "the read-back proposal is byte-identical to what creation returned")

        status, _, body = request("GET", "/api/schedule/proposals/999999")
        check(status == 404, f"reading an unknown proposal id is 404 ({status})")
        check(isinstance(json.loads(body).get("detail"), str), "with a string detail")

        # -------------------------------------------------- approve mismatch
        status, _, body = request(
            "POST", f"/api/schedule/proposals/{proposal['id']}/approve", body={"assignments": []}
        )
        check(status == 409, f"approving with mismatched content is 409 ({status})")

        # ------------------------------------------------------ approve (ok)
        approval_payload = {
            "assignments": [
                {"shift_id": a["shift_id"], "employee_code": a["employee_code"]}
                for a in proposal["assignments"]
            ]
        }
        status, _, body = request(
            "POST", f"/api/schedule/proposals/{proposal['id']}/approve", body=approval_payload
        )
        check(status == 200, f"a correct approval over HTTP is 200 ({status})")
        approved = json.loads(body)
        check(approved["status"] == "approved", f"the response reports approved ({approved['status']})")

        conn = database.get_connection()
        assignment_count = conn.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"]
        conn.close()
        check(assignment_count == len(proposal["assignments"]), f"exactly the proposed assignments were persisted ({assignment_count})")

        # ---------------------------------------------------- idempotent retry
        before_retry = database_snapshot()
        status, _, body = request(
            "POST", f"/api/schedule/proposals/{proposal['id']}/approve", body=approval_payload
        )
        check(status == 200, f"repeating the approval over HTTP is still 200 ({status})")
        after_retry = database_snapshot()
        check(before_retry == after_retry, "repeating the approval over HTTP writes nothing further")

        # -------------------------------------------------- reject conflict
        status, _, body = request("POST", f"/api/schedule/proposals/{proposal['id']}/reject")
        check(status == 409, f"rejecting an already-approved proposal is 409 ({status})")

        # -------------------------------------------------------- replace
        first_shift_id = proposal["assignments"][0]["shift_id"]
        first_employee_code = proposal["assignments"][0]["employee_code"]
        other_employee_code = "SW-002" if first_employee_code == "SW-001" else "SW-001"

        status, _, body = request(
            "POST",
            "/api/schedule/assignments/replace",
            body={
                "shift_id": first_shift_id,
                "outgoing_employee_code": first_employee_code,
                "incoming_employee_code": other_employee_code,
            },
        )
        # The other worker may already hold an overlapping shift from the
        # same approved proposal, which would correctly refuse the
        # replacement (409) rather than double-book them - either outcome
        # proves the route wired the operation through correctly, so both
        # are accepted, but exactly one must occur.
        check(status in (200, 409), f"replace returns a real outcome, not a crash ({status})")
        if status == 200:
            result = json.loads(body)
            check(
                result["outgoing_employee_code"] == first_employee_code
                and result["incoming_employee_code"] == other_employee_code,
                f"a successful replace names outgoing/incoming correctly ({result})",
            )

        status, _, body = request(
            "POST",
            "/api/schedule/assignments/replace",
            body={"shift_id": 999999, "outgoing_employee_code": "SW-001", "incoming_employee_code": "SW-002"},
        )
        check(status == 404, f"replace on an unknown shift is 404 ({status})")
        check(isinstance(json.loads(body).get("detail"), str), "with a string detail")

        # --------------------------------------------- constraint-safe replace
        check(len(proposal["assignments"]) >= 4, "fixture sanity: enough proposed assignments for two independent constraint checks")
        same_worker_shift = proposal["assignments"][2]["shift_id"]
        same_worker_code = proposal["assignments"][2]["employee_code"]
        status, _, body = request(
            "POST",
            "/api/schedule/assignments/replace",
            body={
                "shift_id": same_worker_shift,
                "outgoing_employee_code": same_worker_code,
                "incoming_employee_code": same_worker_code,
            },
        )
        check(status == 409, f"replacing a worker with themselves is 409 ({status})")
        detail = json.loads(body).get("detail")
        check(isinstance(detail, dict) and "replacement_same_worker" in detail.get("reason_codes", []), f"with a structured conflict naming the reason ({detail})")

        overstaffed_shift = proposal["assignments"][3]["shift_id"]
        overstaffed_code = proposal["assignments"][3]["employee_code"]
        overstaffed_other_code = "SW-002" if overstaffed_code == "SW-001" else "SW-001"
        conn = database.get_connection()
        conn.execute(
            "INSERT INTO assignments (employee_id, shift_id)"
            " SELECT id, ? FROM employees WHERE employee_code = ?",
            (overstaffed_shift, overstaffed_other_code),
        )
        conn.commit()
        conn.close()
        status, _, body = request(
            "POST",
            "/api/schedule/assignments/replace",
            body={
                "shift_id": overstaffed_shift,
                "outgoing_employee_code": overstaffed_code,
                "incoming_employee_code": overstaffed_other_code,
            },
        )
        check(status == 409, f"replacing with a worker who already holds the shift is 409 ({status})")
        detail = json.loads(body).get("detail")
        check(isinstance(detail, dict) and "replacement_already_assigned" in detail.get("reason_codes", []), f"with a structured conflict naming the reason ({detail})")

        # --------------------------------------------------------------- CORS
        req = urllib.request.Request(
            f"{base}/api/schedule/proposals/{proposal['id']}",
            method="GET",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            check(
                response.headers.get("access-control-allow-origin") == "http://localhost:5173",
                "the proposal route allows the configured frontend origin",
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
