"""Real-HTTP checks for POST /api/schedule/assignments.

The database path is redirected before importing main. The server uses an
OS-selected port, never 8000. Run with: python verify_manual_assignment_http.py
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

if "main" in sys.modules:
    raise SystemExit("main was imported before database redirection")

import database

_temporary = tempfile.TemporaryDirectory()
database.DATABASE_PATH = Path(_temporary.name) / "manual-assignment-http.sqlite"
if database.DATABASE_PATH.name == "shiftops.db":
    raise SystemExit("refusing to use the managed database")

import main
import uvicorn

failures = []
checks = 0


def check(condition, description):
    global checks
    checks += 1
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def build_fixture():
    connection = database.get_connection()
    for code in ("SW-001", "SW-002"):
        connection.execute(
            "INSERT INTO employees (employee_code, full_name, student_type)"
            " VALUES (?, ?, 'undergraduate')", (code, f"Worker {code}"),
        )
        employee_id = connection.execute(
            "SELECT id FROM employees WHERE employee_code = ?", (code,)
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO semester_schedules"
            " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
            " VALUES (?, '2026-09-21', '2026-10-04', '2026-09-01 09:00', 0)",
            (employee_id,),
        )
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES ('Vega', '2026-09-21 17:00', '2026-09-21 22:00', 1)"
    )
    connection.commit()
    shift_id = connection.execute("SELECT id FROM shifts").fetchone()["id"]
    connection.close()
    return shift_id


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main_test():
    shift_id = build_fixture()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    check(server.started, "isolated Uvicorn server starts")

    def request(method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request_headers = {"Content-Type": "application/json"} if data is not None else {}
        request_headers.update(headers or {})
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=request_headers
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                raw = response.read()
                try:
                    body = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    body = raw.decode()
                return response.status, dict(response.headers), body
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), json.loads(error.read())

    try:
        status, _, body = request("POST", "/api/schedule/assignments", [])
        check(status == 400 and isinstance(body.get("detail"), str),
              "non-object payload is 400 with string detail")
        status, _, body = request("POST", "/api/schedule/assignments", {"shift_id": shift_id})
        check(status == 400 and isinstance(body.get("detail"), str),
              "missing field is 400 with string detail")
        status, _, body = request("POST", "/api/schedule/assignments", {
            "shift_id": 999999, "employee_code": "SW-001"
        })
        check(status == 404 and isinstance(body.get("detail"), str),
              "unknown shift is 404")
        status, _, body = request("POST", "/api/schedule/assignments", {
            "shift_id": shift_id, "employee_code": "SW-001"
        })
        check(status == 201 and body["incoming_employee_code"] == "SW-001",
              "eligible fill is 201 with the saved worker")
        status, _, body = request("POST", "/api/schedule/assignments", {
            "shift_id": shift_id, "employee_code": "SW-002"
        })
        check(status == 409 and body["detail"]["reason_codes"] == ["shift_already_covered"],
              "covered shift is a structured 409")
        status, headers, _ = request("OPTIONS", "/api/schedule/assignments", headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "POST",
        })
        check(status == 200 and headers.get("access-control-allow-origin") == "http://127.0.0.1:5173",
              "manual assignment route supports the configured CORS origin")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        check(not thread.is_alive(), "isolated server stops cleanly")
        _temporary.cleanup()

    if failures:
        print(f"\nFAILED ({len(failures)} of {checks})")
        return 1
    print(f"\nAll {checks} manual-assignment HTTP checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main_test())
