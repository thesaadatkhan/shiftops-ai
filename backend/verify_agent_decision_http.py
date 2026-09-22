"""Real-HTTP checks for Phase 9 increment 2's new contract:

  POST /api/agent/proposals/{proposal_id}/approve
  POST /api/agent/proposals/{proposal_id}/reject

`verify_agent_decision.py` covers the underlying `agent_service` functions
directly; this closes the same gap every other `verify_*_http.py` script
closes - real FastAPI routing, JSON serialization and status codes over a
real Uvicorn server on a free port.

**No real API key is used, and no network request to any AI provider is
ever made** - these two routes never call a model at all (see
`agent_service.approve_agent_proposal`/`reject_agent_proposal`'s own
docstrings: authorization is the HTTP call itself, nothing else).

**It never touches the project's database.** `database.DATABASE_PATH` is
redirected to a temporary file before `main` is imported, and the
redirection is asserted rather than assumed. The server listens on a free
port chosen by the operating system, never the project's 8000.

Run with:  python verify_agent_decision_http.py
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
database.DATABASE_PATH = Path(_TEMPORARY.name) / "agent-decision-http-checks.db"

if database.DATABASE_PATH.name == "shiftops.db":  # pragma: no cover - defensive
    raise SystemExit("refusing to run against the project database")

import main  # noqa: E402
import uvicorn  # noqa: E402
import agent_tools  # noqa: E402

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def add_employee(connection, code, name, limit=20, active=True):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active) VALUES (?, ?, 'undergraduate', ?, ?)",
        (code, name, limit, 1 if active else 0),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]


def add_ready_schedule(connection, employee_id):
    connection.execute(
        "INSERT INTO semester_schedules"
        " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
        " VALUES (?, '2026-08-24', '2026-12-11', '2026-01-01 00:00', 0)",
        (employee_id,),
    )
    connection.commit()


def add_shift(connection, hall, start, end, required_staff=1):
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES (?, ?, ?, ?)",
        (hall, start, end, required_staff),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ?", (hall, start)
    ).fetchone()["id"]


def add_assignment(connection, employee_id, shift_id):
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (employee_id, shift_id),
    )
    connection.commit()


def add_task(connection):
    connection.execute(
        "INSERT INTO agent_tasks (created_at, updated_at, status, request_text)"
        " VALUES ('2026-01-01 00:00', '2026-01-01 00:00', 'awaiting_approval', 'test')"
    )
    connection.commit()
    return connection.execute("SELECT id FROM agent_tasks ORDER BY id DESC").fetchone()["id"]


_shift_counter = {"day": 6}


def build_pending_proposal(connection, outgoing_code="SW-010", incoming_code="SW-011"):
    """A fresh outgoing/incoming pair, a held shift, and a pending
    replacement proposal for it. Returns (proposal_id, shift_id, payload).

    Each call uses a different day of the week so the shifts table's own
    (hall, start_datetime, end_datetime) uniqueness never collides across
    the several proposals this file builds.
    """
    # Deactivate every worker from an earlier call in this same file - an
    # earlier fixture's now-replaced/free outgoing worker would otherwise
    # remain a live, ranked-ahead-alphabetically candidate for this NEW
    # shift too, which is irrelevant to what this specific check tests.
    connection.execute("UPDATE employees SET is_active = 0")
    connection.commit()

    outgoing_id = add_employee(connection, outgoing_code, "Outgoing Worker")
    add_ready_schedule(connection, outgoing_id)
    incoming_id = add_employee(connection, incoming_code, "Incoming Worker")
    add_ready_schedule(connection, incoming_id)
    day = _shift_counter["day"]
    _shift_counter["day"] += 1
    shift_id = add_shift(connection, "Capella", f"2026-10-{day:02d} 22:00", f"2026-10-{day + 1:02d} 03:00")
    add_assignment(connection, outgoing_id, shift_id)
    task_id = add_task(connection)
    proposal = agent_tools.call_tool(
        connection, "propose_replacement",
        {"shift_id": shift_id, "outgoing_employee_code": outgoing_code, "incoming_employee_code": incoming_code},
        task_id=task_id,
    )
    payload = {"shift_id": shift_id, "outgoing_employee_code": outgoing_code, "incoming_employee_code": incoming_code}
    return proposal["id"], shift_id, payload


def run():
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
        req = urllib.request.Request(
            f"{base}{path}",
            method=method,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    try:
        connection = database.get_connection()

        # -------------------------------------------------- unknown proposal
        status, body = request("POST", "/api/agent/proposals/999999/approve", {"shift_id": 1, "incoming_employee_code": "SW-001"})
        check(status == 404, f"approving an unknown proposal_id is 404 ({status})")
        detail = json.loads(body).get("detail")
        check(isinstance(detail, str), "with a string detail (D033)")

        status, body = request("POST", "/api/agent/proposals/999999/reject")
        check(status == 404, f"rejecting an unknown proposal_id is 404 ({status})")

        # ------------------------------------------------- malformed payload
        proposal_id, shift_id, payload = build_pending_proposal(connection, "SW-010", "SW-011")
        status, body = request("POST", f"/api/agent/proposals/{proposal_id}/approve", {"shift_id": shift_id})
        check(status == 400, f"a payload missing incoming_employee_code is 400 ({status})")
        detail = json.loads(body).get("detail")
        check(isinstance(detail, str), "with a string detail")

        status, body = request(
            "POST", f"/api/agent/proposals/{proposal_id}/approve",
            dict(payload, extra_field="unexpected"),
        )
        check(status == 400, f"a payload with an unexpected extra field is 400 ({status})")

        # --------------------------------------------------- content mismatch
        status, body = request(
            "POST", f"/api/agent/proposals/{proposal_id}/approve",
            dict(payload, incoming_employee_code="SW-999"),
        )
        check(status == 409, f"an approval content mismatch is 409 ({status})")
        detail = json.loads(body).get("detail")
        check(isinstance(detail, str), "with a string detail")

        # ------------------------------------------------ successful approval
        status, body = request("POST", f"/api/agent/proposals/{proposal_id}/approve", payload)
        check(status == 200, f"a successful approval is 200 ({status})")
        approved = json.loads(body)
        check(approved["proposal"]["status"] == "approved", "the response shows the proposal approved")
        check(approved["task_status"] == "closed", "the response shows the task closed")
        check(approved["proposal"]["execution_outcome"] == "applied", "execution_outcome is 'applied'")
        check(approved["proposal"]["verification_outcome"] == "verified", "verification_outcome is 'verified'")
        check(approved["readback"] is not None, "a current readback is included on success")

        # -------------------------------------------- read-only decision-state GET
        status, body = request("GET", "/api/agent/proposals/999999")
        check(status == 404, f"GET on an unknown proposal_id is 404 ({status})")

        status, body = request("GET", f"/api/agent/proposals/{proposal_id}")
        check(status == 200, f"GET on a decided proposal is 200 ({status})")
        read_only = json.loads(body)
        check(read_only == approved, "GET reproduces the exact same decision-state response as the approval itself")

        # ----------------------------------------------------- idempotent retry
        status, body = request("POST", f"/api/agent/proposals/{proposal_id}/approve", payload)
        check(status == 200, f"repeating the same successful approval is still 200 ({status})")
        retried = json.loads(body)
        check(retried["proposal"]["decided_at"] == approved["proposal"]["decided_at"], "the retry does not change the decision timestamp")

        # -------------------------------------- approved cannot later be rejected
        status, body = request("POST", f"/api/agent/proposals/{proposal_id}/reject")
        check(status == 409, f"rejecting an already-approved proposal is 409 ({status})")

        # --------------------------------------------------- stale-state conflict
        proposal_id2, shift_id2, payload2 = build_pending_proposal(connection, "SW-020", "SW-021")
        connection.execute("UPDATE employees SET is_active = 0 WHERE employee_code = 'SW-021'")
        connection.commit()
        status, body = request("POST", f"/api/agent/proposals/{proposal_id2}/approve", payload2)
        check(status == 409, f"a stale-eligibility conflict at approval time is 409 ({status})")
        detail = json.loads(body).get("detail")
        check(
            isinstance(detail, dict) and isinstance(detail.get("conflicts"), list) and len(detail["conflicts"]) > 0,
            f"with a structured conflicts list, not a single generic string ({detail})",
        )
        check(
            "worker_inactive" in detail["conflicts"][0].get("reason_codes", []),
            f"the conflict names the real, specific reason code ({detail['conflicts'][0]})",
        )

        # ----------------------------------------------------- successful rejection
        proposal_id3, shift_id3, payload3 = build_pending_proposal(connection, "SW-030", "SW-031")
        status, body = request("POST", f"/api/agent/proposals/{proposal_id3}/reject")
        check(status == 200, f"a successful rejection is 200 ({status})")
        rejected = json.loads(body)
        check(rejected["proposal"]["status"] == "rejected", "the response shows the proposal rejected")
        check(rejected["task_status"] == "closed", "the response shows the task closed")

        status, body = request("POST", f"/api/agent/proposals/{proposal_id3}/approve", payload3)
        check(status == 409, f"approving an already-rejected proposal is 409 ({status})")

        # -------------------------------------------------------------- CORS
        req = urllib.request.Request(
            f"{base}/api/agent/proposals/{proposal_id3}/reject",
            method="POST",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            check(
                response.headers.get("access-control-allow-origin") == "http://localhost:5173",
                "the decision routes allow the configured frontend origin",
            )

        connection.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    if failures:
        print(f"\n{len(failures)} check(s) failed:")
        for description in failures:
            print(f" - {description}")
        sys.exit(1)
    print("\nAll agent decision HTTP checks passed.")


if __name__ == "__main__":
    run()
