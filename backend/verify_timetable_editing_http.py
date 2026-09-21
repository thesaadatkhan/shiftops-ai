"""HTTP checks for the six semester/class-editing routes, over a real socket.

`verify_timetable_editing.py` covers exhaustive domain validation - every
date rule, every overlap case, every ownership check - by calling the
endpoint functions directly. That proves the exception-to-status-code mapping
and the stored result, but nothing about actual FastAPI routing, path
parameter parsing, JSON serialization over the wire, or CORS. This script
closes exactly that gap for the mutation routes, the same way
`verify_details_http.py` does for the read-only details route: a real Uvicorn
server in a thread, a real socket, real requests.

This is deliberately NOT exhaustive - the domain rules are `.py` file's job.
Each status code the routes can produce gets one representative case here,
enough to prove the transport contract holds for every route:

  POST   /api/employees/{code}/semesters                        -> 201
  PUT    /api/employees/{code}/semesters/{id}                    -> 200
  DELETE /api/employees/{code}/semesters/{id}                    -> 200
  POST   /api/employees/{code}/semesters/{id}/blocks             -> 201
  PUT    /api/employees/{code}/semesters/{id}/blocks/{id}        -> 200
  DELETE /api/employees/{code}/semesters/{id}/blocks/{id}        -> 200

plus one representative 400 (validation), 404 (unknown id and cross-worker
ownership), 409 (overlap/duplicate), and CORS on a mutation route.
All four body-taking routes also reject non-object JSON and missing bodies
with the domain's 400/string-detail error, without changing stored records.

**It never touches the project's database.** `database.DATABASE_PATH` is
redirected to a temporary file before `main` is imported, and the redirection
is asserted rather than assumed. The server listens on a free port chosen by
the operating system, never the project's 8000.

Run with:  python verify_timetable_editing_http.py
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
database.DATABASE_PATH = Path(_TEMPORARY.name) / "editing-http-checks.db"

# Belt and braces: if the redirection above ever stopped working, this stops
# the script rather than letting it start a server on the real database.
if database.DATABASE_PATH.name == "shiftops.db":  # pragma: no cover - defensive
    raise SystemExit("refusing to run against the project database")

import main  # noqa: E402
import uvicorn  # noqa: E402

failures = []
NO_BODY = object()


def database_snapshot():
    """Schema and all rows in this verifier's disposable database."""
    connection = database.get_connection()
    try:
        return tuple(connection.iterdump())
    finally:
        connection.close()


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
    """Two workers, so ownership can be tested for real over the wire."""
    connection = database.get_connection()
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active, seed_key)"
        " VALUES ('SW-001', 'Maria Alvarez', 'undergraduate', 20, 1, NULL)"
    )
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active, seed_key)"
        " VALUES ('SW-002', 'Jordan Kim', 'masters', 20, 1, NULL)"
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

    def request(method, path, body=NO_BODY, headers=None):
        # An explicit None must travel as JSON null, separately from no body.
        data = None if body is NO_BODY else json.dumps(body).encode("utf-8")
        all_headers = dict(headers or {})
        if data is not None:
            all_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{base}{path}", data=data, headers=all_headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    try:
        # ---------------------------------------------------- POST semester
        status, headers, body = request(
            "POST",
            "/api/employees/SW-001/semesters",
            {"start_date": "2026-08-24", "end_date": "2026-12-11"},
        )
        check(status == 201, f"POST a semester over HTTP is 201 ({status})")
        check(
            headers.get("content-type", "").startswith("application/json"),
            "and is served as JSON",
        )
        payload = json.loads(body)
        check(
            payload["start_date"] == "2026-08-24" and payload["end_date"] == "2026-12-11",
            f"with the submitted dates round-tripped through JSON ({payload})",
        )
        schedule_id = payload["id"]
        check(isinstance(schedule_id, int), "and a real integer schedule id")

        # ----------------------------------------------------- PUT semester
        status, _, body = request(
            "PUT",
            f"/api/employees/SW-001/semesters/{schedule_id}",
            {"start_date": "2026-09-01", "end_date": "2026-12-18"},
        )
        check(status == 200, f"PUT a semester over HTTP is 200 ({status})")
        payload = json.loads(body)
        check(
            payload["start_date"] == "2026-09-01" and payload["end_date"] == "2026-12-18",
            f"and returns the updated dates ({payload})",
        )

        # ------------------------------------------------------- POST block
        status, headers, body = request(
            "POST",
            f"/api/employees/SW-001/semesters/{schedule_id}/blocks",
            {"day_of_week": 0, "start_time": "09:00", "end_time": "10:15"},
        )
        check(status == 201, f"POST a class block over HTTP is 201 ({status})")
        payload = json.loads(body)
        check(
            payload["day_of_week"] == 0
            and payload["start_time"] == "09:00"
            and payload["end_time"] == "10:15",
            f"with the submitted weekday and times round-tripped ({payload})",
        )
        block_id = payload["id"]
        check(isinstance(block_id, int), "and a real integer block id")

        # -------------------------------------------------------- PUT block
        status, _, body = request(
            "PUT",
            f"/api/employees/SW-001/semesters/{schedule_id}/blocks/{block_id}",
            {"day_of_week": 2, "start_time": "13:00", "end_time": "14:15"},
        )
        check(status == 200, f"PUT a class block over HTTP is 200 ({status})")
        payload = json.loads(body)
        check(
            payload["day_of_week"] == 2 and payload["start_time"] == "13:00",
            f"and returns the updated weekday and times ({payload})",
        )

        # Confirm through the real details route that the writes actually
        # landed, not just that the mutation endpoints echoed something.
        status, _, body = request("GET", "/api/employees/SW-001")
        detail = json.loads(body)
        check(
            detail["semesters"][0]["start_date"] == "2026-09-01"
            and detail["semesters"][0]["class_blocks"][0]["day_of_week"] == 2,
            "the details route reflects both writes over its own request",
        )

        # An invalid outer JSON shape must reach the domain validator on
        # every body-taking route. Valid object requests above are controls.
        # Include key/value pairs so arrays cannot be coerced into objects.
        semester_path = f"/api/employees/SW-001/semesters/{schedule_id}"
        block_path = f"{semester_path}/blocks"
        routes = [
            ("POST", "/api/employees/SW-001/semesters", "semester dates"),
            ("PUT", semester_path, "semester dates"),
            ("POST", block_path, "a weekday and start and end times"),
            ("PUT", f"{block_path}/{block_id}", "a weekday and start and end times"),
        ]
        invalid_bodies = [
            ("empty array", []),
            ("key/value array", [["start_date", "2026-09-01"], ["end_date", "2026-12-18"]]),
            ("string", "not an object"),
            ("integer zero", 0),
            ("fractional number", 1.5),
            ("true", True),
            ("false", False),
            ("JSON null", None),
            ("missing body", NO_BODY),
        ]
        # Make confirmation/provenance preservation observable on rejection.
        connection = database.get_connection()
        connection.execute(
            "UPDATE semester_schedules SET confirmed_at = ?, dates_provisional = 1"
            " WHERE id = ?", ("2026-09-01 00:00", schedule_id),
        )
        connection.commit()
        connection.close()
        before_invalid = database_snapshot()
        for method, path, fields in routes:
            for label, invalid_body in invalid_bodies:
                status, _, body = request(method, path, invalid_body)
                check(
                    status == 400
                    and json.loads(body) == {"detail": f"Expected an object with {fields}."},
                    f"{method} {path}: {label} returns the domain 400/string-detail error",
                )
                check(
                    database_snapshot() == before_invalid,
                    f"{method} {path}: {label} preserves all rows, including confirmation and provenance",
                )

        # ------------------------------------------------ 400 (validation)
        status, _, body = request(
            "POST",
            "/api/employees/SW-001/semesters",
            {"start_date": "2026-12-11", "end_date": "2026-08-24"},
        )
        check(status == 400, f"a reversed date range is a real HTTP 400 ({status})")
        check(
            "detail" in json.loads(body),
            "in the single-detail error shape every route uses (D033)",
        )

        # ------------------------------------------------ 409 (overlap)
        status, _, body = request(
            "POST",
            "/api/employees/SW-001/semesters",
            {"start_date": "2026-09-01", "end_date": "2026-12-18"},
        )
        check(status == 409, f"an overlapping semester is a real HTTP 409 ({status})")
        check(
            "overlaps" in json.loads(body)["detail"],
            "with the backend's overlap explanation",
        )

        # 409 duplicate class, same shape check for the block routes.
        status, _, body = request(
            "POST",
            f"/api/employees/SW-001/semesters/{schedule_id}/blocks",
            {"day_of_week": 2, "start_time": "13:00", "end_time": "14:15"},
        )
        check(status == 409, f"a duplicate class is a real HTTP 409 ({status})")

        # ------------------------------------------------ 404 (unknown id)
        status, _, body = request(
            "PUT",
            "/api/employees/SW-001/semesters/999999",
            {"start_date": "2027-01-01", "end_date": "2027-02-01"},
        )
        check(status == 404, f"an unknown schedule id is a real HTTP 404 ({status})")
        check(
            json.loads(body) == {"detail": "No semester schedule 999999 for this worker."},
            f"naming the id that was not found ({body!r})",
        )

        # ------------------------------------------ 404 (cross-worker owner)
        status, _, body = request(
            "DELETE", f"/api/employees/SW-002/semesters/{schedule_id}"
        )
        check(
            status == 404,
            f"addressing another worker's real schedule id is a real HTTP 404 ({status})",
        )

        # -------------------------------------------------------- CORS
        _, headers, _ = request(
            "POST",
            "/api/employees/SW-002/semesters",
            {"start_date": "2027-01-11", "end_date": "2027-05-07"},
            headers={"Origin": "http://localhost:5173"},
        )
        check(
            headers.get("access-control-allow-origin") == "http://localhost:5173",
            "the allowed frontend origin receives a CORS header on a mutation route",
        )

        _, headers, _ = request(
            "POST",
            "/api/employees/SW-002/semesters",
            {"start_date": "2027-06-01", "end_date": "2027-08-01"},
            headers={"Origin": "http://evil.example"},
        )
        check(
            "access-control-allow-origin" not in {key.lower() for key in headers},
            "a disallowed origin does not",
        )

        # -------------------------------------------------- DELETE block
        status, _, body = request(
            "DELETE",
            f"/api/employees/SW-001/semesters/{schedule_id}/blocks/{block_id}",
        )
        check(status == 200, f"DELETE a class block over HTTP is 200 ({status})")
        payload = json.loads(body)
        check(
            payload["day_of_week"] == 2 and payload["start_time"] == "13:00",
            f"and reports which class was removed ({payload})",
        )

        # ----------------------------------------------- DELETE semester
        status, _, body = request(
            "DELETE", f"/api/employees/SW-001/semesters/{schedule_id}"
        )
        check(status == 200, f"DELETE a semester over HTTP is 200 ({status})")
        payload = json.loads(body)
        check(
            payload["start_date"] == "2026-09-01" and payload["class_blocks"] == 0,
            f"and reports its dates and how many classes went with it ({payload})",
        )

        status, _, body = request("GET", "/api/employees/SW-001")
        detail = json.loads(body)
        check(
            detail["semesters"] == [],
            "the details route confirms the semester is really gone",
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
    print("All timetable-editing HTTP checks passed.")


if __name__ == "__main__":
    run()
