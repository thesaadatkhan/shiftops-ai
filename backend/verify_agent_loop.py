"""Regression checks for Phase 9 increment 1's bounded agent loop
(`agent_service.py`), driven entirely by `agent_model.ScriptedModelAdapter` -
no network call, no API key, fully deterministic.

Runs entirely against throwaway in-memory databases. The project's own
`backend/shiftops.db` is never opened, read, or modified.

Checks:

 1. The primary call-out scenario end to end: find the outgoing worker,
    find the shift, get ranked eligible candidates, propose a replacement,
    give a final answer - produces exactly one PENDING agent proposal,
    zero writes to `assignments`, and task status `awaiting_approval`.
 2. Malformed model tool-call arguments (unparseable JSON) are fed back to
    the model as a controlled error and do not crash the loop.
 3. An unknown tool name requested by the model is fed back as a
    controlled error and does not crash the loop.
 4. A genuine tool failure (a fabricated/nonexistent shift id) is fed back
    as a controlled error and does not crash the loop.
 5. Ambiguity surfaces as `clarification_required`, not a guess or a crash.
 6. Zero eligible candidates surfaces as `blocked`, with no proposal and no
    assignment written.
 7. Exceeding the step limit stops the loop with a controlled `blocked`
    result (`step_limit_exceeded`) rather than looping forever, and does
    not exceed the configured maximum number of model calls.
 8. Task and proposal state persist and can be recovered by a fresh
    `get_task` call - including across a second `send_message` call that
    continues the same task.
 9. A completely malformed model response (not a `ModelTurn` at all) is
    handled as a controlled `blocked` result, not a crash.
10. A model turn that batches TWO propose_replacement calls together
    produces exactly one proposal - the second is skipped without being
    executed, not silently attempted and refused.
11. A follow-up message to a task that is already `awaiting_approval`
    never starts another model run (the scripted adapter would raise if
    asked for another turn than it has) and returns a controlled `blocked`
    result directing the supervisor to approve or reject the existing
    proposal, still recording the supervisor's message in the transcript.

Run with:  python verify_agent_loop.py
Exits non-zero if any check fails.
"""

import sys
from datetime import datetime

import agent_service
from agent_model import ModelTurn, ScriptedModelAdapter, ToolCallRequest
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


def assignment_count(connection):
    return connection.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"]


# ----------------------------------------------------------------- check 1

connection = fixture_database()
jordan = add_employee(connection, "SW-010", "Jordan Kim")
add_ready_schedule(connection, jordan)
sam = add_employee(connection, "SW-011", "Sam Osei")
add_ready_schedule(connection, sam)
shift_id = add_shift(connection, "Capella", "2026-10-06 22:00", "2026-10-07 03:00")
add_assignment(connection, jordan, shift_id)

turns = [
    ModelTurn(tool_calls=[ToolCallRequest(id="c1", name="find_employee", arguments={"query": "Jordan"})]),
    ModelTurn(tool_calls=[ToolCallRequest(id="c2", name="find_shift", arguments={"hall": "Capella", "date": "2026-10-06"})]),
    ModelTurn(tool_calls=[ToolCallRequest(id="c3", name="get_eligible_candidates", arguments={"shift_id": shift_id, "exclude_employee_code": "SW-010"})]),
    ModelTurn(tool_calls=[ToolCallRequest(id="c4", name="propose_replacement", arguments={
        "shift_id": shift_id, "outgoing_employee_code": "SW-010", "incoming_employee_code": "SW-011",
    })]),
    ModelTurn(message="Proposed replacing Jordan Kim with Sam Osei. Awaiting your approval."),
]
before = assignment_count(connection)
outcome = agent_service.create_task(
    connection, "Jordan called out for tonight's Capella shift. Find a replacement.",
    model_adapter=ScriptedModelAdapter(turns),
)
check(outcome["result"]["kind"] == "proposal", "the primary call-out scenario produces a 'proposal' result")
check(outcome["task"]["status"] == "awaiting_approval", "the task status becomes awaiting_approval")
check(len(outcome["task"]["proposals"]) == 1 and outcome["task"]["proposals"][0]["status"] == "pending", "exactly one pending proposal was created")
check(
    outcome["task"]["proposals"][0]["incoming_employee_code"] == "SW-011"
    and outcome["task"]["proposals"][0]["outgoing_employee_code"] == "SW-010",
    "the proposal names the exact outgoing/incoming workers",
)
check(assignment_count(connection) == before, "the call-out produced NO change to assignments - only a pending proposal")

# ----------------------------------------------------------------- check 2

connection = fixture_database()
turns = [
    ModelTurn(tool_calls=[ToolCallRequest(
        id="c1", name="find_shift", arguments={"hall": "Capella", "date": "2026-10-12"}
    )]),
    ModelTurn(message=(
        "The schedule week starting October 12 has not been prepared. "
        "Prepare it in the Schedule screen, then retry."
    )),
]
outcome = agent_service.create_task(
    connection, "Who is eligible for Capella shifts on October 12?",
    model_adapter=ScriptedModelAdapter(turns),
)
check(outcome["result"]["kind"] == "blocked", "an unprepared week is a blocked result, not an ordinary answer")
check(outcome["task"]["status"] == "blocked", "the task records that schedule data must be prepared")
tool_results = [m for m in outcome["task"]["messages"] if m["role"] == "tool_result"]
check(
    len(tool_results) == 1 and "week_not_prepared" in tool_results[0]["content"],
    "the factual unprepared-week result is preserved in the transcript",
)

# ----------------------------------------------------------------- check 2

connection = fixture_database()
add_shift(connection, "Capella", "2026-10-06 22:00", "2026-10-07 03:00")
bad_json_call = ToolCallRequest(id="c1", name="get_shift_details", arguments=None, raw_arguments="{shift_id: 1")
turns = [
    ModelTurn(tool_calls=[bad_json_call]),
    ModelTurn(message="I could not read that tool call; here is what I know instead."),
]
outcome = agent_service.create_task(connection, "test malformed args", model_adapter=ScriptedModelAdapter(turns))
check(outcome["result"]["kind"] == "answer", "malformed tool-call JSON does not crash the loop")
tool_results = [m for m in outcome["task"]["messages"] if m["role"] == "tool_result"]
check(
    len(tool_results) == 1 and "malformed_arguments" in tool_results[0]["content"],
    "the malformed-arguments error is recorded in the transcript as a controlled result",
)

# ----------------------------------------------------------------- check 3

connection = fixture_database()
turns = [
    ModelTurn(tool_calls=[ToolCallRequest(id="c1", name="delete_everything", arguments={})]),
    ModelTurn(message="That tool does not exist; here is what I can do instead."),
]
outcome = agent_service.create_task(connection, "test unknown tool", model_adapter=ScriptedModelAdapter(turns))
check(outcome["result"]["kind"] == "answer", "an unknown tool name does not crash the loop")
tool_results = [m for m in outcome["task"]["messages"] if m["role"] == "tool_result"]
check(
    len(tool_results) == 1 and "unknown_tool" in tool_results[0]["content"],
    "the unknown-tool error is recorded in the transcript",
)

# ----------------------------------------------------------------- check 4

connection = fixture_database()
turns = [
    ModelTurn(tool_calls=[ToolCallRequest(id="c1", name="get_shift_details", arguments={"shift_id": 999999})]),
    ModelTurn(message="That shift does not exist."),
]
outcome = agent_service.create_task(connection, "test tool failure", model_adapter=ScriptedModelAdapter(turns))
check(outcome["result"]["kind"] == "answer", "a genuine tool failure (fabricated shift id) does not crash the loop")
tool_results = [m for m in outcome["task"]["messages"] if m["role"] == "tool_result"]
check(
    len(tool_results) == 1 and "shift_not_found" in tool_results[0]["content"],
    "the tool failure is recorded in the transcript",
)

# ----------------------------------------------------------------- check 5

connection = fixture_database()
add_employee(connection, "SW-020", "Alex Green")
add_employee(connection, "SW-021", "Alex Brown")
turns = [
    ModelTurn(tool_calls=[ToolCallRequest(id="c1", name="find_employee", arguments={"query": "Alex"})]),
    ModelTurn(message="There are two workers named Alex - which one did you mean?"),
]
before = assignment_count(connection)
outcome = agent_service.create_task(connection, "Alex called out", model_adapter=ScriptedModelAdapter(turns))
check(outcome["result"]["kind"] == "clarification_required", "ambiguity surfaces as clarification_required, not a guess")
check(outcome["task"]["status"] == "open", "an ambiguous task stays open, awaiting clarification")
check(outcome["task"]["proposals"] == [], "no proposal is created while ambiguous")
check(assignment_count(connection) == before, "ambiguity produces no assignment writes")

# ----------------------------------------------------------------- check 6

connection = fixture_database()
inactive = add_employee(connection, "SW-030", "Only Worker", active=False)
shift_id_6 = add_shift(connection, "Vega", "2026-10-06 08:00", "2026-10-06 13:00")
turns = [
    ModelTurn(tool_calls=[ToolCallRequest(id="c1", name="get_eligible_candidates", arguments={"shift_id": shift_id_6})]),
    ModelTurn(message="Nobody is currently eligible to cover this shift."),
]
before = assignment_count(connection)
outcome = agent_service.create_task(connection, "Who can cover Vega tomorrow morning?", model_adapter=ScriptedModelAdapter(turns))
check(outcome["result"]["kind"] == "blocked", "zero eligible candidates surfaces as blocked")
check(outcome["task"]["status"] == "blocked", "the task status becomes blocked")
check(outcome["task"]["proposals"] == [], "no proposal is created when nobody is eligible")
check(assignment_count(connection) == before, "no eligible candidates produces no assignment writes")

# ----------------------------------------------------------------- check 7

connection = fixture_database()
add_shift(connection, "Capella", "2026-10-06 22:00", "2026-10-07 03:00")
max_steps = 3
# Every scripted turn keeps calling a real, harmless tool - never a final
# message - so the loop can ONLY stop via the step-limit path, never by
# the model choosing to stop or the script running out first.
endless_turns = [
    ModelTurn(tool_calls=[ToolCallRequest(id=f"c{i}", name="find_employee", arguments={"query": "nobody"})])
    for i in range(max_steps + 2)  # more turns available than the limit allows
]
adapter = ScriptedModelAdapter(endless_turns)
outcome = agent_service.create_task(
    connection, "loop forever", model_adapter=adapter, max_steps=max_steps
)
check(outcome["result"]["kind"] == "blocked", "exceeding the step limit produces a blocked result")
check(outcome["result"]["blocked_reason"] == "step_limit_exceeded", "with the specific step_limit_exceeded reason")
check(outcome["result"]["steps_used"] == max_steps, f"exactly max_steps ({max_steps}) model calls were made, never more")
check(len(adapter.calls) == max_steps, f"the adapter itself was called exactly {max_steps} times, not more")

# ----------------------------------------------------------------- check 8

connection = fixture_database()
jordan2 = add_employee(connection, "SW-040", "Jordan Kim")
add_ready_schedule(connection, jordan2)
sam2 = add_employee(connection, "SW-041", "Sam Osei")
add_ready_schedule(connection, sam2)
shift_id_8 = add_shift(connection, "Capella", "2026-10-06 22:00", "2026-10-07 03:00")
add_assignment(connection, jordan2, shift_id_8)

first_turns = [
    ModelTurn(tool_calls=[ToolCallRequest(id="c1", name="get_shift_details", arguments={"shift_id": shift_id_8})]),
    ModelTurn(message="Jordan Kim is currently assigned to that shift."),
]
outcome = agent_service.create_task(connection, "Who is on the late Capella shift tonight?", model_adapter=ScriptedModelAdapter(first_turns))
task_id = outcome["task"]["id"]
check(outcome["result"]["kind"] == "answer", "the first message in a multi-turn task answers directly")

recovered = agent_service.get_task(connection, task_id)
check(recovered == outcome["task"], "a fresh get_task() call recovers the exact same task state")

second_turns = [
    ModelTurn(tool_calls=[ToolCallRequest(id="c2", name="get_eligible_candidates", arguments={"shift_id": shift_id_8, "exclude_employee_code": "SW-040"})]),
    ModelTurn(tool_calls=[ToolCallRequest(id="c3", name="propose_replacement", arguments={
        "shift_id": shift_id_8, "outgoing_employee_code": "SW-040", "incoming_employee_code": "SW-041",
    })]),
    ModelTurn(message="Proposed replacing Jordan with Sam."),
]
outcome2 = agent_service.send_message(
    connection, task_id, "Actually, he just called out. Find a replacement.",
    model_adapter=ScriptedModelAdapter(second_turns),
)
check(outcome2["task"]["id"] == task_id, "send_message continues the SAME task, not a new one")
check(outcome2["result"]["kind"] == "proposal", "the continued task can still reach a proposal result")
check(len(outcome2["task"]["proposals"]) == 1, "the proposal is attached to the original task")
check(
    len([m for m in outcome2["task"]["messages"] if m["role"] == "supervisor"]) == 2,
    "the transcript contains both supervisor messages across the two calls",
)

final_recovery = agent_service.get_task(connection, task_id)
check(final_recovery == outcome2["task"], "recovery after the second message matches exactly")

# ----------------------------------------------------------------- check 9

connection = fixture_database()


class _BrokenAdapter:
    def complete(self, messages, tools):
        return "not a ModelTurn at all"


outcome = agent_service.create_task(connection, "test broken adapter", model_adapter=_BrokenAdapter())
check(outcome["result"]["kind"] == "blocked", "a completely malformed model response is a controlled blocked result")
check(outcome["result"]["blocked_reason"] == "malformed_model_output", "with the specific malformed_model_output reason")

# ---------------------------------------------------------------- check 10

connection = fixture_database()
jordan10 = add_employee(connection, "SW-050", "Jordan Kim")
add_ready_schedule(connection, jordan10)
sam10 = add_employee(connection, "SW-051", "Sam Osei")
add_ready_schedule(connection, sam10)
alex10 = add_employee(connection, "SW-052", "Alex Nguyen")
add_ready_schedule(connection, alex10)
shift_id_10 = add_shift(connection, "Capella", "2026-10-06 22:00", "2026-10-07 03:00")
add_assignment(connection, jordan10, shift_id_10)

# One turn, TWO propose_replacement calls batched together - the second
# names a different (also-eligible) worker, so if it were actually
# executed it would be refused for a different reason (proposal_already_
# exists) than if it were simply never run at all (proposal_already_
# created, from the loop's own same-turn guard) - the transcript content
# distinguishes which one actually happened.
turns = [
    ModelTurn(tool_calls=[
        ToolCallRequest(id="c1", name="propose_replacement", arguments={
            "shift_id": shift_id_10, "outgoing_employee_code": "SW-050", "incoming_employee_code": "SW-051",
        }),
        ToolCallRequest(id="c2", name="propose_replacement", arguments={
            "shift_id": shift_id_10, "outgoing_employee_code": "SW-050", "incoming_employee_code": "SW-052",
        }),
    ]),
    ModelTurn(message="Proposed replacing Jordan Kim with Sam Osei."),
]
outcome = agent_service.create_task(
    connection, "Jordan called out for tonight's Capella shift.",
    model_adapter=ScriptedModelAdapter(turns),
)
check(outcome["result"]["kind"] == "proposal", "a turn batching two propose_replacement calls still reaches a proposal result")
check(len(outcome["task"]["proposals"]) == 1, "exactly one proposal is created, never two, from one batched turn")
check(
    outcome["task"]["proposals"][0]["incoming_employee_code"] == "SW-051",
    "the proposal reflects the FIRST call in the batch",
)
tool_results = [m for m in outcome["task"]["messages"] if m["role"] == "tool_result" and m["tool_name"] == "propose_replacement"]
check(len(tool_results) == 2, "both tool calls are recorded in the transcript, even though only one ran")
check(
    "proposal_already_created" in tool_results[1]["content"],
    f"the second call's recorded result shows it was SKIPPED, not executed and refused ({tool_results[1]['content']})",
)

# ---------------------------------------------------------------- check 11

connection = fixture_database()
jordan11 = add_employee(connection, "SW-060", "Jordan Kim")
add_ready_schedule(connection, jordan11)
sam11 = add_employee(connection, "SW-061", "Sam Osei")
add_ready_schedule(connection, sam11)
shift_id_11 = add_shift(connection, "Capella", "2026-10-06 22:00", "2026-10-07 03:00")
add_assignment(connection, jordan11, shift_id_11)

turns = [
    ModelTurn(tool_calls=[ToolCallRequest(id="c1", name="propose_replacement", arguments={
        "shift_id": shift_id_11, "outgoing_employee_code": "SW-060", "incoming_employee_code": "SW-061",
    })]),
    ModelTurn(message="Proposed replacing Jordan Kim with Sam Osei."),
]
outcome = agent_service.create_task(
    connection, "Jordan called out.", model_adapter=ScriptedModelAdapter(turns)
)
task_id_11 = outcome["task"]["id"]
check(outcome["task"]["status"] == "awaiting_approval", "fixture sanity: the task is awaiting_approval before the follow-up")

# The adapter is scripted with ZERO turns - if send_message tried to start
# the model loop at all, ScriptedModelAdapter would raise AssertionError
# rather than silently succeed, so this proves no model call is made.
no_turns_adapter = ScriptedModelAdapter([])
before_messages = len(outcome["task"]["messages"])
follow_up = agent_service.send_message(
    connection, task_id_11, "Also, can you check tomorrow's schedule?",
    model_adapter=no_turns_adapter,
)
check(follow_up["result"]["kind"] == "blocked", "a follow-up message to an awaiting-approval task is a controlled blocked result")
check(follow_up["result"]["blocked_reason"] == "awaiting_approval", "with the specific awaiting_approval reason")
check(
    follow_up["task"]["status"] == "awaiting_approval",
    "the task status is unchanged - still awaiting the ORIGINAL proposal's decision",
)
check(len(follow_up["task"]["proposals"]) == 1, "no second proposal was created")
check(len(no_turns_adapter.calls) == 0, "the model was never called at all for this follow-up")
check(
    len([m for m in follow_up["task"]["messages"] if m["role"] == "supervisor"]) == 2,
    "the supervisor's follow-up message is still recorded in the transcript, alongside the original",
)
check(len(follow_up["task"]["messages"]) == before_messages + 2, "a supervisor row and a controlled assistant notice were appended")


if failures:
    print(f"\n{len(failures)} check(s) failed:")
    for description in failures:
        print(f"  - {description}")
    sys.exit(1)

print("\nAll agent loop checks passed (throwaway in-memory databases, scripted fake model only).")
