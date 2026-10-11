"""The five read-only tools the model may call, their schemas, and run_tool().

Each tool takes the database connection and a dict of arguments and returns a small dict of facts.
agent.py sends that dict to the model as compact JSON and prints a short summary of the same dict.

Design rule: tools fetch and compute facts. They never rank workers, score suitability, or explain a
choice. Deciding whom to ask first, and why, is the model's job.
"""

import json
import re
from datetime import timedelta

import eligibility
from eligibility import EligibilityError, parse_local

HALLS = ["Andromeda", "Capella", "Helix", "Vega", "Sirius"]
SHIFT_NAMES = ["Morning", "Afternoon", "Evening", "Overnight", "Early Morning"]
MAX_SHIFT_MATCHES = 10
MAX_WORKERS_PER_CALL = 16
MAX_LOOKBACK_DAYS = 30
MAX_WINDOW_MINUTES = 240
MAX_ARGUMENT_CHARS = 1000
MAX_RESULT_CHARS = 8000
WORKER_ID = re.compile(r"^W\d{2}$")


class ToolInputError(ValueError):
    """A bad argument from the model. It goes back to the model as an error observation."""


# ---------- small helpers ----------

def _text(args, key, required=False):
    value = args.get(key)
    if value is None:
        if required:
            raise ToolInputError(f"'{key}' is required")
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 60:
        raise ToolInputError(f"'{key}' must be a short non-empty string")
    return value.strip()


def _number(args, key, low, high):
    value = args.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ToolInputError(f"'{key}' must be a whole number from {low} to {high}")
    return value


def shift_facts(shift):
    """The public facts about one shift (internal datetime fields are dropped)."""
    return {key: value for key, value in shift.items() if not key.startswith("_")}


def _shift_with_callout(conn, args):
    """Look up shift_id and confirm someone holds it (that person is the one calling out)."""
    shift = eligibility.get_shift(conn, _text(args, "shift_id", required=True))
    if shift["assigned_worker"] is None:
        raise ToolInputError(f"{shift['shift_id']} has no assigned worker, so there is no call-out to cover")
    return shift


def _eligible_workers(conn, args):
    """Validate worker_ids for a context tool: only workers eligible for this shift are allowed."""
    shift = _shift_with_callout(conn, args)
    ids = args.get("worker_ids")
    # Check the shape and element types first; only then is it safe to compare items for duplicates.
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_WORKERS_PER_CALL
            or not all(isinstance(i, str) and WORKER_ID.match(i) for i in ids)):
        raise ToolInputError(f"'worker_ids' must be a list of 1-{MAX_WORKERS_PER_CALL} IDs like 'W05'")
    if len(set(ids)) != len(ids):
        raise ToolInputError("'worker_ids' must not repeat an ID")
    check = eligibility.check_candidates(conn, shift["shift_id"], shift["assigned_worker"]["worker_id"])
    names = {c["worker_id"]: c["name"] for c in check["eligible"]}
    not_eligible = [i for i in ids if i not in names]
    if not_eligible:
        raise ToolInputError(f"not eligible for {shift['shift_id']}: {not_eligible}; use IDs from get_eligible_candidates")
    return shift, ids, names


# ---------- the five tools ----------

def find_shift(conn, args):
    """Find the dated shift(s) and check whether the named worker actually holds one of them."""
    shift_id, day, hall = _text(args, "shift_id"), _text(args, "date"), _text(args, "hall")
    shift_name, start_time, worker = _text(args, "shift_name"), _text(args, "start_time"), _text(args, "worker")
    if hall is not None and hall not in HALLS:
        raise ToolInputError(f"'hall' must be one of {HALLS}")
    if shift_name is not None and shift_name not in SHIFT_NAMES:
        raise ToolInputError(f"'shift_name' must be one of {SHIFT_NAMES}")
    if day is not None:
        eligibility.parse_date(day)
    if start_time is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", start_time):
        raise ToolInputError("'start_time' must be HH:MM in 24-hour time")
    if shift_id is None and day is None:
        raise ToolInputError("give shift_id, or a date (YYYY-MM-DD) with hall/shift_name")

    shifts = []
    for row in conn.execute("SELECT id FROM shifts ORDER BY start_at, id"):
        s = eligibility.get_shift(conn, row["id"])
        # A date matches the calendar start date OR the operating date (Andromeda's night shifts).
        if ((shift_id and s["shift_id"] != shift_id) or (day and day not in (s["start"][:10], s["operating_date"]))
                or (hall and s["hall"] != hall) or (shift_name and s["name"] != shift_name)
                or (start_time and s["start"][11:] != start_time)):
            continue
        shifts.append(s)
    if len(shifts) > MAX_SHIFT_MATCHES:
        raise ToolInputError(f"{len(shifts)} shifts match; add hall, shift_name, or start_time")

    result = {"shifts": [shift_facts(s) for s in shifts]}
    if not shifts:
        return {**result, "status": "no_shift_match"}
    if worker is None:
        return {**result, "status": "worker_needed"}

    needle = worker.lower()
    workers = [dict(w) for w in conn.execute("SELECT id, name FROM workers ORDER BY id")
               if needle in (w["id"].lower(), w["name"].lower()) or needle in w["name"].lower().split()]
    result["worker_matches"] = [{"worker_id": w["id"], "name": w["name"]} for w in workers]
    if not workers:
        return {**result, "status": "no_worker_match"}

    pairs = [(s, w) for s in shifts for w in workers
             if s["assigned_worker"] and s["assigned_worker"]["worker_id"] == w["id"]]
    if len(pairs) == 1:
        s, w = pairs[0]
        return {**result, "status": "resolved",
                "resolved": {"shift_id": s["shift_id"], "worker_id": w["id"], "name": w["name"]}}
    if pairs:
        return {**result, "status": "ambiguous",
                "possible": [{"shift_id": s["shift_id"], "worker_id": w["id"], "name": w["name"]} for s, w in pairs]}

    # Nobody named holds these shifts: show what the named worker(s) hold nearby so the model can ask.
    first = min(s["_start"] for s in shifts) - timedelta(days=1)
    last = max(s["_end"] for s in shifts) + timedelta(days=1)
    held = []
    for w in workers:
        for row in conn.execute("SELECT shift_id FROM assignments WHERE worker_id = ?", (w["id"],)):
            s = eligibility.get_shift(conn, row["shift_id"])
            if first <= s["_start"] <= last:
                held.append({"worker_id": w["id"], **shift_facts(s)})
    return {**result, "status": "not_assigned", "worker_assignments": sorted(held, key=lambda s: s["start"])}


def get_eligible_candidates(conn, args):
    """Apply the hard rules for the whole shift. The outgoing worker is whoever holds the shift."""
    shift = _shift_with_callout(conn, args)
    check = eligibility.check_candidates(conn, shift["shift_id"], shift["assigned_worker"]["worker_id"])
    return {
        "shift": check["shift"],
        "outgoing": shift["assigned_worker"],
        "weekly_cap_hours": eligibility.ABSOLUTE_WEEKLY_CAP_MINUTES // 60,
        "eligible": check["eligible"],
        "excluded": check["excluded"],
    }


def get_preferences(conn, args):
    """Stored preferences for this exact shift. A missing preference means neutral."""
    shift, ids, names = _eligible_workers(conn, args)
    stored = {row["worker_id"]: row["level"] for row in conn.execute(
        "SELECT worker_id, level FROM preferences WHERE shift_id = ?", (shift["shift_id"],))}
    return {"shift_id": shift["shift_id"],
            "preferences": [{"worker_id": wid, "name": names[wid], "level": stored.get(wid, "neutral"),
                             "stored": wid in stored} for wid in ids]}


def get_coverage_history(conn, args):
    """Emergency coverage requests made before the shift starts, as they were known at that time."""
    shift, ids, names = _eligible_workers(conn, args)
    days = _number(args, "lookback_days", 1, MAX_LOOKBACK_DAYS)
    cutoff = shift["_start"]
    since = cutoff - timedelta(days=days)
    workers = []
    for wid in ids:
        counts = {"asked": 0, "declined": 0, "accepted": 0, "completed": 0, "pending": 0}
        requests = []
        rows = conn.execute("SELECT asked_at, outcome, resolved_at, shift_id FROM coverage_requests"
                            " WHERE worker_id = ? ORDER BY asked_at", (wid,))
        for row in rows:
            if not since <= parse_local(row["asked_at"]) < cutoff:
                continue
            # An outcome counts only if it was already known when this shift starts; otherwise it was pending.
            known = row["resolved_at"] is not None and parse_local(row["resolved_at"]) <= cutoff
            outcome = row["outcome"] if known else "pending"
            counts["asked"] += 1
            counts[outcome] += 1
            emergency = eligibility.get_shift(conn, row["shift_id"])
            requests.append({"asked_at": row["asked_at"], "outcome": outcome,
                             "emergency_shift": f"{emergency['hall']} {emergency['name']}, {emergency['time_range']}"})
        workers.append({"worker_id": wid, "name": names[wid], "counts": counts, "requests": requests})
    return {"shift_id": shift["shift_id"], "lookback_days": days, "workers": workers}


def get_nearby_classes(conn, args):
    """Classes ending shortly before or starting shortly after the shift (overlaps are already exclusions)."""
    shift, ids, names = _eligible_workers(conn, args)
    minutes = _number(args, "window_minutes", 1, MAX_WINDOW_MINUTES)
    window = timedelta(minutes=minutes)
    start, end = shift["_start"], shift["_end"]
    workers = []
    for wid in ids:
        classes = []
        for row in conn.execute("SELECT course, start_at, end_at FROM classes WHERE worker_id = ? ORDER BY start_at", (wid,)):
            class_start, class_end = parse_local(row["start_at"]), parse_local(row["end_at"])
            if start - window <= class_end <= start:
                classes.append({"course": row["course"], "relation": "ends before shift",
                                "gap_minutes": int((start - class_end).total_seconds() // 60)})
            elif end <= class_start <= end + window:
                classes.append({"course": row["course"], "relation": "starts after shift",
                                "gap_minutes": int((class_start - end).total_seconds() // 60)})
        workers.append({"worker_id": wid, "name": names[wid], "nearby_classes": classes})
    return {"shift_id": shift["shift_id"], "window_minutes": minutes, "workers": workers}


# The allowed-tool registry: the model can run these five functions and nothing else.
TOOLS = {
    "find_shift": find_shift,
    "get_eligible_candidates": get_eligible_candidates,
    "get_preferences": get_preferences,
    "get_coverage_history": get_coverage_history,
    "get_nearby_classes": get_nearby_classes,
}


def run_tool(conn, name, raw_arguments):
    """Run one registered tool. Model mistakes (unknown tool, bad JSON or arguments, unknown shift)
    come back as {"error": ...} so the model can correct itself; they never crash the run."""
    if name not in TOOLS:
        return {"error": f"unknown tool {name!r}; available: {', '.join(TOOLS)}"}
    if not isinstance(raw_arguments, str) or len(raw_arguments) > MAX_ARGUMENT_CHARS:
        return {"error": f"arguments must be a JSON object under {MAX_ARGUMENT_CHARS} characters"}
    try:
        args = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {"error": "arguments are not valid JSON"}
    if not isinstance(args, dict):
        return {"error": "arguments must be a JSON object"}
    try:
        result = TOOLS[name](conn, args)
    except (ToolInputError, EligibilityError, ValueError) as error:
        return {"error": str(error)}
    if len(json.dumps(result)) > MAX_RESULT_CHARS:
        return {"error": f"result is over {MAX_RESULT_CHARS} characters; ask about fewer workers"}
    return result


# ---------- schemas sent to the model (strict: every field required, null when unused) ----------

def _function(name, description, properties):
    return {"type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}}


def _nullable(schema):
    nullable = {**schema, "type": [schema["type"], "null"]}
    if "enum" in schema:
        nullable["enum"] = schema["enum"] + [None]  # an enum must list null too, or null is rejected
    return nullable


SHIFT_ID = {"type": "string", "description": "Exact shift ID from find_shift, e.g. AND-1012-EVE"}
WORKER_IDS = {"type": "array", "items": {"type": "string"},
              "description": f"1-{MAX_WORKERS_PER_CALL} eligible worker IDs, e.g. ['W05', 'W09']"}

TOOL_SCHEMAS = [
    _function("find_shift",
              "Find the dated shift by shift ID, or by date plus hall/shift name/start time, and check whether the "
              "named outgoing worker holds it. Status: resolved, ambiguous, not_assigned, no_worker_match, "
              "no_shift_match, or worker_needed.",
              {"shift_id": _nullable({"type": "string"}),
               "date": _nullable({"type": "string", "description": "YYYY-MM-DD (start date or operating date)"}),
               "hall": _nullable({"type": "string", "enum": HALLS}),
               "shift_name": _nullable({"type": "string", "enum": SHIFT_NAMES}),
               "start_time": _nullable({"type": "string", "description": "HH:MM 24-hour start time"}),
               "worker": _nullable({"type": "string", "description": "Outgoing worker's name, first name, or ID"})}),
    _function("get_eligible_candidates",
              "Apply the hard rules (class, leave, overlapping assignment, confirmed timetable, absolute 20-hour "
              "weekly cap for the whole shift). Lists eligible workers in ID order with assigned_hours_now, "
              "hours_after_covering, and hours_left_after_covering, plus every exclusion reason.",
              {"shift_id": SHIFT_ID}),
    _function("get_preferences", "Stored preferences (preferred/neutral/low) of eligible workers for this shift.",
              {"shift_id": SHIFT_ID, "worker_ids": WORKER_IDS}),
    _function("get_coverage_history",
              "Emergency coverage requests of eligible workers asked before the shift starts; each outcome "
              "(declined/accepted/completed) appears only if it was known by then, otherwise pending.",
              {"shift_id": SHIFT_ID, "worker_ids": WORKER_IDS,
               "lookback_days": {"type": "integer", "description": f"1-{MAX_LOOKBACK_DAYS}"}}),
    _function("get_nearby_classes",
              "Classes of eligible workers that end shortly before or start shortly after the shift, with gaps.",
              {"shift_id": SHIFT_ID, "worker_ids": WORKER_IDS,
               "window_minutes": {"type": "integer", "description": f"1-{MAX_WINDOW_MINUTES}"}}),
]
