"""The five read-only fact tools, their JSON schemas, and validated dispatch.

Tools return facts with evidence IDs. None of them ranks candidates or writes a rationale;
deciding whom to ask first is left to the model. Every record that the model may cite carries
an "evidence_id", which agent.py later uses to verify the final answer's references.
"""

import json
import re
from datetime import timedelta

import eligibility
from eligibility import EligibilityError, parse_local

HALLS = ["Andromeda", "Capella", "Helix", "Vega", "Sirius"]
SHIFT_NAMES = ["Morning", "Afternoon", "Evening", "Overnight", "Early Morning"]
MAX_SHIFT_MATCHES = 10
MAX_SCHEDULE_MATCHES = 16
MAX_CANDIDATES_PER_CALL = 16
MAX_LOOKBACK_DAYS = 30
MAX_WINDOW_MINUTES = 240
MAX_ARGUMENT_CHARS = 1000
MAX_RESULT_CHARS = 8000
WORKER_ID = re.compile(r"^W\d{2}$")


class ToolInputError(ValueError):
    """Bad arguments from the model; returned to it as an observation."""


def _nullable(schema):
    """Allow null. An enum must list null too, or JSON Schema still rejects it."""
    nullable = {**schema, "type": [schema["type"], "null"]}
    if "enum" in schema:
        nullable["enum"] = schema["enum"] + [None]
    return nullable


def _function(name, description, properties):
    # strict=True asks the provider to make arguments match this schema exactly.
    # Python still validates everything in dispatch(), because the model's output is untrusted.
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


SHIFT_ID = {"type": "string", "description": "Exact shift ID from find_shift, e.g. AND-1012-EVE"}
CANDIDATES = {
    "type": "array",
    "items": {"type": "string"},
    "description": f"Eligible worker IDs for this shift (1-{MAX_CANDIDATES_PER_CALL}), e.g. ['W05', 'W09']",
}

TOOL_SCHEMAS = [
    _function(
        "find_shift",
        "Find shifts by shift ID or by date plus optional hall/shift name/start time, and resolve the "
        "outgoing worker by name or ID. Reports whether the worker is actually assigned, ambiguous, or mismatched.",
        {
            "shift_id": _nullable({"type": "string", "description": "Exact shift ID if known"}),
            "date": _nullable({"type": "string", "description": "YYYY-MM-DD; matches the start date or the operating date"}),
            "hall": _nullable({"type": "string", "enum": HALLS}),
            "shift_name": _nullable({"type": "string", "enum": SHIFT_NAMES}),
            "start_time": _nullable({"type": "string", "description": "HH:MM 24-hour local start time"}),
            "worker": _nullable({"type": "string", "description": "Outgoing worker's name, first name, or ID"}),
        },
    ),
    _function(
        "get_eligible_candidates",
        "Apply the hard rules (class/leave/assignment overlap, confirmed timetable, absolute 20h weekly cap "
        "for the whole shift) and list eligible workers in ID order with weekly hours, plus exclusion reasons.",
        {"shift_id": SHIFT_ID, "outgoing_worker_id": {"type": "string", "description": "Worker ID who called out"}},
    ),
    _function(
        "get_preferences",
        "Stored shift preferences (preferred/neutral/low) for eligible candidates. Absent means neutral.",
        {"shift_id": SHIFT_ID, "candidate_ids": CANDIDATES},
    ),
    _function(
        "get_coverage_history",
        "Emergency coverage requests for eligible candidates asked before the target shift starts, with each "
        "outcome (declined/accepted/completed) shown only if known by then; otherwise pending.",
        {
            "shift_id": SHIFT_ID,
            "candidate_ids": CANDIDATES,
            "lookback_days": {"type": "integer", "description": f"Days before the shift start, 1-{MAX_LOOKBACK_DAYS}"},
        },
    ),
    _function(
        "get_nearby_classes",
        "Classes that end shortly before or start shortly after the shift for eligible candidates, with exact "
        "gaps in minutes. These do not overlap the shift (overlaps are already exclusions).",
        {
            "shift_id": SHIFT_ID,
            "candidate_ids": CANDIDATES,
            "window_minutes": {"type": "integer", "description": f"Minutes before start/after end to search, 1-{MAX_WINDOW_MINUTES}"},
        },
    ),
]


# ---------- argument helpers ----------

TOOL_SCHEMAS.append(_function(
    "get_schedule",
    "Read assigned workers for the complete demo week, filtered by hall, period or start time. "
    "Use for factual schedule questions, not replacement eligibility. At most 16 shifts; narrow broad queries.",
    {"week_start": _nullable({"type": "string", "description": "Monday YYYY-MM-DD; null uses the demo week"}),
     "date": _nullable({"type": "string", "description": "Actual start date YYYY-MM-DD, or null for the whole week"}),
     "hall": _nullable({"type": "string", "enum": HALLS}),
     "shift_name": _nullable({"type": "string", "enum": SHIFT_NAMES}),
     "start_time": _nullable({"type": "string", "description": "HH:MM local start time, or null"})}))


def _text(args, key, required=False, max_len=60):
    value = args.get(key)
    if value is None:
        if required:
            raise ToolInputError(f"'{key}' is required")
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ToolInputError(f"'{key}' must be a non-empty string of at most {max_len} characters")
    return value.strip()


def _int_in_range(args, key, low, high):
    value = args.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ToolInputError(f"'{key}' must be an integer from {low} to {high}")
    return value


def _eligible_candidates(conn, args):
    """Validate shift_id and candidate_ids, restricting context tools to the shift's eligible workers."""
    shift_id = _text(args, "shift_id", required=True)
    shift = eligibility.get_shift(conn, shift_id)
    if shift["assigned_worker"] is None:
        raise ToolInputError(f"{shift_id} has no assigned worker, so there is no call-out to cover")
    ids = args.get("candidate_ids")
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_CANDIDATES_PER_CALL
            or not all(isinstance(i, str) and WORKER_ID.match(i) for i in ids) or len(set(ids)) != len(ids)):
        raise ToolInputError(f"'candidate_ids' must be 1-{MAX_CANDIDATES_PER_CALL} distinct IDs like 'W05'")
    check = eligibility.check_candidates(conn, shift_id, shift["assigned_worker"]["worker_id"])
    eligible = {c["worker_id"]: c["name"] for c in check["eligible"]}
    not_eligible = [i for i in ids if i not in eligible]
    if not_eligible:
        raise ToolInputError(f"not eligible for {shift_id}: {not_eligible}. Use IDs from get_eligible_candidates.")
    return shift, ids, eligible


# ---------- the five tools ----------

def find_shift(conn, args):
    shift_id = _text(args, "shift_id")
    day = _text(args, "date")
    hall = _text(args, "hall")
    shift_name = _text(args, "shift_name")
    start_time = _text(args, "start_time")
    worker_text = _text(args, "worker")
    if hall is not None and hall not in HALLS:
        raise ToolInputError(f"'hall' must be one of {HALLS}")
    if shift_name is not None and shift_name not in SHIFT_NAMES:
        raise ToolInputError(f"'shift_name' must be one of {SHIFT_NAMES}")
    if day is not None:
        eligibility.parse_date(day)
    if start_time is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", start_time):
        raise ToolInputError("'start_time' must be HH:MM in 24-hour time")
    if shift_id is None and day is None:
        raise ToolInputError("provide shift_id, or a date (YYYY-MM-DD) plus optional hall/shift_name")

    shifts = []
    for row in conn.execute("SELECT id FROM shifts ORDER BY start_at, id"):
        s = eligibility.get_shift(conn, row["id"])
        if shift_id is not None and s["shift_id"] != shift_id:
            continue
        if day is not None and day not in (s["start"][:10], s["operating_date"]):
            continue
        if hall is not None and s["hall"] != hall:
            continue
        if shift_name is not None and s["name"] != shift_name:
            continue
        if start_time is not None and s["start"][11:] != start_time:
            continue
        shifts.append(s)
    if len(shifts) > MAX_SHIFT_MATCHES:
        raise ToolInputError(f"{len(shifts)} shifts match; add hall, shift_name, or start_time to narrow it")

    result = {"shifts": [{"evidence_id": f"SHIFT:{s['shift_id']}",
                          **{k: v for k, v in s.items() if not k.startswith("_")}} for s in shifts]}
    if not shifts:
        result["resolution"] = "no_shift_match"
        return result
    if worker_text is None:
        result["resolution"] = "worker_not_given" if len(shifts) == 1 else "ambiguous_shift"
        return result

    needle = worker_text.lower()
    workers = [dict(w) for w in conn.execute("SELECT id, name, active FROM workers ORDER BY id")
               if needle == w["id"].lower() or needle == w["name"].lower() or needle in w["name"].lower().split()]
    result["worker_matches"] = [{"worker_id": w["id"], "name": w["name"]} for w in workers]
    if not workers:
        result["resolution"] = "no_worker_match"
        return result

    pairs = [(s, w) for s in shifts for w in workers
             if s["assigned_worker"] and s["assigned_worker"]["worker_id"] == w["id"]]
    if len(pairs) == 1:
        s, w = pairs[0]
        result["resolution"] = "resolved"
        result["resolved"] = {"shift_id": s["shift_id"], "outgoing_worker_id": w["id"], "name": w["name"]}
    elif pairs:
        result["resolution"] = "ambiguous"
        result["possible_pairs"] = [{"shift_id": s["shift_id"], "worker_id": w["id"], "name": w["name"]} for s, w in pairs]
    else:
        # Mismatch: show what the named worker IS assigned to near those dates, so the model can ask.
        result["resolution"] = "mismatch"
        first = min(s["_start"] for s in shifts) - timedelta(days=1)
        last = max(s["_end"] for s in shifts) + timedelta(days=1)
        nearby = []
        for w in workers:
            for row in conn.execute("SELECT s.id, s.hall, s.name, s.start_at FROM assignments a JOIN shifts s"
                                    " ON s.id = a.shift_id WHERE a.worker_id = ? ORDER BY s.start_at", (w["id"],)):
                if first <= parse_local(row["start_at"]) <= last:
                    nearby.append({"worker_id": w["id"], "shift_id": row["id"], "hall": row["hall"],
                                   "name": row["name"], "start": row["start_at"]})
        result["matched_workers_nearby_assignments"] = nearby
    return result


def get_eligible_candidates(conn, args):
    shift_id = _text(args, "shift_id", required=True)
    outgoing = _text(args, "outgoing_worker_id", required=True)
    check = eligibility.check_candidates(conn, shift_id, outgoing)
    return {
        "shift": {"evidence_id": f"SHIFT:{shift_id}", **check["shift"]},
        "week_start": check["week_start"],
        "weekly_cap_hours": eligibility.ABSOLUTE_WEEKLY_CAP_MINUTES // 60,
        "hours_legend": "assigned_hours_now = hours already assigned this week; hours_after_covering = after taking "
                        "this whole shift; hours_left_after_covering = cap minus hours_after_covering",
        "eligible_count": len(check["eligible"]),
        "eligible": [{"evidence_id": f"ELIG:{shift_id}:{c['worker_id']}", **c} for c in check["eligible"]],
        "excluded": [f"{x['worker_id']} {x['name']}: {'; '.join(x['reasons'])}" for x in check["excluded"]],
    }


def get_preferences(conn, args):
    shift, ids, eligible = _eligible_candidates(conn, args)
    stored = {row["worker_id"]: row for row in conn.execute(
        "SELECT id, worker_id, level FROM preferences WHERE shift_id = ?", (shift["shift_id"],))}
    preferences = []
    for wid in ids:
        if wid in stored:
            preferences.append({"evidence_id": f"PREF:{stored[wid]['id']}", "worker_id": wid,
                                "name": eligible[wid], "level": stored[wid]["level"], "stored": True})
        else:
            preferences.append({"evidence_id": f"PREF:{shift['shift_id']}:{wid}:none", "worker_id": wid,
                                "name": eligible[wid], "level": "neutral", "stored": False})
    return {"shift_id": shift["shift_id"], "preferences": preferences}


def get_coverage_history(conn, args):
    shift, ids, eligible = _eligible_candidates(conn, args)
    days = _int_in_range(args, "lookback_days", 1, MAX_LOOKBACK_DAYS)
    cutoff = shift["_start"]
    since = cutoff - timedelta(days=days)
    workers = []
    for wid in ids:
        records = []
        counts = {"asked": 0, "declined": 0, "accepted": 0, "completed": 0, "pending": 0}
        rows = conn.execute(
            "SELECT r.id, r.shift_id, r.asked_at, r.outcome, r.resolved_at, s.hall, s.name, s.start_at, s.end_at"
            " FROM coverage_requests r JOIN shifts s ON s.id = r.shift_id WHERE r.worker_id = ? ORDER BY r.asked_at",
            (wid,),
        )
        for row in rows:
            # As-of view: include requests asked before the target starts, and show each outcome only if it
            # was already known then (resolved_at <= cutoff). Otherwise it was still pending at that time,
            # so a later decline/acceptance/completion never leaks into an earlier decision.
            if not since <= parse_local(row["asked_at"]) < cutoff:
                continue
            known = row["resolved_at"] is not None and parse_local(row["resolved_at"]) <= cutoff
            outcome = row["outcome"] if known else "pending"
            counts["asked"] += 1
            counts[outcome] += 1
            records.append({"evidence_id": f"REQ:{row['id']}", "asked_at": row["asked_at"], "outcome": outcome,
                            "outcome_known_at": row["resolved_at"] if known else None,
                            "emergency_shift": f"{row['shift_id']} {row['hall']} {row['name']} " +
                            eligibility.format_interval(parse_local(row["start_at"]), parse_local(row["end_at"]))})
        workers.append({"evidence_id": f"HIST:{shift['shift_id']}:{wid}:{days}d", "worker_id": wid,
                        "name": eligible[wid], "counts": counts, "requests": records})
    return {"shift_id": shift["shift_id"], "window": f"{since:%Y-%m-%d %H:%M} to {cutoff:%Y-%m-%d %H:%M}",
            "workers": workers}


def get_nearby_classes(conn, args):
    shift, ids, eligible = _eligible_candidates(conn, args)
    window = _int_in_range(args, "window_minutes", 1, MAX_WINDOW_MINUTES)
    start, end = shift["_start"], shift["_end"]
    workers = []
    for wid in ids:
        nearby = []
        for row in conn.execute("SELECT id, course, start_at, end_at FROM classes WHERE worker_id = ? ORDER BY start_at", (wid,)):
            c_start, c_end = parse_local(row["start_at"]), parse_local(row["end_at"])
            if start - timedelta(minutes=window) <= c_end <= start:
                nearby.append({"evidence_id": f"CLASS:{row['id']}", "course": row["course"], "class_time": f"{row['start_at']}-{row['end_at'][11:]}",
                               "relation": "ends before shift", "gap_minutes": int((start - c_end).total_seconds() // 60)})
            elif end <= c_start <= end + timedelta(minutes=window):
                nearby.append({"evidence_id": f"CLASS:{row['id']}", "course": row["course"], "class_time": f"{row['start_at']}-{row['end_at'][11:]}",
                               "relation": "starts after shift", "gap_minutes": int((c_start - end).total_seconds() // 60)})
        workers.append({"evidence_id": f"NEARBY:{shift['shift_id']}:{wid}:{window}m", "worker_id": wid,
                        "name": eligible[wid], "nearby_classes": nearby})
    return {"shift_id": shift["shift_id"], "window_minutes": window, "workers": workers}


def get_schedule(conn, args):
    """Facts only: dated assignments in the complete demo week, sorted by actual start time."""
    week = eligibility.evaluated_week(conn).isoformat()
    requested = _text(args, "week_start") or week
    if requested != week:
        raise ToolInputError(f"Only {week} has a complete demo schedule; older records are partial history.")
    hall, name, start_time = (_text(args, key) for key in ("hall", "shift_name", "start_time"))
    day = _text(args, "date")
    if hall is not None and hall not in HALLS:
        raise ToolInputError(f"'hall' must be one of {HALLS}")
    if name is not None and name not in SHIFT_NAMES:
        raise ToolInputError(f"'shift_name' must be one of {SHIFT_NAMES}")
    if start_time is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", start_time):
        raise ToolInputError("'start_time' must be HH:MM")
    first = eligibility.parse_local(week + " 00:00")
    last = first + timedelta(days=7)
    if day is not None and not first.date() <= eligibility.parse_date(day) < last.date():
        raise ToolInputError("date must be inside the complete demo week")
    matches = []
    for row in conn.execute("SELECT id FROM shifts ORDER BY start_at, id"):
        shift = eligibility.get_shift(conn, row["id"])
        if not first <= shift["_start"] < last:
            continue
        if day is not None and shift["start"][:10] != day:
            continue
        if hall is not None and shift["hall"] != hall:
            continue
        if name is not None and shift["name"] != name:
            continue
        if start_time is not None and shift["start"][11:] != start_time:
            continue
        matches.append({"evidence_id": f"SHIFT:{shift['shift_id']}",
                        **{key: value for key, value in shift.items() if not key.startswith("_")}})
    if len(matches) > MAX_SCHEDULE_MATCHES:
        raise ToolInputError(f"{len(matches)} shifts match; narrow by hall and shift period or start time.")
    query = {"week_start": week, "date": day, "hall": hall, "shift_name": name, "start_time": start_time}
    return {"evidence_id": f"SCHEDULE:{week}:{day or '*'}:{hall or '*'}:{name or '*'}:{start_time or '*'}",
            "query": query, "shifts": matches, "count": len(matches)}


TOOLS = {
    "get_schedule": get_schedule,
    "find_shift": find_shift,
    "get_eligible_candidates": get_eligible_candidates,
    "get_preferences": get_preferences,
    "get_coverage_history": get_coverage_history,
    "get_nearby_classes": get_nearby_classes,
}


def evidence_ids(value):
    """Collect every evidence_id in a tool result so final citations can be checked."""
    found = set()
    if isinstance(value, dict):
        if isinstance(value.get("evidence_id"), str):
            found.add(value["evidence_id"])
        for item in value.values():
            found |= evidence_ids(item)
    elif isinstance(value, list):
        for item in value:
            found |= evidence_ids(item)
    return found


def dispatch(conn, name, raw_arguments):
    """Run one registered tool. Never raises for model mistakes: unknown tools, malformed JSON,
    bad arguments, and controlled data errors all become {"error": ...} observations."""
    if name not in TOOLS:
        return {"error": f"unknown tool {name!r}; available: {sorted(TOOLS)}"}
    if not isinstance(raw_arguments, str) or len(raw_arguments) > MAX_ARGUMENT_CHARS:
        return {"error": f"arguments must be a JSON object of at most {MAX_ARGUMENT_CHARS} characters"}
    try:
        args = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {"error": "arguments are not valid JSON"}
    if not isinstance(args, dict):
        return {"error": "arguments must be a JSON object"}
    try:
        result = TOOLS[name](conn, args)
    except ValueError as error:  # ToolInputError, EligibilityError, and bad dates are all ValueErrors
        return {"error": str(error)}
    if len(json.dumps(result)) > MAX_RESULT_CHARS:
        return {"error": f"result exceeds {MAX_RESULT_CHARS} characters; request fewer candidates or a narrower query"}
    return result
