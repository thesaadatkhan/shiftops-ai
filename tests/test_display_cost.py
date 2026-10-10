"""Time-range display, weekly-capacity wording, and estimated cost (all offline, SCRIPTED replies)."""

from datetime import datetime

import pytest

import agent
import eligibility
import pricing
import tools
from agent import Limits, ModelReply, ProviderError, agent_steps
from scripted import ScriptedProvider, final_reply, tools_reply
from test_agent import GOOD_FINAL, INVESTIGATION

RATES = pricing.rates_for("gpt-5.6-luna")
D = datetime


# ----- time ranges generated from stored timestamps -----

@pytest.mark.parametrize("start, end, expected", [
    (D(2026, 10, 12, 8), D(2026, 10, 12, 13), "Oct 12, 8 AM–1 PM"),
    (D(2026, 10, 12, 18), D(2026, 10, 12, 23), "Oct 12, 6 PM–11 PM"),
    (D(2026, 10, 12, 23), D(2026, 10, 13, 4), "Oct 12, 11 PM–Oct 13, 4 AM"),
    (D(2026, 10, 13, 4), D(2026, 10, 13, 8), "Oct 13, 4 AM–8 AM"),
    (D(2026, 10, 14, 11), D(2026, 10, 14, 18, 30), "Oct 14, 11 AM–6:30 PM"),
    (D(2026, 10, 14, 12), D(2026, 10, 15, 0), "Oct 14, 12 PM–Oct 15, 12 AM"),
])
def test_format_interval(start, end, expected):
    assert eligibility.format_interval(start, end) == expected


def test_shift_records_carry_time_range_and_keep_ids(conn):
    overnight = eligibility.get_shift(conn, "AND-1012-OVN")
    assert overnight["shift_id"] == "AND-1012-OVN"
    assert overnight["time_range"] == "Oct 12, 11 PM–Oct 13, 4 AM"
    assert eligibility.get_shift(conn, "CAP-1012-MOR")["time_range"] == "Oct 12, 8 AM–1 PM"
    assert agent.shift_line(conn, "AND-1012-EM") == "Andromeda Early Morning, Oct 13, 4 AM–8 AM (AND-1012-EM)"


def test_variable_duration_shift_displays_its_actual_interval(tiny_conn):
    assert eligibility.get_shift(tiny_conn, "SPECIAL-7H")["time_range"] == "Oct 14, 11 AM–6 PM"
    assert eligibility.get_shift(tiny_conn, "OVN")["time_range"] == "Oct 14, 11 PM–Oct 15, 4 AM"


def test_tool_observations_show_time_ranges(conn):
    found = tools.dispatch(conn, "find_shift", '{"date": "2026-10-12", "hall": "Andromeda", "shift_name": "Overnight"}')
    assert found["shifts"][0]["time_range"] == "Oct 12, 11 PM–Oct 13, 4 AM"
    assert "Andromeda Overnight, Oct 12, 11 PM–Oct 13, 4 AM" in agent.summarize("find_shift", found)
    history = tools.dispatch(conn, "get_coverage_history",
                             '{"shift_id": "AND-1012-EVE", "candidate_ids": ["W05"], "lookback_days": 14}')
    shifts = [r["emergency_shift"] for r in history["workers"][0]["requests"]]
    assert "AND-1006-OVN Andromeda Overnight Oct 6, 11 PM–Oct 7, 4 AM" in shifts


# ----- weekly capacity: assigned now, after covering, left afterwards -----

def test_eligibility_observation_distinguishes_three_hour_figures(conn):
    result = tools.dispatch(conn, "get_eligible_candidates", '{"shift_id": "AND-1012-EVE", "outgoing_worker_id": "W01"}')
    dakota = next(c for c in result["eligible"] if c["worker_id"] == "W21")
    assert (dakota["assigned_hours_now"], dakota["hours_after_covering"], dakota["hours_left_after_covering"]) == (15, 20, 0)
    assert "hours_after_covering" in result["hours_legend"] and result["weekly_cap_hours"] == 20
    line = agent.summarize("get_eligible_candidates", result)
    assert "W21 Dakota Lin: 15h assigned now -> 20h after covering, 0h left" in line
    assert line.startswith("Andromeda Evening, Oct 12, 6 PM–11 PM (5h): 4 eligible")


def test_cap_exclusion_wording_uses_assigned_hours(conn):
    result = tools.dispatch(conn, "get_eligible_candidates", '{"shift_id": "AND-1012-EVE", "outgoing_worker_id": "W01"}')
    casey = next(x for x in result["excluded"] if x.startswith("W08"))
    assert "18h assigned + 5h shift = 23h > 20h cap" in casey


def test_final_display_shows_python_computed_capacity(conn):
    events = list(agent_steps("Jordan called out of Andromeda Oct 12 evening", conn,
                              ScriptedProvider(INVESTIGATION + [final_reply(**GOOD_FINAL)]), rates=RATES))
    text = " ".join(agent.render(events[-1], conn).split())
    assert "Shift: Andromeda Evening, Oct 12, 6 PM–11 PM (AND-1012-EVE)" in text
    assert "Weekly hours, W14 Skyler Moreno: 5h assigned now -> 10h after covering, 10h left of the 20h cap" in text
    assert "Weekly hours, W21 Dakota Lin: 15h assigned now -> 20h after covering, 0h left of the 20h cap" in text


# ----- estimated cost from known usage and rate fixtures -----

def test_rates_record_source_date_currency_and_values():
    assert RATES["currency"] == "USD" and RATES["tier"] == "Standard" and RATES["verified"] == "2026-10-10"
    assert RATES["source"].startswith("https://developers.openai.com/")
    assert RATES["per_million"] == {"input": 0.20, "cached_input": 0.02, "cache_write": 0.25, "output": 1.20}


@pytest.mark.parametrize("usage, expected", [
    ((1351, 72, 0, 0), (1351 * 0.20 + 72 * 1.20) / 1e6),                       # Phase 3 run 1, call 1
    ((4000, 100, 3000, 0), (1000 * 0.20 + 3000 * 0.02 + 100 * 1.20) / 1e6),    # cached input is cheaper
    ((2000, 0, 0, 1500), (500 * 0.20 + 1500 * 0.25) / 1e6),                    # cache writes cost more
    ((200_000, 1_000_000, 0, 0), 0.04 + 1.20),
    ((272_000, 0, 0, 0), 272_000 * 0.20 / 1e6),                                # threshold itself is short context
    ((1_000_000, 1_000_000, 0, 0), 0.40 + 1.80),                              # whole request at long-context rates
    ((300_000, 1000, 0, 0), (300_000 * 0.40 + 1000 * 1.80) / 1e6),             # long-context rates over 272K
])
def test_estimate_cost_known_fixtures(usage, expected):
    assert pricing.estimate_cost(RATES, *usage) == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize("rates, usage", [
    (None, (1000, 100, 0, 0)),          # no verified price for the model
    (RATES, (None, 100, 0, 0)),         # usage not reported
    (RATES, (1000, None, 0, 0)),
    (RATES, (1000, 100, None, 0)),      # cache breakdown missing: never assume zero
    (RATES, (1000, 100, 0, None)),
    (RATES, (1000, 100, 800, 300)),     # breakdown exceeds input: inconsistent
    (RATES, (-5, 100, 0, 0)),
    (RATES, (True, 100, 0, 0)),
])
def test_estimate_cost_is_unknown_rather_than_invented(rates, usage):
    assert pricing.estimate_cost(rates, *usage) is None
    assert pricing.format_usd(None) == "unknown"


def test_reasoning_tokens_are_not_counted_twice(conn):
    reply = ModelReply(status="completed", text=final_reply(status="incomplete", rationale="x").text,
                       input_tokens=3990, output_tokens=462, reasoning_tokens=212, cached_tokens=0, cache_write_tokens=0)
    events = list(agent_steps("question", conn, ScriptedProvider([reply]), rates=RATES))
    call = next(e for e in events if e["type"] == "model_call")
    assert call["cost_usd"] == pytest.approx((3990 * 0.20 + 462 * 1.20) / 1e6)
    assert "reasoning 212 incl. in out" in agent.render(call, conn)


def test_per_call_and_cumulative_cost_in_events_and_trace(conn):
    replies = [
        tools_reply(("find_shift", {"shift_id": None, "date": "2026-10-12", "hall": "Andromeda", "shift_name": "Evening",
                                    "start_time": None, "worker": "Jordan"}), cached_tokens=300),
        final_reply(status="incomplete", rationale="stop")]
    events = list(agent_steps("Jordan called out of Andromeda Oct 12 evening", conn, ScriptedProvider(replies), rates=RATES))
    calls = [e for e in events if e["type"] == "model_call"]
    first = (500 * 0.20 + 300 * 0.02 + 60 * 1.20) / 1e6
    second = (1500 * 0.20 + 150 * 1.20) / 1e6
    assert calls[0]["cost_usd"] == pytest.approx(first) and calls[0]["cached_tokens"] == 300
    assert calls[1]["cumulative_cost_usd"] == pytest.approx(first + second)
    assert f"est. cost: ${first:.6f} this call | ${first:.6f} so far" in agent.render(calls[0], conn)
    end = events[-1]
    assert end["usage"]["cost_usd"] == pytest.approx(first + second) and end["usage"]["cost_unknown_calls"] == 0
    assert f"Estimated cost: ${first + second:.6f} (estimate, not a bill)" in agent.render(end, conn)
    assert "cache read 300 / write 0" in agent.render(calls[0], conn)


def test_cache_breakdown_shows_reads_and_writes_or_unreported(conn):
    # Live smoke finding: writes affect cost, so the trace shows them next to cached reads.
    known = ModelReply(status="completed", text=final_reply(status="incomplete", rationale="x").text,
                       input_tokens=1634, output_tokens=34, cached_tokens=1395, cache_write_tokens=239)
    events = list(agent_steps("question", conn, ScriptedProvider([known]), rates=RATES))
    call = next(e for e in events if e["type"] == "model_call")
    text = agent.render(call, conn)
    assert "(cache read 1,395 / write 239)" in text
    assert call["cost_usd"] == pytest.approx((239 * 0.25 + 1395 * 0.02 + 34 * 1.20) / 1e6)
    assert "est. cost: $0.000128 this call" in text  # matches the live smoke trace, call 2

    missing = ModelReply(status="completed", text=known.text, input_tokens=1634, output_tokens=34,
                         cached_tokens=1395, cache_write_tokens=None)
    call = next(e for e in agent_steps("question", conn, ScriptedProvider([missing]), rates=RATES)
                if e["type"] == "model_call")
    assert "cache breakdown unreported" in agent.render(call, conn) and call["cost_usd"] is None


def test_unknown_price_shows_unknown_everywhere(conn):
    events = list(agent_steps("Jordan called out of Andromeda Oct 12 evening", conn,
                              ScriptedProvider(INVESTIGATION + [final_reply(**GOOD_FINAL)]), rates=None))
    assert "no verified price" in agent.render(events[0], conn)
    assert all(e["cost_usd"] is None for e in events if e["type"] == "model_call")
    assert "Estimated cost: unknown" in agent.render(events[-1], conn)
    assert events[-1]["usage"]["input_tokens"] == 3 * 800 + 1500  # tokens are still measured and budgeted


def test_failed_request_cost_unknown_and_tokens_marked_unmeasured(conn):
    replies = [tools_reply(("find_shift", {"shift_id": "AND-1012-EVE", "date": None, "hall": None, "shift_name": None,
                                           "start_time": None, "worker": "Jordan"})),
               ProviderError("APIConnectionError")]
    events = list(agent_steps("Jordan called out of AND-1012-EVE", conn, ScriptedProvider(replies), rates=RATES))
    end = events[-1]
    assert end["status"] == "provider_unavailable" and end["usage"]["cost_unknown_calls"] == 1
    text = agent.render(end, conn)
    known = (800 * 0.20 + 60 * 1.20) / 1e6
    assert "unmeasured tokens charged conservatively" in text
    assert f"Estimated cost: ${known:.6f} for measured calls + unknown for 1" in text


def test_missing_usage_cost_unknown_but_tokens_charged(conn):
    events = list(agent_steps("question", conn, ScriptedProvider([ModelReply(status="incomplete",
                                                                             incomplete_reason="content_filter")]), rates=RATES))
    call = next(e for e in events if e["type"] == "model_call")
    assert call["cost_usd"] is None and "usage not reported" in agent.render(call, conn)
    assert events[-1]["usage"]["unmeasured_tokens"] == events[-1]["usage"]["input_tokens"] + 1000


def test_cost_display_does_not_change_token_budget(conn):
    probe = list(agent_steps("Jordan called out of Andromeda Oct 12 evening", conn,
                             ScriptedProvider(INVESTIGATION[:1] + [final_reply(status="incomplete")]), rates=RATES))
    first_reserved = next(e for e in probe if e["type"] == "model_call")["reserved"]
    for rates in (RATES, None):
        provider = ScriptedProvider(INVESTIGATION)
        events = list(agent_steps("Jordan called out of Andromeda Oct 12 evening", conn, provider,
                                  Limits(token_allowance=first_reserved + 100), rates=rates))
        assert events[-1]["status"] == "token_limit" and len(provider.requests) == 1


# ----- partial usage renders without crashing (finalization review P2) -----

@pytest.mark.parametrize("input_tokens, output_tokens, expected", [
    (100, None, "partial usage: in 100 / out unknown (max output charged)"),
    (None, 60, "partial usage: in unknown / out 60 (reserved input charged)"),
    (None, None, "usage not reported: in unknown / out unknown (reserved input and max output charged)"),
])
def test_partial_usage_renders_and_stays_conservative(conn, input_tokens, output_tokens, expected):
    reply = ModelReply(status="incomplete", incomplete_reason="content_filter", input_tokens=input_tokens,
                       output_tokens=output_tokens, cached_tokens=0, cache_write_tokens=0)
    events = list(agent_steps("question", conn, ScriptedProvider([reply]), rates=RATES))
    call = next(e for e in events if e["type"] == "model_call")
    end = events[-1]
    assert call["cost_usd"] is None and end["usage"]["cost_unknown_calls"] == 1
    text = agent.render(call, conn)
    assert expected in " ".join(text.split()) and "est. cost: unknown this call | unknown so far" in text
    reserved_input = call["reserved"] - 1000
    assert end["usage"]["input_tokens"] == (input_tokens if input_tokens is not None else reserved_input)
    assert end["usage"]["output_tokens"] == (output_tokens if output_tokens is not None else 1000)
    assert end["usage"]["unmeasured_tokens"] == ((0 if input_tokens is not None else reserved_input)
                                                 + (0 if output_tokens is not None else 1000))
    assert "Estimated cost: unknown" in agent.render(end, conn)
