"""The handwritten loop with SCRIPTED model replies (no network): feedback, clarification, Python's answer
check, honest stops, and every limit."""

import hashlib

import pytest

import agent
from agent import ModelReply, ProviderError, agent_steps
from scripted import (S1, S1_CONTEXT, S1_ELIGIBLE, S1_FIND, S1_INVESTIGATION, S1_QUESTION, ScriptedProvider,
                      final_reply, find_args, s1_answer, tools_reply)

ALEX_QUESTION = "Alex called out of Andromeda on Saturday."
ALEX_FIND = tools_reply(("find_shift", find_args(date="2026-10-17", hall="Andromeda", worker="Alex")))
CHEN_FIND = tools_reply(("find_shift", find_args(date="2026-10-17", hall="Andromeda", worker="Alex Chen")))
CHEN_ELIGIBLE = tools_reply(("get_eligible_candidates", {"shift_id": "AND-1017-MOR"}))
CHEN_CONTEXT = tools_reply(("get_preferences", {"shift_id": "AND-1017-MOR", "worker_ids": ["W27", "W29"]}))
ASK_WHICH = final_reply(status="clarification", question="Alex Chen or Alex Rivera?")
CHEN_ANSWER = final_reply(shift_id="AND-1017-MOR", ask_first="W27", backup="W29",
                          explanation="Jesse has the most hours left and no recorded emergency coverage.")


def run(conn, replies, question=S1_QUESTION, **kwargs):
    provider = ScriptedProvider(replies)
    events = list(agent_steps(question, conn, provider, **kwargs))
    return events, provider


def asker(*answers):
    """A fake supervisor: returns the scripted replies in order (or raises if one is an exception)."""
    queue = list(answers)

    def ask(question):
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer
    return ask


# ----- the feedback loop -----

def test_tool_results_feed_back_and_produce_a_checked_recommendation(conn):
    events, provider = run(conn, S1_INVESTIGATION + [s1_answer()])
    end = events[-1]
    assert end["type"] == "final" and end["answer"]["status"] == "recommendation"
    assert end["usage"]["model_calls"] == 4 and end["usage"]["tool_calls"] == 5
    last_input = provider.requests[-1]["input"]
    calls = [m["call_id"] for m in last_input if m.get("type") == "function_call"]
    outputs = [m for m in last_input if m.get("type") == "function_call_output"]
    assert calls == [m["call_id"] for m in outputs] and len(calls) == 5   # every call has its result
    assert "Riley Nguyen" in outputs[1]["output"]                          # the eligibility facts were fed back


def test_model_may_pick_another_eligible_worker(conn):
    events, _ = run(conn, S1_INVESTIGATION + [s1_answer(ask_first="W09", backup=None)])
    assert events[-1]["answer"]["ask_first"] == "W09"


def test_incomplete_is_an_honest_final_answer(conn):
    events, provider = run(conn, [S1_FIND, final_reply(status="incomplete", explanation="Context was not gathered.")],
                           limits={"model_calls": 2})
    assert provider.requests[1]["tool_choice"] == "none"  # last call: tools off, but no forced recommendation
    assert events[-1]["answer"]["status"] == "incomplete"


# ----- Python's check of the answer -----

def test_ineligible_choice_gets_one_correction(conn):
    events, provider = run(conn, S1_INVESTIGATION + [s1_answer(ask_first="W08"), s1_answer()])
    rejected = next(e for e in events if e["type"] == "rejected")
    assert "W08 is not eligible: weekly limit" in rejected["problems"][0]
    assert "Python rejected that answer" in provider.requests[-1]["input"][-1]["content"]
    assert events[-1]["type"] == "final" and events[-1]["usage"]["model_calls"] == 5


def test_second_invalid_answer_stops_without_substituting(conn):
    events, _ = run(conn, S1_INVESTIGATION + [s1_answer(ask_first="W08")] * 2)
    assert events[-1]["status"] == "invalid_answer"
    assert not any(e["type"] == "final" for e in events)


@pytest.mark.parametrize("fields, problem", [
    ({"ask_first": "W01"}, "called out"),
    ({"backup": "W14"}, "different worker"),
    ({"backup": "W08"}, "backup W08 is not eligible"),
    ({"ask_first": "W99"}, "unknown worker"),
    ({"ask_first": None}, "ask_first is required"),
    ({"shift_id": "HEL-1013-MOR"}, "locked to AND-1012-EVE"),
    ({"explanation": " "}, "explain"),
])
def test_invalid_selections_are_rejected(conn, fields, problem):
    events, _ = run(conn, S1_INVESTIGATION + [s1_answer(**fields)] * 2)
    assert any(problem in p for p in next(e for e in events if e["type"] == "rejected")["problems"])


def test_no_candidate_claims_are_confirmed_by_python(conn):
    false_claim = final_reply(status="no_eligible_candidate", shift_id=S1, explanation="Nobody can.")
    events, _ = run(conn, [S1_FIND, S1_ELIGIBLE, false_claim, false_claim])
    assert events[-1]["status"] == "invalid_answer" and "eligible workers exist" in events[-1]["message"]

    drew = [tools_reply(("find_shift", find_args(date="2026-10-16", hall="Capella", shift_name="Afternoon", worker="Drew"))),
            tools_reply(("get_eligible_candidates", {"shift_id": "CAP-1016-AFT"})),
            final_reply(status="no_eligible_candidate", shift_id="CAP-1016-AFT", explanation="Everyone is excluded.")]
    events, _ = run(conn, drew, question="Drew called out of Capella on Friday afternoon.")
    assert events[-1]["answer"]["status"] == "no_eligible_candidate"


def test_answer_without_a_locked_shift_is_rejected(conn):
    events, _ = run(conn, [s1_answer(), s1_answer()])
    assert "no shift is locked" in events[-1]["message"]


@pytest.mark.parametrize("text", [
    "Ask Skyler.",                                                   # not JSON
    "[1, 2]",                                                        # not an object
    '{"status": "recommendation"}',                                  # missing fields
    '{"status": "maybe", "shift_id": null, "ask_first": null, "backup": null, "explanation": "x", "question": null}',
    '{"status": "recommendation", "shift_id": 5, "ask_first": null, "backup": null, "explanation": null, "question": null}',
    '{"status": "clarification", "shift_id": null, "ask_first": null, "backup": null, "explanation": null, "question": null}',
])
def test_malformed_answers_never_crash(conn, text):
    bad = ModelReply(status="completed", text=text, input_tokens=10, output_tokens=5, cached_tokens=0, cache_write_tokens=0)
    events, _ = run(conn, [bad, bad])
    assert events[-1]["type"] == "stop" and events[-1]["status"] == "invalid_answer"


# ----- clarification within one task -----

def test_one_reply_resolves_the_ambiguity(conn):
    events, provider = run(conn, [ALEX_FIND, ASK_WHICH, CHEN_FIND, CHEN_ELIGIBLE, CHEN_CONTEXT, CHEN_ANSWER],
                           question=ALEX_QUESTION, ask=asker("Alex Chen"))
    end = events[-1]
    assert end["type"] == "final" and end["target"] == {"shift_id": "AND-1017-MOR", "worker_id": "W02"}
    assert end["usage"]["clarifications"] == 1 and end["usage"]["model_calls"] == 6   # one shared budget
    assert "Supervisor's reply to your question: Alex Chen" in str(provider.requests[2]["input"])
    assert [e["type"] for e in events].count("clarify") == 1


def test_reply_keeps_the_original_date_and_hall(conn):
    # After "Alex Chen", a lookup of one of Alex Chen's shifts on another day must not lock: Saturday/Andromeda remain.
    other_day = conn.execute("SELECT a.shift_id FROM assignments a JOIN shifts s ON s.id = a.shift_id WHERE a.worker_id"
                             " = 'W02' AND s.start_at LIKE '2026-10-1%' AND a.shift_id != 'AND-1017-MOR'").fetchone()[0]
    wrong = tools_reply(("find_shift", find_args(shift_id=other_day, worker="Alex Chen")))
    events, _ = run(conn, [ALEX_FIND, ASK_WHICH, wrong, CHEN_ANSWER, CHEN_ANSWER],
                    question=ALEX_QUESTION, ask=asker("Alex Chen"))
    note = [e["result"]["target_check"] for e in events if e["type"] == "tool" and "target_check" in e["result"]][0]
    assert note.startswith("not locked") and "2026-10-17" in note
    assert events[-1]["status"] == "invalid_answer"


def test_two_replies_are_allowed(conn):
    which_day = final_reply(status="clarification", question="Which day?")
    events, _ = run(conn, [which_day, ALEX_FIND, ASK_WHICH, CHEN_FIND, CHEN_ELIGIBLE, CHEN_CONTEXT, CHEN_ANSWER],
                    question="Alex called out of Andromeda.", ask=asker("Saturday", "Alex Chen"))
    assert events[-1]["type"] == "final" and events[-1]["usage"]["clarifications"] == 2


def test_a_third_clarification_stops(conn):
    events, _ = run(conn, [ASK_WHICH] * 3, question=ALEX_QUESTION, ask=asker("not sure", "still not sure"))
    assert events[-1]["status"] == "clarification_limit" and events[-1]["usage"]["clarifications"] == 2


@pytest.mark.parametrize("reply", [EOFError(), KeyboardInterrupt(), "", "   "])
def test_eof_cancel_or_empty_reply_stops_gracefully(conn, reply):
    events, _ = run(conn, [ALEX_FIND, ASK_WHICH], question=ALEX_QUESTION, ask=asker(reply))
    assert events[-1]["status"] == "clarification_unanswered"


def test_without_a_person_to_reply_the_question_is_the_answer(conn):
    events, _ = run(conn, [ALEX_FIND, ASK_WHICH], question=ALEX_QUESTION)  # ask=None: noninteractive
    assert events[-1]["type"] == "final" and events[-1]["answer"]["question"] == "Alex Chen or Alex Rivera?"


def test_clarification_shares_the_budget(conn):
    events, provider = run(conn, [ALEX_FIND, ASK_WHICH, final_reply(status="incomplete", explanation="Out of calls.")],
                           question=ALEX_QUESTION, ask=asker("Alex Chen"), limits={"model_calls": 3})
    assert provider.requests[2]["tool_choice"] == "none"   # the reply did not reset the call count
    assert events[-1]["usage"]["model_calls"] == 3


def test_waiting_for_the_supervisor_does_not_count_toward_the_deadline(conn):
    now = [0.0]

    def slow_person(question):
        now[0] += 500  # the supervisor takes 500 seconds to answer
        return "Alex Chen"
    events, _ = run(conn, [ALEX_FIND, ASK_WHICH, CHEN_FIND, CHEN_ELIGIBLE, CHEN_CONTEXT, CHEN_ANSWER],
                    question=ALEX_QUESTION, ask=slow_person, clock=lambda: now[0])
    assert events[-1]["type"] == "final" and events[-1]["usage"]["seconds"] == 0


def test_reply_pointing_to_another_shift_stops(conn):
    confirm = final_reply(status="clarification", question="Confirm Jordan's Oct 12 evening shift?")
    events, _ = run(conn, [S1_FIND, confirm], ask=asker("No, October 13"))
    assert events[-1]["status"] == "target_changed"


# ----- honest stops and limits -----

def test_unknown_tool_and_bad_arguments_are_observations(conn):
    events, _ = run(conn, [tools_reply(("best_candidate", {}), ("get_preferences", {"shift_id": S1, "worker_ids": ["W08"]})),
                           final_reply(status="incomplete", explanation="Could not proceed.")])
    errors = [e["result"]["error"] for e in events if e["type"] == "tool"]
    assert "unknown tool" in errors[0] and "not eligible" in errors[1]
    assert events[-1]["answer"]["status"] == "incomplete"


def test_truncated_and_incomplete_replies_stop(conn):
    cut = ModelReply(status="incomplete", incomplete_reason="max_output_tokens", input_tokens=900, output_tokens=1000)
    assert run(conn, [cut])[0][-1]["status"] == "truncated"
    filtered = ModelReply(status="incomplete", incomplete_reason="content_filter", input_tokens=900, output_tokens=10)
    assert run(conn, [filtered])[0][-1]["status"] == "incomplete"


def test_provider_failures_stop_honestly(conn):
    events, _ = run(conn, [ProviderError("APIConnectionError")])
    end = events[-1]
    assert end["status"] == "provider_unavailable" and end["usage"]["output_tokens"] == agent.LIMITS["output_tokens"]
    assert end["usage"]["unmeasured_tokens"] == end["usage"]["input_tokens"] + end["usage"]["output_tokens"]

    class Down(ScriptedProvider):
        def count_input_tokens(self, request, timeout):
            raise ProviderError("AuthenticationError 401")
    events = list(agent_steps(S1_QUESTION, conn, Down([])))
    assert events[-1]["status"] == "provider_unavailable" and events[-1]["usage"]["model_calls"] == 0


def test_call_limit(conn):
    events, _ = run(conn, [S1_FIND, s1_answer(ask_first="W08")], limits={"model_calls": 2})
    assert events[-1]["status"] == "call_limit"


def test_tools_requested_after_they_are_disabled(conn):
    events, _ = run(conn, [S1_FIND, S1_ELIGIBLE], limits={"model_calls": 2})
    assert events[-1]["status"] == "protocol_error"


def test_tool_limit_skips_extra_calls_and_disables_tools(conn):
    events, provider = run(conn, S1_INVESTIGATION + [final_reply(status="incomplete", explanation="Tool budget used.")],
                           limits={"tool_calls": 4})
    assert sum("tool limit" in e["result"].get("error", "") for e in events if e["type"] == "tool") == 1
    assert provider.requests[-1]["tool_choice"] == "none"
    assert events[-1]["usage"]["tool_calls"] == 4


def test_token_limit_stops_before_a_paid_request(conn):
    probe, _ = run(conn, [S1_FIND, final_reply(status="incomplete", explanation="x")])
    first_reserved = next(e for e in probe if e["type"] == "model_call")["reserved"]
    events, provider = run(conn, S1_INVESTIGATION, limits={"total_tokens": first_reserved + 100})
    assert events[-1]["status"] == "token_limit"
    assert len(provider.requests) == 1 and provider.counts == 2  # the second request was counted, never sent


def test_deadline_is_checked_after_slow_token_counting(conn):
    now = [0.0]

    class SlowCounter(ScriptedProvider):
        def count_input_tokens(self, request, timeout):
            now[0] = 121
            return 10
    provider = SlowCounter([s1_answer()])
    events = list(agent_steps(S1_QUESTION, conn, provider, clock=lambda: now[0]))
    assert events[-1]["status"] == "deadline" and provider.requests == []


def test_late_reply_is_charged_but_not_used(conn):
    now = [0.0]

    class SlowModel(ScriptedProvider):
        def create(self, request, max_output_tokens, timeout):
            now[0] = 125
            return super().create(request, max_output_tokens, timeout)
    events = list(agent_steps(S1_QUESTION, conn, SlowModel([final_reply(status="incomplete", explanation="late")]),
                              clock=lambda: now[0]))
    assert events[-1]["status"] == "deadline" and events[-1]["usage"]["output_tokens"] == 150


def test_missing_usage_is_charged_conservatively(conn):
    events, _ = run(conn, [ModelReply(status="incomplete", incomplete_reason="content_filter", input_tokens=100)])
    usage = events[-1]["usage"]
    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 1000 and usage["unmeasured_tokens"] == 1000


@pytest.mark.parametrize("question", ["", "x" * 501])
def test_invalid_question_makes_no_calls(conn, question):
    events, provider = run(conn, [], question=question)
    assert events[-1]["status"] == "invalid_input" and provider.counts == 0


def test_a_run_leaves_the_database_unchanged(seed_db, conn):
    before = hashlib.sha256(seed_db.read_bytes()).hexdigest()
    run(conn, S1_INVESTIGATION + [s1_answer()])
    run(conn, [ALEX_FIND, ASK_WHICH, CHEN_FIND, CHEN_ELIGIBLE, CHEN_CONTEXT, CHEN_ANSWER], question=ALEX_QUESTION, ask=asker("Alex Chen"))
    assert hashlib.sha256(seed_db.read_bytes()).hexdigest() == before


# ----- review fix 1: the outgoing worker's identity is explicit -----

RIVERA_FIND = tools_reply(("find_shift", find_args(date="2026-10-17", hall="Andromeda", worker="Alex Rivera")))
RIVERA_ANSWER = final_reply(shift_id="AND-1017-EVE", ask_first="W27", backup=None, explanation="Choose Jesse.")


def notes(events):
    return [e["result"]["target_check"] for e in events if e["type"] == "tool" and "target_check" in e["result"]]


def test_full_name_excludes_the_other_alex(conn):
    events, _ = run(conn, [RIVERA_FIND, RIVERA_ANSWER, RIVERA_ANSWER], question="Alex Chen called out of Andromeda on Saturday.")
    assert notes(events) == ["not locked: the request names Alex Chen, not Alex Rivera. Ask the supervisor instead of guessing."]
    assert events[-1]["status"] == "invalid_answer"


def test_reply_naming_chen_excludes_a_later_rivera_lookup(conn):
    events, _ = run(conn, [ALEX_FIND, ASK_WHICH, RIVERA_FIND, RIVERA_ANSWER, RIVERA_ANSWER],
                    question=ALEX_QUESTION, ask=asker("Alex Chen"))
    assert "names Alex Chen, not Alex Rivera" in notes(events)[0]
    assert events[-1]["status"] == "invalid_answer"


def test_explicit_worker_id_is_honored(conn):
    events, _ = run(conn, [RIVERA_FIND, RIVERA_ANSWER, RIVERA_ANSWER], question="W02 called out of Andromeda on Saturday.")
    assert "names Alex Chen, not Alex Rivera" in notes(events)[0]


def test_clarification_resolving_to_chen_locks_chen(conn):
    events, _ = run(conn, [ALEX_FIND, ASK_WHICH, CHEN_FIND, CHEN_ELIGIBLE, CHEN_CONTEXT, CHEN_ANSWER],
                    question=ALEX_QUESTION, ask=asker("Alex Chen"))
    assert notes(events) == ["target locked: AND-1017-MOR / W02 Alex Chen"]
    assert events[-1]["type"] == "final"


def test_first_name_is_enough_when_the_facts_make_it_unique(conn):
    evening = tools_reply(("find_shift", find_args(date="2026-10-17", hall="Andromeda", shift_name="Evening", worker="Alex")))
    events, _ = run(conn, [evening, final_reply(status="incomplete", explanation="stop")],
                    question="Alex called out of Andromeda on Saturday evening.")
    assert notes(events) == ["target locked: AND-1017-EVE / W03 Alex Rivera"]
    s1, _ = run(conn, S1_INVESTIGATION + [s1_answer()])          # "Jordan ... October 12 evening"
    assert s1[-1]["type"] == "final"


def test_ambiguous_first_name_is_never_resolved_by_picking_one(conn):
    # The request fits both Alexes; a lookup that jumps straight to one of them must not lock.
    events, _ = run(conn, [RIVERA_FIND, final_reply(status="clarification", question="Which Alex?")], question=ALEX_QUESTION)
    assert notes(events)[0].startswith("not locked: the request fits more than one assignment")


def test_reply_naming_another_worker_after_lock_stops(conn):
    confirm = final_reply(status="clarification", question="Confirm it was Jordan?")
    events, _ = run(conn, [S1_FIND, confirm], ask=asker("No, it was Morgan Diaz"))
    assert events[-1]["status"] == "target_changed"


# ----- review fix 2: facts must have been returned to the model before it recommends -----

def test_recommendation_after_find_shift_alone_is_rejected(conn):
    events, _ = run(conn, [S1_FIND, s1_answer(), s1_answer()])
    assert "call get_eligible_candidates for AND-1012-EVE" in events[-1]["message"]


def test_recommendation_without_soft_context_is_rejected(conn):
    events, _ = run(conn, [S1_FIND, S1_ELIGIBLE, s1_answer(), s1_answer()])
    assert "fetch soft context" in events[-1]["message"] and "W14, W21" in events[-1]["message"]


def test_failed_or_wrong_shift_context_does_not_count(conn):
    failed = tools_reply(("get_preferences", {"shift_id": S1, "worker_ids": ["W14", "W08"]}))           # error: W08
    other_shift = tools_reply(("get_preferences", {"shift_id": "HEL-1013-MOR", "worker_ids": ["W14", "W21"]}))
    events, _ = run(conn, [S1_FIND, S1_ELIGIBLE, failed, other_shift, s1_answer(), s1_answer()])
    assert events[-1]["status"] == "invalid_answer" and "W14, W21" in events[-1]["message"]


def test_context_for_an_unrelated_worker_does_not_count(conn):
    riley_only = tools_reply(("get_coverage_history", {"shift_id": S1, "worker_ids": ["W05"], "lookback_days": 14}))
    events, _ = run(conn, [S1_FIND, S1_ELIGIBLE, riley_only, s1_answer(backup=None), s1_answer(backup=None)])
    assert "for W14 on AND-1012-EVE" in events[-1]["message"]


def test_a_relevant_subset_of_context_tools_is_enough(conn):
    nearby = tools_reply(("get_nearby_classes", {"shift_id": S1, "worker_ids": ["W14", "W21"], "window_minutes": 120}))
    events, _ = run(conn, [S1_FIND, S1_ELIGIBLE, nearby, s1_answer()])
    assert events[-1]["type"] == "final" and events[-1]["answer"]["ask_first"] == "W14"


def test_premature_recommendation_can_be_corrected_once(conn):
    events, provider = run(conn, [S1_FIND, S1_ELIGIBLE, s1_answer(), S1_CONTEXT, s1_answer()])
    assert any(e["type"] == "rejected" for e in events) and events[-1]["type"] == "final"
    assert "fetch soft context" in provider.requests[3]["input"][-1]["content"]


# ----- review fix 4: malformed worker lists become observations, not crashes -----

@pytest.mark.parametrize("worker_ids", [[{}], [["W05"]], [5], ["W14", "W14"]])
def test_malformed_worker_ids_in_the_loop(conn, worker_ids):
    bad = tools_reply(("get_preferences", {"shift_id": S1, "worker_ids": worker_ids}))
    events, _ = run(conn, [S1_FIND, bad, final_reply(status="incomplete", explanation="stop")])
    error = next(e for e in events if e["type"] == "tool" and e["name"] == "get_preferences")["result"]["error"]
    assert "worker_ids" in error and events[-1]["answer"]["status"] == "incomplete"


def test_prompt_forbids_inventing_unrecorded_personal_facts():
    # Phase C live run 2 referred to a worker as "he"; gender is not in the data.
    assert 'refer to workers by name or as "they"' in " ".join(agent.SYSTEM_PROMPT.split())
    assert "Explain only facts you actually observed" in agent.SYSTEM_PROMPT



def test_prompt_defines_the_hour_fields_the_tools_return(conn):
    # Phase C run 4 called hours_after_covering "remaining capacity", and run 6 said "the only worker with a
    # stored preference" although a stored "low" existed. This checks the guidance given to the model; it cannot
    # guarantee what a future model reply will say.
    import json
    import tools
    prompt = " ".join(agent.SYSTEM_PROMPT.split())
    result = tools.run_tool(conn, "get_eligible_candidates", json.dumps({"shift_id": "AND-1012-EVE"}))
    for field in ("assigned_hours_now", "hours_after_covering", "hours_left_after_covering"):
        assert field in result["eligible"][0] and f"- {field}:" in prompt
    assert "hours_left_after_covering: remaining capacity under the 20-hour cap" in prompt
    assert "do not recompute or relabel them" in prompt
    assert 'A stored "low" preference is still a stored preference' in prompt
    assert '"the only"' in prompt
