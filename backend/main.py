from datetime import timedelta

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from database import ensure_schema, get_connection
from employees import (
    DeactivationBlocked,
    DuplicateEmployeeCode,
    EmployeeNotFound,
    EmployeeValidationError,
    create_employee,
    set_active,
    update_employee,
)
from reporting import (
    InvalidWorkDuration,
    assigned_hours_by_employee,
    minutes_between,
    remaining_capacity_hours,
)
from synthetic_data import WEEK_START, WEEK_END

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    # POST/PUT added for employee creation and editing; the JSON bodies they
    # carry make Content-Type a non-simple header, so it must be allowed too.
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type"],
)

ensure_schema()


@app.get("/api/health")
def health_check():
    return {"status": "ok"}


@app.get("/api/employees")
def list_employees():
    connection = get_connection()
    try:
        employees = connection.execute(
            """
            SELECT
                e.id,
                e.employee_code,
                e.full_name,
                e.student_type,
                e.weekly_hour_limit,
                e.is_active,
                (SELECT COUNT(*) FROM courses c
                  WHERE c.employee_id = e.id) AS course_count,
                (SELECT COUNT(*) FROM approved_leave l
                  WHERE l.employee_id = e.id) AS approved_leave_count
            FROM employees e
            ORDER BY e.employee_code
            """
        ).fetchall()

        meetings = connection.execute(
            """
            SELECT c.employee_id, m.start_time, m.end_time
            FROM class_meetings m
            JOIN courses c ON c.id = m.course_id
            """
        ).fetchall()

        try:
            assigned_hours = assigned_hours_by_employee(connection)
        except InvalidWorkDuration as error:
            # Stored shift data violates the whole-hour work rule. Report it
            # instead of returning a capacity figure derived from bad data.
            raise HTTPException(
                status_code=500,
                detail=f"Stored shift data is invalid: {error}",
            ) from error
    finally:
        connection.close()

    meeting_counts = {}
    class_minutes = {}
    for meeting in meetings:
        employee_id = meeting["employee_id"]
        meeting_counts[employee_id] = meeting_counts.get(employee_id, 0) + 1
        class_minutes[employee_id] = class_minutes.get(employee_id, 0) + minutes_between(
            meeting["start_time"], meeting["end_time"]
        )

    def employee_payload(employee):
        assigned = assigned_hours.get(employee["id"], 0)
        # Theoretical unused work capacity only. Class hours and approved
        # leave are NOT subtracted: this is not an eligibility calculation.
        remaining = remaining_capacity_hours(employee["weekly_hour_limit"], assigned)
        return {
            "employee_code": employee["employee_code"],
            "full_name": employee["full_name"],
            "student_type": employee["student_type"],
            "is_active": bool(employee["is_active"]),
            "weekly_hour_limit": employee["weekly_hour_limit"],
            "course_count": employee["course_count"],
            "class_meeting_count": meeting_counts.get(employee["id"], 0),
            "weekly_class_hours": round(class_minutes.get(employee["id"], 0) / 60, 2),
            "approved_leave_count": employee["approved_leave_count"],
            # Whole hours: work shifts are whole-hour blocks, so these
            # totals never need rounding.
            "assigned_hours": assigned,
            "remaining_capacity_hours": remaining,
        }

    return {
        # The reporting week the capacity figures describe. Fixed to the
        # sample week for now; it becomes a request parameter once schedules
        # span more than one week.
        "week_start": WEEK_START.strftime("%Y-%m-%d"),
        # WEEK_END is the exclusive Monday boundary; show the Sunday instead.
        "week_end": (WEEK_END - timedelta(days=1)).strftime("%Y-%m-%d"),
        "employees": [employee_payload(employee) for employee in employees],
    }


def employee_response(row):
    """The stored row as the frontend sees it after a save."""
    return {
        "employee_code": row["employee_code"],
        "full_name": row["full_name"],
        "student_type": row["student_type"],
        "is_active": bool(row["is_active"]),
        "weekly_hour_limit": row["weekly_hour_limit"],
    }


def write_employee(action):
    """Run a create/update and turn its errors into clear HTTP responses."""
    connection = get_connection()
    try:
        return employee_response(action(connection))
    except EmployeeValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except DuplicateEmployeeCode as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except EmployeeNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DeactivationBlocked as error:
        # 409: the request is valid, but the worker's current state does not
        # allow it. Nothing was changed.
        raise HTTPException(status_code=409, detail=str(error)) from error
    finally:
        connection.close()


@app.post("/api/employees", status_code=201)
def add_employee(payload: dict):
    return write_employee(lambda connection: create_employee(connection, payload))


@app.put("/api/employees/{employee_code}")
def edit_employee(employee_code: str, payload: dict):
    return write_employee(
        lambda connection: update_employee(connection, employee_code, payload)
    )


@app.post("/api/employees/{employee_code}/deactivate")
def deactivate_employee(employee_code: str):
    return write_employee(
        lambda connection: set_active(connection, employee_code, active=False)
    )


@app.post("/api/employees/{employee_code}/reactivate")
def reactivate_employee(employee_code: str):
    return write_employee(
        lambda connection: set_active(connection, employee_code, active=True)
    )
