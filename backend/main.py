from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import ensure_schema, get_connection

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["GET"],
)

ensure_schema()


def minutes_between(start_time, end_time):
    start_hour, start_minute = (int(part) for part in start_time.split(":"))
    end_hour, end_minute = (int(part) for part in end_time.split(":"))
    return (end_hour * 60 + end_minute) - (start_hour * 60 + start_minute)


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

    return [
        {
            "employee_code": employee["employee_code"],
            "full_name": employee["full_name"],
            "student_type": employee["student_type"],
            "weekly_hour_limit": employee["weekly_hour_limit"],
            "course_count": employee["course_count"],
            "class_meeting_count": meeting_counts.get(employee["id"], 0),
            "weekly_class_hours": round(class_minutes.get(employee["id"], 0) / 60, 2),
            "approved_leave_count": employee["approved_leave_count"],
        }
        for employee in employees
    ]
