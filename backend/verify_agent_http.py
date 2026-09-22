"""Real-HTTP checks for Phase 9 increment 1's new contract:

  POST /api/agent/tasks
  POST /api/agent/tasks/{task_id}/messages
  GET  /api/agent/tasks/{task_id}

`verify_agent_tools.py`/`verify_agent_loop.py` cover the underlying
functions directly; this closes the same gap every other `verify_*_http.py`
script closes - real FastAPI routing, JSON serialization and status codes
over a real Uvicorn server on a free port.

**No real API key is used or required.** `main._agent_model_adapter` is
monkeypatched to return a `ScriptedModelAdapter` before every request that
should succeed, so this never contacts the real OpenAI API. One check
explicitly restores the default (real-adapter) factory with
`OPENAI_API_KEY` deliberately unset, to prove the 503
configuration-failure contract works over real HTTP too - and that it
creates zero orphan `agent_tasks`/`agent_messages` rows.

**It never touches the project's database.** `database.DATABASE_PATH` is
redirected to a temporary file before `main` is imported, and the
redirection is asserted rather than assumed. The server listens on a free
port chosen by the operating system, never the project's 8000.

Run with:  python verify_agent_http.py
Exits non-zero if any check fails.
"""

import json
import os
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
database.DATABASE_PATH = Path(_TEMPORARY.name) / "agent-http-checks.db"

if database.DATABASE_PATH.name == "shiftops.db":  # pragma: no cover - defensive
    raise SystemExit("refusing to run against the project database")

import main  # noqa: E402
import uvicorn  # noqa: E402
from agent_model import ModelTurn, ScriptedModelAdapter, ToolCallRequest  # noqa: E402

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def build_fixture():
    """Two active workers, both timetable-ready, one holding a shift."""
    connection = database.get_connection()
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active) VALUES ('SW-010', 'Jordan Kim',"
        " 'undergraduate', 20, 1)"
    )
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active) VALUES ('SW-011', 'Sam Osei',"
        " 'undergraduate', 20, 1)"
    )
    for code in ("SW-010", "SW-011"):
        employee_id = connection.execute(
            "SELECT id FROM employees WHERE employee_code = ?", (code,)
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO semester_schedules"
            " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
            " VALUES (?, '2026-08-24', '2026-12-11', '2026-01-01 00:00', 0)",
            (employee_id,),
        )
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES ('Capella', '2026-10-06 22:00', '2026-10-07 03:00', 1)"
    )
    shift_id = connection.execute(
        "SELECT id FROM shifts WHERE hall = 'Capella'"
    ).fetchone()["id"]
    jordan_id = connection.execute(
        "SELECT id FROM employees WHERE employee_code = 'SW-010'"
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (jordan_id, shift_id),
    )
    connection.commit()
    connection.close()
    return shift_id


def run():
    shift_id = build_fixture()

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
        # ------------------------------------------------ malformed request body
        status, body = request("POST", "/api/agent/tasks", {"not_message": "x"})
        check(status == 400, f"a body without a string 'message' is 400 ({status})")
        detail = json.loads(body).get("detail")
        check(isinstance(detail, str), "with a string detail (D033)")

        # ---------------------------------------------------- create a task
        turns = [
            ModelTurn(tool_calls=[ToolCallRequest(id="c1", name="find_employee", arguments={"query": "Jordan"})]),
            ModelTurn(tool_calls=[ToolCallRequest(id="c2", name="find_shift", arguments={"hall": "Capella", "date": "2026-10-06"})]),
            ModelTurn(tool_calls=[ToolCallRequest(id="c3", name="get_eligible_candidates", arguments={"shift_id": shift_id, "exclude_employee_code": "SW-010"})]),
            ModelTurn(tool_calls=[ToolCallRequest(id="c4", name="propose_replacement", arguments={
                "shift_id": shift_id, "outgoing_employee_code": "SW-010", "incoming_employee_code": "SW-011",
            })]),
            ModelTurn(message="Proposed replacing Jordan Kim with Sam Osei."),
        ]
        main._agent_model_adapter = lambda: ScriptedModelAdapter(turns)

        status, body = request("POST", "/api/agent/tasks", {"message": "Jordan called out for tonight's Capella shift."})
        check(status == 201, f"creating a task over HTTP is 201 ({status})")
        payload = json.loads(body)
        check(payload["result"]["kind"] == "proposal", f"the result kind is 'proposal' ({payload['result']['kind']})")
        check(payload["task"]["status"] == "awaiting_approval", "the task status is awaiting_approval")
        check(len(payload["task"]["proposals"]) == 1, "exactly one proposal is attached")
        task_id = payload["task"]["id"]

        # ------------------------------------------------------- retrieve the task
        status, body = request("GET", f"/api/agent/tasks/{task_id}")
        check(status == 200, f"GET the task over HTTP is 200 ({status})")
        reread = json.loads(body)
        check(reread == payload["task"], "GET returns the exact same task state just created")

        # --------------------------------------------------------- unknown task
        status, body = request("GET", "/api/agent/tasks/999999")
        check(status == 404, f"GET an unknown task_id is 404 ({status})")
        detail = json.loads(body).get("detail")
        check(isinstance(detail, str), "with a string detail")

        status, body = request("POST", "/api/agent/tasks/999999/messages", {"message": "hello"})
        check(status == 404, f"sending a message to an unknown task_id is 404 ({status})")

        # -------------------------------------------------- send another message
        main._agent_model_adapter = lambda: ScriptedModelAdapter(
            [ModelTurn(message="Understood, no further action needed.")]
        )
        status, body = request("POST", f"/api/agent/tasks/{task_id}/messages", {"message": "Thanks, that's all."})
        check(status == 200, f"sending a follow-up message is 200 ({status})")
        payload2 = json.loads(body)
        check(payload2["task"]["id"] == task_id, "the follow-up message continues the SAME task")
        check(
            len([m for m in payload2["task"]["messages"] if m["role"] == "supervisor"]) == 2,
            "the transcript now has both supervisor messages",
        )

        # --------------------------------------------------- configuration failure
        # Restore the real default factory (constructs OpenAIModelAdapter) and
        # ensure no real key is present, so this exercises the actual
        # "AI not configured" path over real HTTP - never a real network call.
        main._agent_model_adapter = lambda: None
        previous_key = os.environ.pop("OPENAI_API_KEY", None)
        try:
            before_conn = database.get_connection()
            tasks_before = before_conn.execute("SELECT COUNT(*) AS n FROM agent_tasks").fetchone()["n"]
            messages_before = before_conn.execute("SELECT COUNT(*) AS n FROM agent_messages").fetchone()["n"]
            before_conn.close()

            status, body = request("POST", "/api/agent/tasks", {"message": "test with no key"})
            check(status == 503, f"a missing OPENAI_API_KEY is a 503 over HTTP ({status})")
            detail = json.loads(body).get("detail")
            check(
                isinstance(detail, str) and "OPENAI_API_KEY" in detail,
                f"with a clear, specific string detail ({detail!r})",
            )

            after_conn = database.get_connection()
            tasks_after = after_conn.execute("SELECT COUNT(*) AS n FROM agent_tasks").fetchone()["n"]
            messages_after = after_conn.execute("SELECT COUNT(*) AS n FROM agent_messages").fetchone()["n"]
            after_conn.close()
            check(
                tasks_after == tasks_before and messages_after == messages_before,
                f"the missing-key request created NO orphan task or message rows "
                f"(tasks {tasks_before}->{tasks_after}, messages {messages_before}->{messages_after})",
            )
        finally:
            if previous_key is not None:
                os.environ["OPENAI_API_KEY"] = previous_key

        # -------------------------------------------------------------- CORS
        req = urllib.request.Request(
            f"{base}/api/agent/tasks/{task_id}",
            method="GET",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            check(
                response.headers.get("access-control-allow-origin") == "http://localhost:5173",
                "the agent routes allow the configured frontend origin",
            )
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    if failures:
        print(f"\n{len(failures)} check(s) failed:")
        for description in failures:
            print(f" - {description}")
        sys.exit(1)
    print("\nAll agent HTTP checks passed.")


if __name__ == "__main__":
    run()
