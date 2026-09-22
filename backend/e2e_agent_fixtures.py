"""AI Assistant (Phase 9) seed data and a deterministic, rule-based fake
model for the isolated browser end-to-end harness (`e2e_server.py`,
extended for increment 3).

Never a real network call and never `OPENAI_API_KEY` - `E2EAgentModel`
below is a plain Python object implementing the same `.complete(messages,
tools)` interface `agent_model.OpenAIModelAdapter`/`ScriptedModelAdapter`
do, driven entirely by matching the supervisor's own chat text against a
fixed table of canned tool-calling scripts. It never calls a provider and
never reads `OPENAI_API_KEY`.

**Isolation trick, used everywhere below:** every "action" scenario (one
that calls `get_eligible_candidates`/`propose_replacement`) gets its own
shift dated at least a week apart from every other such scenario, AND its
own dedicated worker(s) have a semester schedule window covering ONLY the
one or two calendar dates their own shift actually touches - never a wide
shared window. A wide shared window was tried first and was wrong: two
scenarios' workers both being "eligible, active, and free" for a date
either one's shift touches makes `propose_replacement`'s top-ranked-only
rule fail unpredictably depending on employee_code alphabetical order
across scenarios that should know nothing about each other. With
one-or-two-day windows and a week of spacing between scenario dates, no
worker from one scenario can ever be eligible for another scenario's
shift, and this is true regardless of how many scenarios exist or what
order they run in.
"""

from agent_model import ModelTurn, ToolCallRequest


def _add_worker(connection, code, name, start, end, weekly_hour_limit=20):
    connection.execute(
        "INSERT INTO employees (employee_code, full_name, student_type,"
        " weekly_hour_limit, is_active) VALUES (?, ?, 'undergraduate', ?, 1)",
        (code, name, weekly_hour_limit),
    )
    employee_id = connection.execute(
        "SELECT id FROM employees WHERE employee_code = ?", (code,)
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO semester_schedules"
        " (employee_id, start_date, end_date, confirmed_at, dates_provisional)"
        " VALUES (?, ?, ?, '2026-01-01 00:00', 0)",
        (employee_id, start, end),
    )
    return employee_id


def _add_shift(connection, hall, start_datetime, end_datetime, required_staff=1):
    connection.execute(
        "INSERT INTO shifts (hall, start_datetime, end_datetime, required_staff)"
        " VALUES (?, ?, ?, ?)",
        (hall, start_datetime, end_datetime, required_staff),
    )
    return connection.execute(
        "SELECT id FROM shifts WHERE hall = ? AND start_datetime = ?",
        (hall, start_datetime),
    ).fetchone()["id"]


def _add_assignment(connection, employee_id, shift_id):
    connection.execute(
        "INSERT INTO assignments (employee_id, shift_id) VALUES (?, ?)",
        (employee_id, shift_id),
    )


def seed(connection):
    """Insert every AI Assistant browser-journey fixture and return a dict
    of scenario data (`shift_id`s and employee codes) the fake model and
    the Playwright driver both need to reference exact records."""
    scenarios = {}

    # ---- informational question, also the "model text says 'approved'
    # cannot execute anything" trap: the assistant's own final reply
    # includes the word "approved" as ordinary prose, with no proposal ever
    # created and no tool capable of approving anything.
    _add_worker(connection, "SW-701", "Taylor Brooks", start="2026-08-24", end="2026-12-11")
    scenarios["info"] = {"employee_code": "SW-701", "full_name": "Taylor Brooks"}

    # ---- ambiguity -> supervisor clarification. Two workers sharing a
    # first name not used anywhere else in this fixture set.
    _add_worker(connection, "SW-702", "Blaine Ortiz", start="2026-08-24", end="2026-12-11")
    _add_worker(connection, "SW-703", "Blaine Sato", start="2026-08-24", end="2026-12-11")
    scenarios["ambiguous"] = {
        "query": "Blaine",
        "resolved_code": "SW-703",
        "resolved_name": "Blaine Sato",
    }

    # ---- call-out -> exactly one pending replacement proposal. Also reused
    # for: successful approval + verification, and refresh/navigation
    # recovery (the browser driver refreshes BEFORE approving this one).
    # Cross-midnight, so the worker window must cover BOTH calendar dates.
    out_id = _add_worker(connection, "SW-704", "Jordan Rivera", start="2027-01-11", end="2027-01-12")
    in_id = _add_worker(connection, "SW-705", "Sam Osei", start="2027-01-11", end="2027-01-12")
    shift_id = _add_shift(connection, "Capella", "2027-01-11 22:00", "2027-01-12 03:00")
    _add_assignment(connection, out_id, shift_id)
    scenarios["callout"] = {
        "shift_id": shift_id,
        "hall": "Capella",
        "date": "2027-01-11",
        "outgoing_code": "SW-704",
        "outgoing_name": "Jordan Rivera",
        "incoming_code": "SW-705",
        "incoming_name": "Sam Osei",
    }

    # ---- explicit rejection changes no assignment. A week after the
    # call-out scenario's date, and its own narrow window, so the worker
    # this approves onto the callout shift can never compete here.
    out_id = _add_worker(connection, "SW-706", "Devon Cole", start="2027-01-18", end="2027-01-18")
    in_id = _add_worker(connection, "SW-707", "Casey Lin", start="2027-01-18", end="2027-01-18")
    shift_id = _add_shift(connection, "Vega", "2027-01-18 08:00", "2027-01-18 13:00")
    _add_assignment(connection, out_id, shift_id)
    scenarios["reject"] = {
        "shift_id": shift_id,
        "hall": "Vega",
        "date": "2027-01-18",
        "outgoing_code": "SW-706",
        "outgoing_name": "Devon Cole",
        "incoming_code": "SW-707",
        "incoming_name": "Casey Lin",
    }

    # ---- no eligible candidates: the sole worker whose schedule covers
    # this date holds the shift already; nobody else has a schedule
    # covering this specific week at all.
    out_id = _add_worker(connection, "SW-708", "Morgan Reyes", start="2027-01-25", end="2027-01-25")
    shift_id = _add_shift(connection, "Helix", "2027-01-25 08:00", "2027-01-25 13:00")
    _add_assignment(connection, out_id, shift_id)
    scenarios["no_candidates"] = {
        "shift_id": shift_id,
        "hall": "Helix",
        "date": "2027-01-25",
        "outgoing_code": "SW-708",
        "outgoing_name": "Morgan Reyes",
    }

    # ---- stale approval: a real proposal, then the incoming worker is
    # deactivated (via the real deactivate endpoint) before the browser
    # confirms approval - a genuine, backend-verified 409 conflict.
    out_id = _add_worker(connection, "SW-709", "Riley Chen", start="2027-02-01", end="2027-02-02")
    in_id = _add_worker(connection, "SW-710", "Avery Kim", start="2027-02-01", end="2027-02-02")
    shift_id = _add_shift(connection, "Sirius", "2027-02-01 22:00", "2027-02-02 03:00")
    _add_assignment(connection, out_id, shift_id)
    scenarios["stale_conflict"] = {
        "shift_id": shift_id,
        "hall": "Sirius",
        "date": "2027-02-01",
        "outgoing_code": "SW-709",
        "outgoing_name": "Riley Chen",
        "incoming_code": "SW-710",
        "incoming_name": "Avery Kim",
    }

    # ---- duplicate-click guard on Approve.
    out_id = _add_worker(connection, "SW-711", "Jamie Fox", start="2027-02-08", end="2027-02-09")
    in_id = _add_worker(connection, "SW-712", "Drew Park", start="2027-02-08", end="2027-02-09")
    shift_id = _add_shift(connection, "Andromeda", "2027-02-08 22:00", "2027-02-09 03:00")
    _add_assignment(connection, out_id, shift_id)
    scenarios["duplicate_click"] = {
        "shift_id": shift_id,
        "hall": "Andromeda",
        "date": "2027-02-08",
        "outgoing_code": "SW-711",
        "outgoing_name": "Jamie Fox",
        "incoming_code": "SW-712",
        "incoming_name": "Drew Park",
    }

    # ---- network-unknown approval outcome, reconciled via GET.
    out_id = _add_worker(connection, "SW-713", "Quinn Adams", start="2027-02-15", end="2027-02-16")
    in_id = _add_worker(connection, "SW-714", "Harper Diaz", start="2027-02-15", end="2027-02-16")
    shift_id = _add_shift(connection, "Capella", "2027-02-15 22:00", "2027-02-16 03:00")
    _add_assignment(connection, out_id, shift_id)
    scenarios["network_unknown"] = {
        "shift_id": shift_id,
        "hall": "Capella",
        "date": "2027-02-15",
        "outgoing_code": "SW-713",
        "outgoing_name": "Quinn Adams",
        "incoming_code": "SW-714",
        "incoming_name": "Harper Diaz",
    }

    # ---- applied-but-verification-failed display (response mocked after a
    # real commit - see the driver for why this cannot be produced by the
    # real backend alone without deliberately breaking it).
    out_id = _add_worker(connection, "SW-715", "Skyler Moss", start="2027-02-22", end="2027-02-22")
    in_id = _add_worker(connection, "SW-716", "Rowan Vega", start="2027-02-22", end="2027-02-22")
    shift_id = _add_shift(connection, "Vega", "2027-02-22 08:00", "2027-02-22 13:00")
    _add_assignment(connection, out_id, shift_id)
    scenarios["verification_failed"] = {
        "shift_id": shift_id,
        "hall": "Vega",
        "date": "2027-02-22",
        "outgoing_code": "SW-715",
        "outgoing_name": "Skyler Moss",
        "incoming_code": "SW-716",
        "incoming_name": "Rowan Vega",
    }

    # ---- applied-but-unverified reconcile: a real commit + real
    # verification happens on the first approval, exactly like every other
    # scenario - the driver corrupts only the FIRST response's delivered
    # verification_outcome/task_status to display the recovery-gap state,
    # then clicks Reconcile, which issues a genuine second approval request
    # that the real backend answers through its idempotent branch (the
    # already-true stored 'verified' result - see D053's addendum).
    out_id = _add_worker(connection, "SW-717", "Reagan Wells", start="2027-03-01", end="2027-03-01")
    in_id = _add_worker(connection, "SW-718", "Sasha Ito", start="2027-03-01", end="2027-03-01")
    shift_id = _add_shift(connection, "Helix", "2027-03-01 08:00", "2027-03-01 13:00")
    _add_assignment(connection, out_id, shift_id)
    scenarios["applied_unverified_reconcile"] = {
        "shift_id": shift_id,
        "hall": "Helix",
        "date": "2027-03-01",
        "outgoing_code": "SW-717",
        "outgoing_name": "Reagan Wells",
        "incoming_code": "SW-718",
        "incoming_name": "Sasha Ito",
    }

    connection.commit()
    return scenarios


def _latest_user_index(messages):
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return -1


def _steps_since_latest_user(messages):
    """How many `complete()` calls have already happened since the latest
    supervisor message - i.e. which script index to serve next.

    Counting raw messages would overcount: a single tool-calling turn
    appends BOTH one 'assistant' entry (that turn's tool_calls) AND one
    'tool' entry per call it made, all from ONE `complete()` call. Only the
    'assistant' entries are one-per-turn, so counting those is what
    actually corresponds one-to-one with prior `complete()` calls."""
    index = _latest_user_index(messages)
    return sum(1 for message in messages[index + 1 :] if message.get("role") == "assistant")


def _latest_user_text(messages):
    index = _latest_user_index(messages)
    return messages[index]["content"] if index >= 0 else ""


def _replacement_script(shift, outgoing_code, incoming_code, incoming_name):
    return [
        ModelTurn(
            tool_calls=[
                ToolCallRequest(id="c1", name="find_shift", arguments={"shift_id": shift})
            ]
        ),
        ModelTurn(
            tool_calls=[
                ToolCallRequest(
                    id="c2",
                    name="get_eligible_candidates",
                    arguments={"shift_id": shift, "exclude_employee_code": outgoing_code},
                )
            ]
        ),
        ModelTurn(
            tool_calls=[
                ToolCallRequest(
                    id="c3",
                    name="propose_replacement",
                    arguments={
                        "shift_id": shift,
                        "outgoing_employee_code": outgoing_code,
                        "incoming_employee_code": incoming_code,
                    },
                )
            ]
        ),
        ModelTurn(message=f"Proposed replacing the outgoing worker with {incoming_name}. Awaiting your approval."),
    ]


def _no_candidates_script(shift, outgoing_code):
    return [
        ModelTurn(
            tool_calls=[
                ToolCallRequest(
                    id="c1",
                    name="get_eligible_candidates",
                    arguments={"shift_id": shift, "exclude_employee_code": outgoing_code},
                )
            ]
        ),
        ModelTurn(message="Nobody is currently eligible to cover this shift."),
    ]


class E2EAgentModel:
    """Routes on the LATEST supervisor message text (never on hidden
    per-instance state, since `main._agent_model_adapter()` constructs a
    fresh instance on every single HTTP request - a follow-up
    `send_message` call gets a brand-new `E2EAgentModel`, and only the
    reloaded conversation text tells it what has already happened) to a
    fixed table of canned tool-calling scripts, one script step per call to
    `complete()`. Exhausting a script's steps (should never happen given
    how the driver's chat messages are written) is a safe generic reply
    rather than a crash.
    """

    def __init__(self, scenarios):
        s = scenarios
        self._scripts = [
            (
                "taylor brooks",
                [
                    ModelTurn(
                        tool_calls=[
                            ToolCallRequest(
                                id="c1",
                                name="get_employee_hours",
                                arguments={"employee_code": s["info"]["employee_code"], "week_start": "2026-09-21"},
                            )
                        ]
                    ),
                    ModelTurn(
                        message=(
                            "Taylor Brooks has no recorded hours for that week; everything on their "
                            "record remains approved as originally scheduled. I cannot approve or change "
                            "anything myself - only a supervisor decision through the proposal workflow can."
                        )
                    ),
                ],
            ),
            # The specific "resolved" trigger is checked BEFORE the general
            # ambiguous-query trigger below, since the clarification reply
            # ("Blaine Sato") also contains the general query text
            # ("Blaine") as a substring - matching order, not just presence,
            # is what tells the two apart.
            (
                s["ambiguous"]["resolved_name"].lower(),
                [
                    ModelTurn(
                        tool_calls=[
                            ToolCallRequest(
                                id="c1", name="find_employee", arguments={"query": s["ambiguous"]["resolved_name"]}
                            )
                        ]
                    ),
                    ModelTurn(message=f"Found {s['ambiguous']['resolved_name']}. What would you like to know?"),
                ],
            ),
            (
                s["ambiguous"]["query"].lower(),
                [
                    ModelTurn(
                        tool_calls=[
                            ToolCallRequest(id="c1", name="find_employee", arguments={"query": s["ambiguous"]["query"]})
                        ]
                    ),
                    ModelTurn(message="There are two workers matching that name - which one did you mean?"),
                ],
            ),
            (
                s["callout"]["outgoing_name"].lower(),
                _replacement_script(
                    s["callout"]["shift_id"],
                    s["callout"]["outgoing_code"],
                    s["callout"]["incoming_code"],
                    s["callout"]["incoming_name"],
                ),
            ),
            (
                s["reject"]["outgoing_name"].lower(),
                _replacement_script(
                    s["reject"]["shift_id"],
                    s["reject"]["outgoing_code"],
                    s["reject"]["incoming_code"],
                    s["reject"]["incoming_name"],
                ),
            ),
            (
                s["no_candidates"]["outgoing_name"].lower(),
                _no_candidates_script(s["no_candidates"]["shift_id"], s["no_candidates"]["outgoing_code"]),
            ),
            (
                s["stale_conflict"]["outgoing_name"].lower(),
                _replacement_script(
                    s["stale_conflict"]["shift_id"],
                    s["stale_conflict"]["outgoing_code"],
                    s["stale_conflict"]["incoming_code"],
                    s["stale_conflict"]["incoming_name"],
                ),
            ),
            (
                s["duplicate_click"]["outgoing_name"].lower(),
                _replacement_script(
                    s["duplicate_click"]["shift_id"],
                    s["duplicate_click"]["outgoing_code"],
                    s["duplicate_click"]["incoming_code"],
                    s["duplicate_click"]["incoming_name"],
                ),
            ),
            (
                s["network_unknown"]["outgoing_name"].lower(),
                _replacement_script(
                    s["network_unknown"]["shift_id"],
                    s["network_unknown"]["outgoing_code"],
                    s["network_unknown"]["incoming_code"],
                    s["network_unknown"]["incoming_name"],
                ),
            ),
            (
                s["verification_failed"]["outgoing_name"].lower(),
                _replacement_script(
                    s["verification_failed"]["shift_id"],
                    s["verification_failed"]["outgoing_code"],
                    s["verification_failed"]["incoming_code"],
                    s["verification_failed"]["incoming_name"],
                ),
            ),
            (
                s["applied_unverified_reconcile"]["outgoing_name"].lower(),
                _replacement_script(
                    s["applied_unverified_reconcile"]["shift_id"],
                    s["applied_unverified_reconcile"]["outgoing_code"],
                    s["applied_unverified_reconcile"]["incoming_code"],
                    s["applied_unverified_reconcile"]["incoming_name"],
                ),
            ),
        ]

    def complete(self, messages, tools):  # noqa: ARG002 - `tools` unused: this fake model never reads it
        text = _latest_user_text(messages).lower()
        step = _steps_since_latest_user(messages)
        for trigger, script in self._scripts:
            if trigger in text:
                if step < len(script):
                    return script[step]
                return ModelTurn(message="Nothing further to report on this request.")
        return ModelTurn(message="I could not understand that request in this test environment.")
