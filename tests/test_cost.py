"""Estimated API cost from measured usage, and time-range labels from stored timestamps."""

from datetime import datetime

import pytest

import eligibility
import pricing
from agent import ModelReply, ProviderError, agent_steps
from scripted import S1_FIND, S1_INVESTIGATION, S1_QUESTION, ScriptedProvider, final_reply

RATES = pricing.rates_for("gpt-5.6-luna")
D = datetime


@pytest.mark.parametrize("start, end, expected", [
    (D(2026, 10, 12, 8), D(2026, 10, 12, 13), "Oct 12, 8 AM–1 PM"),
    (D(2026, 10, 12, 18), D(2026, 10, 12, 23), "Oct 12, 6 PM–11 PM"),
    (D(2026, 10, 12, 23), D(2026, 10, 13, 4), "Oct 12, 11 PM–Oct 13, 4 AM"),
    (D(2026, 10, 14, 11), D(2026, 10, 14, 18, 30), "Oct 14, 11 AM–6:30 PM"),
    (D(2026, 10, 14, 12), D(2026, 10, 15, 0), "Oct 14, 12 PM–Oct 15, 12 AM"),
])
def test_time_range_labels(start, end, expected):
    assert eligibility.format_interval(start, end) == expected


def test_rates_record_source_date_currency_and_values():
    assert (RATES["currency"], RATES["tier"], RATES["verified"]) == ("USD", "Standard", "2026-10-10")
    assert RATES["per_million"] == {"input": 0.20, "cached_input": 0.02, "cache_write": 0.25, "output": 1.20}


@pytest.mark.parametrize("usage, expected", [
    ((1351, 72, 0, 0), (1351 * 0.20 + 72 * 1.20) / 1e6),
    ((4000, 100, 3000, 0), (1000 * 0.20 + 3000 * 0.02 + 100 * 1.20) / 1e6),      # cached reads are cheaper
    ((2000, 0, 0, 1500), (500 * 0.20 + 1500 * 0.25) / 1e6),                      # cache writes cost more
    ((1634, 34, 1395, 239), (239 * 0.25 + 1395 * 0.02 + 34 * 1.20) / 1e6),        # live smoke run, call 2
    ((300_000, 1000, 0, 0), (300_000 * 0.40 + 1000 * 1.80) / 1e6),               # long-context rates
])
def test_estimate_cost(usage, expected):
    assert pricing.estimate_cost(RATES, *usage) == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize("rates, usage", [
    (None, (1000, 100, 0, 0)), (RATES, (None, 100, 0, 0)), (RATES, (1000, None, 0, 0)),
    (RATES, (1000, 100, None, 0)), (RATES, (1000, 100, 0, None)), (RATES, (1000, 100, 800, 300)),
])
def test_cost_is_unknown_rather_than_invented(rates, usage):
    assert pricing.estimate_cost(rates, *usage) is None


def test_reasoning_tokens_are_not_counted_twice(conn):
    reply = ModelReply(status="completed", text=final_reply(status="incomplete", explanation="x").text,
                       input_tokens=3990, output_tokens=462, reasoning_tokens=212, cached_tokens=0, cache_write_tokens=0)
    call = next(e for e in agent_steps(S1_QUESTION, conn, ScriptedProvider([reply]), rates=RATES)
                if e["type"] == "model_call")
    assert call["cost"] == pytest.approx((3990 * 0.20 + 462 * 1.20) / 1e6)


def test_per_step_and_cumulative_cost(conn):
    events = list(agent_steps(S1_QUESTION, conn, ScriptedProvider([S1_FIND, final_reply(status="incomplete", explanation="x")]),
                              rates=RATES))
    first = (800 * 0.20 + 60 * 1.20) / 1e6
    second = (1500 * 0.20 + 150 * 1.20) / 1e6
    calls = [e for e in events if e["type"] == "model_call"]
    assert calls[0]["cost"] == pytest.approx(first)
    assert calls[1]["usage"]["cost_usd"] == pytest.approx(first + second)
    assert events[-1]["usage"]["cost_unknown_calls"] == 0


def test_failed_request_leaves_cost_partly_unknown(conn):
    events = list(agent_steps(S1_QUESTION, conn, ScriptedProvider([S1_FIND, ProviderError("APIConnectionError")]), rates=RATES))
    usage = events[-1]["usage"]
    assert usage["cost_unknown_calls"] == 1 and usage["cost_usd"] == pytest.approx((800 * 0.20 + 60 * 1.20) / 1e6)


@pytest.mark.parametrize("input_tokens, output_tokens", [(100, None), (None, 60), (None, None)])
def test_partial_usage_has_unknown_cost_and_conservative_tokens(conn, input_tokens, output_tokens):
    reply = ModelReply(status="incomplete", incomplete_reason="content_filter", input_tokens=input_tokens,
                       output_tokens=output_tokens, cached_tokens=0, cache_write_tokens=0)
    events = list(agent_steps(S1_QUESTION, conn, ScriptedProvider([reply]), rates=RATES))
    call = next(e for e in events if e["type"] == "model_call")
    reserved_input = call["reserved"] - 1000
    usage = events[-1]["usage"]
    assert call["cost"] is None and usage["cost_unknown_calls"] == 1
    assert usage["unmeasured_tokens"] == (0 if input_tokens is not None else reserved_input) + \
                                         (0 if output_tokens is not None else 1000)


def test_cost_display_does_not_change_the_token_budget(conn):
    probe = list(agent_steps(S1_QUESTION, conn, ScriptedProvider([S1_FIND, final_reply(status="incomplete", explanation="x")])))
    reserved = next(e for e in probe if e["type"] == "model_call")["reserved"]
    for rates in (RATES, None):
        provider = ScriptedProvider(S1_INVESTIGATION)
        events = list(agent_steps(S1_QUESTION, conn, provider, limits={"total_tokens": reserved + 100}, rates=rates))
        assert events[-1]["status"] == "token_limit" and len(provider.requests) == 1
