"""Regression checks for Phase 9 increment 1's deterministic tool registry
(`agent_tools.py`).

Runs entirely against throwaway in-memory databases with purpose-built
workers, shifts and schedules. The project's own `backend/shiftops.db` is
never opened, read, or modified.

Checks:

 1. Exact worker resolution by code and by full name.
 2. Ambiguous worker resolution (a name substring matching two workers)
    returns every match explicitly, never a guess.
 3. Exact shift resolution by id, and by hall/date/time.
 4. Ambiguous shift resolution (hall/date with no time, matching more than
    one shift that day) returns every match explicitly.
 5. Not-found results for a nonexistent worker/shift.
 6. get_shift_details reports current assignments and coverage accurately.
 7. get_eligible_candidates excludes the outgoing worker and ranks the
    remaining candidates by the documented rule (preferred > neutral > low,
    then lower projected weekly hours, then employee_code).
 8. rank_candidates implements that exact rule standalone (an internal
    function only - NOT exposed as a model-facing tool; calling it BY NAME
    through call_tool is rejected as an unknown tool).
 9. inspect_uncovered_shift reports zero eligible candidates and a
    populated ineligibility reason tally when nobody qualifies.
10. get_employee_hours reports the documented assigned/remaining figures
    for a requested week, and rejects an invalid week_start.
11. propose_replacement computes its rationale ENTIRELY server-side (no
    rationale argument exists at all - passing one is rejected as an
    unexpected argument), requires the incoming worker to be the
    authoritative TOP-RANKED eligible candidate (an eligible-but-lower-
    ranked candidate is refused, naming the actual top candidate; a
    fabricated/ineligible one is refused too), and writes NOTHING to
    `assignments` in either the success or failure case - only a new
    `agent_proposals` row on success.
12. At most one proposal per task: a second propose_replacement call for a
    task that already has one - whether the task's own prior proposal or a
    same-turn duplicate call - is a controlled `proposal_already_exists`,
    never a second row, and the `UNIQUE(task_id)` constraint itself backs
    this up at the schema level.
13. Filling an uncovered shift (no outgoing_employee_code) requires the
    shift to be genuinely under-staffed: an exactly-covered or overstaffed
    shift is refused as `shift_already_covered`, and a genuinely uncovered
    shift with an eligible worker succeeds. The replacement path (a real
    outgoing worker) is unaffected by this check.
14. Malformed/fabricated tool arguments are rejected: wrong argument types,
    an unexpected extra argument, a fabricated (nonexistent) employee code
    or shift id - all `ToolError`s, never a crash and never treated as if
    they resolved to something real.
15. An unknown tool name is a controlled `ToolError`, not a crash.
16. Calling `create_schema` twice against the same database is idempotent -
    the three new agent tables exist exactly once, with no duplicate-object
    errors, matching every other additive migration in this project.
17. A real two-connection concurrency test (temp FILE database, two
    threads racing `propose_replacement` for the SAME task at the same
    instant): exactly one proposal is created; the other attempt gets the
    controlled `proposal_already_exists` refusal, never a raw
    "database is locked" error and never a second row.

Run with:  python verify_agent_tools.py
Exits non-zero if any check fails.
"""

import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import agent_tools
from database import create_schema, get_connection

DATETIME_FORMAT = "%Y-%m-%d %H:%M"

failures = []


def check(condition, description):
    print(f"{'PASS ' if condition else 'FAIL '} {description}")
    if not condition:
        failures.append(description)


def fixture_database():
    connection = get_connection(":memory:")
    create_schema(connection)
    return connection


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


def add_task(connection, request_text="test task"):
    now = datetime.now().strftime(DATETIME_FORMAT)
    connection.execute(
        "INSERT INTO agent_tasks (created_at, updated_at, status, request_text)"
        " VALUES (?, ?, 'open', ?)",
        (now, now, request_text),
    )
    connection.commit()
    return connection.execute("SELECT id FROM agent_tasks ORDER BY id DESC").fetchone()["id"]


def assignment_count(connection):
    return connection.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"]


# --------------------------------------------------------------- checks 1-5

connection = fixture_database()
jordan = add_employee(connection, "SW-010", "Jordan Kim")
add_ready_schedule(connection, jordan)
jordan_two = add_employee(connection, "SW-012", "Jordan Alvarez")
add_ready_schedule(connection, jordan_two)
sam = add_employee(connection, "SW-011", "Sam Osei")
add_ready_schedule(connection, sam)

result = agent_tools.call_tool(connection, "find_employee", {"query": "SW-010"})
check(result["status"] == "found" and result["employee"]["full_name"] == "Jordan Kim", "exact worker resolution by code")

result = agent_tools.call_tool(connection, "find_employee", {"query": "Sam Osei"})
check(result["status"] == "found" and result["employee"]["employee_code"] == "SW-011", "exact worker resolution by full name")

result = agent_tools.call_tool(connection, "find_employee", {"query": "Jordan"})
check(
    result["status"] == "ambiguous" and {m["employee_code"] for m in result["matches"]} == {"SW-010", "SW-012"},
    "ambiguous worker resolution names every match explicitly, not a guess",
)

result = agent_tools.call_tool(connection, "find_employee", {"query": "Nobody Real"})
check(result["status"] == "not_found", "a nonexistent worker is not_found, not fabricated")

shift_a = add_shift(connection, "Capella", "2026-10-06 22:00", "2026-10-07 03:00")
shift_b = add_shift(connection, "Capella", "2026-10-06 08:00", "2026-10-06 13:00")

result = agent_tools.call_tool(connection, "find_shift", {"shift_id": shift_a})
check(result["status"] == "found" and result["shift"]["shift_id"] == shift_a, "exact shift resolution by id")

result = agent_tools.call_tool(connection, "find_shift", {"hall": "Capella", "date": "2026-10-06", "time": "22:00"})
check(result["status"] == "found" and result["shift"]["shift_id"] == shift_a, "exact shift resolution by hall/date/time")

result = agent_tools.call_tool(connection, "find_shift", {"hall": "Capella", "date": "2026-10-06"})
check(
    result["status"] == "ambiguous" and {m["shift_id"] for m in result["matches"]} == {shift_a, shift_b},
    "ambiguous shift resolution (hall/date, no time) names every match explicitly",
)

result = agent_tools.call_tool(connection, "find_shift", {"shift_id": 999999})
check(result["status"] == "not_found", "a nonexistent shift_id is not_found, not fabricated")

result = agent_tools.call_tool(
    connection, "find_shift", {"hall": "Capella", "date": "2026-10-12"}
)
check(
    result["status"] == "week_not_prepared"
    and result["week_start"] == "2026-10-12"
    and "Prepare that week" in result["message"],
    "an unprepared schedule week is distinguished from a genuine missing shift",
)

result = agent_tools.call_tool(
    connection, "find_shift", {"hall": "Vega", "date": "2026-10-06"}
)
check(
    result["status"] == "not_found",
    "a missing hall shift inside a prepared week remains a genuine not_found",
)

try:
    agent_tools.call_tool(
        connection, "find_shift", {"hall": "Capella", "date": "2026-99-99"}
    )
    check(False, "an impossible calendar date is rejected")
except agent_tools.ToolError as error:
    check(error.code == "invalid_arguments", "an impossible calendar date is rejected")

# ----------------------------------------------------------------- check 6

add_assignment(connection, jordan, shift_a)
result = agent_tools.call_tool(connection, "get_shift_details", {"shift_id": shift_a})
check(
    result["assigned_count"] == 1 and result["covered"] is True and result["assigned_employees"][0]["employee_code"] == "SW-010",
    "get_shift_details reports the exact current assignment and coverage",
)

# --------------------------------------------------------------- checks 7-8

result = agent_tools.call_tool(
    connection, "get_eligible_candidates", {"shift_id": shift_a, "exclude_employee_code": "SW-010"}
)
codes = [c["employee_code"] for c in result["eligible_candidates"]]
check("SW-010" not in codes, "the outgoing worker is excluded from eligible candidates")
check(codes == sorted(codes), "eligible candidates come back already ranked (employee_code order here, since both are neutral/equal hours)")

preferred_and_low = [
    {"employee_code": "SW-090", "preference": "low", "projected_weekly_hours": 5},
    {"employee_code": "SW-091", "preference": "preferred", "projected_weekly_hours": 10},
    {"employee_code": "SW-092", "preference": "neutral", "projected_weekly_hours": 3},
    {"employee_code": "SW-093", "preference": "preferred", "projected_weekly_hours": 4},
]
ranked = agent_tools.rank_candidates(preferred_and_low)
check(
    [c["employee_code"] for c in ranked] == ["SW-093", "SW-091", "SW-092", "SW-090"],
    "rank_candidates: preferred before neutral before low, then lower projected hours, then employee_code",
)

try:
    agent_tools.call_tool(connection, "rank_candidates", {"candidates": [1]})
    check(False, "rank_candidates is not exposed as a model-facing tool")
except agent_tools.ToolError as error:
    check(
        error.code == "unknown_tool",
        f"rank_candidates is not exposed as a model-facing tool ({error.code}) - "
        "removing it entirely closes the malformed-nested-candidate exception path "
        "a standalone tool would otherwise need per-field validation to close",
    )

# ----------------------------------------------------------------- check 9

connection2 = fixture_database()
lonely = add_employee(connection2, "SW-020", "Lonely Worker", active=False)
shift_uncovered = add_shift(connection2, "Vega", "2026-10-06 08:00", "2026-10-06 13:00")
result = agent_tools.call_tool(connection2, "inspect_uncovered_shift", {"shift_id": shift_uncovered})
check(result["covered"] is False and result["assigned_count"] == 0, "inspect_uncovered_shift reports zero assigned, not covered")
check(result["eligible_candidates"] == [], "and zero eligible candidates when the only worker is inactive")
check(
    result["ineligibility_reason_tally"].get("worker_inactive", 0) >= 1,
    "and tallies WHY - at least one worker_inactive reason",
)

# ---------------------------------------------------------------- check 10

result = agent_tools.call_tool(connection, "get_employee_hours", {"employee_code": "SW-010", "week_start": "2026-10-05"})
check(
    result["assigned_hours"] == 5 and result["remaining_capacity_hours"] == 15,
    "get_employee_hours reports assigned/remaining hours for the requested week",
)
try:
    agent_tools.call_tool(connection, "get_employee_hours", {"employee_code": "SW-010", "week_start": "not-a-date"})
    check(False, "an invalid week_start is rejected")
except agent_tools.ToolError as error:
    check(error.code == "invalid_week", f"an invalid week_start is rejected ({error.code})")

# ---------------------------------------------------------------- check 11

task_id = add_task(connection)
before = assignment_count(connection)
try:
    agent_tools.call_tool(
        connection,
        "propose_replacement",
        {
            "shift_id": shift_a,
            "outgoing_employee_code": "SW-010",
            "incoming_employee_code": "SW-011",
            "rationale": "Sam is eligible and available.",
        },
        task_id=task_id,
    )
    check(False, "a model-supplied rationale argument is rejected - there is no such parameter any more")
except agent_tools.ToolError as error:
    check(
        error.code == "invalid_arguments",
        f"a model-supplied rationale argument is rejected ({error.code})",
    )

proposal = agent_tools.call_tool(
    connection,
    "propose_replacement",
    {"shift_id": shift_a, "outgoing_employee_code": "SW-010", "incoming_employee_code": "SW-011"},
    task_id=task_id,
)
check(proposal["status"] == "pending", "propose_replacement creates a pending agent proposal")
check(assignment_count(connection) == before, "creating a proposal writes NOTHING to assignments")
check(
    "SW-011" in proposal["rationale"] and "eligible" in proposal["rationale"].lower(),
    f"the stored rationale is computed server-side from deterministic facts ({proposal['rationale']!r})",
)

connection3 = fixture_database()
inactive = add_employee(connection3, "SW-030", "Inactive Worker", active=False)
outgoing = add_employee(connection3, "SW-031", "Outgoing Worker")
add_ready_schedule(connection3, outgoing)
shift_c = add_shift(connection3, "Helix", "2026-10-06 08:00", "2026-10-06 13:00")
add_assignment(connection3, outgoing, shift_c)
task_id_3 = add_task(connection3)
before3 = assignment_count(connection3)
try:
    agent_tools.call_tool(
        connection3,
        "propose_replacement",
        {"shift_id": shift_c, "outgoing_employee_code": "SW-031", "incoming_employee_code": "SW-030"},
        task_id=task_id_3,
    )
    check(False, "proposing an INELIGIBLE incoming worker (inactive) is refused")
except agent_tools.ToolError as error:
    check(error.code == "candidate_ineligible", f"proposing an ineligible incoming worker is refused ({error.code})")
check(assignment_count(connection3) == before3, "a refused proposal writes nothing to assignments")
check(
    connection3.execute("SELECT COUNT(*) AS n FROM agent_proposals").fetchone()["n"] == 0,
    "a refused proposal writes nothing to agent_proposals either",
)

# Eligible but NOT top-ranked: two eligible candidates, one clearly better
# ranked (fewer projected hours) than the requested one.
connection5 = fixture_database()
outgoing5 = add_employee(connection5, "SW-040", "Outgoing Fifth")
add_ready_schedule(connection5, outgoing5)
better = add_employee(connection5, "SW-041", "Better Ranked")  # 0 existing hours -> lower projected hours
add_ready_schedule(connection5, better)
worse = add_employee(connection5, "SW-042", "Worse Ranked")
add_ready_schedule(connection5, worse)
shift_5 = add_shift(connection5, "Sirius", "2026-10-06 08:00", "2026-10-06 13:00")
add_assignment(connection5, outgoing5, shift_5)
# Give `worse` existing hours elsewhere this week so their projected hours
# for shift_5 are higher than `better`'s, making `better` rank first.
other_shift_5 = add_shift(connection5, "Sirius", "2026-10-07 08:00", "2026-10-07 13:00")
add_assignment(connection5, worse, other_shift_5)
task_id_5 = add_task(connection5)
try:
    agent_tools.call_tool(
        connection5,
        "propose_replacement",
        {"shift_id": shift_5, "outgoing_employee_code": "SW-040", "incoming_employee_code": "SW-042"},
        task_id=task_id_5,
    )
    check(False, "proposing an eligible but NOT top-ranked candidate is refused")
except agent_tools.ToolError as error:
    check(
        error.code == "candidate_not_top_ranked" and "SW-041" in error.message,
        f"proposing an eligible but not-top-ranked candidate is refused and names the actual top candidate ({error.code}: {error.message})",
    )
check(
    connection5.execute("SELECT COUNT(*) AS n FROM agent_proposals").fetchone()["n"] == 0,
    "no proposal is written when the requested candidate is not top-ranked",
)
# The actual top-ranked candidate is accepted.
top_proposal = agent_tools.call_tool(
    connection5,
    "propose_replacement",
    {"shift_id": shift_5, "outgoing_employee_code": "SW-040", "incoming_employee_code": "SW-041"},
    task_id=task_id_5,
)
check(top_proposal["incoming_employee_code"] == "SW-041", "the actual top-ranked candidate is accepted")

# ---------------------------------------------------------------- check 12

connection6 = fixture_database()
worker6a = add_employee(connection6, "SW-050", "Worker A")
add_ready_schedule(connection6, worker6a)
worker6b = add_employee(connection6, "SW-051", "Worker B")
add_ready_schedule(connection6, worker6b)
worker6c = add_employee(connection6, "SW-052", "Worker C")
add_ready_schedule(connection6, worker6c)
shift_6 = add_shift(connection6, "Andromeda", "2026-10-06 08:00", "2026-10-06 13:00")
add_assignment(connection6, worker6a, shift_6)
task_id_6 = add_task(connection6)
first_proposal = agent_tools.call_tool(
    connection6,
    "propose_replacement",
    {"shift_id": shift_6, "outgoing_employee_code": "SW-050", "incoming_employee_code": "SW-051"},
    task_id=task_id_6,
)
check(first_proposal["status"] == "pending", "the first proposal for a task succeeds")

try:
    agent_tools.call_tool(
        connection6,
        "propose_replacement",
        {"shift_id": shift_6, "outgoing_employee_code": "SW-051", "incoming_employee_code": "SW-052"},
        task_id=task_id_6,
    )
    check(False, "a second proposal for the SAME task is refused")
except agent_tools.ToolError as error:
    check(
        error.code == "proposal_already_exists",
        f"a second proposal for the same task is refused ({error.code})",
    )
check(
    connection6.execute("SELECT COUNT(*) AS n FROM agent_proposals WHERE task_id = ?", (task_id_6,)).fetchone()["n"] == 1,
    "exactly one agent_proposals row exists for the task after the refused second attempt",
)

# The UNIQUE(task_id) constraint itself is the schema-level backstop -
# bypassing the domain function entirely and inserting directly still fails.
try:
    connection6.execute(
        "INSERT INTO agent_proposals"
        " (task_id, created_at, status, action_type, shift_id, incoming_employee_id, rationale)"
        " VALUES (?, '2026-01-01 00:00', 'pending', 'replace_assignment', ?, ?, 'x')",
        (task_id_6, shift_6, worker6c),
    )
    check(False, "the UNIQUE(task_id) constraint itself refuses a second row for the same task")
except Exception as error:  # noqa: BLE001 - proving the raw constraint fires, whatever its exact type
    check(
        "UNIQUE" in str(error).upper(),
        f"the UNIQUE(task_id) constraint itself refuses a second row for the same task ({error})",
    )

# ---------------------------------------------------------------- check 13

connection7 = fixture_database()
fill_candidate = add_employee(connection7, "SW-060", "Fill Candidate")
add_ready_schedule(connection7, fill_candidate)

uncovered_shift = add_shift(connection7, "Helix", "2026-10-06 08:00", "2026-10-06 13:00", required_staff=2)
task_uncovered = add_task(connection7)
filled = agent_tools.call_tool(
    connection7,
    "propose_replacement",
    {"shift_id": uncovered_shift, "incoming_employee_code": "SW-060"},
    task_id=task_uncovered,
)
check(
    filled["status"] == "pending" and filled["outgoing_employee_code"] is None,
    "filling a genuinely uncovered shift (0 of 2 required) succeeds with no outgoing worker",
)

exactly_covered_shift = add_shift(connection7, "Helix", "2026-10-06 14:00", "2026-10-06 19:00", required_staff=1)
add_assignment(connection7, fill_candidate, exactly_covered_shift)
task_covered = add_task(connection7)
try:
    agent_tools.call_tool(
        connection7,
        "propose_replacement",
        {"shift_id": exactly_covered_shift, "incoming_employee_code": "SW-060"},
        task_id=task_covered,
    )
    check(False, "filling an EXACTLY covered shift (1 of 1 required) is refused")
except agent_tools.ToolError as error:
    check(
        error.code == "shift_already_covered",
        f"filling an exactly covered shift is refused ({error.code})",
    )

overstaffed_worker = add_employee(connection7, "SW-061", "Second Worker")
add_ready_schedule(connection7, overstaffed_worker)
overstaffed_shift = add_shift(connection7, "Helix", "2026-10-06 20:00", "2026-10-07 01:00", required_staff=1)
add_assignment(connection7, fill_candidate, overstaffed_shift)
add_assignment(connection7, overstaffed_worker, overstaffed_shift)
task_overstaffed = add_task(connection7)
try:
    agent_tools.call_tool(
        connection7,
        "propose_replacement",
        {"shift_id": overstaffed_shift, "incoming_employee_code": "SW-060"},
        task_id=task_overstaffed,
    )
    check(False, "filling an OVERSTAFFED shift (2 of 1 required) is refused")
except agent_tools.ToolError as error:
    check(
        error.code == "shift_already_covered",
        f"filling an overstaffed shift is refused ({error.code})",
    )
check(
    connection7.execute("SELECT COUNT(*) AS n FROM agent_proposals").fetchone()["n"] == 1,
    "only the genuinely-uncovered fill produced a proposal; the covered/overstaffed attempts wrote nothing",
)

# The replacement path (a real outgoing worker) is unaffected by the
# uncovered-shift staffing check, even on an already-fully-staffed shift.
task_replace_on_covered = add_task(connection7)
replace_on_covered = agent_tools.call_tool(
    connection7,
    "propose_replacement",
    {
        "shift_id": exactly_covered_shift,
        "outgoing_employee_code": "SW-060",
        "incoming_employee_code": "SW-061",
    },
    task_id=task_replace_on_covered,
)
check(
    replace_on_covered["status"] == "pending",
    "the replacement path still works on an already-fully-staffed shift, unaffected by the fill-only check",
)

# ---------------------------------------------------------------- check 14

try:
    agent_tools.call_tool(connection, "get_shift_details", {"shift_id": "not-an-int"})
    check(False, "a wrong-typed argument is rejected")
except agent_tools.ToolError as error:
    check(error.code == "invalid_arguments", f"a wrong-typed argument is rejected ({error.code})")

try:
    agent_tools.call_tool(connection, "get_shift_details", {"shift_id": shift_a, "unexpected_field": "x"})
    check(False, "an unexpected extra argument is rejected")
except agent_tools.ToolError as error:
    check(error.code == "invalid_arguments", f"an unexpected extra argument is rejected ({error.code})")

try:
    agent_tools.call_tool(connection, "get_employee_hours", {"employee_code": "SW-999", "week_start": "2026-10-05"})
    check(False, "a fabricated (nonexistent) employee code is rejected, not treated as real")
except agent_tools.ToolError as error:
    check(error.code == "employee_not_found", f"a fabricated employee code is rejected ({error.code})")

try:
    agent_tools.call_tool(connection, "get_eligible_candidates", {"shift_id": 987654})
    check(False, "a fabricated (nonexistent) shift_id is rejected, not treated as real")
except agent_tools.ToolError as error:
    check(error.code == "shift_not_found", f"a fabricated shift_id is rejected ({error.code})")

try:
    agent_tools.call_tool(connection, "find_employee", {})
    check(False, "a missing required argument is rejected")
except agent_tools.ToolError as error:
    check(error.code == "invalid_arguments", f"a missing required argument is rejected ({error.code})")

# ---------------------------------------------------------------- check 15

try:
    agent_tools.call_tool(connection, "delete_everything", {})
    check(False, "an unknown tool name is rejected")
except agent_tools.ToolError as error:
    check(error.code == "unknown_tool", f"an unknown tool name is rejected ({error.code})")

# ---------------------------------------------------------------- check 16

connection4 = get_connection(":memory:")
applied_first = create_schema(connection4)
applied_second = create_schema(connection4)
for table in ("agent_tasks", "agent_messages", "agent_proposals"):
    check(
        connection4.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0,
        f"{table} exists and is queryable after two create_schema() calls",
    )
check(True, f"create_schema is idempotent for the new agent tables (first applied={len(applied_first)}, second applied={len(applied_second)})")

# ---------------------------------------------------------------- check 17
#
# A real two-connection concurrency test - `:memory:` databases are each a
# separate, unshared database per connection, so this needs a real temp
# FILE database, exactly like verify_code_allocation.py's own concurrent-
# creates check. Two threads call propose_replacement for the SAME task at
# the same instant (synchronized with a Barrier); BEGIN IMMEDIATE's lock
# ordering means the second one BLOCKS until the first's transaction ends,
# then sees the first's proposal already there - never a raw
# "database is locked" error and never two proposals.

with tempfile.TemporaryDirectory() as folder:
    concurrent_path = Path(folder) / "concurrent-proposals.db"
    setup_connection = get_connection(concurrent_path)
    create_schema(setup_connection)
    outgoing_c = add_employee(setup_connection, "SW-070", "Outgoing Concurrent")
    add_ready_schedule(setup_connection, outgoing_c)
    # Deliberately the ONLY eligible candidate for this shift - both threads
    # request the SAME (unambiguously top-ranked) worker, so ranking can
    # never be what distinguishes the two outcomes. Only the "does a
    # proposal already exist for this task" check can, which is exactly the
    # concurrency behavior this test targets.
    sole_candidate = add_employee(setup_connection, "SW-071", "Sole Candidate")
    add_ready_schedule(setup_connection, sole_candidate)
    shift_concurrent = add_shift(setup_connection, "Vega", "2026-10-06 08:00", "2026-10-06 13:00")
    add_assignment(setup_connection, outgoing_c, shift_concurrent)
    shared_task_id = add_task(setup_connection, "concurrent proposal attempt")
    setup_connection.close()

    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def attempt(incoming_code):
        own_connection = get_connection(concurrent_path)
        try:
            barrier.wait()
            try:
                result = agent_tools.call_tool(
                    own_connection,
                    "propose_replacement",
                    {
                        "shift_id": shift_concurrent,
                        "outgoing_employee_code": "SW-070",
                        "incoming_employee_code": incoming_code,
                    },
                    task_id=shared_task_id,
                )
                with lock:
                    outcomes.append(("success", result["incoming_employee_code"]))
            except agent_tools.ToolError as error:
                with lock:
                    outcomes.append(("tool_error", error.code))
        finally:
            own_connection.close()

    threads = [
        threading.Thread(target=attempt, args=("SW-071",)),
        threading.Thread(target=attempt, args=("SW-071",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    check(len(outcomes) == 2, f"both concurrent attempts finished (no hang, no uncaught exception) ({outcomes})")
    successes = [o for o in outcomes if o[0] == "success"]
    refusals = [o for o in outcomes if o[0] == "tool_error"]
    check(len(successes) == 1, f"exactly one of the two concurrent attempts succeeded ({outcomes})")
    check(
        len(refusals) == 1 and refusals[0][1] == "proposal_already_exists",
        f"the other attempt got the controlled proposal_already_exists refusal, never a raw locked-database error ({outcomes})",
    )

    verify_connection = get_connection(concurrent_path)
    check(
        verify_connection.execute(
            "SELECT COUNT(*) AS n FROM agent_proposals WHERE task_id = ?", (shared_task_id,)
        ).fetchone()["n"] == 1,
        "exactly one agent_proposals row exists for the task after both concurrent attempts",
    )
    verify_connection.close()


if failures:
    print(f"\n{len(failures)} check(s) failed:")
    for description in failures:
        print(f"  - {description}")
    sys.exit(1)

print("\nAll agent tool checks passed (throwaway in-memory databases only).")
