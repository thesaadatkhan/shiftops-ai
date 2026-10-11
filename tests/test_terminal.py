"""Narrow, readable terminal output; printed limits come from LIMITS; redirected output survives UTF-8."""

import io
import sys

import pytest

import agent
import pricing
from agent import agent_steps
from scripted import S1_INVESTIGATION, S1_QUESTION, ScriptedProvider, final_reply, find_args, s1_answer, tools_reply

RATES = pricing.rates_for("gpt-5.6-luna")


def flat(text):
    """Wrapped lines joined back into one string, for checking content regardless of where lines break."""
    return " ".join(text.split())


def trace(conn, replies, width=76, question=S1_QUESTION, **kwargs):
    events = list(agent_steps(question, conn, ScriptedProvider(replies), rates=RATES, **kwargs))
    return "\n".join(agent.render(e, conn, width) for e in events)


@pytest.mark.parametrize("width", [76, 50])
def test_trace_stays_within_the_width(conn, width):
    text = trace(conn, S1_INVESTIGATION + [s1_answer()], width)
    assert max(len(line) for line in text.splitlines()) <= width
    assert not any(line.startswith(" Step") for line in text.splitlines())   # left-aligned step labels


def test_steps_are_model_calls_and_can_hold_several_actions(conn):
    text = trace(conn, S1_INVESTIGATION + [s1_answer()])
    assert [line.split(" |")[0] for line in text.splitlines() if line.startswith("Step-")] == \
           ["Step-1", "Step-2", "Step-3", "Step-4"]
    step3 = flat(text.split("Step-3")[1].split("Step-4")[0])
    assert step3.count("action: ") == 3       # three tool actions, one paid model call
    assert "action: get_preferences | shift_id AND-1012-EVE | worker_ids W05, W09, W14, W21" in step3


def test_trace_shows_usage_cost_and_python_facts(conn):
    text = flat(trace(conn, S1_INVESTIGATION + [s1_answer()]))
    assert "tokens in 800 | out 60 | cache read 0 / write 0" in text
    assert "est. $0.000232 this step | $0.000232 so far" in text
    assert "resolved | Andromeda Evening | Oct 12, 6 PM–11 PM | Jordan Lee" in text
    assert "Dakota Lin (W21) | 15h now -> 20h after | 0h left" in text
    assert "Shift: Andromeda Evening | Mon Oct 12, 6 PM–11 PM | AND-1012-EVE" in text
    assert "Ask first: Skyler Moreno (W14) | 5h now -> 10h after | 10h left of 20h" in text
    assert "Estimated API cost: $0.001176 (estimate, not an invoice)" in text
    assert "{" not in text   # no raw JSON blobs


def test_overnight_shift_shows_both_dates(conn):
    overnight = tools_reply(("find_shift", find_args(date="2026-10-12", hall="Andromeda", shift_name="Overnight")))
    text = flat(trace(conn, [overnight, final_reply(status="incomplete", explanation="stop")]))
    assert "Oct 12, 11 PM–Oct 13, 4 AM" in text


def test_printed_limits_come_from_the_enforced_values(conn):
    text = flat(trace(conn, [final_reply(status="incomplete", explanation="x")], limits={"model_calls": 7, "total_tokens": 9000}))
    assert "Limits: 7 model calls | 12 tool calls" in text and "9,000 total tokens" in text


def test_clarification_and_unknown_cost_are_shown(conn):
    alex = tools_reply(("find_shift", find_args(date="2026-10-17", hall="Andromeda", worker="Alex")))
    ask = final_reply(status="clarification", question="Alex Chen or Alex Rivera?")
    events = list(agent_steps("Alex called out of Andromeda on Saturday.", conn, ScriptedProvider([alex, ask]),
                              rates=None, ask=lambda q: ""))
    text = flat("\n".join(agent.render(e, conn) for e in events))
    assert "Question for you: Alex Chen or Alex Rivera?" in text
    assert "Alex Chen (W02) | Andromeda Morning | Oct 17, 8 AM–1 PM" in text
    assert "STOPPED (clarification_unanswered)" in text and "Estimated API cost: unknown" in text


def test_terminal_width_is_capped_and_floored(monkeypatch):
    monkeypatch.setattr(agent.shutil, "get_terminal_size", lambda fallback: type("S", (), {"columns": 200})())
    assert agent.terminal_width() == 76
    monkeypatch.setattr(agent.shutil, "get_terminal_size", lambda fallback: type("S", (), {"columns": 30})())
    assert agent.terminal_width() == 30   # narrower terminals are honored
    monkeypatch.setattr(agent.shutil, "get_terminal_size", lambda fallback: type("S", (), {"columns": 5})())
    assert agent.terminal_width() == 20   # with a small sensible minimum


def test_redirected_output_survives_non_cp1252_text(monkeypatch, seed_db):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(agent.database, "DB_PATH", seed_db)
    answer = s1_answer(explanation="Skyler stays ≤ 20h → safest first ask.")
    monkeypatch.setattr(agent, "provider_from_env", lambda: (ScriptedProvider(S1_INVESTIGATION + [answer]), "scripted"))
    assert agent.main([S1_QUESTION], ask=None) == 0
    stream.flush()
    assert "≤ 20h →" in raw.getvalue().decode("utf-8")


def test_quoted_question_with_a_scripted_supervisor_reply(monkeypatch, capsys, seed_db):
    monkeypatch.setattr(agent.database, "DB_PATH", seed_db)
    alex = tools_reply(("find_shift", find_args(date="2026-10-17", hall="Andromeda", worker="Alex")))
    chen = tools_reply(("find_shift", find_args(date="2026-10-17", hall="Andromeda", worker="Alex Chen")))
    replies = [alex, final_reply(status="clarification", question="Alex Chen or Alex Rivera?"), chen,
               tools_reply(("get_eligible_candidates", {"shift_id": "AND-1017-MOR"})),
               tools_reply(("get_coverage_history", {"shift_id": "AND-1017-MOR", "worker_ids": ["W27"], "lookback_days": 14})),
               final_reply(shift_id="AND-1017-MOR", ask_first="W27", backup=None, explanation="Most hours left.")]
    monkeypatch.setattr(agent, "provider_from_env", lambda: (ScriptedProvider(replies), "scripted"))
    assert agent.main(["Alex called out of Andromeda on Saturday."], ask=lambda q: "Alex Chen") == 0
    out = flat(capsys.readouterr().out)
    assert "Your reply: Alex Chen" in out and "Ask first: Jesse Park (W27)" in out


def test_help_and_missing_configuration(monkeypatch, capsys, seed_db):
    assert agent.main(["--help"]) == 0
    monkeypatch.setattr(agent.database, "DB_PATH", seed_db)
    monkeypatch.setattr(agent, "provider_from_env",
                        lambda: (_ for _ in ()).throw(agent.ProviderError("OPENAI_API_KEY and OPENAI_MODEL must be set in .env")))
    assert agent.main(["Who covers?"]) == 1
    assert "provider_unavailable" in capsys.readouterr().out



def test_no_candidate_trace_shows_grouped_exclusion_reasons(conn):
    drew = [tools_reply(("find_shift", find_args(date="2026-10-16", hall="Capella", shift_name="Afternoon", worker="Drew"))),
            tools_reply(("get_eligible_candidates", {"shift_id": "CAP-1016-AFT"})),
            final_reply(status="no_eligible_candidate", shift_id="CAP-1016-AFT", explanation="Everyone is excluded.")]
    text = flat(trace(conn, drew, question="Drew called out of Capella on Friday afternoon."))
    assert "0 eligible | 30 excluded" in text   # 29 others + the worker who called out
    assert "excluded, approved leave: W05, W09, W14, W28" in text
    assert "excluded, over 20h cap:" in text and "excluded, class overlap:" in text
    assert "NO ELIGIBLE REPLACEMENT (confirmed by Python)" in text


def test_history_trace_shows_every_outcome_and_dated_requests(conn):
    text = flat(trace(conn, S1_INVESTIGATION + [s1_answer()]))
    assert "Riley Nguyen | asked 3 | completed 3 | accepted 0 | declined 0 | pending 0" in text
    assert "asked Oct 11: completed | Vega Morning, Oct 12, 8 AM–1 PM" in text
    assert "asked Oct 09: declined | Capella Afternoon, Oct 10, 1 PM–6 PM" in text


def test_malformed_list_arguments_render_readably(conn):
    event = {"type": "tool", "name": "get_preferences", "arguments": '{"shift_id": "AND-1012-EVE", "worker_ids": [123, {}]}',
             "result": {"error": "bad worker_ids"}}
    assert "worker_ids 123, {}" in flat(agent.render(event, conn))



@pytest.mark.parametrize("stdin_tty, stdout_tty, prompted", [(True, True, True), (True, False, False), (False, True, False)])
def test_replies_are_requested_only_when_someone_can_see_and_type(monkeypatch, capsys, seed_db, stdin_tty, stdout_tty, prompted):
    # Phase C live run 5: stdin from NUL looked like a terminal while output went to a file, so the question was
    # invisible to the person who would have to answer it.
    monkeypatch.setattr(agent.database, "DB_PATH", seed_db)
    alex = tools_reply(("find_shift", find_args(date="2026-10-17", hall="Andromeda", worker="Alex")))
    replies = [alex, final_reply(status="clarification", question="Alex Chen or Alex Rivera?")]
    monkeypatch.setattr(agent, "provider_from_env", lambda: (ScriptedProvider(replies), "scripted"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: stdin_tty, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: stdout_tty, raising=False)
    asked = []
    monkeypatch.setattr(agent, "ask_supervisor", lambda question: asked.append(question) or "")
    agent.main(["Alex called out of Andromeda on Saturday."])
    out = flat(capsys.readouterr().out)
    assert bool(asked) == prompted
    assert ("CLARIFICATION NEEDED" in out) == (not prompted)   # otherwise the question is the final answer



def test_overnight_exclusion_lists_each_worker_once(conn):
    # Phase C live run 6: W30's timetable is unconfirmed for both dates the overnight shift touches.
    import json, tools
    result = tools.run_tool(conn, "get_eligible_candidates", json.dumps({"shift_id": "AND-1015-OVN"}))
    lines = agent.result_lines(conn, "get_eligible_candidates", result)
    assert "excluded, timetable not confirmed: W30" in lines
