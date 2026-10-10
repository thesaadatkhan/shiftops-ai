"""ShiftOps advisor: the manual agent loop and the terminal entry point.

    python agent.py "Jordan called out for Andromeda's October 12 evening shift. Who should I ask first?"
    python agent.py --chat

--chat keeps follow-up context while this process is open. /new clears the topic; /quit exits.
The same token, step, and tool caps cover the entire chat session, including all replies.

How a run works (agent_steps):
  1. Send instructions, the conversation so far, and the tool schemas to the model.
  2. The model either requests tool calls or returns a final JSON answer.
  3. Python runs only registered tools (tools.dispatch) and appends each observation.
  4. Repeat until a final answer passes Python validation, or a guardrail stops the run.

Who does what:
  - Python fetches facts, applies the hard rules, enforces limits, and validates the final answer.
  - The model chooses which context to look at and decides whom to ask first, explaining the tradeoff.
The run is read-only: it never assigns shifts or contacts anyone.
"""

import json
import math
import os
import re
import sys
import shutil
import textwrap
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import database
import eligibility
import pricing
import tools
from eligibility import EligibilityError

ROOT = Path(__file__).resolve().parent


@dataclass
class Limits:
    max_model_calls: int = 10        # includes final answers and repair requests
    max_tool_calls: int = 12         # tool executions across the whole run
    max_output_tokens: int = 1000    # per model call (reasoning tokens count toward this)
    token_allowance: int = 50000    # input + output tokens summed across all calls
    request_timeout: float = 30.0    # seconds per provider request
    deadline: float = 120.0          # seconds for the whole run
    max_question_chars: int = 500
    max_repairs: int = 1             # correction requests after a failed validation
    input_margin: float = 1.10       # safety margin on counted input tokens


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass
class ModelReply:
    """Provider-neutral model reply, so tests can script replies without the network."""
    status: str                          # "completed" or "incomplete"
    tool_calls: list = field(default_factory=list)
    text: str = ""
    incomplete_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None   # already included in output_tokens
    cached_tokens: int | None = None      # part of input_tokens read from the prompt cache
    cache_write_tokens: int | None = None  # part of input_tokens written to the prompt cache


class ProviderError(Exception):
    """The provider could not be reached or rejected the request. Message never contains secrets."""


# ---------- instructions and final-answer contract ----------

INSTRUCTIONS = """You advise a residence-hall supervisor on whom to ASK FIRST to cover a student worker's call-out.
You recommend only; you never assign shifts or contact anyone.

Data: synthetic, on one local clock. The schedule covers Monday 2026-10-12 to Sunday 2026-10-18.
A weekday name without a date means that week. Use exact dates (YYYY-MM-DD) in tool calls.
Andromeda runs 24 hours; its Overnight (23:00-04:00) and Early Morning (04:00-08:00) shifts belong to the
operating day on which they begin at or after 08:00 the previous day, so a date can match two early-morning shifts.

Hard rules (classes, approved leave, overlapping assignments, confirmed timetable, absolute 20-hour weekly
cap for the WHOLE shift) are applied by get_eligible_candidates. You cannot override them, split a shift,
or shorten it. Only workers it lists as eligible may be recommended.

Python binds the run to the shift and outgoing worker that find_shift resolves. It must agree with the
worker, hall, shift ID, date or weekday, shift period, and time stated in the user's question; relative days
such as "tonight" are not bound. A recommendation or no_eligible_candidate answer must use exactly that target. If the request is ambiguous or seems to point elsewhere, ask a clarification instead.

Investigate as you see fit: resolve the shift and outgoing worker with find_shift, check eligibility, then
fetch whichever context helps you choose among eligible workers (preferences, recent emergency coverage,
nearby classes). Weekly hours are already in the eligibility result. Avoid repeating identical calls.

Describe shifts with their time_range. Weekly hours have three meanings: assigned_hours_now (already
assigned this week), hours_after_covering (after taking this whole shift), and hours_left_after_covering.

Judgment: weigh preferences, recent emergency coverage burden, tight gaps to nearby classes, and remaining
weekly hours. Say what you are trading off. Do not claim someone is exhausted, willing, or likely to accept
unless a retrieved record shows it (for example, recorded declines). Absent preference means neutral.

Final answer: JSON only, matching the schema.
- status "recommendation": shift_id, outgoing_worker_id, recommended_worker_id, optional backup_worker_id
  (another eligible worker or null), a concise rationale and tradeoff, and evidence_refs.
- status "clarification": if the worker or shift is ambiguous, or the named worker is not assigned to the
  named shift. Put the question in clarification_question and explain what you found in rationale.
- status "no_eligible_candidate": if get_eligible_candidates returns nobody. Include shift_id and outgoing_worker_id.
- status "incomplete": if you cannot finish for another reason.
evidence_refs must be evidence_id values copied exactly from tool results that support your claims.
For a recommendation include ELIG:<shift>:<worker> for each chosen worker and the context records you relied on.
Unused fields are null; keep rationale and tradeoff under 80 words each."""

INSTRUCTIONS += """
You also answer factual schedule questions with get_schedule. For these, use status 'schedule', cite its
SCHEDULE evidence_id, and leave all shift/worker proposal fields null. Python displays the complete dated
assignments from that source; never invent names or assignments. 'This week' means the demo week, not today.
Interactive conversations may include earlier requests and clarifications. Use that context for short
replies such as 'Alex Chen' or 'why that worker?'. A new topic is announced explicitly in the conversation.
"""

FINAL_STATUSES = ["recommendation", "clarification", "no_eligible_candidate", "incomplete", "schedule"]

FINAL_FORMAT = {
    "format": {
        "type": "json_schema",
        "name": "shiftops_final",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": FINAL_STATUSES},
                "shift_id": {"type": ["string", "null"]},
                "outgoing_worker_id": {"type": ["string", "null"]},
                "recommended_worker_id": {"type": ["string", "null"]},
                "backup_worker_id": {"type": ["string", "null"]},
                "rationale": {"type": "string"},
                "tradeoff": {"type": "string"},
                "clarification_question": {"type": ["string", "null"]},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["status", "shift_id", "outgoing_worker_id", "recommended_worker_id", "backup_worker_id",
                         "rationale", "tradeoff", "clarification_question", "evidence_refs"],
            "additionalProperties": False,
        },
    }
}

SOFT_CONTEXT_PREFIXES = ("PREF:", "HIST:", "REQ:", "NEARBY:", "CLASS:")


# ---------- mandatory final validation (Python, never skipped) ----------

NULLABLE_TEXT_FIELDS = ("shift_id", "outgoing_worker_id", "recommended_worker_id", "backup_worker_id",
                        "clarification_question")


def field_type_problems(final):
    """Check every field's type before any value is used, so malformed JSON cannot crash validation."""
    if not isinstance(final, dict) or set(final) != set(FINAL_FORMAT["format"]["schema"]["required"]):
        return ["final answer does not match the required JSON fields"]
    problems = [f"{key} must be a string or null" for key in NULLABLE_TEXT_FIELDS
                if final[key] is not None and not isinstance(final[key], str)]
    problems += [f"{key} must be a string" for key in ("status", "rationale", "tradeoff")
                 if not isinstance(final[key], str)]
    refs = final["evidence_refs"]
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        problems.append("evidence_refs must be a list of strings")
    if not problems and final["status"] not in FINAL_STATUSES:
        problems.append(f"unknown status {final['status']!r}")
    return problems


def new_target():
    """The run's call-out target. Python sets it from find_shift results, never from the final answer."""
    return {"shift_id": None, "outgoing_worker_id": None, "conflict": None}


MONTHS = {name: number for number, names in enumerate(
    [("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
     ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"),
     ("november", "nov"), ("december", "dec")], 1) for name in names}
WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
            "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sunday": 6}
RELATIVE_DAYS = ("today", "tonight", "tomorrow", "yesterday")
SHIFT_ID_PATTERN = re.compile(r"\b(?:AND|CAP|HEL|VEG|SIR)-\d{4}-(?:MOR|AFT|EVE|OVN|EM)\b", re.IGNORECASE)
MONTH_WORD = "|".join(sorted(MONTHS, key=len, reverse=True))


def question_constraints(question, week_start):
    """Explicit shift details stated in the question, read with a few fixed patterns (not a full parser).

    Returns shift_ids, dates, periods (shift names), time ranges/starts/ends/times ("HH:MM"; see read_times),
    and 'uncertain' reasons. Supported:
    shift IDs like AND-1012-EVE; dates as 2026-10-12, October 12 / Oct. 12th, 12 October, or 10/12;
    weekday names, which mean that day of the demo week starting week_start (a Monday); Morning, Afternoon,
    Evening, Overnight, Early Morning, and "night" (Evening or Overnight); times like 6 PM, 6:30pm, 18:00,
    noon, midnight. Relative days ("tonight") or two different dates are uncertain: the run must clarify."""
    found = {"shift_ids": set(), "dates": set(), "periods": set(), "ranges": set(), "starts": set(), "ends": set(),
             "times": set(), "uncertain": []}
    found["shift_ids"] = {m.upper() for m in SHIFT_ID_PATTERN.findall(question)}
    text = SHIFT_ID_PATTERN.sub(" ", question.lower())

    def add_date(year, month, day):
        try:
            found["dates"].add(date(int(year), int(month), int(day)))
        except ValueError:
            found["uncertain"].append(f"'{month}/{day}' is not a valid date")

    patterns = [
        (r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lambda m: add_date(m[1], m[2], m[3])),
        (rf"\b({MONTH_WORD})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(\d{{4}}))?",
         lambda m: add_date(m[3] or week_start.year, MONTHS[m[1]], m[2])),
        (rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTH_WORD})\b(?:,?\s*(\d{{4}}))?",
         lambda m: add_date(m[3] or week_start.year, MONTHS[m[2]], m[1])),
        (r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", lambda m: add_date(m[3] or week_start.year, m[1], m[2])),
    ]
    for pattern, handle in patterns:
        for match in re.finditer(pattern, text):
            handle(match)
        text = re.sub(pattern, " ", text)  # so a date's digits are not reread as a time
    words = re.findall(r"[a-z]+", text)
    for word in words:
        if word in WEEKDAYS:
            found["dates"].add(week_start + timedelta(days=WEEKDAYS[word]))
        if word in RELATIVE_DAYS:
            found["uncertain"].append(f"relative day '{word}' needs an explicit date")
    if len(found["dates"]) > 1:
        found["uncertain"].append("the question mentions different dates: "
                                  + ", ".join(sorted(d.isoformat() for d in found["dates"])))

    periods_text = re.sub(r"\bearly[\s-]+morning\b", " ", text)
    if periods_text != text:
        found["periods"].add("Early Morning")
    for word, names in (("morning", {"Morning"}), ("afternoon", {"Afternoon"}), ("evening", {"Evening"}),
                        ("overnight", {"Overnight"}), ("night", {"Evening", "Overnight"})):
        if re.search(rf"\b{word}\b", periods_text):
            found["periods"] |= names

    read_times(text, found)
    return found


TIME_TOKEN = r"(\bnoon\b|\bmidnight\b|\d{1,2}(?::[0-5]\d)?(?:\s*[ap]\.?m\b\.?)?)"
EXPLICIT_TIME = r"(\bnoon\b|\bmidnight\b|\d{1,2}(?::[0-5]\d)?\s*[ap]\.?m\b\.?|\b(?:[01]?\d|2[0-3]):[0-5]\d\b)"
RANGE_PATTERNS = [
    rf"\bbetween\s+{TIME_TOKEN}\s+and\s+{TIME_TOKEN}",
    rf"(?<![\d:]){TIME_TOKEN}\s*(?:-|–|—|\bto\b|\buntil\b|\btill\b|\bthrough\b)\s*{TIME_TOKEN}",
]
START_PATTERN = rf"\b(?:from|starting|starts|start|beginning|begins|begin)(?:\s+at)?\s+{EXPLICIT_TIME}"
END_PATTERN = rf"\b(?:until|till|to|ending|ends|end|through)(?:\s+at)?\s+{EXPLICIT_TIME}"


def _clock_parts(token):
    """(hour, minute, 'a'/'p'/None, has_colon) for one time token; noon/midnight are already explicit."""
    token = token.strip()
    if token in ("noon", "midnight"):
        return (12 if token == "noon" else 0), 0, "24h", True
    m = re.fullmatch(r"(\d{1,2})(?::([0-5]\d))?\s*(?:([ap])\.?m\.?)?", token)
    return int(m[1]), int(m[2] or 0), m[3], m[2] is not None


def _hhmm(hour, minute, meridiem):
    """'HH:MM' from 12-hour (meridiem 'a'/'p') or 24-hour (meridiem '24h'/None) parts, or None if invalid."""
    if meridiem in ("a", "p"):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    elif hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def read_times(text, found):
    """Times in the question, with their roles.

    ranges: (start, end) pairs, such as "6-11 PM", "from 6 PM to 11 PM", "11 PM to 4 AM", "18:00-23:00".
            If only one endpoint has AM/PM ("6-11 pm"), the other inherits it; if that gives an interval
            over 12 hours ("11-4 am"), or neither endpoint has AM/PM or a colon ("7 to 11"), it is uncertain.
    starts / ends: single times with a role word ("starting at 6 PM", "until 11 PM").
    times: any other explicit time, role unknown ("the 6 PM shift")."""
    for pattern in RANGE_PATTERNS:
        for match in re.finditer(pattern, text):
            (h1, m1, a1, colon1), (h2, m2, a2, colon2) = _clock_parts(match[1]), _clock_parts(match[2])
            inherited = False
            if a1 is None and a2 is None:
                if not (colon1 and colon2):
                    found["uncertain"].append(f"time range '{match[0].strip()}' needs AM/PM")
                    continue
            elif a1 is None and a2 in ("a", "p") and not (colon1 and h1 > 12):
                a1, inherited = a2, True
            elif a2 is None and a1 in ("a", "p") and not (colon2 and h2 > 12):
                a2, inherited = a1, True
            first, second = _hhmm(h1, m1, a1), _hhmm(h2, m2, a2)
            if first is None or second is None:
                found["uncertain"].append(f"'{match[0].strip()}' is not a valid time range")
                continue
            minutes = (int(second[:2]) * 60 + int(second[3:]) - int(first[:2]) * 60 - int(first[3:])) % 1440
            if minutes == 0 or (inherited and minutes > 720):
                found["uncertain"].append(f"time range '{match[0].strip()}' is ambiguous; give AM/PM for both ends")
                continue
            found["ranges"].add((first, second))
        text = re.sub(pattern, " ", text)

    for pattern, role in ((START_PATTERN, "starts"), (END_PATTERN, "ends"), (EXPLICIT_TIME, "times")):
        for match in re.finditer(pattern, text):
            hour, minute, meridiem, _ = _clock_parts(match[1])
            value = _hhmm(hour, minute, meridiem)
            if value is None:
                found["uncertain"].append(f"'{match[1].strip()}' is not a valid time")
            else:
                found[role].add(value)
        text = re.sub(pattern, " ", text)


def question_mismatches(constraints, shift):
    """Ways a resolved shift contradicts the question's explicit details (empty list = compatible).

    A date matches the shift's start date or its operating date, so "October 12 early morning" still fits
    both early-morning shifts it could mean. A stated range must equal the shift's start AND end; a stated
    start or end must equal that endpoint; a time with no role must equal the start or the end."""
    problems = list(constraints["uncertain"])
    start, end = shift["start"], shift["end"]
    if constraints["shift_ids"] and shift["shift_id"] not in constraints["shift_ids"]:
        problems.append(f"the question names shift {sorted(constraints['shift_ids'])}, not {shift['shift_id']}")
    shift_dates = {start[:10], shift["operating_date"]}
    if constraints["dates"] and not shift_dates & {d.isoformat() for d in constraints["dates"]}:
        problems.append(f"the question says {', '.join(sorted(d.isoformat() for d in constraints['dates']))}, but "
                        f"{shift['shift_id']} starts {start} (operating day {shift['operating_date']})")
    if constraints["periods"] and shift["name"] not in constraints["periods"]:
        problems.append(f"the question says {' or '.join(sorted(constraints['periods']))}, but "
                        f"{shift['shift_id']} is the {shift['name']} shift")
    for first, second in sorted(constraints["ranges"]):
        if (first, second) != (start[11:], end[11:]):
            problems.append(f"the question gives {first}-{second}, but {shift['shift_id']} runs {start[11:]}-{end[11:]}")
    for value in sorted(constraints["starts"] - {start[11:]}):
        problems.append(f"the question says it starts at {value}, but {shift['shift_id']} starts at {start[11:]}")
    for value in sorted(constraints["ends"] - {end[11:]}):
        problems.append(f"the question says it ends at {value}, but {shift['shift_id']} ends at {end[11:]}")
    if constraints["times"] and not {start[11:], end[11:]} & constraints["times"]:
        problems.append(f"the question mentions {', '.join(sorted(constraints['times']))}, but "
                        f"{shift['shift_id']} runs {start[11:]}-{end[11:]}")
    return problems


def bind_target(conn, question, result, target, constraints=None, allowed_workers=None):
    """Record the shift/worker pair that find_shift resolved, if it fits the user's question.

    The resolved worker (by ID or a name word) must appear in the question; any hall the question names must
    match; and the shift must not contradict an explicit shift ID, date, weekday, shift period, or time in the
    question (see question_constraints). Uncertain wording also blocks binding. A second, different
    resolution is a conflict. In every conflict only a clarification can be accepted. Returns a short note
    shown to the model and in the trace."""
    resolved = result["resolved"]
    shift = next(s for s in result["shifts"] if s["shift_id"] == resolved["shift_id"])
    pair = (resolved["shift_id"], resolved["outgoing_worker_id"])
    words = set(re.findall(r"[a-z0-9]+", question.lower()))
    name_words = set(re.findall(r"[a-z0-9]+", resolved["name"].lower())) | {pair[1].lower()}
    halls_named = {hall for hall in tools.HALLS if hall.lower() in words}
    mismatches = question_mismatches(constraints or question_constraints(question, eligibility.evaluated_week(conn)), shift)
    if allowed_workers and pair[1] not in allowed_workers:
        mismatches.append(f"the clarification names workers {sorted(allowed_workers)}, not {pair[1]}")
    if target["conflict"]:
        return f"target already in conflict: {target['conflict']}"
    if not name_words & words:
        target["conflict"] = f"find_shift resolved {pair[1]} {resolved['name']}, who is not named in the question"
    elif halls_named and shift["hall"] not in halls_named:
        target["conflict"] = f"find_shift resolved a {shift['hall']} shift, but the question names {sorted(halls_named)}"
    elif mismatches:
        target["conflict"] = "; ".join(mismatches)
    elif target["shift_id"] is None:
        target["shift_id"], target["outgoing_worker_id"] = pair
        return f"target bound: {pair[0]} / {pair[1]} {resolved['name']}"
    elif (target["shift_id"], target["outgoing_worker_id"]) != pair:
        target["conflict"] = (f"find_shift resolved {pair[0]} / {pair[1]} after the run was bound to "
                              f"{target['shift_id']} / {target['outgoing_worker_id']}")
    else:
        return f"target confirmed: {pair[0]} / {pair[1]}"
    return f"target conflict: {target['conflict']}. Only a clarification can be accepted now."


def validate_final(conn, final, evidence_seen, target, schedules=None, question="", constraints=None):
    """Return a list of problems with the model's final answer. Empty list means it is accepted.

    Checks: field types; every cited evidence ID was returned by a tool in this run; recommendations and
    no-candidate answers match the target Python bound from find_shift; and the chosen workers pass freshly
    computed eligibility. Nothing is silently corrected or substituted."""
    problems = field_type_problems(final)
    if problems:
        return problems
    status = final["status"]
    refs = final["evidence_refs"]
    problems = [f"evidence {r!r} was never returned by a tool in this run" for r in refs if r not in evidence_seen]

    if status == "schedule":
        if not re.search(r"\b(?:schedule|scheduled|assigned|covering|working|covers|all|every)\b", question, re.I):
            return problems + ["this question requests replacement advice, not a schedule list"]
        sources = [r for r in refs if r.startswith("SCHEDULE:") and r in (schedules or {})]
        if len(sources) != 1:
            return problems + ["cite exactly one retrieved SCHEDULE record for the complete requested list"]
        record = schedules[sources[0]]
        fresh = tools.get_schedule(conn, record["query"])
        if fresh != record:
            problems.append("schedule changed since retrieval; fetch it again")
        words = set(re.findall(r"[a-z0-9]+", question.lower()))
        halls = {hall for hall in tools.HALLS if hall.lower() in words}
        details = constraints or question_constraints(question, eligibility.evaluated_week(conn))
        if details["uncertain"]:
            problems.extend(details["uncertain"])
        if halls and record["query"]["hall"] not in halls:
            problems.append("schedule query does not match the named hall")
        if details["periods"] and record["query"]["shift_name"] not in details["periods"]:
            problems.append("schedule query does not match the requested shift period")
        if details["dates"]:
            week = eligibility.parse_date(record["query"]["week_start"])
            if "week" in words:
                if not all(week <= day < week + timedelta(days=7) for day in details["dates"]):
                    problems.append("schedule week does not match the requested date")
            elif {record["query"]["date"]} != {day.isoformat() for day in details["dates"]}:
                problems.append("schedule date does not match the requested day")
        interval_constraints = dict(details, dates=set())
        for shift in fresh["shifts"]:
            if question_mismatches(interval_constraints, shift):
                problems.append("schedule contains an interval contradicting the requested times or period")
                break
        if any(final[key] is not None for key in ("shift_id", "outgoing_worker_id", "recommended_worker_id", "backup_worker_id")):
            problems.append("a schedule answer lists facts; it must not contain a replacement proposal")
        return problems

    if status == "clarification":
        if not (final["clarification_question"] or "").strip():
            problems.append("clarification needs a clarification_question")
        return problems
    if status == "incomplete":
        return problems

    if target["conflict"]:
        return problems + [f"target conflict ({target['conflict']}); ask a clarification instead"]
    if target["shift_id"] is None:
        return problems + ["no target was resolved; use find_shift with the shift and the outgoing worker named "
                           "in the question, or ask a clarification"]
    if (final["shift_id"], final["outgoing_worker_id"]) != (target["shift_id"], target["outgoing_worker_id"]):
        return problems + [f"final answer is for {final['shift_id']} / {final['outgoing_worker_id']}, but this run "
                           f"resolved {target['shift_id']} / {target['outgoing_worker_id']}; ask a clarification "
                           "before switching targets"]
    try:
        check = eligibility.check_candidates(conn, final["shift_id"], final["outgoing_worker_id"])
    except (EligibilityError, ValueError, TypeError) as error:
        return problems + [f"shift/outgoing worker invalid: {error}"]
    eligible = {c["worker_id"] for c in check["eligible"]}
    shift_id = final["shift_id"]

    if status == "no_eligible_candidate":
        if eligible:
            problems.append(f"claimed no eligible candidate, but eligible workers exist: {sorted(eligible)}")
        if f"SHIFT:{shift_id}" not in refs:
            problems.append(f"cite SHIFT:{shift_id} from the eligibility result")
        return problems

    # status == "recommendation"
    chosen = final["recommended_worker_id"]
    backup = final["backup_worker_id"]
    reasons = {x["worker_id"]: x["reasons"] for x in check["excluded"]}
    if chosen not in eligible:
        problems.append(f"recommended worker {chosen!r} is not eligible"
                        + (f": {'; '.join(reasons[chosen])}" if chosen in reasons else " (unknown worker)"))
    if backup is not None:
        if backup == chosen:
            problems.append("backup must differ from the recommended worker")
        elif backup not in eligible:
            problems.append(f"backup worker {backup!r} is not eligible"
                            + (f": {'; '.join(reasons[backup])}" if backup in reasons else " (unknown worker)"))
    for worker in filter(None, (chosen, backup)):
        if worker in eligible and f"ELIG:{shift_id}:{worker}" not in refs:
            problems.append(f"cite ELIG:{shift_id}:{worker} to show eligibility was checked")
    if len(eligible) > 1 and not any(r.startswith(SOFT_CONTEXT_PREFIXES) for r in refs):
        problems.append("with several eligible workers, cite at least one retrieved context record "
                        "(preference, history, or nearby classes) that supports the choice")
    if not final["rationale"].strip() or not final["tradeoff"].strip():
        problems.append("rationale and tradeoff must not be empty")
    return problems


# ---------- in-memory interactive context ----------

def empty_usage():
    return {"model_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0,
            "unmeasured_tokens": 0, "cost_usd": 0.0, "cost_unknown_calls": 0}


def named_worker_ids(conn, question):
    """Prefer explicit IDs/full names over ambiguous first names in a clarification."""
    text = question.lower()
    words = set(re.findall(r"[a-z0-9]+", text))
    workers = list(conn.execute("SELECT id, name FROM workers"))
    exact = {w["id"] for w in workers if w["id"].lower() in words
             or re.search(r"\b" + re.escape(w["name"].lower()) + r"\b", text)}
    return exact or {w["id"] for w in workers if w["name"].lower().split()[0] in words}


@dataclass
class ChatSession:
    """Context exists only in this process. Token/call/tool caps apply across all turns."""
    messages: list = field(default_factory=list)
    usage: dict = field(default_factory=empty_usage)
    target: dict = field(default_factory=new_target)
    evidence: set = field(default_factory=set)
    schedules: dict = field(default_factory=dict)
    binding_question: str = ""
    constraints: dict | None = None
    allowed_workers: set = field(default_factory=set)
    last_status: str | None = None

    def new_topic(self):
        """Clear context, not the session's spent budget."""
        self.messages.clear()
        self.target = new_target()
        self.evidence.clear()
        self.schedules.clear()
        self.binding_question = ""
        self.constraints = None
        self.allowed_workers.clear()
        self.last_status = None

    def prepare(self, conn, question):
        details = question_constraints(question, eligibility.evaluated_week(conn))
        named = named_worker_ids(conn, question)
        words = set(re.findall(r"[a-z0-9]+", question.lower()))
        # A fully specified new request changes topics; /new also does so explicitly.
        explicit_hall = any(hall.lower() in words for hall in tools.HALLS)
        old_words = set(re.findall(r"[a-z0-9]+", self.binding_question.lower()))
        old_halls = {hall for hall in tools.HALLS if hall.lower() in old_words}
        new_halls = {hall for hall in tools.HALLS if hall.lower() in words}
        if self.messages and explicit_hall and new_halls.isdisjoint(old_halls) and (
                details["dates"] or details["periods"] or details["ranges"] or "week" in words):
            self.new_topic()
        changes_interval = any(details[key] for key in (
            "shift_ids", "dates", "periods", "ranges", "starts", "ends", "times"))
        if changes_interval and self.target["shift_id"] is not None:
            changes_interval = bool(question_mismatches(details, eligibility.get_shift(conn, self.target["shift_id"])))
        if not self.binding_question:
            self.binding_question = question
            self.constraints = details
            self.allowed_workers = named
        elif self.last_status in ("clarification", "schedule") or changes_interval:
            # New explicit details answer the clarification, replacing ambiguous earlier fields.
            for key in ("shift_ids", "dates", "periods", "ranges", "starts", "ends", "times"):
                if details[key]:
                    self.constraints[key] = details[key]
            if details["dates"]:
                self.constraints["uncertain"] = details["uncertain"]
            elif details["uncertain"]:
                self.constraints["uncertain"] += details["uncertain"]
            if named:
                self.allowed_workers = named
            self.binding_question += "\nClarification: " + question
            self.target = new_target()
        self.messages.append({"role": "user", "content": question})


# ---------- the agent loop ----------

def _function_call_item(call):
    return {"type": "function_call", "call_id": call.call_id, "name": call.name, "arguments": call.arguments}


def agent_steps(question, conn, provider, limits=None, clock=time.monotonic, rates=None, session=None):
    """Generator that runs the loop and yields observable events (dicts) as they happen.

    It yields actions, observations, usage, and stop statuses. It never yields hidden reasoning."""
    limits = limits or Limits()
    started = clock()
    # unmeasured_tokens: tokens charged conservatively because the provider reported no usage.
    # cost_usd sums estimated costs of calls whose usage and price are known; cost_unknown_calls counts the rest.
    # Cost is display only and never affects the token budget.
    usage = session.usage if session else empty_usage()
    target = session.target if session else new_target()
    evidence_seen = session.evidence if session else set()
    schedules = session.schedules if session else {}
    seen_calls = set()
    repairs = 0
    duplicate_turns = 0

    def time_left():
        return limits.deadline - (clock() - started)

    def stop(status, message, **extra):
        return {"type": "stop", "status": status, "message": message,
                "usage": dict(usage, seconds=round(clock() - started, 1)), **extra}

    question = (question or "").strip()
    if not question:
        yield stop("invalid_input", "Ask a question, e.g. who should cover a specific shift.")
        return
    if len(question) > limits.max_question_chars:
        yield stop("invalid_input", f"Question is longer than {limits.max_question_chars} characters.")
        return

    if session:
        session.prepare(conn, question)
        target = session.target
        conversation = session.messages
        binding_question = session.binding_question
        constraints = session.constraints
    else:
        conversation = [{"role": "user", "content": question}]
        binding_question, constraints = question, None
    yield {"type": "start", "question": question, "limits": limits, "rates": rates,
           "session": session is not None}

    while True:
        # ----- guardrails checked BEFORE spending anything -----
        if time_left() < 1:
            yield stop("deadline", f"Stopped: {limits.deadline:.0f}-second run deadline reached.")
            return
        if usage["model_calls"] >= limits.max_model_calls:
            yield stop("call_limit", f"Stopped: {limits.max_model_calls}-model-call limit reached without a valid answer.")
            return
        last_call = usage["model_calls"] == limits.max_model_calls - 1
        tools_exhausted = usage["tool_calls"] >= limits.max_tool_calls
        request = {
            "instructions": INSTRUCTIONS,
            "input": conversation,
            "tools": tools.TOOL_SCHEMAS,
            "text": FINAL_FORMAT,
            # On the last allowed call, or with no tool budget left, the model must answer now.
            "tool_choice": "none" if (last_call or tools_exhausted) else "auto",
        }

        # Reserve counted input (with margin) plus the maximum possible output inside the allowance.
        try:
            counted = provider.count_input_tokens(request, timeout=min(limits.request_timeout, time_left()))
        except ProviderError as error:
            yield stop("provider_unavailable", f"Provider unavailable while counting tokens: {error}")
            return
        reserved_input = math.ceil(counted * limits.input_margin)
        reserved = reserved_input + limits.max_output_tokens
        spent = usage["input_tokens"] + usage["output_tokens"]
        if spent + reserved > limits.token_allowance:
            yield stop("token_limit", f"Stopped before call {usage['model_calls'] + 1}: needs up to {reserved:,} tokens "
                       f"(input ~{reserved_input:,} + output {limits.max_output_tokens:,}) but only "
                       f"{limits.token_allowance - spent:,} of {limits.token_allowance:,} remain.")
            return

        # Token counting takes time too, so re-check the deadline before paying for generation.
        if time_left() < 1:
            yield stop("deadline", f"Stopped before call {usage['model_calls'] + 1}: {limits.deadline:.0f}-second "
                       "run deadline reached while counting tokens.")
            return

        # ----- the paid model call -----
        call_started = clock()
        usage["model_calls"] += 1
        try:
            reply = provider.create(request, max_output_tokens=limits.max_output_tokens,
                                    timeout=min(limits.request_timeout, time_left()))
        except ProviderError as error:
            # A failed request may still have consumed input and generated output, and usage is unknown,
            # so charge the whole reservation and label it as unmeasured.
            usage["input_tokens"] += reserved_input
            usage["output_tokens"] += limits.max_output_tokens
            usage["unmeasured_tokens"] += reserved
            usage["cost_unknown_calls"] += 1
            yield stop("provider_unavailable", f"Provider request failed: {error}. Usage unknown; charged the full "
                       f"reservation of {reserved:,} tokens.")
            return
        # If the provider omits usage, charge the reservation rather than pretending it was free.
        if reply.input_tokens is None:
            usage["input_tokens"] += reserved_input
            usage["unmeasured_tokens"] += reserved_input
        else:
            usage["input_tokens"] += reply.input_tokens
        if reply.output_tokens is None:
            usage["output_tokens"] += limits.max_output_tokens
            usage["unmeasured_tokens"] += limits.max_output_tokens
        else:
            usage["output_tokens"] += reply.output_tokens
        cost = pricing.estimate_cost(rates, reply.input_tokens, reply.output_tokens,
                                     reply.cached_tokens, reply.cache_write_tokens)
        if cost is None:
            usage["cost_unknown_calls"] += 1
        else:
            usage["cost_usd"] += cost
        yield {"type": "model_call", "step": usage["model_calls"], "counted_input": counted, "reserved": reserved,
               "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
               "reasoning_tokens": reply.reasoning_tokens, "cached_tokens": reply.cached_tokens,
               "cache_write_tokens": reply.cache_write_tokens,
               "seconds": round(clock() - call_started, 1),
               "cumulative": usage["input_tokens"] + usage["output_tokens"], "allowance": limits.token_allowance,
               "tool_choice": request["tool_choice"], "cost_usd": cost,
               "tool_names": [call.name for call in reply.tool_calls], "reply_status": reply.status,
               "cumulative_cost_usd": usage["cost_usd"], "cost_unknown_calls": usage["cost_unknown_calls"]}

        # A reply that arrives after the run deadline is charged but not used.
        if time_left() < 0:
            yield stop("deadline", f"Model reply arrived after the {limits.deadline:.0f}-second run deadline; "
                       "it was charged but discarded.")
            return

        if reply.status != "completed":
            if reply.incomplete_reason == "max_output_tokens":
                yield stop("truncated", f"Model output was cut off at {limits.max_output_tokens} tokens; "
                           "no partial answer is used.")
            else:
                yield stop("incomplete", f"Model reply incomplete ({reply.incomplete_reason or 'unknown reason'}).")
            return

        # ----- tool calls: validate, dispatch, append paired observations -----
        if reply.tool_calls:
            if request["tool_choice"] == "none":
                yield stop("protocol_error", "Model requested tools after tools were disabled.")
                return
            if reply.text:
                conversation.append({"role": "assistant", "content": reply.text})
            new_calls = 0
            for call in reply.tool_calls:
                conversation.append(_function_call_item(call))
                yield {"type": "tool_call", "name": call.name, "arguments": call.arguments}
                key = (call.name, call.arguments)
                if usage["tool_calls"] >= limits.max_tool_calls:
                    result = {"error": f"tool limit ({limits.max_tool_calls}) reached; not executed. Give your final answer."}
                elif key in seen_calls:
                    usage["tool_calls"] += 1
                    result = {"error": "duplicate of an earlier call; that result is already in the conversation."}
                else:
                    usage["tool_calls"] += 1
                    seen_calls.add(key)
                    new_calls += 1
                    result = tools.dispatch(conn, call.name, call.arguments)
                    evidence_seen.update(tools.evidence_ids(result))
                    if call.name == "get_schedule" and "error" not in result:
                        schedules[result["evidence_id"]] = result
                    if call.name == "find_shift" and result.get("resolution") == "resolved":
                        result["target_check"] = bind_target(conn, binding_question, result, target,
                                                             constraints, session.allowed_workers if session else None)
                yield {"type": "observation", "name": call.name, "result": result}
                conversation.append({"type": "function_call_output", "call_id": call.call_id,
                                     "output": json.dumps(result, separators=(",", ":"))})
            duplicate_turns = duplicate_turns + 1 if new_calls == 0 else 0
            if duplicate_turns >= 2:
                yield stop("repeating", "Stopped: the model repeated calls without new progress twice in a row.")
                return
            continue

        # ----- final answer: parse, then mandatory Python validation -----
        try:
            final = json.loads(reply.text)
            problems = validate_final(conn, final, evidence_seen, target, schedules, binding_question, constraints)
        except json.JSONDecodeError:
            final, problems = None, ["final answer was not valid JSON"]
        if not problems:
            if session:
                conversation.append({"role": "assistant", "content": reply.text})
                session.last_status = final["status"]
            schedule = next((schedules[r] for r in final["evidence_refs"] if r in schedules), None)
            yield {"type": "final", "status": final["status"], "result": final,
                   "usage": dict(usage, seconds=round(clock() - started, 1)),
                   "schedule": schedule, "session": session is not None}
            return
        yield {"type": "validation_failed", "problems": problems, "result": final}
        if repairs >= limits.max_repairs:
            yield stop("invalid_recommendation", "Final answer failed Python validation; no recommendation is shown.",
                       problems=problems)
            return
        repairs += 1
        conversation.append({"role": "assistant", "content": reply.text})
        conversation.append({"role": "user", "content":
                             "Python validation rejected that final answer:\n- " + "\n- ".join(problems) +
                             "\nReturn a corrected final JSON answer. Choose only eligible workers and cite only "
                             "evidence IDs that appeared in tool results."})


# ---------- OpenAI provider (the only code that talks to the network) ----------

class OpenAIProvider:
    """Responses API with structured function calling. store=False keeps no server-side state;
    automatic SDK retries are disabled so every paid request is visible and counted."""

    def __init__(self, model, api_key, reasoning_effort=None):
        from openai import OpenAI
        self.model = model
        self.reasoning = {"effort": reasoning_effort} if reasoning_effort else None
        self.client = OpenAI(api_key=api_key, max_retries=0)

    def _params(self, request):
        params = {"model": self.model, "instructions": request["instructions"], "input": request["input"],
                  "tools": request["tools"], "text": request["text"], "tool_choice": request["tool_choice"]}
        if self.reasoning:
            params["reasoning"] = self.reasoning
        return params

    @staticmethod
    def _describe(error):
        status = getattr(error, "status_code", None)
        code = getattr(error, "code", None)
        return " ".join(str(x) for x in (type(error).__name__, status, code) if x)

    def count_input_tokens(self, request, timeout):
        import openai
        try:
            return self.client.responses.input_tokens.count(**self._params(request), timeout=timeout).input_tokens
        except openai.OpenAIError as error:
            raise ProviderError(self._describe(error)) from None

    def create(self, request, max_output_tokens, timeout):
        import openai
        try:
            response = self.client.responses.create(**self._params(request), max_output_tokens=max_output_tokens,
                                                    store=False, timeout=timeout)
        except openai.OpenAIError as error:
            raise ProviderError(self._describe(error)) from None
        calls = [ToolCall(item.call_id, item.name, item.arguments) for item in response.output
                 if item.type == "function_call"]
        usage = response.usage
        details = getattr(usage, "output_tokens_details", None) if usage else None
        input_details = getattr(usage, "input_tokens_details", None) if usage else None
        return ModelReply(
            status=response.status,
            tool_calls=calls,
            text=response.output_text or "",
            incomplete_reason=getattr(response.incomplete_details, "reason", None),
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            reasoning_tokens=getattr(details, "reasoning_tokens", None),
            cached_tokens=getattr(input_details, "cached_tokens", None),
            cache_write_tokens=getattr(input_details, "cache_write_tokens", None),
        )


def provider_from_env():
    """Read root .env. Never prints the key."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    key, model = os.getenv("OPENAI_API_KEY"), os.getenv("OPENAI_MODEL")
    if not key or not model:
        raise ProviderError("OPENAI_API_KEY and OPENAI_MODEL must be set in .env")
    return OpenAIProvider(model, key, os.getenv("OPENAI_REASONING_EFFORT") or None), model


# ---------- terminal display ----------

def summarize(name, result):
    """One or two readable lines per observation. The full result still goes to the model."""
    if "error" in result:
        return f"error: {result['error']}"
    if name == "find_shift":
        shifts = ", ".join(f"{s['shift_id']} ({s['hall']} {s['name']}, {s['time_range']}, "
                           f"{s['assigned_worker']['worker_id'] if s['assigned_worker'] else 'unassigned'})"
                           for s in result["shifts"]) or "none"
        detail = result.get("resolved") or result.get("possible_pairs") or result.get("worker_matches") or ""
        note = f" [{result['target_check']}]" if "target_check" in result else ""
        return f"{result['resolution']}: shifts {shifts} {json.dumps(detail) if detail else ''}".rstrip() + note
    if name == "get_eligible_candidates":
        s = result["shift"]
        eligible = "; ".join(f"{c['worker_id']} {c['name']}: {hours_text(c)}" for c in result["eligible"]) or "nobody"
        return (f"{s['hall']} {s['name']}, {s['time_range']} ({eligibility.format_minutes(s['duration_minutes'])}): "
                f"{result['eligible_count']} eligible: {eligible}; {len(result['excluded'])} excluded")
    if name == "get_preferences":
        return ", ".join(f"{p['worker_id']} {p['level']}" for p in result["preferences"])
    if name == "get_coverage_history":
        return "; ".join(f"{w['worker_id']} asked {w['counts']['asked']} (completed {w['counts']['completed']}, "
                         f"declined {w['counts']['declined']})" for w in result["workers"])
    if name == "get_nearby_classes":
        return "; ".join(f"{w['worker_id']} " + (", ".join(f"{c['course']} {c['relation']} gap {c['gap_minutes']}m"
                                                            for c in w["nearby_classes"]) or "none")
                         for w in result["workers"])
    return json.dumps(result)[:200]


def hours_text(capacity):
    return (f"{capacity['assigned_hours_now']:g}h assigned now -> {capacity['hours_after_covering']:g}h after "
            f"covering, {capacity['hours_left_after_covering']:g}h left")


def cost_text(amount, unknown_calls):
    if unknown_calls == 0:
        return pricing.format_usd(amount)
    if amount:
        return f"{pricing.format_usd(amount)} for measured calls + unknown for {unknown_calls}"
    return "unknown"


def shift_line(conn, shift_id):
    try:
        s = eligibility.get_shift(conn, shift_id)
    except (EligibilityError, ValueError):
        return str(shift_id)
    return f"{s['hall']} {s['name']}, {s['time_range']} ({s['shift_id']})"


def worker_name(conn, worker_id):
    row = conn.execute("SELECT name FROM workers WHERE id = ?", (worker_id,)).fetchone() if worker_id else None
    return f"{worker_id} {row['name']}" if row else str(worker_id)


OUTPUT_WIDTH = 76

TOOL_ACTIONS = {
    "get_schedule": "Read scheduled assignments",
    "find_shift": "Identify the shift and outgoing worker",
    "get_eligible_candidates": "Check who can cover the whole shift",
    "get_preferences": "Read candidate preferences",
    "get_coverage_history": "Read recent emergency coverage",
    "get_nearby_classes": "Check nearby classes",
}


def terminal_width():
    """Use a narrow left-aligned column, even when the terminal is very wide."""
    return min(OUTPUT_WIDTH, max(20, shutil.get_terminal_size((80, 24)).columns - 2))


def terminal_text(text, width=OUTPUT_WIDTH):
    """Wrap every line without dropping content; preserve indentation on continuation lines."""
    lines = []
    for line in str(text).splitlines():
        if not line.strip():
            lines.append("")
            continue
        indent = line[:len(line) - len(line.lstrip())]
        continuation = indent + ("  " if line.lstrip().startswith("- ") else "")
        lines.append(textwrap.fill(line, width=max(20, width),
                                   subsequent_indent=continuation,
                                   break_long_words=True, break_on_hyphens=False))
    return "\n".join(lines)


def observation_lines(name, result):
    """Display facts as separate records rather than horizontal JSON or candidate lists."""
    if "error" in result:
        return [f"  Result: error - {result['error']}"]
    if name == "get_schedule":
        return [f"  Result: {result['count']} scheduled shifts | Week: {result['query']['week_start']}"] + [
            f"    {shift['time_range']} | {shift['hall']} | "
            + (f"{shift['assigned_worker']['name']} ({shift['assigned_worker']['worker_id']})"
               if shift["assigned_worker"] else "Unassigned") for shift in result["shifts"]]
    if name == "find_shift":
        lines = [f"  Result: {result['resolution'].replace('_', ' ')}"]
        for shift in result["shifts"]:
            lines += [f"    {shift['hall']} | {shift['time_range']}",
                      f"    Shift ID: {shift['shift_id']}"]
            worker = shift["assigned_worker"]
            lines.append(f"    Assigned: {worker['name']} ({worker['worker_id']})" if worker else "    Assigned: nobody")
        if not result["shifts"]:
            lines.append("    No matching shifts.")
        if result.get("worker_matches") and result["resolution"] != "resolved":
            lines.append("    Worker matches:")
            lines += [f"      - {w['name']} ({w['worker_id']})" for w in result["worker_matches"]]
        if result.get("possible_pairs"):
            lines.append("    Possible call-outs:")
            lines += [f"      - {p['name']} ({p['worker_id']}): {p['shift_id']}" for p in result["possible_pairs"]]
        if result.get("matched_workers_nearby_assignments"):
            lines.append("    Named worker's nearby assignments:")
            lines += [f"      - {p['worker_id']}: {p['hall']}, {p['start']} ({p['shift_id']})"
                      for p in result["matched_workers_nearby_assignments"]]
        if result.get("target_check"):
            lines.append(f"    Target check: {result['target_check']}")
        return lines
    if name == "get_eligible_candidates":
        shift = result["shift"]
        lines = [f"  Result: {shift['hall']} | {shift['time_range']}",
                 f"    Duration: {eligibility.format_minutes(shift['duration_minutes'])}",
                 f"    Eligible: {result['eligible_count']} | Excluded: {len(result['excluded'])}"]
        if not result["eligible"]:
            lines.append("    Nobody can cover the whole interval.")
        for worker in result["eligible"]:
            lines += [f"    - {worker['name']} ({worker['worker_id']})",
                      f"      {hours_text(worker)}"]
        return lines
    if name == "get_preferences":
        return ["  Result: candidate preferences"] + [
            f"    - {p['name']} ({p['worker_id']}): {p['level']}" for p in result["preferences"]]
    if name == "get_coverage_history":
        lines = ["  Result: emergency coverage history", f"    Window: {result['window']}"]
        for worker in result["workers"]:
            counts = worker["counts"]
            lines += [f"    - {worker['name']} ({worker['worker_id']}) | Asked: {counts['asked']} | "
                      f"Completed: {counts['completed']} | Declined: {counts['declined']} | "
                      f"Accepted: {counts['accepted']} | Pending: {counts['pending']}"]
        return lines
    if name == "get_nearby_classes":
        lines = [f"  Result: classes within {result['window_minutes']} minutes"]
        for worker in result["workers"]:
            if not worker["nearby_classes"]:
                lines.append(f"    - {worker['name']} ({worker['worker_id']}) | No nearby classes in this window.")
            for block in worker["nearby_classes"]:
                lines += [f"    - {worker['name']} ({worker['worker_id']}) | {block['course']}: "
                          f"{block['relation']} | Gap: {block['gap_minutes']} minutes"]
        return lines
    return ["  Result: " + json.dumps(result)]


def render(event, conn, width=OUTPUT_WIDTH):
    """Human-readable terminal trace. One Step is one model call, with its requested tools below."""
    kind = event["type"]
    lines = []
    if kind == "start":
        limits = event["limits"]
        lines = ["Limits",
                 f"  {limits.max_model_calls} steps | {limits.max_tool_calls} tool actions",
                 f"  {limits.max_output_tokens:,} output tokens/step | {limits.token_allowance:,} tokens/run",
                 f"  {limits.request_timeout:g}s/request | {limits.deadline:g}s/run",
                 "", pricing.describe(event["rates"]), "Estimates only; limits use tokens."]
        if event.get("session"):
            lines.append("Interactive mode: token/step/tool caps cover the whole session.")
    elif kind == "model_call":
        lines = ["", f"Step-{event['step']}", "-" * min(36, width)]
        names = event.get("tool_names", [])
        if names:
            lines.append("Action: Call tools")
            lines.append("Tools: " + ", ".join(names))
        else:
            lines.append("Action: " + ("Prepare final answer" if event.get("reply_status") == "completed"
                                      else "Report incomplete model reply"))
        tokens_in, tokens_out = event["input_tokens"], event["output_tokens"]
        actual = (f"in {tokens_in:,}" if tokens_in is not None else "in unknown") + " / " + \
                 (f"out {tokens_out:,}" if tokens_out is not None else "out unknown")
        missing = [name for name, value in (("reserved input", tokens_in), ("max output", tokens_out)) if value is None]
        if missing:
            prefix = "usage not reported: " if len(missing) == 2 else "partial usage: "
            actual = f"{prefix}{actual} ({' and '.join(missing)} charged)"
        lines += [f"Tokens: {actual} | Time: {event['seconds']}s"]
        cache = (f"cache read {event['cached_tokens']:,} / write {event['cache_write_tokens']:,}"
                 if event["cached_tokens"] is not None and event["cache_write_tokens"] is not None
                 else "cache breakdown unreported")
        lines.append(f"Cache: ({cache})")
        if event["reasoning_tokens"]:
            lines.append(f"Usage detail: reasoning {event['reasoning_tokens']:,} incl. in out")
        lines += [f"Reserved: {event['reserved']:,} | Used so far: {event['cumulative']:,}/{event['allowance']:,} tokens",
                  f"est. cost: {pricing.format_usd(event['cost_usd'])} this call | "
                  f"{cost_text(event['cumulative_cost_usd'], event['cost_unknown_calls'])} so far"]
        if event["tool_choice"] == "none":
            lines.append("Tools disabled: final response required by run limits.")
    elif kind == "tool_call":
        lines = ["", f"  Tool: {event['name']} | Action: {TOOL_ACTIONS.get(event['name'], 'Requested tool')}"]
        try:
            arguments = json.loads(event["arguments"])
        except (ValueError, TypeError):
            arguments = event["arguments"]
        if isinstance(arguments, dict):
            useful = [(key, value) for key, value in arguments.items() if value is not None]
            if useful:
                shown_arguments = []
                for key, value in useful:
                    shown = ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
                    shown_arguments.append(f"{key}: {shown}")
                lines.append("  Arguments: " + " | ".join(shown_arguments))
        else:
            lines.append(f"  Arguments: {arguments}")
    elif kind == "observation":
        lines = observation_lines(event["name"], event["result"])
    elif kind == "validation_failed":
        lines = ["", "Validation: answer rejected by Python"]
        lines += [f"  - {problem}" for problem in event["problems"]]
    else:
        usage = event["usage"]
        if kind == "stop":
            lines = ["", f"STOPPED ({event['status']})", event["message"]]
            if event.get("problems"):
                lines += [f"  - {problem}" for problem in event["problems"]]
        else:
            result = event["result"]
            lines = ["", f"FINAL ({result['status']}, validated by Python)"]
            if result["status"] == "schedule":
                record = event["schedule"]
                lines.append("Scheduled assignments (from SQLite):")
                lines += [f"  {s['time_range']} | {s['hall']} | "
                          + (worker_name(conn, s['assigned_worker']['worker_id']) if s['assigned_worker'] else "Unassigned")
                          for s in record["shifts"]]
                if not record["shifts"]:
                    lines.append("  No assignments match these filters.")
            if result["status"] in ("recommendation", "no_eligible_candidate"):
                lines += [f"  Shift: {shift_line(conn, result['shift_id'])}",
                          f"  Outgoing: {worker_name(conn, result['outgoing_worker_id'])}"]
            if result["status"] == "recommendation":
                lines.append(f"  Ask first: {worker_name(conn, result['recommended_worker_id'])}")
                if result["backup_worker_id"]:
                    lines.append(f"  Backup: {worker_name(conn, result['backup_worker_id'])}")
                check = eligibility.check_candidates(conn, result["shift_id"], result["outgoing_worker_id"])
                capacity = {candidate["worker_id"]: candidate for candidate in check["eligible"]}
                for worker in filter(None, (result["recommended_worker_id"], result["backup_worker_id"])):
                    lines += [f"  Weekly hours, {worker_name(conn, worker)}: {hours_text(capacity[worker])} "
                              f"of the {eligibility.ABSOLUTE_WEEKLY_CAP_MINUTES // 60}h cap"]
            if result["clarification_question"]:
                lines += ["", f"Question: {result['clarification_question']}"]
            lines += ["", f"Why: {result['rationale']}"]
            if result["tradeoff"]:
                lines += ["", f"Tradeoff: {result['tradeoff']}"]
            lines += ["", "Evidence:"]
            lines += [f"  - {ref}" for ref in result["evidence_refs"]] or ["  none"]
            lines += ["", "Read-only: nothing was assigned or sent." if result["status"] == "schedule"
                      else "Recommendation only: nothing was assigned or sent."]
        lines += ["", "Session totals" if event.get("session") else "Run totals",
                  f"  Steps: {usage['model_calls']} | Tool actions: {usage['tool_calls']}",
                  f"  Tokens: {usage['input_tokens']:,} input + {usage['output_tokens']:,} output",
                  f"  {'Time this turn' if event.get('session') else 'Time'}: {usage['seconds']}s"]
        if usage["unmeasured_tokens"]:
            lines.append(f"  Includes {usage['unmeasured_tokens']:,} unmeasured tokens charged conservatively")
        lines.append(f"Estimated cost: {cost_text(usage['cost_usd'], usage['cost_unknown_calls'])} (estimate, not a bill)")
    return terminal_text("\n".join(lines), width)


def chat(conn, provider, rates, width, initial_question=""):
    """Interactive input stays in this process; /new clears context without resetting spent budget."""
    session = ChatSession()
    print(terminal_text("Chat: /new starts a new topic | /quit exits. Context lasts only here.\n"
                        "Caps: 10 model steps, 12 tools, 50,000 tokens for the entire session.", width))
    pending = initial_question
    while True:
        try:
            question = pending or input("\nYou> ")
        except (EOFError, KeyboardInterrupt):
            print("\nChat closed.")
            return 0
        pending = ""
        if question.strip().lower() in ("/quit", "/exit"):
            print("Chat closed.")
            return 0
        if question.strip().lower() == "/new":
            session.new_topic()
            print("Context cleared; spent session budget retained.")
            continue
        status = None
        for event in agent_steps(question, conn, provider, rates=rates, session=session):
            print(render(event, conn, width), flush=True)
            if event["type"] in ("final", "stop"):
                status = event["status"]
        if status in ("call_limit", "tool_limit", "token_limit", "provider_unavailable", "deadline"):
            print("Chat stopped. Start a new invocation deliberately if needed.")
            return 2


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(terminal_text(__doc__.strip(), terminal_width()))
        return 0
    # Model text may contain characters like "≤" or "→". When output is piped or redirected on Windows,
    # Python defaults to cp1252 and would crash mid-run; write UTF-8 and replace anything unencodable.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    interactive = argv[0] == "--chat"
    question = " ".join(argv[1:] if interactive else argv)
    try:
        conn = database.connect(database.DB_PATH, read_only=True)
    except FileNotFoundError as error:
        print(terminal_text(f"Error: {error}", terminal_width()), file=sys.stderr)
        return 1
    try:
        provider, model = provider_from_env()
    except ProviderError as error:
        print(terminal_text(f"STOPPED (provider_unavailable): {error}", terminal_width()))
        return 1
    width = terminal_width()
    print(terminal_text(f"ShiftOps advisor\nModel: {model}\nDatabase: read-only", width))
    if interactive:
        try:
            return chat(conn, provider, pricing.rates_for(model), width, question)
        finally:
            conn.close()
    print(terminal_text(f"Question: {question}", width))
    final_status = None
    for event in agent_steps(question, conn, provider, rates=pricing.rates_for(model)):
        print(render(event, conn, width), flush=True)
        if event["type"] in ("final", "stop"):
            final_status = event["status"]
    conn.close()
    return 0 if final_status in FINAL_STATUSES else 2


if __name__ == "__main__":
    sys.exit(main())
