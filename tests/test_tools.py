"""The five tools return bounded facts with evidence IDs, and turn bad input into error observations."""

import json

import tools


def call(conn, name, **args):
    return tools.dispatch(conn, name, json.dumps(args))


def test_find_shift_resolves_assigned_worker(conn):
    r = call(conn, "find_shift", date="2026-10-12", hall="Andromeda", shift_name="Evening", worker="Jordan")
    assert r["resolution"] == "resolved"
    assert r["resolved"] == {"shift_id": "AND-1012-EVE", "outgoing_worker_id": "W01", "name": "Jordan Lee"}


def test_find_shift_reports_ambiguous_worker(conn):
    r = call(conn, "find_shift", date="2026-10-17", hall="Andromeda", worker="Alex")
    assert r["resolution"] == "ambiguous"
    assert {p["worker_id"] for p in r["possible_pairs"]} == {"W02", "W03"}


def test_find_shift_reports_mismatch_with_actual_assignments(conn):
    r = call(conn, "find_shift", date="2026-10-13", hall="Capella", shift_name="Afternoon", worker="Sam")
    assert r["resolution"] == "mismatch"
    assert any(a["shift_id"] == "SIR-1013-MOR" for a in r["matched_workers_nearby_assignments"])


def test_find_shift_early_morning_date_is_ambiguous(conn):
    # "Oct 12 early morning" could mean 04:00 on Oct 12 (operating day Oct 11) or op day Oct 12 (04:00 Oct 13).
    r = call(conn, "find_shift", date="2026-10-12", hall="Andromeda", shift_name="Early Morning")
    assert r["resolution"] == "ambiguous_shift"
    assert [s["shift_id"] for s in r["shifts"]] == ["AND-1011-EM", "AND-1012-EM"]


def test_find_shift_requires_narrowing_and_valid_inputs(conn):
    assert "error" in call(conn, "find_shift", date="2026-10-14")  # 13 shifts match
    assert "error" in call(conn, "find_shift", date="Oct 14")
    assert "error" in call(conn, "find_shift", hall="Vega")
    assert call(conn, "find_shift", date="2026-10-12", hall="Vega", shift_name="Morning", worker="Zed")["resolution"] == "no_worker_match"


def test_eligible_candidates_have_evidence_ids(conn):
    r = call(conn, "get_eligible_candidates", shift_id="AND-1012-EVE", outgoing_worker_id="W01")
    assert r["eligible_count"] == 4
    assert r["eligible"][0]["evidence_id"] == "ELIG:AND-1012-EVE:W05"
    assert "SHIFT:AND-1012-EVE" in tools.evidence_ids(r)


def test_eligibility_mismatch_is_an_observation(conn):
    r = call(conn, "get_eligible_candidates", shift_id="CAP-1013-AFT", outgoing_worker_id="W04")
    assert "not assigned" in r["error"]


def test_preferences_absent_means_neutral(conn):
    r = call(conn, "get_preferences", shift_id="AND-1012-EVE", candidate_ids=["W05", "W09"])
    levels = {p["worker_id"]: (p["level"], p["stored"]) for p in r["preferences"]}
    assert levels == {"W05": ("preferred", True), "W09": ("neutral", False)}


def test_context_tools_only_accept_eligible_candidates(conn):
    r = call(conn, "get_preferences", shift_id="AND-1012-EVE", candidate_ids=["W08"])  # 18h worker
    assert "not eligible" in r["error"]


def test_history_uses_only_events_before_target(conn):
    early = call(conn, "get_coverage_history", shift_id="AND-1012-EVE", candidate_ids=["W05", "W14"], lookback_days=14)
    by_worker = {w["worker_id"]: w for w in early["workers"]}
    assert by_worker["W05"]["counts"]["completed"] == 3
    assert [r["evidence_id"] for r in by_worker["W14"]["requests"]] == ["REQ:5", "REQ:7"]  # not REQ:10 (Oct 13)

    later = call(conn, "get_coverage_history", shift_id="SIR-1015-MOR", candidate_ids=["W14"], lookback_days=14)
    assert "REQ:10" in tools.evidence_ids(later)


def test_history_lookback_window_is_bounded(conn):
    short = call(conn, "get_coverage_history", shift_id="AND-1012-EVE", candidate_ids=["W05"], lookback_days=2)
    assert short["workers"][0]["counts"]["completed"] == 1  # only the Oct 11 request
    assert "error" in call(conn, "get_coverage_history", shift_id="AND-1012-EVE", candidate_ids=["W05"], lookback_days=365)


def test_nearby_classes_reports_exact_gap(conn):
    r = call(conn, "get_nearby_classes", shift_id="AND-1012-EVE", candidate_ids=["W09", "W14"], window_minutes=120)
    jamie = next(w for w in r["workers"] if w["worker_id"] == "W09")
    assert jamie["nearby_classes"][0]["gap_minutes"] == 15
    assert next(w for w in r["workers"] if w["worker_id"] == "W14")["nearby_classes"] == []

    r2 = call(conn, "get_nearby_classes", shift_id="HEL-1013-MOR", candidate_ids=["W29"], window_minutes=120)
    assert r2["workers"][0]["nearby_classes"][0]["gap_minutes"] == 10
    assert r2["workers"][0]["nearby_classes"][0]["relation"] == "starts after shift"


def test_dispatch_turns_bad_calls_into_errors(conn):
    assert "unknown tool" in tools.dispatch(conn, "best_candidate", "{}")["error"]
    assert "not valid JSON" in tools.dispatch(conn, "find_shift", "{not json")["error"]
    assert "JSON object" in tools.dispatch(conn, "find_shift", "[1, 2]")["error"]
    assert "at most" in tools.dispatch(conn, "find_shift", json.dumps({"worker": "x" * 2000}))["error"]
    assert "candidate_ids" in call(conn, "get_preferences", shift_id="AND-1012-EVE", candidate_ids="W05")["error"]
    assert "unknown shift" in call(conn, "get_eligible_candidates", shift_id="XYZ", outgoing_worker_id="W01")["error"]


def test_no_tool_named_like_a_ranking():
    assert set(tools.TOOLS) == {"find_shift", "get_eligible_candidates", "get_preferences",
                                "get_coverage_history", "get_nearby_classes", "get_schedule"}


# ----- regression tests: history is an as-of view (review P2) -----

def _history_conn(requests, extra_shifts=()):
    import sqlite3
    import database
    from conftest import tiny_seed
    seed = tiny_seed()
    seed["shifts"] += list(extra_shifts)
    seed["coverage_requests"] = requests
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(database.SCHEMA)
    database.load_seed(connection, seed)
    return connection


EMERGENCY_SHIFTS = [
    {"id": "THU-EMERG", "hall": "Vega", "name": "Morning", "start": "2026-10-15 08:00", "end": "2026-10-15 12:00", "assigned": "W04"},
    {"id": "FRI-EMERG", "hall": "Vega", "name": "Afternoon", "start": "2026-10-16 13:00", "end": "2026-10-16 17:00", "assigned": None},
]


def test_history_shows_outcomes_only_as_known_at_target_start():
    # Target SPECIAL-7H starts Wed 2026-10-14 11:00. W04 is eligible for it.
    conn = _history_conn([
        # Completed on Thursday: at Wednesday 11:00 it was still pending, so "completed" must not leak.
        {"id": 1, "worker_id": "W04", "shift_id": "THU-EMERG", "asked_at": "2026-10-12 10:00",
         "outcome": "completed", "resolved_at": "2026-10-15 12:00"},
        # Declined on Monday for a FUTURE shift: already known, so it is kept as a decline.
        {"id": 2, "worker_id": "W04", "shift_id": "FRI-EMERG", "asked_at": "2026-10-12 11:00",
         "outcome": "declined", "resolved_at": "2026-10-12 11:30"},
        # Asked before the target but declined after it started: pending as of the target.
        {"id": 3, "worker_id": "W04", "shift_id": "FRI-EMERG", "asked_at": "2026-10-14 09:00",
         "outcome": "declined", "resolved_at": "2026-10-14 12:00"},
        # Asked after the target started: invisible.
        {"id": 4, "worker_id": "W04", "shift_id": "FRI-EMERG", "asked_at": "2026-10-14 12:00",
         "outcome": "pending", "resolved_at": None},
    ], EMERGENCY_SHIFTS)
    result = tools.get_coverage_history(conn, {"shift_id": "SPECIAL-7H", "candidate_ids": ["W04"], "lookback_days": 7})
    worker = result["workers"][0]
    assert worker["counts"] == {"asked": 3, "declined": 1, "accepted": 0, "completed": 0, "pending": 2}
    outcomes = {r["evidence_id"]: (r["outcome"], r["outcome_known_at"]) for r in worker["requests"]}
    assert outcomes == {"REQ:1": ("pending", None), "REQ:2": ("declined", "2026-10-12 11:30"),
                        "REQ:3": ("pending", None)}


def test_seed_completions_are_visible_only_after_the_emergency_shift_ended(conn):
    # REQ:9 (Riley, Vega Mon 08:00-13:00) completed at 13:00: visible for an 18:00 target...
    r = call(conn, "get_coverage_history", shift_id="AND-1012-EVE", candidate_ids=["W05"], lookback_days=14)
    req9 = next(x for x in r["workers"][0]["requests"] if x["evidence_id"] == "REQ:9")
    assert req9["outcome"] == "completed" and req9["outcome_known_at"] == "2026-10-12 13:00"
