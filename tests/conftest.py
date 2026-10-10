import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import database  # noqa: E402


@pytest.fixture(scope="session")
def seed_db(tmp_path_factory):
    """The real tracked seed, built and validated into a temporary database."""
    path = tmp_path_factory.mktemp("seed") / "shiftops.db"
    database.init_demo(path)
    return path


@pytest.fixture
def conn(seed_db):
    connection = database.connect(seed_db)
    yield connection
    connection.close()


def tiny_seed(**overrides):
    """A small hand-built dataset for exact boundary tests. It is not a full baseline week,
    so it is loaded without baseline validation. Week of Mon 2026-10-12."""
    seed = {
        "meta": {"evaluated_week_start": "2026-10-12"},
        "workers": [
            {"id": "W01", "name": "Outgoing Person", "active": True, "weekly_limit_hours": 20},
            {"id": "W02", "name": "Thirteen Hours", "active": True, "weekly_limit_hours": 20},
            {"id": "W03", "name": "Eighteen Hours", "active": True, "weekly_limit_hours": 20},
            {"id": "W04", "name": "Class Touching", "active": True, "weekly_limit_hours": 20},
            {"id": "W05", "name": "Class Overlapping", "active": True, "weekly_limit_hours": 20},
            {"id": "W06", "name": "Partial Timetable", "active": True, "weekly_limit_hours": 20},
            {"id": "W07", "name": "Generous Limit", "active": True, "weekly_limit_hours": 25},
            {"id": "W08", "name": "Inactive Person", "active": False, "weekly_limit_hours": 20},
        ],
        "timetable_coverage": [
            {"worker_id": w, "start_date": "2026-10-05", "end_date": "2026-10-25", "confirmed_complete": True}
            for w in ("W01", "W02", "W03", "W04", "W05", "W07", "W08")
        ] + [{"worker_id": "W06", "start_date": "2026-10-05", "end_date": "2026-10-14", "confirmed_complete": True}],
        "shifts": [
            # A seven-hour special shift (variable duration) on Wednesday.
            {"id": "SPECIAL-7H", "hall": "Vega", "name": "Move-in Support", "start": "2026-10-14 11:00", "end": "2026-10-14 18:00", "assigned": "W01"},
            # An overnight shift crossing into Thursday.
            {"id": "OVN", "hall": "Andromeda", "name": "Overnight", "start": "2026-10-14 23:00", "end": "2026-10-15 04:00", "assigned": "W01"},
            # Sunday overnight charged to this week; Monday 04:00 shift charged to next week.
            {"id": "SUN-OVN", "hall": "Andromeda", "name": "Overnight", "start": "2026-10-18 23:00", "end": "2026-10-19 04:00", "assigned": "W01"},
            {"id": "NEXT-EM", "hall": "Andromeda", "name": "Early Morning", "start": "2026-10-19 04:00", "end": "2026-10-19 08:00", "assigned": "W03"},
            # Existing hours: W02 = 13h, W03 = 18h, W07 = 16h this week.
            {"id": "A1", "hall": "Capella", "name": "Morning", "start": "2026-10-12 08:00", "end": "2026-10-12 13:00", "assigned": "W02"},
            {"id": "A2", "hall": "Capella", "name": "Morning", "start": "2026-10-13 08:00", "end": "2026-10-13 12:00", "assigned": "W02"},
            {"id": "A3", "hall": "Capella", "name": "Morning", "start": "2026-10-16 08:00", "end": "2026-10-16 12:00", "assigned": "W02"},
            {"id": "B1", "hall": "Helix", "name": "Morning", "start": "2026-10-12 08:00", "end": "2026-10-12 13:00", "assigned": "W03"},
            {"id": "B2", "hall": "Helix", "name": "Morning", "start": "2026-10-13 08:00", "end": "2026-10-13 13:00", "assigned": "W03"},
            {"id": "B3", "hall": "Helix", "name": "Afternoon", "start": "2026-10-13 13:00", "end": "2026-10-13 17:00", "assigned": "W03"},
            {"id": "B4", "hall": "Helix", "name": "Morning", "start": "2026-10-16 08:00", "end": "2026-10-16 12:00", "assigned": "W03"},
            {"id": "C1", "hall": "Sirius", "name": "Morning", "start": "2026-10-12 08:00", "end": "2026-10-12 12:00", "assigned": "W07"},
            {"id": "C2", "hall": "Sirius", "name": "Morning", "start": "2026-10-13 08:00", "end": "2026-10-13 12:00", "assigned": "W07"},
            {"id": "C3", "hall": "Sirius", "name": "Morning", "start": "2026-10-15 08:00", "end": "2026-10-15 12:00", "assigned": "W07"},
            {"id": "C4", "hall": "Sirius", "name": "Morning", "start": "2026-10-16 08:00", "end": "2026-10-16 12:00", "assigned": "W07"},
        ],
        "classes": [
            {"id": 1, "worker_id": "W04", "course": "Ends at start", "start": "2026-10-14 09:30", "end": "2026-10-14 11:00"},
            {"id": 2, "worker_id": "W05", "course": "One minute overlap", "start": "2026-10-14 17:59", "end": "2026-10-14 19:00"},
        ],
        "approved_leave": [],
        "preferences": [],
        "coverage_requests": [],
    }
    seed.update(overrides)
    return seed


@pytest.fixture
def tiny_conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(database.SCHEMA)
    database.load_seed(connection, tiny_seed())
    yield connection
    connection.close()
