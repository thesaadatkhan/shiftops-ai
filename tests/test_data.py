"""The seed builds into a valid, genuinely staffed baseline week, and initialization is safe."""

import json
import sqlite3
from datetime import datetime

import pytest

import database
import eligibility


def test_seed_has_thirty_workers_and_full_week(conn):
    assert conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 30
    # 7 operating days x 13 shifts (8 day-hall + 5 Andromeda), each with exactly one worker.
    rows = conn.execute(
        "SELECT s.id, a.worker_id FROM shifts s LEFT JOIN assignments a ON a.shift_id = s.id"
        " WHERE s.start_at >= '2026-10-12 08:00' AND s.start_at < '2026-10-19 08:00'"
    ).fetchall()
    assert len(rows) == 91
    assert all(row["worker_id"] for row in rows)


def test_baseline_is_valid_and_448_hours(conn):
    assert database.validate_structure(conn) == []
    assert eligibility.validate_baseline(conn) == []
    week = datetime(2026, 10, 12)
    hours = {w["id"]: eligibility.weekly_minutes(conn, w["id"], week)
             for w in conn.execute("SELECT id FROM workers")}
    assert sum(hours.values()) == 448 * 60  # charged by start time, including Mon 04:00 early morning
    assert max(hours.values()) <= 20 * 60


def test_init_refuses_existing_database_without_reset(seed_db):
    with pytest.raises(FileExistsError):
        database.init_demo(seed_db)


def test_reset_rebuilds(tmp_path):
    path = tmp_path / "db.sqlite"
    database.init_demo(path)
    database.init_demo(path, reset=True)
    assert path.exists()


def _write_seed(tmp_path, mutate):
    seed = json.loads(database.SEED_PATH.read_text(encoding="utf-8"))
    mutate(seed)
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(seed), encoding="utf-8")
    return path


def test_coverage_gap_is_rejected_and_no_database_left(tmp_path):
    seed_path = _write_seed(tmp_path, lambda s: s["shifts"].remove(next(x for x in s["shifts"] if x["id"] == "VEG-1014-AFT")))
    db = tmp_path / "bad.db"
    with pytest.raises(ValueError, match="coverage gap|operating hours"):
        database.init_demo(db, seed_path)
    assert not db.exists()


def test_conflicting_assignment_is_rejected(tmp_path):
    # Jamie (W09) has a Monday 16:00-17:45 class; assigning Monday afternoon must fail validation.
    def mutate(seed):
        next(x for x in seed["shifts"] if x["id"] == "CAP-1012-AFT")["assigned"] = "W09"
    with pytest.raises(ValueError, match="class overlap"):
        database.init_demo(tmp_path / "bad.db", _write_seed(tmp_path, mutate))


def test_weekly_cap_violation_is_rejected(tmp_path):
    # Alex Chen (W02) already has 20 hours; one more shift exceeds the cap.
    def mutate(seed):
        next(x for x in seed["shifts"] if x["id"] == "SIR-1018-MOR")["assigned"] = "W02"
    with pytest.raises(ValueError, match="weekly limit"):
        database.init_demo(tmp_path / "bad.db", _write_seed(tmp_path, mutate))


def test_foreign_keys_and_value_checks(tmp_path):
    def bad_worker(seed):
        seed["shifts"][0]["assigned"] = "W99"
    with pytest.raises(sqlite3.IntegrityError):
        database.init_demo(tmp_path / "a.db", _write_seed(tmp_path, bad_worker))

    def bad_preference(seed):
        seed["preferences"][0]["level"] = "loves it"
    with pytest.raises(sqlite3.IntegrityError):
        database.init_demo(tmp_path / "b.db", _write_seed(tmp_path, bad_preference))

    def reversed_interval(seed):
        seed["classes"][0]["end"] = seed["classes"][0]["start"]
    with pytest.raises(sqlite3.IntegrityError):
        database.init_demo(tmp_path / "c.db", _write_seed(tmp_path, reversed_interval))
    assert not any(tmp_path.glob("*.db"))


def test_timezone_suffix_is_rejected():
    with pytest.raises(ValueError):
        eligibility.parse_local("2026-10-12 18:00-05:00")
    with pytest.raises(ValueError):
        eligibility.parse_local("2026-10-12T18:00Z")


def test_read_only_connection_refuses_writes(conn):
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM assignments")


# ----- regression tests: coverage-request consistency (review P2) -----

def _tiny_with_request(request, extra_shift=None):
    from conftest import tiny_seed
    seed = tiny_seed()
    if extra_shift:
        seed["shifts"].append(extra_shift)
    seed["coverage_requests"] = [request]
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(database.SCHEMA)
    database.load_seed(connection, seed)
    return connection


ONGOING = {"id": "HIST-ONGOING", "hall": "Andromeda", "name": "Evening", "start": "2026-10-12 17:00",
           "end": "2026-10-12 22:00", "assigned": None}


def test_completed_request_must_match_assignment_and_end_time():
    # The reviewer's probe: a "completed" request for a shift nobody holds, resolved before it ended.
    conn = _tiny_with_request({"id": 999, "worker_id": "W04", "shift_id": "HIST-ONGOING", "asked_at": "2026-10-12 16:00",
                               "outcome": "completed", "resolved_at": "2026-10-12 18:00"}, ONGOING)
    problems = database.validate_structure(conn)
    assert any("not assigned to that shift" in p for p in problems)
    assert any("before the shift ended" in p for p in problems)


def test_consistent_completed_request_passes():
    conn = _tiny_with_request({"id": 1, "worker_id": "W04", "shift_id": "HIST-ONGOING", "asked_at": "2026-10-12 16:00",
                               "outcome": "completed", "resolved_at": "2026-10-12 22:00"}, dict(ONGOING, assigned="W04"))
    assert database.validate_structure(conn) == []


def test_accepted_after_shift_start_is_rejected():
    conn = _tiny_with_request({"id": 1, "worker_id": "W04", "shift_id": "HIST-ONGOING", "asked_at": "2026-10-12 16:00",
                               "outcome": "accepted", "resolved_at": "2026-10-12 17:30"}, dict(ONGOING, assigned="W04"))
    assert any("after the shift started" in p for p in database.validate_structure(conn))


@pytest.mark.parametrize("outcome, resolved_at", [
    ("pending", "2026-10-12 16:30"),     # pending cannot have an outcome time
    ("declined", None),                  # resolved outcomes need one
    ("declined", "2026-10-12 15:00"),    # outcome before the request was made
])
def test_outcome_time_sql_checks(outcome, resolved_at):
    with pytest.raises(sqlite3.IntegrityError):
        _tiny_with_request({"id": 1, "worker_id": "W04", "shift_id": "HIST-ONGOING", "asked_at": "2026-10-12 16:00",
                            "outcome": outcome, "resolved_at": resolved_at}, ONGOING)
