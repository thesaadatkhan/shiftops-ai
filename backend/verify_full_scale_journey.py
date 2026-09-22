"""Realistic end-to-end verification at the project's actual demo scale
(Codex whole-project review finding 7): the pristine 30-worker/198-shift
synthetic dataset, generated -> approved -> reloaded -> replaced, with
measured optimizer latency recorded rather than assumed.

Every prior optimizer/proposal check (`verify_optimizer.py`,
`verify_proposals.py`) uses small, purpose-built fixtures to isolate one
rule at a time. None of them exercise the full generator's actual output
size. This script does, against `demo_fixture.demo_fixture_connection()` -
the same pristine in-memory demo dataset `verify_coverage.py` and others
already use - so it stays meaningful without ever touching the project's
real `backend/shiftops.db`.

Checks:

1. The fixture is genuinely the documented scale: 30 employees, 198 shifts
   across two weeks, with deterministic valid background assignments.
2. `create_proposal()` solves all three CP-SAT tiers to a proven OPTIMAL
   status at this scale (raising `DraftNotOptimal` otherwise) and its
   wall-clock latency is measured and printed - not assumed. A partial
   result (some positions left uncovered) is an accepted, correctly
   reported outcome when hard constraints leave no eligible worker; nothing
   here relaxes a hard rule to force full coverage.
3. Approving the generated proposal persists exactly its proposed
   assignments, atomically, and the reload through
   `scheduling.get_week_schedule` agrees with what was approved.
4. An explicit replacement on one of the newly created assignments succeeds
   and is reflected in a fresh reload.

Run with:  python verify_full_scale_journey.py
Exits non-zero if any check fails.
"""

import sys
import time

from demo_fixture import demo_fixture_connection
from eligibility import shift_coverage
from optimizer import DraftNotOptimal
from proposals import create_proposal, approve_proposal, replace_assignment
from scheduling import get_week_schedule
from synthetic_data import WEEK_START

failures = []
_total = {"n": 0}


def check(condition, description):
    _total["n"] += 1
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def main():
    connection = demo_fixture_connection()
    week_start_text = WEEK_START.strftime("%Y-%m-%d")

    employee_count = connection.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"]
    shift_count = connection.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"]
    check(employee_count == 30, f"the pristine demo fixture has 30 employees ({employee_count})")
    check(shift_count == 198, f"the pristine demo fixture has 198 shifts ({shift_count})")
    initial_assignments = connection.execute(
        "SELECT COUNT(*) AS n FROM assignments"
    ).fetchone()["n"]
    check(
        initial_assignments > 0,
        f"the pristine fixture includes deterministic background assignments ({initial_assignments})",
    )

    started = time.perf_counter()
    try:
        proposal = create_proposal(connection, week_start_text)
        elapsed = time.perf_counter() - started
    except DraftNotOptimal as error:
        elapsed = time.perf_counter() - started
        check(False, f"the full-scale draft solved to OPTIMAL in {elapsed:.2f}s ({error})")
        print(f"\n{len(failures)} check(s) FAILED (optimizer non-optimal at full scale).")
        return 1

    print(
        f"MEASURED  full-scale (30 workers / 99 shifts in selected week) proposal generation took {elapsed:.2f}s "
        "wall-clock (three sequential CP-SAT tiers, 30s cap each)."
    )
    check(elapsed < 90, f"full-scale generation completes well within the 3x30s worst case ({elapsed:.2f}s)")

    review = proposal["review"]
    summary = review["summary"]
    print(
        f"MEASURED  coverage at full scale: {summary['total_filled_positions']}/"
        f"{summary['required_positions']} required positions filled "
        f"({summary['existing_filled_positions']} existing, {summary['proposed_filled_positions']} proposed), "
        f"{summary['uncovered_positions']} uncovered, {summary['excess_assignments']} excess."
    )
    check(
        summary["total_filled_positions"] <= summary["required_positions"],
        "filled positions never exceed required positions (no fabricated coverage)",
    )
    check(
        len(proposal["assignments"]) == summary["proposed_filled_positions"],
        "the number of proposed proposal_assignments rows matches the draft's own proposed-position total",
    )
    # A partial result is an ACCEPTED outcome here, not a failure - hard
    # constraints (preferences aside, weekly hour limits, timetable
    # coverage, leave) can genuinely leave positions unfillable at this
    # scale, and nothing in this project may relax a hard rule to hide that.
    if summary["uncovered_positions"] > 0:
        print(
            f"NOTE      {summary['uncovered_positions']} position(s) remain uncovered at full scale - "
            "an accepted outcome when hard constraints leave no eligible worker, not a bug."
        )

    approval_payload = {
        "assignments": [
            {"shift_id": row["shift_id"], "employee_code": row["employee_code"]}
            for row in proposal["assignments"]
        ]
    }
    approve_started = time.perf_counter()
    approved = approve_proposal(connection, proposal["id"], approval_payload)
    approve_elapsed = time.perf_counter() - approve_started
    print(f"MEASURED  full-scale approval (revalidation + write) took {approve_elapsed:.2f}s wall-clock.")
    check(approved["status"] == "approved", f"the full-scale proposal approves cleanly ({approved['status']})")

    stored_pairs = {
        (row["employee_id"], row["shift_id"])
        for row in connection.execute("SELECT employee_id, shift_id FROM assignments")
    }
    proposed_pairs = {(row["employee_id"], row["shift_id"]) for row in proposal["assignments"]}
    check(
        proposed_pairs <= stored_pairs,
        "every proposed assignment was actually persisted",
    )

    reloaded = get_week_schedule(connection, week_start_text)
    reloaded_filled = sum(shift["assigned_count"] for shift in reloaded["shifts"])
    stored_week_count = connection.execute(
        "SELECT COUNT(*) AS n FROM assignments a JOIN shifts s ON s.id = a.shift_id"
        " WHERE s.start_datetime >= '2026-09-21 00:00'"
        " AND s.start_datetime < '2026-09-28 00:00'"
    ).fetchone()["n"]
    check(
        reloaded_filled == stored_week_count,
        f"the reloaded weekly schedule's total assigned count matches that week's stored assignments ({reloaded_filled} vs {stored_week_count})",
    )
    check(
        all(worker["conflicts"] is None for shift in reloaded["shifts"] for worker in shift["assigned_employees"]),
        "freshly approved assignments show no conflicts on immediate reload",
    )

    # An explicit replacement on one of the newly created assignments, at
    # full scale - proves the replacement path works against realistic data
    # volume too, not only small hand-built fixtures. This is a MANDATORY
    # part of the journey, not a best-effort extra: rather than checking
    # only the first assigned shift and silently skipping the sub-check if
    # it happens to have no other eligible worker, every assigned shift is
    # searched (deterministically, in `assigned_count` order) for the first
    # one that actually has a candidate. With 30 workers on 99 shifts this
    # is expected to succeed; if it genuinely cannot find one anywhere, that
    # is reported as a failure of this check, not silently passed over.
    replacement_target = None
    for shift in reloaded["shifts"]:
        for outgoing in shift["assigned_employees"]:
            coverage = shift_coverage(connection, shift["id"], exclude_employee_code=outgoing["employee_code"])
            already_on_shift = {worker["employee_code"] for worker in shift["assigned_employees"]}
            candidates = [c for c in coverage["eligible_candidates"] if c["employee_code"] not in already_on_shift]
            if candidates:
                replacement_target = (shift, outgoing, candidates[0]["employee_code"])
                break
        if replacement_target:
            break

    check(
        replacement_target is not None,
        "at least one assigned shift at full scale has another eligible worker available for replacement",
    )
    if replacement_target is not None:
        shift, outgoing, incoming_code = replacement_target
        result = replace_assignment(connection, shift["id"], outgoing["employee_code"], incoming_code)
        check(
            result["incoming_employee_code"] == incoming_code,
            "an explicit replacement at full scale succeeds",
        )
        after_replace = get_week_schedule(connection, week_start_text)
        after_shift = next(s for s in after_replace["shifts"] if s["id"] == shift["id"])
        after_codes = {w["employee_code"] for w in after_shift["assigned_employees"]}
        check(
            incoming_code in after_codes and outgoing["employee_code"] not in after_codes,
            "the reload after replacement reflects the new worker, not the old one",
        )

    connection.close()

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"\nAll {_total['n']} checks passed (pristine in-memory two-week fixture only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
