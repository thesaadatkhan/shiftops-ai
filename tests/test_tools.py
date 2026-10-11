"""The five tools return structured facts, respect history timing, and turn bad calls into errors."""

import json
import sqlite3

import pytest

import database
import tools
from conftest import tiny_seed
from scripted import find_args


def call(conn, name, **args):
    return tools.run_tool(conn, name, json.dumps(args))


def find(conn, **fields):
    return call(conn, "find_shift", **find_args(**fields))


# ----- find_shift -----

def test_find_shift_resolves_worker_and_shows_time_range(conn):
    r = find(conn, date="2026-10-12", hall="Andromeda", shift_name="Evening", worker="Jordan")
    assert r["status"] == "resolved"
    assert r["resolved"] == {"shift_id": "AND-1012-EVE", "worker_id": "W01", "name": "Jordan Lee"}
    assert r["shifts"][0]["time_range"] == "Oct 12, 6 PM–11 PM"


def test_find_shift_reports_two_alexes_as_ambiguous(conn):
    r = find(conn, date="2026-10-17", hall="Andromeda", worker="Alex")
    assert r["status"] == "ambiguous"
    assert {p["worker_id"] for p in r["possible"]} == {"W02", "W03"}


def test_find_shift_reports_worker_not_holding_the_shift(conn):
    r = find(conn, date="2026-10-13", hall="Capella", shift_name="Afternoon", worker="Sam")
    assert r["status"] == "not_assigned"
    assert any(a["shift_id"] == "SIR-1013-MOR" for a in r["worker_assignments"])


def test_one_date_can_match_two_early_morning_shifts(conn):
    r = find(conn, date="2026-10-12", hall="Andromeda", shift_name="Early Morning")
    assert r["status"] == "worker_needed"
    assert [s["shift_id"] for s in r["shifts"]] == ["AND-1011-EM", "AND-1012-EM"]


def test_find_shift_input_problems(conn):
    assert "14 shifts match" in find(conn, date="2026-10-14")["error"]  # 13 by operating day + AND-1013-EM at 4 AM
    assert "error" in find(conn, date="Oct 14")
    assert "error" in find(conn, hall="Vega")
    assert "error" in find(conn, date="2026-10-12", hall="Mars")
    assert find(conn, date="2026-10-12", hall="Vega", shift_name="Morning", worker="Zed")["status"] == "no_worker_match"


# ----- get_eligible_candidates -----

def test_eligible_candidates_for_the_demo_scenarios(conn):
    s1 = call(conn, "get_eligible_candidates", shift_id="AND-1012-EVE")
    assert s1["outgoing"] == {"worker_id": "W01", "name": "Jordan Lee"}
    assert [c["worker_id"] for c in s1["eligible"]] == ["W05", "W09", "W14", "W21"]  # ID order, not a ranking
    dakota = next(c for c in s1["eligible"] if c["worker_id"] == "W21")
    assert (dakota["assigned_hours_now"], dakota["hours_after_covering"], dakota["hours_left_after_covering"]) == (15, 20, 0)
    casey = next(x for x in s1["excluded"] if x["worker_id"] == "W08")
    assert any("18h assigned + 5h shift = 23h > 20h cap" in r for r in casey["reasons"])

    s2 = call(conn, "get_eligible_candidates", shift_id="HEL-1013-MOR")
    assert [c["worker_id"] for c in s2["eligible"]] == ["W14", "W21", "W29"]
    assert call(conn, "get_eligible_candidates", shift_id="CAP-1016-AFT")["eligible"] == []
    assert "unknown shift" in call(conn, "get_eligible_candidates", shift_id="XYZ")["error"]


def test_every_week_result_fits_the_size_limit(conn):
    for row in conn.execute("SELECT shift_id FROM assignments a JOIN shifts s ON s.id = a.shift_id"
                            " WHERE s.start_at >= '2026-10-11'"):
        assert "error" not in call(conn, "get_eligible_candidates", shift_id=row["shift_id"])


# ----- context tools -----

def test_preferences_missing_means_neutral(conn):
    r = call(conn, "get_preferences", shift_id="AND-1012-EVE", worker_ids=["W05", "W09"])
    assert [(p["worker_id"], p["level"], p["stored"]) for p in r["preferences"]] == [("W05", "preferred", True),
                                                                                     ("W09", "neutral", False)]


@pytest.mark.parametrize("name, extra", [("get_preferences", {}), ("get_coverage_history", {"lookback_days": 14}),
                                         ("get_nearby_classes", {"window_minutes": 120})])
def test_context_tools_only_accept_eligible_workers(conn, name, extra):
    assert "not eligible" in call(conn, name, shift_id="AND-1012-EVE", worker_ids=["W08"], **extra)["error"]


def test_history_uses_only_what_was_known_before_the_shift(conn):
    early = call(conn, "get_coverage_history", shift_id="AND-1012-EVE", worker_ids=["W05", "W14"], lookback_days=14)
    riley, skyler = early["workers"]
    assert riley["counts"]["completed"] == 3
    assert skyler["counts"] == {"asked": 2, "declined": 2, "accepted": 0, "completed": 0, "pending": 0}  # not the Oct 13 decline
    later = call(conn, "get_coverage_history", shift_id="SIR-1015-MOR", worker_ids=["W14"], lookback_days=14)
    assert later["workers"][0]["counts"]["declined"] == 3


def test_history_lookback_is_bounded(conn):
    short = call(conn, "get_coverage_history", shift_id="AND-1012-EVE", worker_ids=["W05"], lookback_days=2)
    assert short["workers"][0]["counts"]["completed"] == 1
    assert "error" in call(conn, "get_coverage_history", shift_id="AND-1012-EVE", worker_ids=["W05"], lookback_days=365)


def test_nearby_classes_report_exact_gaps(conn):
    r = call(conn, "get_nearby_classes", shift_id="AND-1012-EVE", worker_ids=["W09", "W14"], window_minutes=120)
    assert r["workers"][0]["nearby_classes"] == [{"course": "CSCI 340", "relation": "ends before shift", "gap_minutes": 15}]
    assert r["workers"][1]["nearby_classes"] == []
    r2 = call(conn, "get_nearby_classes", shift_id="HEL-1013-MOR", worker_ids=["W29"], window_minutes=120)
    assert r2["workers"][0]["nearby_classes"][0]["gap_minutes"] == 10


def test_history_outcome_timing_with_small_fixture():
    # Target SPECIAL-7H starts Wed 2026-10-14 11:00; W04 is eligible for it.
    seed = tiny_seed()
    seed["shifts"] += [
        {"id": "THU-EMERG", "hall": "Vega", "name": "Morning", "start": "2026-10-15 08:00", "end": "2026-10-15 12:00", "assigned": "W04"},
        {"id": "FRI-EMERG", "hall": "Vega", "name": "Afternoon", "start": "2026-10-16 13:00", "end": "2026-10-16 17:00", "assigned": None}]
    seed["coverage_requests"] = [
        # completed on Thursday: still pending when the Wednesday shift starts
        {"id": 1, "worker_id": "W04", "shift_id": "THU-EMERG", "asked_at": "2026-10-12 10:00", "outcome": "completed", "resolved_at": "2026-10-15 12:00"},
        # declined on Monday for a future shift: already known, so kept as a decline
        {"id": 2, "worker_id": "W04", "shift_id": "FRI-EMERG", "asked_at": "2026-10-12 11:00", "outcome": "declined", "resolved_at": "2026-10-12 11:30"},
        # asked after the shift started: invisible
        {"id": 3, "worker_id": "W04", "shift_id": "FRI-EMERG", "asked_at": "2026-10-14 12:00", "outcome": "pending", "resolved_at": None}]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(database.SCHEMA)
    database.load_seed(conn, seed)
    r = tools.get_coverage_history(conn, {"shift_id": "SPECIAL-7H", "worker_ids": ["W04"], "lookback_days": 7})
    assert r["workers"][0]["counts"] == {"asked": 2, "declined": 1, "accepted": 0, "completed": 0, "pending": 1}
    assert [req["outcome"] for req in r["workers"][0]["requests"]] == ["pending", "declined"]


# ----- run_tool -----

def test_bad_calls_become_error_observations(conn):
    assert "unknown tool" in tools.run_tool(conn, "best_candidate", "{}")["error"]
    assert "not valid JSON" in tools.run_tool(conn, "find_shift", "{oops")["error"]
    assert "JSON object" in tools.run_tool(conn, "find_shift", "[1, 2]")["error"]
    assert "under" in tools.run_tool(conn, "find_shift", json.dumps({"worker": "x" * 2000}))["error"]
    assert "worker_ids" in call(conn, "get_preferences", shift_id="AND-1012-EVE", worker_ids="W05")["error"]


def test_only_the_five_tools_are_registered():
    assert set(tools.TOOLS) == {"find_shift", "get_eligible_candidates", "get_preferences",
                                "get_coverage_history", "get_nearby_classes"}


@pytest.mark.parametrize("worker_ids, message", [
    ([{}], "list of 1-16 IDs"), ([["W05"]], "list of 1-16 IDs"), ([5], "list of 1-16 IDs"),
    (["W05", "W05"], "must not repeat"),
])
def test_malformed_worker_lists_are_controlled_errors(conn, worker_ids, message):
    assert message in call(conn, "get_preferences", shift_id="AND-1012-EVE", worker_ids=worker_ids)["error"]
    assert "preferences" in call(conn, "get_preferences", shift_id="AND-1012-EVE", worker_ids=["W05", "W09"])
