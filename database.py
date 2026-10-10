"""SQLite schema, seed loading, and safe demo initialization.

All data is synthetic. Every timestamp is a naive "YYYY-MM-DD HH:MM" string on one
fictional America/Chicago local clock (the demo weeks avoid daylight-saving changes).

Usage:
    python database.py --init-demo           # create shiftops.db from data/seed.json
    python database.py --init-demo --reset   # replace an existing database
    python database.py --check               # re-run validation on an existing database
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import eligibility

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "shiftops.db"
SEED_PATH = ROOT / "data" / "seed.json"

SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE workers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    weekly_limit_hours INTEGER NOT NULL CHECK (weekly_limit_hours >= 0)
);

CREATE TABLE timetable_coverage (
    id INTEGER PRIMARY KEY,
    worker_id TEXT NOT NULL REFERENCES workers(id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    confirmed_complete INTEGER NOT NULL CHECK (confirmed_complete IN (0, 1)),
    CHECK (end_date >= start_date)
);

CREATE TABLE shifts (
    id TEXT PRIMARY KEY,
    hall TEXT NOT NULL,
    name TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    required_staff INTEGER NOT NULL DEFAULT 1 CHECK (required_staff = 1),
    CHECK (end_at > start_at)
);

CREATE TABLE assignments (
    id INTEGER PRIMARY KEY,
    worker_id TEXT NOT NULL REFERENCES workers(id),
    shift_id TEXT NOT NULL UNIQUE REFERENCES shifts(id)
);

CREATE TABLE classes (
    id INTEGER PRIMARY KEY,
    worker_id TEXT NOT NULL REFERENCES workers(id),
    course TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    CHECK (end_at > start_at)
);

CREATE TABLE approved_leave (
    id INTEGER PRIMARY KEY,
    worker_id TEXT NOT NULL REFERENCES workers(id),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    CHECK (end_at > start_at)
);

CREATE TABLE preferences (
    id INTEGER PRIMARY KEY,
    worker_id TEXT NOT NULL REFERENCES workers(id),
    shift_id TEXT NOT NULL REFERENCES shifts(id),
    level TEXT NOT NULL CHECK (level IN ('preferred', 'neutral', 'low')),
    UNIQUE (worker_id, shift_id)
);

CREATE TABLE coverage_requests (
    id INTEGER PRIMARY KEY,
    worker_id TEXT NOT NULL REFERENCES workers(id),
    shift_id TEXT NOT NULL REFERENCES shifts(id),
    asked_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('pending', 'declined', 'accepted', 'completed')),
    -- When the current outcome became known. Pending requests have none; others must have one.
    resolved_at TEXT,
    CHECK ((outcome = 'pending') = (resolved_at IS NULL)),
    CHECK (resolved_at IS NULL OR resolved_at >= asked_at)
);
"""

# Columns containing local timestamps; each is re-parsed in Python during validation.
TIMESTAMP_COLUMNS = {
    "shifts": ("start_at", "end_at"),
    "classes": ("start_at", "end_at"),
    "approved_leave": ("start_at", "end_at"),
    "coverage_requests": ("asked_at", "resolved_at"),
}


def connect(db_path=DB_PATH, read_only=True):
    """Open the database. The agent always uses read_only=True, so SQLite itself refuses writes."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path.name} not found. Run: python database.py --init-demo")
    if read_only:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_seed(conn, seed):
    """Insert seed JSON into an empty schema. Shifts carry their assigned worker in the JSON
    for readability; here they are split into the shifts and assignments tables."""
    conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", seed["meta"].items())
    conn.executemany(
        "INSERT INTO workers (id, name, active, weekly_limit_hours) VALUES (?, ?, ?, ?)",
        [(w["id"], w["name"], int(w["active"]), w["weekly_limit_hours"]) for w in seed["workers"]],
    )
    conn.executemany(
        "INSERT INTO timetable_coverage (worker_id, start_date, end_date, confirmed_complete)"
        " VALUES (?, ?, ?, ?)",
        [(t["worker_id"], t["start_date"], t["end_date"], int(t["confirmed_complete"]))
         for t in seed["timetable_coverage"]],
    )
    conn.executemany(
        "INSERT INTO shifts (id, hall, name, start_at, end_at) VALUES (?, ?, ?, ?, ?)",
        [(s["id"], s["hall"], s["name"], s["start"], s["end"]) for s in seed["shifts"]],
    )
    conn.executemany(
        "INSERT INTO assignments (worker_id, shift_id) VALUES (?, ?)",
        [(s["assigned"], s["id"]) for s in seed["shifts"] if s.get("assigned")],
    )
    conn.executemany(
        "INSERT INTO classes (id, worker_id, course, start_at, end_at) VALUES (?, ?, ?, ?, ?)",
        [(c["id"], c["worker_id"], c["course"], c["start"], c["end"]) for c in seed["classes"]],
    )
    conn.executemany(
        "INSERT INTO approved_leave (id, worker_id, start_at, end_at) VALUES (?, ?, ?, ?)",
        [(l["id"], l["worker_id"], l["start"], l["end"]) for l in seed["approved_leave"]],
    )
    conn.executemany(
        "INSERT INTO preferences (id, worker_id, shift_id, level) VALUES (?, ?, ?, ?)",
        [(p["id"], p["worker_id"], p["shift_id"], p["level"]) for p in seed["preferences"]],
    )
    conn.executemany(
        "INSERT INTO coverage_requests (id, worker_id, shift_id, asked_at, outcome, resolved_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(r["id"], r["worker_id"], r["shift_id"], r["asked_at"], r["outcome"], r.get("resolved_at"))
         for r in seed["coverage_requests"]],
    )


def validate_structure(conn):
    """Return a list of problems that SQL constraints alone do not catch."""
    problems = [f"foreign key violation: {tuple(row)}"
                for row in conn.execute("PRAGMA foreign_key_check")]
    for table, columns in TIMESTAMP_COLUMNS.items():
        for row in conn.execute(f"SELECT rowid, {', '.join(columns)} FROM {table}"):
            for column in columns:
                if row[column] is None and column == "resolved_at":
                    continue
                try:
                    eligibility.parse_local(row[column])
                except ValueError as error:
                    problems.append(f"{table} row {row['rowid']} {column}: {error}")
    problems += validate_coverage_requests(conn)
    for row in conn.execute("SELECT id, start_date, end_date FROM timetable_coverage"):
        for column in ("start_date", "end_date"):
            try:
                eligibility.parse_date(row[column])
            except ValueError as error:
                problems.append(f"timetable_coverage row {row['id']} {column}: {error}")
    return problems


def validate_coverage_requests(conn):
    """Request outcomes must agree with the schedule and the clock.

    accepted/completed: the worker must actually hold that shift's assignment.
    accepted: known no later than the shift start. completed: known no earlier than the shift end."""
    problems = []
    rows = conn.execute(
        "SELECT r.id, r.worker_id, r.outcome, r.resolved_at, s.start_at, s.end_at, a.worker_id AS assigned"
        " FROM coverage_requests r JOIN shifts s ON s.id = r.shift_id"
        " LEFT JOIN assignments a ON a.shift_id = r.shift_id"
    )
    for row in rows:
        if row["outcome"] not in ("accepted", "completed"):
            continue
        if row["assigned"] != row["worker_id"]:
            problems.append(f"coverage_requests {row['id']}: {row['outcome']} but {row['worker_id']} is not assigned to that shift")
        if row["resolved_at"] is None:
            continue  # the SQL CHECK already rejects this
        try:
            resolved = eligibility.parse_local(row["resolved_at"])
            start, end = eligibility.parse_local(row["start_at"]), eligibility.parse_local(row["end_at"])
        except ValueError:
            continue  # reported by the timestamp checks
        if row["outcome"] == "completed" and resolved < end:
            problems.append(f"coverage_requests {row['id']}: completed at {row['resolved_at']}, before the shift ended")
        if row["outcome"] == "accepted" and resolved > start:
            problems.append(f"coverage_requests {row['id']}: accepted at {row['resolved_at']}, after the shift started")
    return problems


def init_demo(db_path=DB_PATH, seed_path=SEED_PATH, reset=False):
    """Build a fresh database from the seed. Refuses to touch an existing file unless reset=True.
    If any validation fails, the half-built file is removed so no invalid database is left behind."""
    db_path = Path(db_path)
    if db_path.exists():
        if not reset:
            raise FileExistsError(f"{db_path.name} already exists. Use --reset to replace it.")
        db_path.unlink()

    seed = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(SCHEMA)
        load_seed(conn, seed)
        conn.commit()
        problems = validate_structure(conn) or eligibility.validate_baseline(conn)
    except Exception:
        conn.close()
        db_path.unlink()
        raise
    conn.close()
    if problems:
        db_path.unlink()
        raise ValueError("Seed validation failed:\n  " + "\n  ".join(problems))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create or check the synthetic ShiftOps database.")
    parser.add_argument("--init-demo", action="store_true", help="create shiftops.db from data/seed.json")
    parser.add_argument("--reset", action="store_true", help="allow --init-demo to replace an existing database")
    parser.add_argument("--check", action="store_true", help="validate an existing database")
    parser.add_argument("--db", default=str(DB_PATH), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        if args.init_demo:
            init_demo(args.db, reset=args.reset)
            print(f"Created {Path(args.db).name} from data/seed.json; structure and baseline week validated.")
        if args.check or not args.init_demo:
            conn = connect(args.db)
            problems = validate_structure(conn) + eligibility.validate_baseline(conn)
            if problems:
                print("Validation problems:\n  " + "\n  ".join(problems))
                return 1
            print(eligibility.baseline_summary(conn))
    except (FileExistsError, FileNotFoundError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
