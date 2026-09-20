"""Regression checks for the employee endpoints' request/response contract.

**Read this before running it.** `main.py` calls `ensure_schema()` at import
time, against whatever `database.DATABASE_PATH` points at. So this module
redirects that path to a throwaway file *before* importing `main`, and
refuses to run if `main` has somehow been imported already. The project's own
`backend/shiftops.db` is therefore never created, opened or migrated by these
checks.

The endpoints are called as plain functions rather than over HTTP. Starlette's
TestClient needs `httpx2`, which this project does not depend on, and adding a
dependency to run a test is a poor trade. Calling the functions still covers
what matters here: which exception becomes which status code, and the exact
shape of the body.

Checks:

1. Creating returns 201-shaped data carrying the code the backend issued.
2. Codes are issued in sequence across successive requests.
3. A request that supplies employee_code is a 400 naming the rule, and
   creates nobody.
4. Missing or invalid fields are 400.
5. Editing a worker's name keeps the issued code; changing the code is 400,
   including a case-only change.
6. Editing an unknown worker is 404, not 400.
7. The list endpoint reports the issued codes.

Run with:  python verify_employee_api.py
Exits non-zero if any check fails.
"""

import sys
import tempfile
from pathlib import Path

if "main" in sys.modules:  # pragma: no cover - defensive
    raise SystemExit(
        "main was imported before the database path was redirected; refusing to run."
    )

import database  # noqa: E402  - imported early on purpose, see the docstring

_TEMPORARY = tempfile.TemporaryDirectory()
database.DATABASE_PATH = Path(_TEMPORARY.name) / "api-fixture.db"

# Only now is it safe: main's import-time ensure_schema() will build the
# throwaway database above rather than the real one.
import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def expect_status(status, action, description):
    try:
        action()
        check(False, f"{description} (no HTTPException raised)")
        return None
    except HTTPException as error:
        check(
            error.status_code == status,
            f"{description} -> {error.status_code} {error.detail}",
        )
        return error


def employee_count():
    connection = database.get_connection()
    try:
        return connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"]
    finally:
        connection.close()


def main_checks():
    check(
        str(database.DATABASE_PATH).startswith(_TEMPORARY.name),
        f"the API is pointed at a throwaway database ({database.DATABASE_PATH})",
    )
    check(database.DATABASE_PATH.exists(), "import-time schema creation built that file")
    check(employee_count() == 0, "the fixture database starts empty")

    # 1-2. Creating issues codes in sequence.
    first = main.add_employee({"full_name": "Alice Adams", "student_type": "masters"})
    check(first["employee_code"] == "SW-001", f"the first create returns SW-001 ({first['employee_code']})")
    check(
        set(first) == {"employee_code", "full_name", "student_type", "is_active", "weekly_hour_limit"},
        f"the response keeps its existing shape {sorted(first)}",
    )
    check(first["full_name"] == "Alice Adams" and first["student_type"] == "masters",
          "the response echoes the submitted details")
    check(first["is_active"] is True and first["weekly_hour_limit"] == 20,
          "the response reports the defaults")

    second = main.add_employee({"full_name": "Bob Brown", "student_type": "undergraduate"})
    check(second["employee_code"] == "SW-002", f"the second create returns SW-002 ({second['employee_code']})")

    # 3. A supplied code is refused.
    before = employee_count()
    error = expect_status(
        400,
        lambda: main.add_employee(
            {"employee_code": "SW-900", "full_name": "Chooser", "student_type": "masters"}
        ),
        "supplying employee_code is a 400",
    )
    check(error is not None and "assigned automatically" in str(error.detail),
          "the 400 explains that IDs are assigned automatically")
    check(employee_count() == before, "the refused request created nobody")
    third = main.add_employee({"full_name": "Cara Cruz", "student_type": "masters"})
    check(third["employee_code"] == "SW-003",
          f"the refused request did not consume a number ({third['employee_code']})")

    # 4. Invalid details.
    expect_status(400, lambda: main.add_employee({"student_type": "masters"}), "a missing name is a 400")
    expect_status(400, lambda: main.add_employee({"full_name": "X", "student_type": "phd"}),
                  "an unsupported student type is a 400")
    expect_status(400, lambda: main.add_employee({"full_name": "  ", "student_type": "masters"}),
                  "a whitespace-only name is a 400")

    # 5. Editing.
    edited = main.edit_employee("SW-001", {
        "employee_code": "SW-001", "full_name": "Alice Renamed", "student_type": "undergraduate"})
    check(edited["employee_code"] == "SW-001" and edited["full_name"] == "Alice Renamed",
          "editing the name keeps the issued code")
    expect_status(
        400,
        lambda: main.edit_employee("SW-001", {
            "employee_code": "SW-002", "full_name": "X", "student_type": "masters"}),
        "changing the code through the API is a 400",
    )
    expect_status(
        400,
        lambda: main.edit_employee("SW-001", {
            "employee_code": "sw-001", "full_name": "X", "student_type": "masters"}),
        "a case-only code change is a 400",
    )
    check(
        main.edit_employee("SW-001", {
            "employee_code": "SW-001", "full_name": "Alice Renamed", "student_type": "undergraduate"}
        )["employee_code"] == "SW-001",
        "the worker is still addressable under the issued code after refusals",
    )

    # 6. Unknown worker.
    expect_status(
        404,
        lambda: main.edit_employee("SW-404", {
            "employee_code": "SW-404", "full_name": "Ghost", "student_type": "masters"}),
        "editing an unknown worker is a 404",
    )
    expect_status(
        404,
        lambda: main.edit_employee("SW-404", {
            "employee_code": "SW-001", "full_name": "Ghost", "student_type": "masters"}),
        "an unknown worker is a 404 even when the submitted code differs",
    )

    # 7. The list endpoint reports the issued codes.
    listed = main.list_employees()
    codes = [employee["employee_code"] for employee in listed["employees"]]
    check(codes == ["SW-001", "SW-002", "SW-003"], f"the list reports the issued codes {codes}")

    # Lifecycle endpoints still work against an issued code.
    check(main.deactivate_employee("SW-003")["is_active"] is False, "deactivate works on an issued code")
    check(main.reactivate_employee("SW-003")["is_active"] is True, "reactivate works on an issued code")
    removed = main.remove_employee("SW-003")
    check(removed["employee_code"] == "SW-003", "delete works on an issued code")
    after_delete = main.add_employee({"full_name": "After Delete", "student_type": "masters"})
    check(after_delete["employee_code"] == "SW-004",
          f"the deleted code is not reissued through the API ({after_delete['employee_code']})")


def run():
    try:
        main_checks()
    finally:
        _TEMPORARY.cleanup()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll employee API checks passed (throwaway database only).")
    return 0


if __name__ == "__main__":
    sys.exit(run())
