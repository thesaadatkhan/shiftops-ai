"""Terminal trace usability: narrow columns, visible actions, and intact facts (no network)."""

import os

import pytest

import agent
import tools
from scripted import ScriptedProvider, final_reply
from test_agent import GOOD_FINAL, INVESTIGATION


@pytest.mark.parametrize("width", [40, 76])
def test_complete_trace_stays_in_column_and_preserves_results(conn, width):
    events = list(agent.agent_steps("Jordan called out of Andromeda Oct 12 evening", conn,
                                    ScriptedProvider(INVESTIGATION + [final_reply(**GOOD_FINAL)]),
                                    rates=agent.pricing.rates_for("gpt-5.6-luna")))
    rendered = [agent.render(event, conn, width) for event in events]
    assert all(len(line) <= width for block in rendered for line in block.splitlines())
    headers = [block.splitlines()[1] for block, event in zip(rendered, events)
               if event["type"] == "model_call"]
    assert headers == ["Step-1", "Step-2", "Step-3", "Step-4"]
    step3 = next(block for block, event in zip(rendered, events)
                 if event["type"] == "model_call" and event["step"] == 3)
    assert "Action: Call tools" in step3
    assert "get_preferences" in step3 and "get_coverage_history" in step3
    final = " ".join(rendered[-1].split())
    assert "Ask first: W14 Skyler Moreno" in final and "Backup: W21 Dakota Lin" in final
    assert "5h assigned now -> 10h after covering, 10h left" in final
    assert "ELIG:AND-1012-EVE:W14" in final and "HIST:AND-1012-EVE:W14:14d" in final
    assert "Estimated cost: $0.001176" in final
    assert events[-1]["usage"]["model_calls"] == 4 and events[-1]["usage"]["tool_calls"] == 5


def test_tool_actions_are_readable_without_null_json_filters(conn):
    event = {"type": "tool_call", "name": "find_shift",
             "arguments": '{"shift_id":null,"date":"2026-10-12","hall":"Andromeda","worker":"Jordan"}'}
    text = agent.render(event, conn)
    assert "Action: Identify the shift and outgoing worker" in text
    assert "Tool: find_shift" in text
    assert "date: 2026-10-12" in text and "worker: Jordan" in text
    assert "null" not in text and "{" not in text


@pytest.mark.parametrize("name,args,expected", [
    ("get_preferences", {"shift_id":"AND-1012-EVE","candidate_ids":["W05","W14"]}, ["Riley Nguyen", "preferred", "Skyler Moreno", "low"]),
    ("get_coverage_history", {"shift_id":"AND-1012-EVE","candidate_ids":["W05","W14"],"lookback_days":30}, ["Completed: 3", "Declined: 2"]),
    ("get_nearby_classes", {"shift_id":"AND-1012-EVE","candidate_ids":["W09"],"window_minutes":240}, ["Jamie Ortiz", "CSCI 340", "Gap: 15 minutes"]),
])
def test_context_results_have_separate_worker_records(conn, name, args, expected):
    import json
    result = tools.dispatch(conn, name, json.dumps(args))
    text = agent.render({"type":"observation","name":name,"result":result}, conn)
    assert all(part in " ".join(text.split()) for part in expected)
    assert all(len(line) <= 76 for line in text.splitlines())


def test_ambiguity_and_mismatch_stay_visible(conn):
    import json
    for worker, date, hall in [("Alex","2026-10-17","Andromeda"),("Sam","2026-10-13","Capella")]:
        result=tools.dispatch(conn,"find_shift",json.dumps({"worker":worker,"date":date,"hall":hall}))
        text=agent.render({"type":"observation","name":"find_shift","result":result},conn)
        if worker=="Alex":
            assert "Possible call-outs:" in text and "Alex Chen" in text and "Alex Rivera" in text
        else:
            assert "Result: mismatch" in text and "Sam Patel" in text and "Robin Castillo" in text


def test_terminal_width_does_not_expand_to_full_wide_screen(monkeypatch):
    monkeypatch.setattr(agent.shutil,"get_terminal_size",lambda fallback: os.terminal_size((190,40)))
    assert agent.terminal_width()==76
    monkeypatch.setattr(agent.shutil,"get_terminal_size",lambda fallback: os.terminal_size((42,40)))
    assert agent.terminal_width()==40


def test_cli_header_help_and_trace_wrap_together(monkeypatch, capsys, seed_db):
    monkeypatch.setattr(agent,"terminal_width",lambda:40)
    monkeypatch.setattr(agent.database,"DB_PATH",seed_db)
    monkeypatch.setattr(agent,"provider_from_env",lambda:(
        ScriptedProvider(INVESTIGATION+[final_reply(**GOOD_FINAL)]),"scripted"))
    assert agent.main(["Jordan called out for Andromeda on October 12 evening. Who should I ask first?"])==0
    text=capsys.readouterr().out
    assert all(len(line)<=40 for line in text.splitlines())
    assert "Step-1" in text and "Step-4" in text
    assert agent.main(["--help"])==0
    assert all(len(line)<=40 for line in capsys.readouterr().out.splitlines())


def test_wrapping_does_not_drop_long_error_or_argument_content(conn):
    message="x"*180
    text=agent.render({"type":"observation","name":"find_shift","result":{"error":message}},conn,40)
    assert all(len(line)<=40 for line in text.splitlines())
    assert "".join(text.split()).endswith(message)
