"""Named seed functions for the isolated browser end-to-end harness
(`e2e_server.py`, Phase 7 closeout finding 5).

Every function here takes an already-open connection to an ISOLATED,
throwaway database (never `backend/shiftops.db`) and inserts exactly the
records a given set of browser journeys needs. Nothing here is imported by
the application itself - this module only exists for test seeding.
"""

from database import create_schema

WEEK_A = "2026-10-05"  # matches frontend/src/weeks.js DEFAULT_WEEK_START
WEEK_B = "2026-10-12"


def _add_worker(connection, code, name, weekly_hour_limit=20):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active) VALUES (?, ?, 'undergraduate', ?, 1)",
        (code, name, weekly_hour_limit),
    )
    employee_id = connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]
    # Confirmed, non-provisional, and long enough to cover both prepared
    # weeks - every seeded worker starts fully eligible for anything, so
    # each journey can introduce exactly the one conflict it is testing
    # (a leave edit, say) without other noise.
    connection.execute(
        "INSERT INTO semester_schedules"
        " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
        " VALUES (?, '2026-08-24', '2026-12-11', '2026-01-01 00:00', 0)",
        (employee_id,),
    )
    return employee_id


def journeys(connection):
    """The one shared fixture the Playwright driver's journeys run against,
    in sequence, within a single server/database instance - later journeys
    deliberately build on earlier ones' state (an approved assignment, for
    instance) rather than each re-seeding independently, matching this
    project's "reuse existing fixtures" instruction.
    """
    create_schema(connection)

    # Five workers, not three: with only three, the optimizer's own maximum-
    # coverage objective packs all of them close to their weekly hour cap,
    # leaving genuinely no OTHER eligible worker for almost any assigned
    # shift - which starves the replacement journeys of a real candidate.
    # Five gives enough slack that a free, eligible replacement candidate
    # reliably exists somewhere without needing to fabricate one.
    for code, name in (
        ("SW-201", "Journey Alpha"),
        ("SW-202", "Journey Bravo"),
        ("SW-203", "Journey Charlie"),
        ("SW-204", "Journey Delta"),
        ("SW-205", "Journey Echo"),
    ):
        _add_worker(connection, code, name)

    from scheduling import prepare_week

    prepare_week(connection, WEEK_A)
    prepare_week(connection, WEEK_B)

    # A manual, direct assignment on WEEK_B, deliberately NOT going through
    # the optimizer - Journey A's Generate Schedule call saturates every
    # seeded worker toward their weekly hour cap on WEEK_A (maximizing
    # coverage naturally uses nearly all of everyone's hours), which leaves
    # no genuine replacement candidate anywhere on that week. WEEK_B is
    # otherwise untouched, so every OTHER seeded worker is guaranteed free
    # and eligible here - a real replacement candidate always exists for
    # this one assignment, by construction, without relying on how the
    # CP-SAT solver happens to pack a given run.
    alpha_id = connection.execute(
        "SELECT id FROM employees WHERE employee_code = 'SW-201'"
    ).fetchone()["id"]
    week_b_shift = connection.execute(
        "SELECT id FROM shifts WHERE hall = 'Andromeda' AND start_datetime LIKE ?"
        " ORDER BY start_datetime LIMIT 1",
        (f"{WEEK_B}%",),
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (alpha_id, week_b_shift),
    )
    connection.commit()
