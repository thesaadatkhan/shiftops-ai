"""Task/message/proposal persistence and the bounded agent loop (Phase 9
increment 1).

**The write boundary, stated once and enforced by construction:** the only
tool that writes anything at all is `agent_tools.propose_replacement`, and
it writes only a `agent_proposals` row - never `assignments`,
`approved_leave`, or `employees`. There is no tool in the registry that can
approve or execute a proposal; that is a later, separate supervisor action
this increment does not implement at all. A call-out statement alone can
therefore never remove an assignment, create leave, or change a worker,
regardless of what the model asks for - the capability simply does not
exist in this process.

**The loop is bounded and every step is validated.** `run_loop` calls the
model adapter at most `max_steps` times. Every tool call the model requests
is validated by `agent_tools.call_tool` (unknown tool, malformed/fabricated
arguments, a record that does not exist, an ineligible candidate) before it
can do anything, and every outcome - success or failure - is fed back to
the model as a `tool_result` message and persisted to `agent_messages`.
Exhausting `max_steps` without a final answer is a controlled `blocked`
result, never a silent loop or a crash.

**The response kind is derived from backend facts, never from the model's
own words.** Whether a reply is an `answer`, needs `clarification`, is
`blocked`, or carries a `proposal` is decided by what the tools actually
returned during this run (an ambiguous lookup, zero eligible candidates, a
created proposal) - the model's prose is not trusted to self-report which
case applies.

**The persisted transcript is factual, not chain-of-thought.** Every stored
`agent_messages` row is either the supervisor's own words, the model's
visible reply text, an exact tool call the model requested, or that tool's
exact result. Nothing here stores or fabricates the model's internal
reasoning.

**At most one proposal per task, and no new model run once one exists.**
`agent_tools.propose_replacement` itself refuses a second proposal for the
same task; `run_loop` additionally stops executing any FURTHER tool calls
in the same model turn once one has been created (a model that batches two
`propose_replacement` calls together only ever gets one proposal); and
`send_message` refuses to start the model loop again at all once a task is
`awaiting_approval`, returning a controlled response that directs the
supervisor to approve or reject the existing proposal instead.

**No orphan tasks on missing configuration.** `create_task` resolves AI
configuration BEFORE writing anything when the caller wants the real
provider - a missing `OPENAI_API_KEY` leaves zero new rows. See
`create_task`'s own docstring.
"""

import json
from datetime import datetime

import agent_tools
from agent_model import ModelTurn, OpenAIModelAdapter
from ai_config import resolve_ai_config
from synthetic_data import TIME_FORMAT

TIMESTAMP_FORMAT = TIME_FORMAT
DEFAULT_MAX_STEPS = 8


class AgentTaskNotFound(LookupError):
    """No such agent task. Maps to HTTP 404."""


class AgentValidationError(ValueError):
    """The submitted request is malformed. Maps to HTTP 400."""


def _now(reference_time=None):
    return (reference_time or datetime.now()).strftime(TIMESTAMP_FORMAT)


def _validate_text(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise AgentValidationError(f"'{field_name}' must be a non-empty string.")
    return value.strip()


def _next_sequence(connection, task_id):
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS n FROM agent_messages WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return row["n"] + 1


def _append_message(connection, task_id, role, content, tool_name=None, reference_time=None):
    # Committed immediately, not batched until the loop finishes - so a
    # crash or a process kill mid-loop leaves an accurate, resumable
    # transcript of exactly what happened up to that point, matching this
    # module's own documented claim. (`connection.execute` alone is not
    # enough: sqlite3's Python driver stays in an open transaction until
    # `commit()` is called, and the request handler closes this connection
    # as soon as the response is built - an uncommitted write would be
    # silently lost the moment that happens.)
    #
    # BEGIN IMMEDIATE wraps the sequence-number read AND the insert in one
    # write-locked transaction (Codex review: reading MAX(sequence) and
    # inserting it as separate, unlocked statements let two concurrent
    # messages for the SAME task compute the same next sequence number and
    # then both try to insert it, hitting the `UNIQUE(task_id, sequence)`
    # constraint as an uncontrolled `IntegrityError`). The write lock is
    # taken before the read, the same ordering `migrate_schema` and every
    # other write path in this project already uses for exactly this class
    # of race - a concurrent second caller simply serializes behind the
    # first and then computes a genuinely fresh sequence number.
    previous_isolation = connection.isolation_level
    connection.isolation_level = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            sequence = _next_sequence(connection, task_id)
            connection.execute(
                "INSERT INTO agent_messages (task_id, created_at, sequence, role, tool_name, content)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, _now(reference_time), sequence, role, tool_name, content),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.isolation_level = previous_isolation


def _message_payload(row):
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "sequence": row["sequence"],
        "role": row["role"],
        "tool_name": row["tool_name"],
        "content": row["content"],
    }


def task_payload(connection, task_id):
    """The stored task exactly as persisted: its status, full transcript in
    order, and every proposal it has produced. Read-only."""
    task = connection.execute(
        "SELECT id, created_at, updated_at, status, request_text, step_count"
        " FROM agent_tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if task is None:
        raise AgentTaskNotFound(f"No agent task {task_id}.")

    messages = connection.execute(
        "SELECT id, created_at, sequence, role, tool_name, content"
        " FROM agent_messages WHERE task_id = ? ORDER BY sequence",
        (task_id,),
    ).fetchall()

    proposal_ids = [
        row["id"]
        for row in connection.execute(
            "SELECT id FROM agent_proposals WHERE task_id = ? ORDER BY id", (task_id,)
        )
    ]

    return {
        "id": task["id"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "status": task["status"],
        "request_text": task["request_text"],
        "step_count": task["step_count"],
        "messages": [_message_payload(row) for row in messages],
        "proposals": [
            agent_tools.proposal_payload(connection, proposal_id)
            for proposal_id in proposal_ids
        ],
    }


def _load_prior_conversation(connection, task_id):
    """The supervisor/assistant TEXT turns so far, in OpenAI wire format,
    for CONTINUING a task across separate `send_message` calls.

    Deliberately excludes prior `tool_call`/`tool_result` rows: those
    already accomplished their effect (a fact was read, or a proposal was
    created) and are fully visible in `task_payload`'s transcript, but
    replaying their exact tool-call wire structure back into a NEW request
    is not needed for the model to continue the conversation - it can
    always call a tool again if it needs a fresh fact. This keeps the
    provider-independent loop simple without losing any stored history.
    """
    rows = connection.execute(
        "SELECT role, content FROM agent_messages"
        " WHERE task_id = ? AND role IN ('supervisor', 'assistant') ORDER BY sequence",
        (task_id,),
    ).fetchall()
    return [
        {"role": "user" if row["role"] == "supervisor" else "assistant", "content": row["content"]}
        for row in rows
    ]


def _update_flags(tool_name, result, flags):
    if not isinstance(result, dict):
        return
    if result.get("status") == "ambiguous":
        flags["ambiguous"] = True
    if tool_name == "get_eligible_candidates" and result.get("eligible_count") == 0:
        flags["no_candidates"] = True
    if tool_name == "inspect_uncovered_shift" and result.get("eligible_candidates") == []:
        flags["no_candidates"] = True


def run_loop(connection, task_id, model_adapter=None, max_steps=DEFAULT_MAX_STEPS):
    """Run the bounded tool-calling loop forward for one task, from its
    current stored state, until the model gives a final answer, a proposal
    is created, or `max_steps` model calls have been made.

    Every step's tool calls and results are persisted to `agent_messages`
    as they happen - not batched at the end - so a crash mid-loop still
    leaves an accurate, resumable record of what actually happened.
    """
    adapter = model_adapter or OpenAIModelAdapter()
    messages = _load_prior_conversation(connection, task_id)
    flags = {"ambiguous": False, "no_candidates": False}
    created_proposal = None
    steps_used = 0
    final_message = None
    blocked_reason = None

    for _ in range(max_steps):
        steps_used += 1
        turn = adapter.complete(messages, agent_tools.TOOL_SPECS)

        if not isinstance(turn, ModelTurn):
            blocked_reason = "malformed_model_output"
            final_message = "The AI provider returned a response this application could not understand."
            _append_message(connection, task_id, "assistant", final_message)
            break

        if not turn.tool_calls and not turn.message:
            blocked_reason = "empty_model_output"
            final_message = "The AI provider returned an empty response with no answer and no tool call."
            _append_message(connection, task_id, "assistant", final_message)
            break

        if turn.tool_calls:
            wire_tool_calls = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments or json.dumps(call.arguments or {}),
                    },
                }
                for call in turn.tool_calls
            ]
            messages.append(
                {"role": "assistant", "content": turn.message, "tool_calls": wire_tool_calls}
            )
            if turn.message:
                _append_message(connection, task_id, "assistant", turn.message)

            for call in turn.tool_calls:
                _append_message(
                    connection,
                    task_id,
                    "tool_call",
                    json.dumps({"id": call.id, "name": call.name, "arguments": call.arguments}),
                    tool_name=call.name,
                )
                if created_proposal is not None:
                    # A proposal was already created earlier in THIS SAME
                    # turn (the model batched more than one tool call
                    # together) - stop processing immediately rather than
                    # executing anything further. `propose_replacement`'s
                    # own domain check would refuse a second proposal
                    # attempt anyway, but a non-proposal call here (say,
                    # another lookup) is skipped too: once a proposal
                    # exists, this run is done investigating.
                    result = {
                        "error": "proposal_already_created",
                        "message": (
                            "A proposal was already created earlier in this turn; "
                            "no further tool calls are processed."
                        ),
                    }
                elif call.arguments is None:
                    result = {
                        "error": "malformed_arguments",
                        "message": (
                            f"Could not parse JSON arguments for tool '{call.name}': "
                            f"{call.raw_arguments!r}"
                        ),
                    }
                else:
                    try:
                        result = agent_tools.call_tool(
                            connection, call.name, call.arguments, task_id=task_id
                        )
                        _update_flags(call.name, result, flags)
                        if call.name == "propose_replacement":
                            created_proposal = result
                    except agent_tools.ToolError as error:
                        result = {"error": error.code, "message": error.message}

                _append_message(
                    connection, task_id, "tool_result", json.dumps(result), tool_name=call.name
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
                )
            continue

        # No tool calls: a final, visible answer.
        final_message = turn.message
        _append_message(connection, task_id, "assistant", final_message)
        break
    else:
        blocked_reason = "step_limit_exceeded"
        final_message = (
            f"This task reached its maximum of {max_steps} tool-calling steps "
            "without reaching a final answer. Send another message to continue, "
            "or rephrase the request."
        )
        _append_message(connection, task_id, "assistant", final_message)

    if created_proposal is not None:
        kind = "proposal"
        status = "awaiting_approval"
    elif blocked_reason is not None:
        kind = "blocked"
        status = "blocked"
    elif flags["no_candidates"]:
        kind = "blocked"
        status = "blocked"
    elif flags["ambiguous"]:
        kind = "clarification_required"
        status = "open"
    else:
        kind = "answer"
        status = "open"

    connection.execute(
        "UPDATE agent_tasks SET status = ?, updated_at = ?, step_count = step_count + ? WHERE id = ?",
        (status, _now(), steps_used, task_id),
    )
    connection.commit()

    return {
        "kind": kind,
        "message": final_message,
        "proposal": created_proposal,
        "steps_used": steps_used,
        "blocked_reason": blocked_reason,
    }


def create_task(connection, request_text, model_adapter=None, max_steps=DEFAULT_MAX_STEPS):
    """Start a new agent task with the supervisor's opening message, then
    run the bounded loop forward once.

    **No orphan task on missing configuration.** When `model_adapter` is
    `None` (the caller wants the real provider), `resolve_ai_config()` is
    called HERE, before anything is written - a missing/blank
    `OPENAI_API_KEY` raises `AIConfigurationError` with zero new rows in
    either `agent_tasks` or `agent_messages` (Codex review: the previous
    version only discovered a missing key once `run_loop` made its first
    model call, by which point the task row and opening message were
    already committed, leaving a permanently stuck, unreachable-by-the-UI
    task behind). A caller that passes an explicit `model_adapter` (every
    test, via `ScriptedModelAdapter`) skips this check entirely and never
    needs a key.

    A provider failure that happens LATER - after the task already exists
    and the loop has genuinely started - is a different case, handled
    inside `run_loop`/`OpenAIModelAdapter.complete()`: that task's state is
    intentionally preserved (it is a real, resumable task, not an orphan),
    and the error message never includes the provider's raw exception text
    (see `agent_model.OpenAIModelAdapter.complete`).
    """
    text = _validate_text(request_text, "message")
    if model_adapter is None:
        resolve_ai_config()
        model_adapter = OpenAIModelAdapter()

    now = _now()
    cursor = connection.execute(
        "INSERT INTO agent_tasks (created_at, updated_at, status, request_text)"
        " VALUES (?, ?, 'open', ?)",
        (now, now, text),
    )
    connection.commit()
    task_id = cursor.lastrowid
    _append_message(connection, task_id, "supervisor", text)

    result = run_loop(connection, task_id, model_adapter=model_adapter, max_steps=max_steps)
    return {"task": task_payload(connection, task_id), "result": result}


def send_message(connection, task_id, message_text, model_adapter=None, max_steps=DEFAULT_MAX_STEPS):
    """Append another supervisor message to an existing task and run the
    bounded loop forward again from its current state.

    **A task `awaiting_approval` never starts another model run.** Its one
    allowed proposal already exists and is unresolved; running the loop
    again could only ever be refused by `propose_replacement`'s own
    `proposal_already_exists` check if the model tried to propose again, but
    letting the model spend a whole run just to be refused - or to answer
    some unrelated question while a decision is pending - is not useful
    supervisor UX either. The supervisor's message is still recorded (they
    really did say it), but the response directs them to approve or reject
    the existing proposal instead of calling the model at all.
    """
    text = _validate_text(message_text, "message")

    existing = connection.execute(
        "SELECT id, status FROM agent_tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if existing is None:
        raise AgentTaskNotFound(f"No agent task {task_id}.")

    _append_message(connection, task_id, "supervisor", text)

    if existing["status"] == "awaiting_approval":
        pending = connection.execute(
            "SELECT id FROM agent_proposals WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        notice = (
            "This task already has a proposal awaiting your approval or rejection. "
            "Approve or reject it before sending further instructions - a new "
            "investigation cannot start until then."
        )
        _append_message(connection, task_id, "assistant", notice)
        result = {
            "kind": "blocked",
            "message": notice,
            "proposal": agent_tools.proposal_payload(connection, pending["id"]) if pending else None,
            "steps_used": 0,
            "blocked_reason": "awaiting_approval",
        }
        return {"task": task_payload(connection, task_id), "result": result}

    result = run_loop(connection, task_id, model_adapter=model_adapter, max_steps=max_steps)
    return {"task": task_payload(connection, task_id), "result": result}


def get_task(connection, task_id):
    """Read-only: the task exactly as persisted."""
    return task_payload(connection, task_id)
