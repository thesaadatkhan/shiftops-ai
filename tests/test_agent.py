"""The manual loop, guardrails, and mandatory final validation, using SCRIPTED replies (no network)."""

import hashlib

import pytest

import agent
from agent import Limits, ModelReply, ProviderError, agent_steps
from scripted import ScriptedProvider, final_reply, tools_reply

S1 = "AND-1012-EVE"
S1_CONTEXT = [("get_preferences", {"shift_id": S1, "candidate_ids": ["W05", "W09", "W14", "W21"]}),
              ("get_coverage_history", {"shift_id": S1, "candidate_ids": ["W05", "W09", "W14", "W21"], "lookback_days": 14}),
              ("get_nearby_classes", {"shift_id": S1, "candidate_ids": ["W05", "W09", "W14", "W21"], "window_minutes": 120})]
INVESTIGATION = [
    tools_reply(("find_shift", {"shift_id": None, "date": "2026-10-12", "hall": "Andromeda", "shift_name": "Evening",
                                "start_time": None, "worker": "Jordan"})),
    tools_reply(("get_eligible_candidates", {"shift_id": S1, "outgoing_worker_id": "W01"})),
    tools_reply(*S1_CONTEXT),
]
GOOD_FINAL = dict(shift_id=S1, outgoing_worker_id="W01", recommended_worker_id="W14", backup_worker_id="W21",
                  rationale="Skyler has no recent emergency coverage and the most spare hours.",
                  tradeoff="Skyler marked this shift low; Riley prefers it but covered three emergencies recently.",
                  evidence_refs=[f"ELIG:{S1}:W14", f"ELIG:{S1}:W21", "PREF:2", "HIST:AND-1012-EVE:W14:14d", "REQ:2"])


def run(conn, replies, limits=None, question="Jordan called out of Andromeda Oct 12 evening", **kwargs):
    provider = ScriptedProvider(replies)
    events = list(agent_steps(question, conn, provider, limits, **kwargs))
    return events, provider


def last(events):
    return events[-1]


def test_full_loop_produces_validated_recommendation(conn):
    events, provider = run(conn, INVESTIGATION + [final_reply(**GOOD_FINAL)])
    end = last(events)
    assert end["type"] == "final" and end["status"] == "recommendation"
    assert end["usage"]["model_calls"] == 4 and end["usage"]["tool_calls"] == 5
    assert end["usage"]["input_tokens"] == 3 * 800 + 1500
    # Every model call is reported with actual usage.
    assert [e["step"] for e in events if e["type"] == "model_call"] == [1, 2, 3, 4]
    # Each tool call in the conversation is followed by its matching observation.
    final_request = provider.requests[-1]["input"]
    call_ids = [i["call_id"] for i in final_request if i.get("type") == "function_call"]
    output_ids = [i["call_id"] for i in final_request if i.get("type") == "function_call_output"]
    assert call_ids == output_ids and len(call_ids) == 5


def test_model_can_choose_a_different_eligible_worker(conn):
    # The loop does not predetermine the winner: another defensible choice also validates.
    final = dict(GOOD_FINAL, recommended_worker_id="W09", backup_worker_id=None,
                 evidence_refs=[f"ELIG:{S1}:W09", "PREF:AND-1012-EVE:W09:none", "CLASS:7"])
    events, _ = run(conn, INVESTIGATION + [final_reply(**final)])
    assert last(events)["status"] == "recommendation"


def test_invented_worker_gets_one_repair_charged_to_same_budget(conn):
    events, provider = run(conn, INVESTIGATION + [final_reply(**dict(GOOD_FINAL, recommended_worker_id="W99")),
                                                  final_reply(**GOOD_FINAL)])
    assert any(e["type"] == "validation_failed" and "W99" in e["problems"][0] for e in events)
    assert last(events)["status"] == "recommendation"
    assert last(events)["usage"]["model_calls"] == 5
    assert "Python validation rejected" in provider.requests[-1]["input"][-1]["content"]


def test_ineligible_choice_is_never_substituted(conn):
    bad = final_reply(**dict(GOOD_FINAL, recommended_worker_id="W08", evidence_refs=GOOD_FINAL["evidence_refs"]))
    events, _ = run(conn, INVESTIGATION + [bad, bad])
    end = last(events)
    assert end["type"] == "stop" and end["status"] == "invalid_recommendation"
    assert any("weekly limit" in p for p in end["problems"])
    assert not any(e["type"] == "final" for e in events)


def test_evidence_must_have_been_retrieved_in_this_run(conn):
    # REQ:10 exists in the database but was never returned (it is after the target shift).
    refs = GOOD_FINAL["evidence_refs"] + ["REQ:10"]
    events, _ = run(conn, INVESTIGATION + [final_reply(**dict(GOOD_FINAL, evidence_refs=refs))] * 2)
    assert last(events)["status"] == "invalid_recommendation"
    assert any("REQ:10" in p for p in last(events)["problems"])


def test_recommendation_without_context_or_eligibility_citation_is_rejected(conn):
    final = dict(GOOD_FINAL, evidence_refs=[f"ELIG:{S1}:W14"])
    events, _ = run(conn, INVESTIGATION + [final_reply(**final)] * 2)
    problems = last(events)["problems"]
    assert any("ELIG:AND-1012-EVE:W21" in p for p in problems)
    assert any("context record" in p for p in problems)


def test_false_no_candidate_claim_is_rejected_and_true_one_accepted(conn):
    false_claim = final_reply(status="no_eligible_candidate", shift_id=S1, outgoing_worker_id="W01",
                              rationale="none", evidence_refs=[f"SHIFT:{S1}"])
    events, _ = run(conn, INVESTIGATION[:2] + [false_claim, false_claim])
    assert last(events)["status"] == "invalid_recommendation"

    s3 = [tools_reply(("find_shift", {"shift_id": None, "date": "2026-10-16", "hall": "Capella", "shift_name": "Afternoon",
                                      "start_time": None, "worker": "Drew"})),
          tools_reply(("get_eligible_candidates", {"shift_id": "CAP-1016-AFT", "outgoing_worker_id": "W12"})),
          final_reply(status="no_eligible_candidate", shift_id="CAP-1016-AFT", outgoing_worker_id="W12",
                      rationale="Every other worker is excluded.", evidence_refs=["SHIFT:CAP-1016-AFT"])]
    events, _ = run(conn, s3, question="Drew called out of Capella Friday afternoon")
    assert last(events)["status"] == "no_eligible_candidate"


def test_clarification_ends_the_run(conn):
    replies = [tools_reply(("find_shift", {"shift_id": None, "date": "2026-10-17", "hall": "Andromeda", "shift_name": None,
                                           "start_time": None, "worker": "Alex"})),
               final_reply(status="clarification", rationale="Two Alexes work Andromeda that day.",
                           clarification_question="Alex Chen (Morning) or Alex Rivera (Evening)?")]
    events, _ = run(conn, replies, question="Alex called out of Andromeda on Saturday")
    assert last(events)["status"] == "clarification"


def test_unknown_tool_and_bad_arguments_become_observations(conn):
    replies = [tools_reply(("best_candidate", {}), ("get_preferences", {"shift_id": S1, "candidate_ids": ["W08"]})),
               final_reply(status="incomplete", rationale="Could not proceed.")]
    events, _ = run(conn, replies)
    errors = [e["result"]["error"] for e in events if e["type"] == "observation"]
    assert "unknown tool" in errors[0] and "not eligible" in errors[1]
    assert last(events)["status"] == "incomplete"


def test_truncated_output_stops_with_usage(conn):
    truncated = ModelReply(status="incomplete", incomplete_reason="max_output_tokens", input_tokens=900, output_tokens=1000)
    events, _ = run(conn, [truncated])
    end = last(events)
    assert end["status"] == "truncated" and end["usage"]["output_tokens"] == 1000


def test_provider_unavailable(conn):
    events, _ = run(conn, [ProviderError("APIConnectionError")])
    assert last(events)["status"] == "provider_unavailable"

    class DownProvider(ScriptedProvider):
        def count_input_tokens(self, request, timeout):
            raise ProviderError("AuthenticationError 401 expired_secret_key")
    events = list(agent_steps("question", conn, DownProvider([])))
    assert last(events)["status"] == "provider_unavailable" and last(events)["usage"]["model_calls"] == 0


def test_last_allowed_call_disables_tools_and_call_limit_stops(conn):
    limits = Limits(max_model_calls=2)
    replies = [INVESTIGATION[0], final_reply(**dict(GOOD_FINAL, recommended_worker_id="W99"))]
    events, provider = run(conn, replies, limits)
    assert provider.requests[0]["tool_choice"] == "auto"
    assert provider.requests[1]["tool_choice"] == "none"
    assert last(events)["status"] == "call_limit"


def test_tools_requested_after_disabled_is_protocol_error(conn):
    events, _ = run(conn, [INVESTIGATION[0], INVESTIGATION[1]], Limits(max_model_calls=2))
    assert last(events)["status"] == "protocol_error"


def test_tool_limit_skips_extra_calls_and_forces_answer(conn):
    limits = Limits(max_tool_calls=4)
    replies = INVESTIGATION + [final_reply(status="incomplete", rationale="Tool budget ran out.")]
    events, provider = run(conn, replies, limits)
    skipped = [e for e in events if e["type"] == "observation" and "tool limit" in e["result"].get("error", "")]
    assert len(skipped) == 1  # the 5th tool call was not executed
    assert provider.requests[-1]["tool_choice"] == "none"
    assert last(events)["usage"]["tool_calls"] == 4


def test_repeated_calls_are_not_rerun_and_eventually_stop(conn):
    same = INVESTIGATION[0]
    events, _ = run(conn, [same, same, same])
    duplicates = [e for e in events if e["type"] == "observation" and "duplicate" in e["result"].get("error", "")]
    assert len(duplicates) == 2
    assert last(events)["status"] == "repeating"


def test_token_reservation_stops_before_a_paid_request(conn):
    probe, _ = run(conn, INVESTIGATION[:1] + [final_reply(status="incomplete")])
    first_reserved = next(e for e in probe if e["type"] == "model_call")["reserved"]
    events, provider = run(conn, INVESTIGATION, Limits(token_allowance=first_reserved + 100))
    assert last(events)["status"] == "token_limit"
    # The first call fits; the second request (larger input + 1,000 reserved output) does not.
    assert len(provider.requests) == 1 and provider.counts == 2


def test_missing_usage_is_charged_conservatively(conn):
    no_usage = ModelReply(status="incomplete", incomplete_reason="content_filter")
    events, _ = run(conn, [no_usage])
    assert last(events)["usage"]["output_tokens"] == 1000 and last(events)["status"] == "incomplete"


def test_deadline(conn):
    ticks = iter([0, 0, 200, 200, 200, 200, 200])
    events, _ = run(conn, INVESTIGATION, clock=lambda: next(ticks))
    assert last(events)["status"] == "deadline"


# ----- regression tests for the Phase 1-2 review findings -----

def test_final_for_a_different_valid_target_is_rejected(conn):
    # Review P1: the question is about Jordan/Andromeda Monday, but the model investigates Morgan's Helix
    # Tuesday shift and recommends an eligible, properly cited worker for it. That must not be accepted.
    wrong = dict(GOOD_FINAL, shift_id="HEL-1013-MOR", outgoing_worker_id="W06", recommended_worker_id="W14",
                 backup_worker_id=None, evidence_refs=["ELIG:HEL-1013-MOR:W14", "PREF:HEL-1013-MOR:W14:none"])
    detour = tools_reply(("get_eligible_candidates", {"shift_id": "HEL-1013-MOR", "outgoing_worker_id": "W06"}),
                         ("get_preferences", {"shift_id": "HEL-1013-MOR", "candidate_ids": ["W14"]}))
    # Without any find_shift resolution, there is no bound target.
    events, _ = run(conn, [detour, final_reply(**wrong), final_reply(**wrong)],
                    question="Jordan called out of Andromeda Monday evening")
    assert last(events)["status"] == "invalid_recommendation"
    assert any("no target was resolved" in p for p in last(events)["problems"])
    # With Jordan's shift resolved first, switching to Morgan's shift is rejected.
    events, _ = run(conn, INVESTIGATION[:1] + [detour, final_reply(**wrong), final_reply(**wrong)])
    assert last(events)["status"] == "invalid_recommendation"
    assert any("before switching targets" in p for p in last(events)["problems"])


def test_target_binding_events_and_conflicts(conn):
    events, provider = run(conn, INVESTIGATION + [final_reply(**GOOD_FINAL)])
    note = next(e for e in events if e["type"] == "observation")["result"]["target_check"]
    assert note == "target bound: AND-1012-EVE / W01 Jordan Lee"
    assert "target bound" in provider.requests[1]["input"][2]["output"]  # the model sees the note too

    # A second, different resolution is a conflict: a recommendation is refused, a clarification is fine.
    morgan = tools_reply(("find_shift", {"shift_id": "HEL-1013-MOR", "date": None, "hall": None, "shift_name": None,
                                         "start_time": None, "worker": "Morgan"}))
    question = "Jordan or Morgan called out, Andromeda Monday evening or Helix Tuesday morning?"
    events, _ = run(conn, INVESTIGATION[:1] + [morgan] + INVESTIGATION[1:] + [final_reply(**GOOD_FINAL)] * 2,
                    question=question)
    assert any("Only a clarification" in e["result"].get("target_check", "") for e in events if e["type"] == "observation")
    assert last(events)["status"] == "invalid_recommendation"
    clarify = final_reply(status="clarification", rationale="Two call-outs were mentioned.",
                          clarification_question="Which call-out do you mean?")
    events, _ = run(conn, INVESTIGATION[:1] + [morgan, clarify], question=question)
    assert last(events)["status"] == "clarification"


def test_resolution_for_worker_or_hall_not_in_question_is_a_conflict(conn):
    # find_shift resolves Jordan, but the question names Morgan: never bound.
    events, _ = run(conn, INVESTIGATION + [final_reply(**GOOD_FINAL)] * 2,
                    question="Morgan called out of Andromeda Oct 12 evening")
    assert "not named in the question" in last(events)["problems"][-1]
    # The question names Helix, but the resolved shift is in Andromeda.
    events, _ = run(conn, INVESTIGATION + [final_reply(**GOOD_FINAL)] * 2,
                    question="Jordan called out of Helix on Oct 12")
    assert "question names ['Helix']" in last(events)["problems"][-1]


@pytest.mark.parametrize("bad_fields", [
    {"rationale": None},
    {"tradeoff": 5},
    {"recommended_worker_id": ["W14"]},
    {"backup_worker_id": {"id": "W21"}},
    {"shift_id": 12},
    {"status": ["recommendation"]},
    {"evidence_refs": ["PREF:2", None]},
    {"clarification_question": False},
])
def test_malformed_but_valid_json_fields_get_controlled_errors(conn, bad_fields):
    # Review P2: syntactically valid JSON with wrong field types must not crash validation.
    events, _ = run(conn, INVESTIGATION + [final_reply(**dict(GOOD_FINAL, **bad_fields))] * 2)
    end = last(events)
    assert end["type"] == "stop" and end["status"] == "invalid_recommendation"
    assert any("must be" in p for p in end["problems"])
    assert sum(1 for e in events if e["type"] == "validation_failed") == 2  # the repair was charged and used


def test_non_object_final_json_is_rejected(conn):
    events, _ = run(conn, [ModelReply(status="completed", text="[1, 2]", input_tokens=5, output_tokens=5)] * 2)
    assert last(events)["problems"] == ["final answer does not match the required JSON fields"]


def test_deadline_rechecked_after_slow_token_count(conn):
    # Review P2: counting pushes the clock past the deadline, so no paid generation request is made.
    now = [0]

    class SlowCounter(ScriptedProvider):
        def count_input_tokens(self, request, timeout):
            now[0] = 121
            return 10
    provider = SlowCounter([final_reply(status="incomplete", rationale="unused")])
    events = list(agent_steps("question", conn, provider, clock=lambda: now[0]))
    assert last(events)["status"] == "deadline" and "while counting tokens" in last(events)["message"]
    assert provider.requests == [] and last(events)["usage"]["model_calls"] == 0


def test_late_generation_reply_is_charged_but_discarded(conn):
    now = [0]

    class SlowGenerator(ScriptedProvider):
        def create(self, request, max_output_tokens, timeout):
            assert timeout == 30  # full per-request timeout fits inside the remaining 120 s
            now[0] = 125
            return super().create(request, max_output_tokens, timeout)
    events = list(agent_steps("question", conn, SlowGenerator([final_reply(status="incomplete", rationale="late")]),
                              clock=lambda: now[0]))
    end = last(events)
    assert end["status"] == "deadline" and "discarded" in end["message"]
    assert end["usage"]["output_tokens"] == 150  # measured usage is still charged
    assert not any(e["type"] == "final" for e in events)


def test_generation_timeout_uses_time_left_after_counting(conn):
    # Counting finishes at 100 s of a 120 s run, so generation may wait at most 20 s, not the full 30 s.
    now = [0]
    seen = []

    class Recorder(ScriptedProvider):
        def count_input_tokens(self, request, timeout):
            now[0] = 100
            return 10

        def create(self, request, max_output_tokens, timeout):
            seen.append(timeout)
            return super().create(request, max_output_tokens, timeout)
    events = list(agent_steps("question", conn, Recorder([final_reply(status="incomplete", rationale="x")]),
                              clock=lambda: now[0]))
    assert seen == [20] and last(events)["status"] == "incomplete"


def test_failed_request_charges_full_reservation_as_unmeasured(conn):
    # Review P3: a failed request may have generated output; charge input and maximum output.
    events, _ = run(conn, [ProviderError("APIConnectionError")])
    end = last(events)
    assert end["usage"]["output_tokens"] == Limits().max_output_tokens
    assert end["usage"]["unmeasured_tokens"] == end["usage"]["input_tokens"] + end["usage"]["output_tokens"]
    rendered = agent.render(end, conn)
    assert "unmeasured tokens charged conservatively" in rendered


def test_measured_usage_is_not_labeled_unmeasured(conn):
    events, _ = run(conn, INVESTIGATION + [final_reply(**GOOD_FINAL)])
    assert last(events)["usage"]["unmeasured_tokens"] == 0
    assert "unmeasured" not in agent.render(last(events), conn)


def test_invalid_input_makes_no_provider_calls(conn):
    for question in ("", "x" * 501):
        events, provider = run(conn, [], question=question)
        assert last(events)["status"] == "invalid_input" and provider.counts == 0


def test_invalid_json_final_is_rejected(conn):
    bad = ModelReply(status="completed", text="Ask Skyler.", input_tokens=10, output_tokens=5)
    events, _ = run(conn, [bad, bad])
    assert last(events)["status"] == "invalid_recommendation"


def test_run_leaves_database_unchanged(seed_db, conn):
    before = hashlib.sha256(seed_db.read_bytes()).hexdigest()
    run(conn, INVESTIGATION + [final_reply(**GOOD_FINAL)])
    assert hashlib.sha256(seed_db.read_bytes()).hexdigest() == before


def test_cli_without_configuration_reports_unavailable(monkeypatch, capsys, seed_db):
    monkeypatch.setattr(agent, "provider_from_env", lambda: (_ for _ in ()).throw(ProviderError("OPENAI_API_KEY and OPENAI_MODEL must be set in .env")))
    monkeypatch.setattr(agent.database, "DB_PATH", seed_db)
    assert agent.main(["Who covers?"]) == 1
    assert "provider_unavailable" in capsys.readouterr().out
    assert agent.main(["--help"]) == 0


def test_cli_output_survives_non_cp1252_model_text(monkeypatch, seed_db):
    # Phase 3 finding: piped/redirected Windows output defaults to cp1252, and model text such as
    # "≤" or "→" crashed the CLI after the paid calls. main() now writes UTF-8 with replacement.
    import io
    import sys
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    final = dict(GOOD_FINAL, rationale="Skyler stays ≤ 20h → safest first ask.")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(agent.database, "DB_PATH", seed_db)
    monkeypatch.setattr(agent, "provider_from_env",
                        lambda: (ScriptedProvider(INVESTIGATION + [final_reply(**final)]), "scripted"))
    assert agent.main(["Jordan called out of Andromeda Oct 12 evening"]) == 0
    stream.flush()
    assert "≤ 20h →" in raw.getvalue().decode("utf-8")
