"""Deterministic hard-eligibility rules and baseline validation.

Rules (docs/PROJECT_SPEC.md):
- Intervals are half-open [start, end): touching endpoints do not overlap.
- Durations are exact integer minutes from the stored start/end; nothing assumes five hours.
- Weeks run Monday 00:00 to the next Monday 00:00. A whole assignment is charged to the
  week containing its start, including overnight shifts.
- The weekly work cap is an absolute 20 hours. A stored limit above 20 cannot raise it.
- A candidate must cover the entire interval; there is no partial or split coverage.

Nothing here ranks candidates. Eligible workers are returned in stable worker-ID order.
"""

from datetime import date, datetime, time, timedelta

ABSOLUTE_WEEKLY_CAP_MINUTES = 20 * 60
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M"
DAY_HALLS = ("Capella", "Helix", "Vega", "Sirius")
TWENTY_FOUR_HOUR_HALL = "Andromeda"
OPERATING_DAY_START = time(8, 0)


class EligibilityError(ValueError):
    """A controlled error the agent can show to the model as an observation."""


# ---------- time helpers ----------

def parse_local(text):
    """Parse 'YYYY-MM-DD HH:MM' on the fictional local clock. Timezone offsets are rejected
    so that two clocks are never silently mixed."""
    if not isinstance(text, str):
        raise ValueError(f"timestamp must be text, got {text!r}")
    try:
        return datetime.strptime(text, TIMESTAMP_FORMAT)
    except ValueError:
        raise ValueError(f"timestamp {text!r} must look like 'YYYY-MM-DD HH:MM' (local, no timezone)")


def parse_date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError(f"date {text!r} must look like 'YYYY-MM-DD'")


def minutes_between(start, end):
    """Exact positive duration in whole minutes. Invalid intervals are rejected, never rounded."""
    seconds = (end - start).total_seconds()
    if seconds <= 0 or seconds % 60:
        raise ValueError(f"invalid interval {start} to {end}")
    return int(seconds // 60)


def overlaps(start_a, end_a, start_b, end_b):
    """Half-open overlap test: [a) and [b) overlap only if each starts before the other ends."""
    return start_a < end_b and start_b < end_a


def week_start_for(moment):
    """Monday 00:00 of the week containing this moment."""
    monday = moment.date() - timedelta(days=moment.weekday())
    return datetime.combine(monday, time(0, 0))


def operating_date(start):
    """Andromeda's operating day runs 8 AM to 8 AM, so a shift starting before 8 AM belongs to the
    previous calendar day's operation. Day-hall shifts always start at or after 8 AM."""
    return start.date() if start.time() >= OPERATING_DAY_START else start.date() - timedelta(days=1)


def dates_touched(start, end):
    """Calendar dates covered by [start, end). A shift ending exactly at midnight does not touch the next day."""
    last = (end - timedelta(minutes=1)).date()
    days = []
    current = start.date()
    while current <= last:
        days.append(current)
        current += timedelta(days=1)
    return days


def format_minutes(minutes):
    hours, mins = divmod(minutes, 60)
    return f"{hours}h" if mins == 0 else f"{hours}h{mins:02d}m"


def format_clock(moment):
    """12-hour display time: 8 AM, 6:30 PM, 12 PM (noon), 12 AM (midnight)."""
    hour = moment.hour % 12 or 12
    suffix = "AM" if moment.hour < 12 else "PM"
    return f"{hour} {suffix}" if moment.minute == 0 else f"{hour}:{moment.minute:02d} {suffix}"


def format_interval(start, end):
    """Readable range generated from the actual stored timestamps (display only; IDs stay the identity).

    Same day: "Oct 12, 6 PM–11 PM". Crossing midnight shows both dates: "Oct 12, 11 PM–Oct 13, 4 AM"."""
    first = f"{start:%b} {start.day}, {format_clock(start)}"
    if end.date() == start.date():
        return f"{first}–{format_clock(end)}"
    return f"{first}–{end:%b} {end.day}, {format_clock(end)}"


# ---------- database reads ----------

def get_shift(conn, shift_id):
    row = conn.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()
    if row is None:
        raise EligibilityError(f"unknown shift_id {shift_id!r}")
    start, end = parse_local(row["start_at"]), parse_local(row["end_at"])
    assigned = conn.execute(
        "SELECT w.id, w.name FROM assignments a JOIN workers w ON w.id = a.worker_id WHERE a.shift_id = ?",
        (shift_id,),
    ).fetchone()
    return {
        "shift_id": row["id"],
        "hall": row["hall"],
        "name": row["name"],
        "start": row["start_at"],
        "end": row["end_at"],
        "time_range": format_interval(start, end),
        "duration_minutes": minutes_between(start, end),
        "operating_date": operating_date(start).isoformat(),
        "assigned_worker": {"worker_id": assigned["id"], "name": assigned["name"]} if assigned else None,
        "_start": start,
        "_end": end,
    }


def weekly_minutes(conn, worker_id, week_start):
    """Total minutes of the worker's assignments that START inside the given Monday-Sunday week."""
    week_end = week_start + timedelta(days=7)
    total = 0
    rows = conn.execute(
        "SELECT s.start_at, s.end_at FROM assignments a JOIN shifts s ON s.id = a.shift_id"
        " WHERE a.worker_id = ?",
        (worker_id,),
    )
    for row in rows:
        start = parse_local(row["start_at"])
        if week_start <= start < week_end:
            total += minutes_between(start, parse_local(row["end_at"]))
    return total


def effective_cap_minutes(stored_limit_hours):
    """The stored limit may be lower than 20 hours, but never raises the absolute cap."""
    return min(stored_limit_hours * 60, ABSOLUTE_WEEKLY_CAP_MINUTES)


def exclusion_reasons(conn, worker, shift, ignore_shift_id=None):
    """Every hard reason this worker cannot take the whole shift. Empty list means eligible.

    ignore_shift_id lets baseline validation check a worker against their own assignment."""
    reasons = []
    start, end = shift["_start"], shift["_end"]
    worker_id = worker["id"]

    if not worker["active"]:
        reasons.append("inactive worker")

    # Timetable must be confirmed complete for every date the shift touches.
    confirmed = conn.execute(
        "SELECT start_date, end_date FROM timetable_coverage WHERE worker_id = ? AND confirmed_complete = 1",
        (worker_id,),
    ).fetchall()
    for day in dates_touched(start, end):
        if not any(parse_date(c["start_date"]) <= day <= parse_date(c["end_date"]) for c in confirmed):
            reasons.append(f"class timetable not confirmed for {day.isoformat()}")

    for row in conn.execute("SELECT id, course, start_at, end_at FROM classes WHERE worker_id = ?", (worker_id,)):
        if overlaps(start, end, parse_local(row["start_at"]), parse_local(row["end_at"])):
            reasons.append(f"class overlap CLASS:{row['id']} {row['course']} {row['start_at']}-{row['end_at'][11:]}")

    for row in conn.execute("SELECT id, start_at, end_at FROM approved_leave WHERE worker_id = ?", (worker_id,)):
        if overlaps(start, end, parse_local(row["start_at"]), parse_local(row["end_at"])):
            reasons.append(f"approved leave overlap LEAVE:{row['id']} {row['start_at']} to {row['end_at']}")

    rows = conn.execute(
        "SELECT s.id, s.start_at, s.end_at FROM assignments a JOIN shifts s ON s.id = a.shift_id"
        " WHERE a.worker_id = ?",
        (worker_id,),
    )
    for row in rows:
        if row["id"] == ignore_shift_id:
            continue
        if row["id"] == shift["shift_id"]:
            reasons.append("already assigned to this shift")
        elif overlaps(start, end, parse_local(row["start_at"]), parse_local(row["end_at"])):
            reasons.append(f"overlaps own assignment {row['id']} {row['start_at']}-{row['end_at'][11:]}")

    cap = effective_cap_minutes(worker["weekly_limit_hours"])
    current = weekly_minutes(conn, worker_id, week_start_for(start))
    if ignore_shift_id is None:
        projected = current + shift["duration_minutes"]
    else:
        projected = current  # baseline check: the shift is already counted
    if projected > cap:
        reasons.append(
            f"weekly limit: {format_minutes(current)} assigned + {format_minutes(shift['duration_minutes'])} "
            f"shift = {format_minutes(projected)} > {format_minutes(cap)} cap"
            if ignore_shift_id is None else
            f"weekly limit: {format_minutes(projected)} assigned > {format_minutes(cap)} cap"
        )
    return reasons


def weekly_capacity(conn, worker, shift, week):
    """Three distinct numbers for the shift's week (hours charged by shift start):
    assigned now, after covering this whole shift, and left under the cap afterwards."""
    assigned = weekly_minutes(conn, worker["id"], week)
    after = assigned + shift["duration_minutes"]
    return {
        "assigned_hours_now": assigned / 60,
        "hours_after_covering": after / 60,
        "hours_left_after_covering": (effective_cap_minutes(worker["weekly_limit_hours"]) - after) / 60,
    }


def check_candidates(conn, shift_id, outgoing_worker_id):
    """Split all workers into eligible and excluded for one exact shift.

    Raises EligibilityError if the shift is unknown or the outgoing worker is not actually
    assigned to it, so a call-out is never invented for the wrong position."""
    shift = get_shift(conn, shift_id)
    assigned = shift["assigned_worker"]
    if assigned is None or assigned["worker_id"] != outgoing_worker_id:
        holder = f"{assigned['name']} ({assigned['worker_id']})" if assigned else "nobody"
        raise EligibilityError(
            f"worker {outgoing_worker_id!r} is not assigned to {shift_id}; it is assigned to {holder}"
        )

    week = week_start_for(shift["_start"])
    eligible, excluded = [], []
    for worker in conn.execute("SELECT * FROM workers ORDER BY id"):
        if worker["id"] == outgoing_worker_id:
            excluded.append({"worker_id": worker["id"], "name": worker["name"],
                             "reasons": ["outgoing worker (called out)"]})
            continue
        reasons = exclusion_reasons(conn, worker, shift)
        if reasons:
            excluded.append({"worker_id": worker["id"], "name": worker["name"], "reasons": reasons})
        else:
            eligible.append({"worker_id": worker["id"], "name": worker["name"],
                             **weekly_capacity(conn, worker, shift, week)})
    public_shift = {k: v for k, v in shift.items() if not k.startswith("_")}
    return {"shift": public_shift, "week_start": week.date().isoformat(), "eligible": eligible, "excluded": excluded}


# ---------- baseline validation ----------

def evaluated_week(conn):
    row = conn.execute("SELECT value FROM meta WHERE key = 'evaluated_week_start'").fetchone()
    if row is None:
        raise ValueError("meta.evaluated_week_start is missing")
    monday = parse_date(row["value"])
    if monday.weekday() != 0:
        raise ValueError("evaluated_week_start must be a Monday")
    return monday


def validate_baseline(conn):
    """Prove the evaluated week is genuinely staffed, not just that total hours look sufficient.

    For each operating day and hall, the stored shifts must tile the operating hours exactly
    (no gaps, no double coverage); each shift needs one assignment; every assigned worker must
    pass the same hard rules used for replacements; and nobody exceeds 20 hours in any week."""
    problems = []
    monday = evaluated_week(conn)
    shifts = [get_shift(conn, row["id"]) for row in conn.execute("SELECT id FROM shifts")]

    for offset in range(7):
        day = monday + timedelta(days=offset)
        for hall in DAY_HALLS + (TWENTY_FOUR_HOUR_HALL,):
            open_at = datetime.combine(day, OPERATING_DAY_START)
            close_at = open_at + (timedelta(days=1) if hall == TWENTY_FOUR_HOUR_HALL else timedelta(hours=10))
            todays = sorted((s for s in shifts if s["hall"] == hall and s["operating_date"] == day.isoformat()),
                            key=lambda s: s["_start"])
            cursor = open_at
            for s in todays:
                if s["_start"] != cursor:
                    problems.append(f"{hall} {day}: coverage gap or overlap at {cursor:%Y-%m-%d %H:%M} ({s['shift_id']})")
                cursor = max(cursor, s["_end"])
                if s["assigned_worker"] is None:
                    problems.append(f"{s['shift_id']} has no assigned worker")
            if cursor != close_at:
                problems.append(f"{hall} {day}: operating hours end at {close_at:%H:%M} but coverage ends {cursor:%Y-%m-%d %H:%M}")

    # Assignment rules are checked for the evaluated week plus the previous operating day,
    # whose overnight/early-morning shifts spill into Monday. Older shifts exist only as
    # history referenced by coverage requests.
    first_day, last_day = (monday - timedelta(days=1)).isoformat(), (monday + timedelta(days=6)).isoformat()
    workers = {w["id"]: w for w in conn.execute("SELECT * FROM workers")}
    for s in shifts:
        if s["assigned_worker"] is None or not first_day <= s["operating_date"] <= last_day:
            continue
        worker = workers[s["assigned_worker"]["worker_id"]]
        for reason in exclusion_reasons(conn, worker, s, ignore_shift_id=s["shift_id"]):
            problems.append(f"{s['shift_id']} assigned to {worker['id']}: {reason}")
    return sorted(set(problems))


def baseline_summary(conn):
    monday = evaluated_week(conn)
    week = datetime.combine(monday, time(0, 0))
    hours = {w["id"]: weekly_minutes(conn, w["id"], week) for w in conn.execute("SELECT id FROM workers")}
    total = sum(hours.values())
    return (f"Week of {monday}: {len(hours)} workers, {format_minutes(total)} assigned "
            f"(charged by start time), max {format_minutes(max(hours.values()))} per worker. Baseline OK.")
