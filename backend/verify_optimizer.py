"""Regression checks for Phase 7 increment 2: the read-only draft optimizer
(`optimizer.py`).

Runs entirely against throwaway in-memory databases with purpose-built
workers, shifts, schedules, leave and assignments. The project's own
`backend/shiftops.db` is never opened, read or modified.

Checks:

 1. A fully coverable week produces a complete draft.
 2. A partially coverable week returns the best partial draft with uncovered
    positions and a useful reason.
 3. Coverage is prioritized over preference (the max-coverage assignment is
    chosen even when it forces a worker onto their low-preference shift).
 4. Preferred beats neutral, and neutral beats low, when coverage is equal.
 5. Proposed assignments for one worker never overlap.
 6. The cumulative hours of multiple proposed shifts cannot exceed a
    worker's weekly limit.
 7. Existing assignments constrain the draft (no re-proposal for an
    already-filled position) and are reported back unchanged.
 8. Inactive, unconfirmed-timetable, provisional-timetable, class-conflicted
    and leave-conflicted workers are all excluded using the existing
    eligibility rules, with their reasons tallied.
 9. A confirmed "no classes" semester remains eligible.
10. `required_staff = 2` can produce two distinct proposed workers.
11. Repeated identical runs over identical data return identical results.
12. Draft generation performs no database writes.
13. An unprepared week raises a controlled error without creating shifts.
14. A malformed or non-Monday `week_start` raises `weeks.InvalidWeekStart`.
15. Neutral beats low when coverage is equal (no preferred option present).
16. The workload tier chooses the lower maximum projected weekly workload
    when coverage and preference are already tied.
17. A non-optimal solver status is handled explicitly - `DraftNotOptimal` is
    raised and no ordinary draft is returned.
18. Existing assignments above `required_staff` produce truthful, never
    negative, per-shift and summary counts, with the excess reported
    explicitly rather than silently lost.
19. A two-person shift with only one distinct eligible worker reports the
    accurate `insufficient_eligible_workers` reason, not a claim that nobody
    is eligible.
20. Multiple existing assignments on one shift come back in a stable,
    deterministic order (by employee_code, then id), not insertion order.

Run with:  python verify_optimizer.py
Exits non-zero if any check fails.
"""

import sys
from datetime import datetime
from unittest.mock import patch

from ortools.sat.python import cp_model

from database import create_schema, get_connection
from optimizer import DraftNotOptimal, WeekNotPrepared, generate_draft
from weeks import InvalidWeekStart

DATETIME_FORMAT = "%Y-%m-%d %H:%M"

failures = []
_total = {"n": 0}


def check(condition, description):
    _total["n"] += 1
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fixture_database():
    connection = get_connection(":memory:")
    create_schema(connection)
    return connection


def add_employee(connection, code, limit=20, active=True, student_type="undergraduate"):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active) VALUES (?, ?, ?, ?, ?)",
        (code, f"Fixture {code}", student_type, limit, 1 if active else 0),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]


def add_shift(connection, start, end, hall="Helix", required_staff=1):
    start_text, end_text = start.strftime(DATETIME_FORMAT), end.strftime(DATETIME_FORMAT)
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES (?, ?, ?, ?)",
        (hall, start_text, end_text, required_staff),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ? AND end_datetime = ?",
        (hall, start_text, end_text),
    ).fetchone()["id"]


def add_schedule(connection, employee_id, start_date, end_date, confirmed=True, provisional=False):
    confirmed_at = "2026-01-01 00:00" if confirmed else None
    connection.execute(
        "INSERT INTO semester_schedules"
        " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
        " VALUES (?, ?, ?, ?, ?)",
        (employee_id, start_date, end_date, confirmed_at, 1 if provisional else 0),
    )
    connection.commit()
    return connection.execute(
        "SELECT id FROM semester_schedules WHERE employee_id = ?"
        " AND start_date = ? AND end_date = ?",
        (employee_id, start_date, end_date),
    ).fetchone()["id"]


def add_block(connection, schedule_id, day_of_week, start_time, end_time):
    connection.execute(
        "INSERT INTO class_blocks (schedule_id, day_of_week, start_time, end_time)"
        " VALUES (?, ?, ?, ?)",
        (schedule_id, day_of_week, start_time, end_time),
    )
    connection.commit()


def add_leave(connection, employee_id, start, end):
    connection.execute(
        "INSERT INTO approved_leave (employee_id, start_datetime, end_datetime)"
        " VALUES (?, ?, ?)",
        (employee_id, start.strftime(DATETIME_FORMAT), end.strftime(DATETIME_FORMAT)),
    )
    connection.commit()


def add_assignment(connection, employee_id, shift_id):
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (employee_id, shift_id),
    )
    connection.commit()


def set_preference(connection, employee_id, shift_id, preference):
    connection.execute(
        "INSERT INTO shift_preferences (employee_id, shift_id, preference)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT (employee_id, shift_id) DO UPDATE SET preference = excluded.preference",
        (employee_id, shift_id, preference),
    )
    connection.commit()


def confirmed_worker(connection, code, week_start="2026-11-02", week_end="2026-11-08", limit=20):
    """A ready-to-use worker: active, confirmed and non-provisional for the
    whole week, no classes, no leave, no assignments."""
    employee_id = add_employee(connection, code, limit=limit)
    add_schedule(connection, employee_id, week_start, week_end, confirmed=True, provisional=False)
    return employee_id


def full_snapshot(connection):
    return {
        table: tuple(sorted(map(tuple, connection.execute(f"SELECT * FROM {table}"))))
        for table in (
            "employees",
            "shifts",
            "semester_schedules",
            "class_blocks",
            "shift_preferences",
            "approved_leave",
            "assignments",
        )
    }


def find_shift(result, shift_id):
    return next(s for s in result["shifts"] if s["id"] == shift_id)


def proposed_codes(shift_payload):
    return {row["employee_code"] for row in shift_payload["proposed_assignments"]}


def check_fully_coverable_week():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    w2 = confirmed_worker(connection, "SW-002")
    shift_a = add_shift(connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0), hall="Andromeda")
    shift_b = add_shift(connection, datetime(2026, 11, 3, 17, 0), datetime(2026, 11, 3, 22, 0), hall="Capella")

    result = generate_draft(connection, "2026-11-02")
    check(result["status"] == "complete", f"a fully coverable week is reported complete ({result['status']})")
    check(result["summary"]["uncovered_positions"] == 0, "zero uncovered positions in the summary")
    check(find_shift(result, shift_a)["covered"] is True, "shift A is covered")
    check(find_shift(result, shift_b)["covered"] is True, "shift B is covered")
    connection.close()


def check_partially_coverable_week():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    shift_a = add_shift(connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0), hall="Andromeda")
    shift_b = add_shift(connection, datetime(2026, 11, 3, 17, 0), datetime(2026, 11, 3, 22, 0), hall="Capella")
    # SW-001 has approved leave covering shift B, so only shift A is
    # coverable; nobody else exists to cover B.
    add_leave(connection, w1, datetime(2026, 11, 3, 16, 0), datetime(2026, 11, 3, 23, 0))

    result = generate_draft(connection, "2026-11-02")
    check(result["status"] == "partial", f"a partially coverable week is reported partial ({result['status']})")
    check(result["summary"]["uncovered_positions"] == 1, "exactly one uncovered position in the summary")
    check(find_shift(result, shift_a)["covered"] is True, "shift A is still covered")
    b_payload = find_shift(result, shift_b)
    check(b_payload["covered"] is False, "shift B is not covered")
    check(
        b_payload["uncovered_reasons"][0]["reason_code"] == "no_eligible_workers"
        and b_payload["uncovered_reasons"][0]["reason_counts"].get("leave_conflict") == 1,
        f"shift B's uncovered reason names the leave conflict ({b_payload.get('uncovered_reasons')})",
    )
    connection.close()


def check_coverage_prioritized_over_preference():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")  # can only do shift B
    w2 = confirmed_worker(connection, "SW-002")  # can do either, but not both (they overlap)
    # A and B overlap (17:00-22:00 vs 18:00-23:00), so one worker can cover
    # at most one of them - the "never overlap" constraint itself forces
    # that choice, not this test.
    shift_a = add_shift(connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0), hall="Andromeda")
    shift_b = add_shift(connection, datetime(2026, 11, 2, 18, 0), datetime(2026, 11, 2, 23, 0), hall="Capella")

    # SW-001 is blocked from shift A ONLY (leave ends exactly at 18:00, when
    # B starts - the touching-endpoint rule means it does not overlap B) by
    # approved leave, so only SW-002 could cover A - but SW-002 strongly
    # prefers shift B. Maximizing preference alone would put SW-002 on B
    # (their preferred shift), leaving A uncovered since SW-002 cannot also
    # take A (the overlap) and SW-001 cannot take A (the leave) - only 1 of
    # 2 positions filled. Maximizing coverage FIRST requires SW-002 on A
    # (their low preference) so SW-001 can fill B, covering both.
    add_leave(connection, w1, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 18, 0))
    set_preference(connection, w2, shift_a, "low")
    set_preference(connection, w2, shift_b, "preferred")

    result = generate_draft(connection, "2026-11-02")
    check(result["status"] == "complete", f"the max-coverage draft covers both shifts ({result['summary']})")
    check(
        proposed_codes(find_shift(result, shift_a)) == {"SW-002"},
        f"SW-002 is proposed for their LOW-preference shift A, because that is required for full coverage ({proposed_codes(find_shift(result, shift_a))})",
    )
    check(
        proposed_codes(find_shift(result, shift_b)) == {"SW-001"},
        f"SW-001 fills shift B, the only shift they can cover ({proposed_codes(find_shift(result, shift_b))})",
    )
    connection.close()


def check_preference_ordering_when_coverage_equal():
    connection = fixture_database()
    w_low = confirmed_worker(connection, "SW-001")
    w_neutral = confirmed_worker(connection, "SW-002")
    w_preferred = confirmed_worker(connection, "SW-003")
    shift_a = add_shift(connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0), hall="Andromeda")

    set_preference(connection, w_low, shift_a, "low")
    set_preference(connection, w_preferred, shift_a, "preferred")
    # w_neutral has no stored preference row at all - neutral is the
    # absence of a row (D025).

    result = generate_draft(connection, "2026-11-02")
    check(
        proposed_codes(find_shift(result, shift_a)) == {"SW-003"},
        f"the preferred worker is chosen over neutral and low when coverage is identical either way ({proposed_codes(find_shift(result, shift_a))})",
    )
    connection.close()


def check_neutral_beats_low_when_coverage_equal():
    connection = fixture_database()
    w_low = confirmed_worker(connection, "SW-001")
    w_neutral = confirmed_worker(connection, "SW-002")
    shift_a = add_shift(connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0), hall="Andromeda")

    set_preference(connection, w_low, shift_a, "low")
    # w_neutral has no stored preference row - neutral is the absence of a
    # row (D025). No "preferred" worker exists in this fixture at all, so
    # this isolates neutral-vs-low specifically.

    result = generate_draft(connection, "2026-11-02")
    check(
        proposed_codes(find_shift(result, shift_a)) == {"SW-002"},
        f"the neutral worker is chosen over the low-preference one when coverage is identical either way ({proposed_codes(find_shift(result, shift_a))})",
    )
    connection.close()


def check_workload_tier_minimizes_max_when_tied():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    w2 = confirmed_worker(connection, "SW-002")

    # SW-001 already has 3 existing hours this week, from a shift that does
    # not conflict with anything below.
    existing_shift = add_shift(connection, datetime(2026, 11, 3, 8, 0), datetime(2026, 11, 3, 11, 0), hall="Vega")
    add_assignment(connection, w1, existing_shift)

    # Two new, OVERLAPPING shifts (so exactly one worker takes each, forcing
    # both workers to be used - both A and B must be covered, and no worker
    # can take both). Neither worker has a stored preference for either, so
    # coverage (2, either way) and preference (both neutral, sum tied either
    # way) are identical regardless of which worker takes which shift - only
    # tier 3 (minimize the higher of the two final loads) can distinguish
    # between (SW-001->A, SW-002->B) [loads 3+8=11, 0+4=4, max=11] and
    # (SW-001->B, SW-002->A) [loads 3+4=7, 0+8=8, max=8]. The second is
    # strictly better and must be the one chosen.
    shift_a = add_shift(connection, datetime(2026, 11, 2, 8, 0), datetime(2026, 11, 2, 16, 0), hall="Andromeda")  # 8h
    shift_b = add_shift(connection, datetime(2026, 11, 2, 10, 0), datetime(2026, 11, 2, 14, 0), hall="Capella")  # 4h

    result = generate_draft(connection, "2026-11-02")
    check(result["status"] == "complete", f"both new shifts are covered ({result['summary']})")
    check(
        proposed_codes(find_shift(result, shift_a)) == {"SW-002"}
        and proposed_codes(find_shift(result, shift_b)) == {"SW-001"},
        "the lower-maximum-load split is chosen (SW-002 takes the 8h shift "
        f"since they start at 0 hours; SW-001 takes the 4h one) "
        f"(A={proposed_codes(find_shift(result, shift_a))}, B={proposed_codes(find_shift(result, shift_b))})",
    )
    connection.close()


def check_non_optimal_status_raises_controlled_error():
    connection = fixture_database()
    confirmed_worker(connection, "SW-001")
    add_shift(connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0), hall="Andromeda")

    with patch.object(cp_model.CpSolver, "Solve", return_value=cp_model.UNKNOWN):
        try:
            generate_draft(connection, "2026-11-02")
            check(False, "a non-OPTIMAL solver status should have raised a controlled error, not returned a draft")
        except DraftNotOptimal:
            check(True, "a non-OPTIMAL (UNKNOWN) solver status raises DraftNotOptimal instead of an ordinary draft")
    connection.close()


def check_overstaffed_shift_accounting():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    w2 = confirmed_worker(connection, "SW-002")
    # required_staff=1 but TWO existing assignments - overstaffed, a shape
    # the optimizer never creates itself but stored data can already have.
    shift = add_shift(connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0), hall="Andromeda", required_staff=1)
    add_assignment(connection, w1, shift)
    add_assignment(connection, w2, shift)

    result = generate_draft(connection, "2026-11-02")
    payload = find_shift(result, shift)
    check(len(payload["existing_assignments"]) == 2, "both existing assignments are still returned in full")
    check(payload["filled_count"] == 1, f"filled_count is capped at required_staff, not double-counted ({payload['filled_count']})")
    check(payload["excess_assignments"] == 1, f"the one excess assignment is reported explicitly ({payload['excess_assignments']})")
    check(payload["uncovered_positions"] == 0, "uncovered_positions is zero, never negative")
    check(payload["covered"] is True, "the overstaffed shift is still reported as covered")
    check(payload["proposed_assignments"] == [], "no proposal is added to an already-overstaffed shift")

    summary = result["summary"]
    check(summary["uncovered_positions"] >= 0, f"summary uncovered_positions is never negative ({summary['uncovered_positions']})")
    check(summary["excess_assignments"] == 1, f"the summary totals the excess too ({summary['excess_assignments']})")
    check(
        summary["total_filled_positions"] == summary["required_positions"] == 1,
        f"the summary's totals reflect the capped count, not the raw assignment count ({summary})",
    )
    connection.close()


def check_existing_assignments_stable_order():
    """Multiple existing assignments on one shift come back in a stable,
    deterministic order (by employee_code, then id) - never insertion order,
    which SQLite does not otherwise guarantee for a join without ORDER BY.
    """
    connection = fixture_database()
    # Inserted deliberately out of employee_code order.
    w3 = confirmed_worker(connection, "SW-003")
    w1 = confirmed_worker(connection, "SW-001")
    w2 = confirmed_worker(connection, "SW-002")
    shift = add_shift(
        connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0),
        hall="Andromeda", required_staff=3,
    )
    add_assignment(connection, w3, shift)
    add_assignment(connection, w1, shift)
    add_assignment(connection, w2, shift)

    first = generate_draft(connection, "2026-11-02")
    second = generate_draft(connection, "2026-11-02")
    codes_first = [row["employee_code"] for row in find_shift(first, shift)["existing_assignments"]]
    codes_second = [row["employee_code"] for row in find_shift(second, shift)["existing_assignments"]]
    check(
        codes_first == ["SW-001", "SW-002", "SW-003"],
        f"existing_assignments is sorted by employee_code regardless of insertion order ({codes_first})",
    )
    check(codes_first == codes_second, f"the order is identical across repeated calls ({codes_first}, {codes_second})")
    connection.close()


def check_insufficient_eligible_workers_reason():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")  # the only eligible worker
    shift = add_shift(
        connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0),
        hall="Andromeda", required_staff=2,
    )

    result = generate_draft(connection, "2026-11-02")
    payload = find_shift(result, shift)
    check(payload["covered"] is False, "a 2-person shift with only 1 eligible worker is not covered")
    check(proposed_codes(payload) == {"SW-001"}, "the one eligible worker is still proposed for one of the two positions")
    check(payload["uncovered_positions"] == 1, f"exactly one position remains uncovered ({payload['uncovered_positions']})")
    reason = payload["uncovered_reasons"][0]
    check(
        reason["reason_code"] == "insufficient_eligible_workers",
        f"the reason correctly names an insufficient pool of eligible workers, not 'no one is eligible' ({reason})",
    )
    connection.close()


def check_proposed_assignments_never_overlap():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    # Two overlapping shifts at different halls; only SW-001 is eligible for
    # either.
    shift_a = add_shift(connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0), hall="Andromeda")
    shift_b = add_shift(connection, datetime(2026, 11, 2, 18, 0), datetime(2026, 11, 2, 23, 0), hall="Capella")

    result = generate_draft(connection, "2026-11-02")
    a_codes = proposed_codes(find_shift(result, shift_a))
    b_codes = proposed_codes(find_shift(result, shift_b))
    check(
        not (a_codes and b_codes),
        f"SW-001 is proposed for at most one of the two overlapping shifts (A={a_codes}, B={b_codes})",
    )
    check(
        len(a_codes) + len(b_codes) == 1,
        "exactly one of the two overlapping shifts is covered - the other reports no proposal",
    )
    uncovered = find_shift(result, shift_b) if not b_codes else find_shift(result, shift_a)
    check(
        uncovered["uncovered_reasons"][0]["reason_code"] == "capacity_allocated_elsewhere",
        f"the shift that lost out reports the worker was available but used elsewhere ({uncovered.get('uncovered_reasons')})",
    )
    connection.close()


def check_weekly_limit_not_exceeded_across_proposals():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001", limit=20)
    # Two non-overlapping 12-hour shifts; individually within the 20-hour
    # limit, but 12 + 12 = 24 exceeds it, so both cannot be proposed to the
    # same worker even though nothing else conflicts between them.
    shift_a = add_shift(connection, datetime(2026, 11, 2, 8, 0), datetime(2026, 11, 2, 20, 0), hall="Andromeda")
    shift_b = add_shift(connection, datetime(2026, 11, 3, 8, 0), datetime(2026, 11, 3, 20, 0), hall="Andromeda")

    result = generate_draft(connection, "2026-11-02")
    a_codes = proposed_codes(find_shift(result, shift_a))
    b_codes = proposed_codes(find_shift(result, shift_b))
    check(
        len(a_codes) + len(b_codes) == 1,
        f"only one of the two 12-hour shifts is proposed to SW-001, since both together would exceed the 20-hour limit (A={a_codes}, B={b_codes})",
    )
    proposed_shift = find_shift(result, shift_a) if a_codes else find_shift(result, shift_b)
    row = proposed_shift["proposed_assignments"][0]
    check(
        row["projected_hours_after"] == 12,
        f"the one proposed shift's projected hours reflect only that shift, not both (projected={row['projected_hours_after']})",
    )
    connection.close()


def check_existing_assignments_constrain_and_are_preserved():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    w2 = confirmed_worker(connection, "SW-002")
    shift_a = add_shift(connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0), hall="Andromeda")
    add_assignment(connection, w1, shift_a)

    result = generate_draft(connection, "2026-11-02")
    a_payload = find_shift(result, shift_a)
    check(
        a_payload["existing_assignments"] == [
            {"employee_id": w1, "employee_code": "SW-001", "full_name": "Fixture SW-001"}
        ],
        f"the existing assignment is reported back exactly, unchanged ({a_payload['existing_assignments']})",
    )
    check(a_payload["proposed_assignments"] == [], "an already-fully-staffed shift receives no new proposal")
    check(a_payload["covered"] is True, "the shift remains covered by its existing assignment alone")

    before = full_snapshot(connection)
    generate_draft(connection, "2026-11-02")
    after = full_snapshot(connection)
    check(before == after, "the existing assignment itself is never touched by draft generation")
    connection.close()


def check_hard_rules_exclude_using_existing_eligibility():
    connection = fixture_database()
    shift = add_shift(connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0), hall="Andromeda")

    inactive = add_employee(connection, "SW-010", active=False)
    add_schedule(connection, inactive, "2026-11-02", "2026-11-08")

    unconfirmed = add_employee(connection, "SW-011")
    add_schedule(connection, unconfirmed, "2026-11-02", "2026-11-08", confirmed=False)

    provisional = add_employee(connection, "SW-012")
    add_schedule(connection, provisional, "2026-11-02", "2026-11-08", confirmed=True, provisional=True)

    class_conflicted = confirmed_worker(connection, "SW-013")
    schedule_id = connection.execute(
        "SELECT id FROM semester_schedules WHERE employee_id = ?", (class_conflicted,)
    ).fetchone()["id"]
    add_block(connection, schedule_id, 0, "17:00", "18:00")  # Monday, overlaps the shift

    leave_conflicted = confirmed_worker(connection, "SW-014")
    add_leave(connection, leave_conflicted, datetime(2026, 11, 2, 16, 0), datetime(2026, 11, 2, 23, 0))

    result = generate_draft(connection, "2026-11-02")
    payload = find_shift(result, shift)
    check(payload["proposed_assignments"] == [], "no worker with a hard-rule violation is proposed")
    check(payload["covered"] is False, "the shift is correctly reported uncovered")
    counts = payload["uncovered_reasons"][0]["reason_counts"]
    check(
        counts.get("worker_inactive") == 1
        and counts.get("timetable_not_confirmed") == 2  # unconfirmed + provisional
        and counts.get("class_conflict") == 1
        and counts.get("leave_conflict") == 1,
        f"the reason tally names every hard-rule failure correctly ({counts})",
    )
    connection.close()


def check_confirmed_no_classes_remains_eligible():
    connection = fixture_database()
    # confirmed_worker() already builds exactly this: confirmed, non-
    # provisional, zero class blocks.
    worker = confirmed_worker(connection, "SW-001")
    shift = add_shift(connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0), hall="Andromeda")

    result = generate_draft(connection, "2026-11-02")
    check(
        proposed_codes(find_shift(result, shift)) == {"SW-001"},
        "a worker with a confirmed, non-provisional, zero-class semester is proposed like any other eligible worker",
    )
    connection.close()


def check_required_staff_two_produces_two_workers():
    connection = fixture_database()
    w1 = confirmed_worker(connection, "SW-001")
    w2 = confirmed_worker(connection, "SW-002")
    shift = add_shift(
        connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0),
        hall="Andromeda", required_staff=2,
    )

    result = generate_draft(connection, "2026-11-02")
    payload = find_shift(result, shift)
    check(payload["covered"] is True, "a required_staff=2 shift with two eligible workers is fully covered")
    check(
        proposed_codes(payload) == {"SW-001", "SW-002"},
        f"both distinct workers are proposed for the two open positions ({proposed_codes(payload)})",
    )
    connection.close()


def check_repeated_runs_identical():
    connection = fixture_database()
    for index in range(1, 6):
        confirmed_worker(connection, f"SW-{index:03d}")
    for day in (2, 3, 4):
        add_shift(connection, datetime(2026, 11, day, 17, 0), datetime(2026, 11, day, 22, 0), hall="Andromeda")
        add_shift(connection, datetime(2026, 11, day, 22, 0), datetime(2026, 11, day, 23, 0), hall="Capella")

    first = generate_draft(connection, "2026-11-02")
    second = generate_draft(connection, "2026-11-02")
    check(first == second, "two runs over identical, unchanged data return byte-identical drafts")
    connection.close()


def check_no_database_writes():
    connection = fixture_database()
    confirmed_worker(connection, "SW-001")
    add_shift(connection, datetime(2026, 11, 2, 17, 0), datetime(2026, 11, 2, 22, 0), hall="Andromeda")

    before = full_snapshot(connection)
    generate_draft(connection, "2026-11-02")
    after = full_snapshot(connection)
    check(before == after, "generate_draft makes no changes to any table")
    connection.close()


def check_unprepared_week_raises_without_creating_shifts():
    connection = fixture_database()
    confirmed_worker(connection, "SW-001")

    before_count = connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"]
    try:
        generate_draft(connection, "2026-12-07")
        check(False, "an unprepared week should have raised WeekNotPrepared")
    except WeekNotPrepared:
        check(True, "an unprepared week raises WeekNotPrepared rather than silently returning an empty draft")
    after_count = connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"]
    check(before_count == after_count == 0, "no shift was created while handling the unprepared week")
    connection.close()


def check_invalid_week_start_rejected():
    connection = fixture_database()
    for bad in ("not-a-date", "2026-11-03", "2026-1-2"):
        try:
            generate_draft(connection, bad)
            check(False, f"invalid week_start {bad!r} should have been rejected")
        except InvalidWeekStart:
            check(True, f"invalid week_start {bad!r} raises InvalidWeekStart")
    connection.close()


def main_entry():
    check_fully_coverable_week()
    check_partially_coverable_week()
    check_coverage_prioritized_over_preference()
    check_preference_ordering_when_coverage_equal()
    check_neutral_beats_low_when_coverage_equal()
    check_workload_tier_minimizes_max_when_tied()
    check_non_optimal_status_raises_controlled_error()
    check_overstaffed_shift_accounting()
    check_existing_assignments_stable_order()
    check_insufficient_eligible_workers_reason()
    check_proposed_assignments_never_overlap()
    check_weekly_limit_not_exceeded_across_proposals()
    check_existing_assignments_constrain_and_are_preserved()
    check_hard_rules_exclude_using_existing_eligibility()
    check_confirmed_no_classes_remains_eligible()
    check_required_staff_two_produces_two_workers()
    check_repeated_runs_identical()
    check_no_database_writes()
    check_unprepared_week_raises_without_creating_shifts()
    check_invalid_week_start_rejected()

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"\nAll {_total['n']} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
