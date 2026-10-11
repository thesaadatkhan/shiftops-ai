"""ShiftOps advisor: whom should a housing supervisor ask first when a student worker calls out?

    python agent.py "Jordan called out for Andromeda's October 12 evening shift. Who should I ask first?"

Python decides who CAN cover (hard rules) and checks the final answer.
The model decides who SHOULD be asked first among eligible workers, and explains the tradeoff.

The loop (agent_steps) is written by hand:
  1. Send the conversation and the five tool schemas to the model.
  2. If the model asks for tools, run them in Python and append each result to the conversation.
  3. If the model answers, Python checks the answer. A clarification question can be answered by
     the supervisor up to twice; everything shares one budget.
  4. Repeat until a checked answer, or until a limit stops the run.
Read-only: nothing is assigned and nobody is contacted.
"""

import json
import math
import os
import re
import shutil
import sys
import textwrap
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import database
import eligibility
import pricing
import tools
from eligibility import EligibilityError

ROOT = Path(__file__).resolve().parent

# Every limit lives here. The terminal header is printed from these same values.
LIMITS = {
    "model_calls": 10,        # paid model calls per task (includes the final answer and any correction)
    "tool_calls": 12,         # tool executions per task
    "output_tokens": 1000,    # maximum output tokens per model call (reasoning tokens count toward it)
    "total_tokens": 50_000,   # input + output tokens summed across the whole task
    "request_seconds": 30,    # timeout for one provider request
    "run_seconds": 120,       # execution deadline; time spent waiting for the supervisor's reply is excluded
    "clarifications": 2,      # supervisor replies to clarification questions
    "corrections": 1,         # retries after Python rejects a final answer
    "question_chars": 500,    # maximum length of the question or a reply
}
INPUT_MARGIN = 1.10           # safety margin on the provider's input-token count before each paid call

SYSTEM_PROMPT = """You help a residence-hall supervisor decide whom to ASK FIRST when a student worker calls out.
You only recommend: you never assign shifts or contact anyone. You cannot see data directly; use the tools.

The demo schedule covers Monday 2026-10-12 to Sunday 2026-10-18 on one local clock. A weekday name means that
week. Andromeda runs 24 hours. Its Overnight (23:00-04:00) and Early Morning (04:00-08:00) shifts belong to the
operating day that began at 08:00, so one date can match two early-morning shifts.

TOOLS
- find_shift: find the dated shift and check that the named worker holds it. Python locks the first shift that
  matches the request; your answer must be about that shift.
- get_eligible_candidates: Python's hard rules (classes, approved leave, overlapping assignments, confirmed
  timetable, absolute 20-hour weekly cap for the whole shift). Only workers listed as eligible may be
  recommended. Nobody can override these rules, split a shift, or shorten it.
- get_preferences, get_coverage_history, get_nearby_classes: soft facts about eligible workers. Fetch the ones
  that help you decide; you do not have to call every tool.

WEEKLY HOURS (use the tool's numbers exactly; do not recompute or relabel them)
- assigned_hours_now: hours the worker is already scheduled this week.
- hours_after_covering: total scheduled hours after adding this whole shift.
- hours_left_after_covering: remaining capacity under the 20-hour cap after covering.

PREFERENCES
"preferred", "neutral", and "low" are levels. A stored "low" preference is still a stored preference, so
"preferred" is not the same as "has a stored preference". A missing preference is neutral.

JUDGMENT
Weigh preferences, recent emergency coverage, tight gaps to nearby classes, and weekly hours. Say what you are
trading off. Do not say someone is "the only" or "the most" anything unless the fetched facts show that
comparison across the eligible workers. Explain only facts you actually observed in tool results.
Before recommending, get the eligible list and at least one kind of soft context for every worker you name.
Do not claim someone is tired, willing, or likely to accept unless a record shows it.
The records contain no gender: refer to workers by name or as "they".

FINAL ANSWER (JSON only; unused fields are null)
- "recommendation": shift_id, ask_first (worker ID), backup (another eligible worker ID or null), explanation
  (under 90 words, including the tradeoff).
- "clarification": the worker or shift is ambiguous, or the named worker does not hold that shift. Put one short
  question in question, for example "Alex Chen or Alex Rivera?". The supervisor may answer it.
- "no_eligible_candidate": nobody is eligible. Give shift_id and explanation.
- "incomplete": the facts you have are not enough to recommend responsibly. Explain what is missing."""

STATUSES = ("recommendation", "clarification", "no_eligible_candidate", "incomplete")
ANSWER_FIELDS = ("status", "shift_id", "ask_first", "backup", "explanation", "question")
ANSWER_FORMAT = {"format": {"type": "json_schema", "name": "shiftops_answer", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": list(ANSWER_FIELDS),
    "properties": {"status": {"type": "string", "enum": list(STATUSES)},
                   **{key: {"type": ["string", "null"]} for key in ANSWER_FIELDS[1:]}}}}}


# ---------- the model provider (the only code that talks to the network) ----------

@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass
class ModelReply:
    """What one model call returned. Tests build these by hand instead of calling the API."""
    status: str                            # "completed" or "incomplete"
    tool_calls: list = field(default_factory=list)
    text: str = ""
    incomplete_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None       # already includes reasoning tokens
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None       # part of input_tokens read from the prompt cache
    cache_write_tokens: int | None = None  # part of input_tokens written to the prompt cache


class ProviderError(Exception):
    """The provider could not be reached or refused the request. The message never contains secrets."""


class OpenAIProvider:
    """OpenAI Responses API with structured tool calls. store=False keeps no server-side conversation, and
    automatic retries are off so every paid request is visible and counted."""

    def __init__(self, model, api_key, reasoning_effort=None):
        from openai import OpenAI
        self.model = model
        self.reasoning = {"effort": reasoning_effort} if reasoning_effort else None
        self.client = OpenAI(api_key=api_key, max_retries=0)

    def _params(self, request):
        params = {"model": self.model, **request}
        if self.reasoning:
            params["reasoning"] = self.reasoning
        return params

    def count_input_tokens(self, request, timeout):
        import openai
        try:
            return self.client.responses.input_tokens.count(**self._params(request), timeout=timeout).input_tokens
        except openai.OpenAIError as error:
            raise ProviderError(_describe(error)) from None

    def create(self, request, max_output_tokens, timeout):
        import openai
        try:
            response = self.client.responses.create(**self._params(request), max_output_tokens=max_output_tokens,
                                                    store=False, timeout=timeout)
        except openai.OpenAIError as error:
            raise ProviderError(_describe(error)) from None
        usage = response.usage
        inputs = getattr(usage, "input_tokens_details", None)
        outputs = getattr(usage, "output_tokens_details", None)
        return ModelReply(
            status=response.status,
            tool_calls=[ToolCall(item.call_id, item.name, item.arguments)
                        for item in response.output if item.type == "function_call"],
            text=response.output_text or "",
            incomplete_reason=getattr(response.incomplete_details, "reason", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            reasoning_tokens=getattr(outputs, "reasoning_tokens", None),
            cached_tokens=getattr(inputs, "cached_tokens", None),
            cache_write_tokens=getattr(inputs, "cache_write_tokens", None),
        )


def _describe(error):
    return " ".join(str(part) for part in (type(error).__name__, getattr(error, "status_code", None),
                                           getattr(error, "code", None)) if part)


def provider_from_env():
    """Read the API key and model from the root .env. The key is never printed."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    key, model = os.getenv("OPENAI_API_KEY"), os.getenv("OPENAI_MODEL")
    if not key or not model:
        raise ProviderError("OPENAI_API_KEY and OPENAI_MODEL must be set in .env")
    return OpenAIProvider(model, key, os.getenv("OPENAI_REASONING_EFFORT") or None), model


# ---------- correct-shift protection ----------
# Python reads a few explicit forms from the request so that it never locks onto a shift that contradicts it.
# Supported:
#   workers  W02, a full name (Alex Chen), or a first name (Alex, which may match several workers)
#   halls    Andromeda, Capella, Helix, Vega, Sirius; shift IDs such as AND-1012-EVE
#   dates    2026-10-12; Oct 12 / October 12th, optionally with a year (October 12, 2027); 10/12 or 10/12/2027;
#            weekday names (Monday...Sunday; Mon/Tue/Wed/Thu/Fri), meaning that day of the demo week
#   periods  morning, afternoon, evening, overnight, early morning, night (= evening or overnight)
#   times    6 PM, 6:30 p.m., 18:00, noon, midnight; ranges "6-11 PM", "6 PM to 11 PM", "18:00-23:00";
#            "starting at 6 PM" / "until 11 PM"
# Anything unclear ("tonight", two different dates, "7 to 11" without AM/PM) means: ask, do not guess.

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MONTH_WORD = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "thursday": 3,
            "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sunday": 6}
SHIFT_ID = re.compile(r"\b(?:and|cap|hel|veg|sir)-\d{4}-(?:mor|aft|eve|ovn|em)\b")
TIME = r"(\bnoon\b|\bmidnight\b|\d{1,2}(?::[0-5]\d)?(?:\s*[ap]\.?m\b\.?)?)"
EXPLICIT_TIME = r"(\bnoon\b|\bmidnight\b|\d{1,2}(?::[0-5]\d)?\s*[ap]\.?m\b\.?|\b(?:[01]?\d|2[0-3]):[0-5]\d\b)"
RANGE = rf"(?<![\d:]){TIME}\s*(?:-|–|—|\bto\b|\buntil\b|\btill\b)\s*{TIME}"
START = rf"\b(?:from|starting|starts)(?:\s+at)?\s+{EXPLICIT_TIME}"
END = rf"\b(?:until|till|ending|ends)(?:\s+at)?\s+{EXPLICIT_TIME}"
GROUPS = {"workers": ("workers",), "shift_ids": ("shift_ids",), "halls": ("halls",), "dates": ("dates",),
          "periods": ("periods",), "times": ("ranges", "starts", "ends", "times")}


def _clock(token, meridiem=None):
    """'6 pm' -> '18:00', '18:00' -> '18:00', 'noon' -> '12:00'. A missing am/pm can be supplied. None if invalid
    (a bare number such as '7' is not a time)."""
    token = token.strip()
    if token in ("noon", "midnight"):
        return "12:00" if token == "noon" else "00:00"
    m = re.fullmatch(r"(\d{1,2})(?::(\d\d))?\s*(?:([ap])\.?m\.?)?", token)
    hour, minute, ap = int(m[1]), int(m[2] or 0), m[3] or meridiem
    if ap:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ap == "p" else 0)
    elif m[2] is None or hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def named_workers(text, workers):
    """Worker IDs that the text names. An explicit ID (W02) or full name (Alex Chen) wins; otherwise a first name
    (Alex) names every worker with that first name, which may be more than one."""
    exact = {w["id"] for w in workers if re.search(rf"\b({w['id']}|{re.escape(w['name'])})\b", text, re.IGNORECASE)}
    if exact:
        return exact
    words = set(re.findall(r"[a-z]+", text.lower()))
    return {w["id"] for w in workers if w["name"].split()[0].lower() in words}


def question_details(text, week_start, workers=()):
    """The explicit details in one piece of supervisor text (supported forms only)."""
    d = {key: set() for keys in GROUPS.values() for key in keys}
    d["unclear"] = {}
    d["workers"] = named_workers(text, workers)
    text = text.lower()

    def unclear(group, reason):
        d["unclear"].setdefault(group, []).append(reason)

    d["shift_ids"] = {sid.upper() for sid in SHIFT_ID.findall(text)}
    text = SHIFT_ID.sub(" ", text)
    d["halls"] = {hall for hall in tools.HALLS if re.search(rf"\b{hall.lower()}\b", text)}

    def add_date(year, month, day):
        try:
            d["dates"].add(week_start.replace(year=int(year), month=int(month), day=int(day)))
        except ValueError:
            unclear("dates", f"'{month}/{day}' is not a real date")

    date_forms = [(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lambda m: add_date(m[1], m[2], m[3])),
                  # An explicit year is honored; only a missing year means the demo year.
                  (rf"\b{MONTH_WORD}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?",
                   lambda m: add_date(m[3] or week_start.year, MONTHS.index(m[1][:3]) + 1, m[2])),
                  (r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", lambda m: add_date(m[3] or week_start.year, m[1], m[2]))]
    for pattern, add in date_forms:
        for match in re.finditer(pattern, text):
            add(match)
        text = re.sub(pattern, " ", text)  # so a date's digits are not read again as a time
    for word in re.findall(r"[a-z]+", text):
        if word in WEEKDAYS:
            d["dates"].add(week_start + timedelta(days=WEEKDAYS[word]))
        elif word in ("today", "tonight", "tomorrow", "yesterday"):
            unclear("dates", f"'{word}' needs an explicit date")
    if len(d["dates"]) > 1:
        unclear("dates", "more than one date is mentioned")

    no_early = re.sub(r"\bearly[\s-]+morning\b", " ", text)
    if no_early != text:
        d["periods"].add("Early Morning")
    for word, names in (("morning", {"Morning"}), ("afternoon", {"Afternoon"}), ("evening", {"Evening"}),
                        ("overnight", {"Overnight"}), ("night", {"Evening", "Overnight"})):
        if re.search(rf"\b{word}\b", no_early):
            d["periods"] |= names

    for match in re.finditer(RANGE, text):
        first, second = match[1], match[2]
        ap1, ap2 = re.search(r"([ap])\.?m", first), re.search(r"([ap])\.?m", second)
        named = [t.strip() in ("noon", "midnight") for t in (first, second)]
        if not (ap1 or ap2 or any(named) or (":" in first and ":" in second)):
            unclear("times", f"time range '{match[0].strip()}' needs AM/PM")
            continue
        start = _clock(first, None if ap1 else (ap2[1] if ap2 else None))
        end = _clock(second, None if ap2 else (ap1[1] if ap1 else None))
        inherited = bool(ap1) != bool(ap2)
        if start is None or end is None:
            unclear("times", f"'{match[0].strip()}' is not a valid time range")
            continue
        minutes = (int(end[:2]) * 60 + int(end[3:]) - int(start[:2]) * 60 - int(start[3:])) % 1440
        if minutes == 0 or (inherited and minutes > 720):
            unclear("times", f"time range '{match[0].strip()}' is ambiguous; give AM/PM for both ends")
            continue
        d["ranges"].add((start, end))
    text = re.sub(RANGE, " ", text)
    for pattern, key in ((START, "starts"), (END, "ends"), (EXPLICIT_TIME, "times")):
        for match in re.finditer(pattern, text):
            value = _clock(match[1])
            if value is None:
                unclear("times", f"'{match[1].strip()}' is not a valid time")
            else:
                d[key].add(value)
        text = re.sub(pattern, " ", text)
    return d


def merge_details(earlier, reply):
    """A clarification reply replaces only the kinds of detail it mentions, so 'Alex Chen' keeps the original
    hall, date, and time, while 'October 13' replaces only the date."""
    merged = {key: set(value) for key, value in earlier.items() if key != "unclear"}
    merged["unclear"] = dict(earlier["unclear"])
    for group, keys in GROUPS.items():
        if any(reply[key] for key in keys) or group in reply["unclear"]:
            for key in keys:
                merged[key] = set(reply[key])
            merged["unclear"].pop(group, None)
            if group in reply["unclear"]:
                merged["unclear"][group] = reply["unclear"][group]
    return merged


def shift_conflicts(details, shift):
    """Every way a stored shift contradicts the explicit details (empty list = compatible).
    A date matches the start date or the operating date; a range must match both ends in order."""
    problems = [reason for reasons in details["unclear"].values() for reason in reasons]
    start, end = shift["start"][11:], shift["end"][11:]
    label = f"{shift['shift_id']} ({shift['hall']} {shift['name']}, {shift['time_range']})"
    if details["shift_ids"] and shift["shift_id"] not in details["shift_ids"]:
        problems.append(f"the request names {', '.join(sorted(details['shift_ids']))}, not {shift['shift_id']}")
    if details["halls"] and shift["hall"] not in details["halls"]:
        problems.append(f"the request names {', '.join(sorted(details['halls']))}, not {shift['hall']}")
    if details["dates"] and not {shift["start"][:10], shift["operating_date"]} & {d.isoformat() for d in details["dates"]}:
        problems.append(f"the request says {', '.join(sorted(d.isoformat() for d in details['dates']))}, but {label} is another day")
    if details["periods"] and shift["name"] not in details["periods"]:
        problems.append(f"the request says {' or '.join(sorted(details['periods']))}, but {label} is the {shift['name']} shift")
    for first, second in sorted(details["ranges"]):
        if (first, second) != (start, end):
            problems.append(f"the request says {first}-{second}, but {label} runs {start}-{end}")
    problems += [f"the request says it starts at {t}, but {label} starts at {start}" for t in sorted(details["starts"] - {start})]
    problems += [f"the request says it ends at {t}, but {label} ends at {end}" for t in sorted(details["ends"] - {end})]
    if details["times"] and not details["times"] & {start, end}:
        problems.append(f"the request mentions {', '.join(sorted(details['times']))}, but {label} runs {start}-{end}")
    return problems


def fitting_assignments(conn, details):
    """Every (shift, worker) assignment in the demo week that fits everything the request states."""
    first_day = (eligibility.evaluated_week(conn) - timedelta(days=1)).isoformat()
    rows = conn.execute("SELECT a.shift_id, a.worker_id FROM assignments a JOIN shifts s ON s.id = a.shift_id"
                        " WHERE s.start_at >= ? ORDER BY s.start_at", (first_day,))
    return [(row["shift_id"], row["worker_id"]) for row in rows if row["worker_id"] in details["workers"]
            and not shift_conflicts(details, eligibility.get_shift(conn, row["shift_id"]))]


def lock_target(conn, task, resolved):
    """Lock the task to the resolved worker+shift only if it is the one assignment the request can mean.
    A locked target never changes. Returns a short note that the model and the trace both see."""
    pair = {"shift_id": resolved["shift_id"], "worker_id": resolved["worker_id"]}
    if task["target"] is not None:
        if task["target"] == pair:
            return "same locked target"
        return f"not changed: this task is locked to {task['target']['shift_id']} / {task['target']['worker_id']}"
    details = task["details"]
    if not details["workers"]:
        return "not locked: the request does not name the outgoing worker. Ask the supervisor."
    if pair["worker_id"] not in details["workers"]:
        named = ", ".join(sorted(task["names"][w] for w in details["workers"]))
        return f"not locked: the request names {named}, not {resolved['name']}. Ask the supervisor instead of guessing."
    problems = shift_conflicts(details, eligibility.get_shift(conn, pair["shift_id"]))
    if problems:
        return "not locked: " + "; ".join(problems) + ". Ask the supervisor instead of guessing."
    fits = fitting_assignments(conn, details)
    if len(fits) > 1:
        options = "; ".join(f"{task['names'][w]} on {s}" for s, w in fits[:4])
        return f"not locked: the request fits more than one assignment ({options}). Ask which one."
    task["target"] = pair
    return f"target locked: {pair['shift_id']} / {pair['worker_id']} {resolved['name']}"


def record_retrieval(task, name, result):
    """Remember which facts were actually returned to the model: the eligible list per shift, and soft
    context per shift and worker. Failed calls (errors) record nothing."""
    if "error" in result:
        return
    if name == "get_eligible_candidates":
        task["shortlists"].add(result["shift"]["shift_id"])
    elif name in ("get_preferences", "get_coverage_history", "get_nearby_classes"):
        rows = result.get("preferences") or result.get("workers") or []
        task["context"].setdefault(result["shift_id"], set()).update(row["worker_id"] for row in rows)


# ---------- Python's check of the final answer (never skipped) ----------

def answer_problems(conn, text, task):
    """Parse the model's final answer and list everything wrong with it. Returns (answer, problems)."""
    try:
        answer = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, ["the final answer was not valid JSON"]
    if not isinstance(answer, dict) or set(answer) != set(ANSWER_FIELDS):
        return None, [f"the final answer must be a JSON object with exactly: {', '.join(ANSWER_FIELDS)}"]
    problems = [f"{key} must be text or null" for key in ANSWER_FIELDS
                if answer[key] is not None and not isinstance(answer[key], str)]
    if problems:
        return None, problems
    status = answer["status"]
    if status not in STATUSES:
        return None, [f"status must be one of {', '.join(STATUSES)}"]
    if status == "clarification":
        return answer, [] if (answer["question"] or "").strip() else ["a clarification needs a question"]
    if status == "incomplete":
        return answer, [] if (answer["explanation"] or "").strip() else ["say what information is missing"]

    # recommendation or no_eligible_candidate: must be about the locked shift, checked with fresh eligibility.
    target = task["target"]
    if target is None:
        return answer, ["no shift is locked yet; use find_shift with the details in the request, or ask"]
    if answer["shift_id"] != target["shift_id"]:
        return answer, [f"the answer is about {answer['shift_id']}, but this task is locked to {target['shift_id']}"]
    if target["shift_id"] not in task["shortlists"]:
        return answer, [f"call get_eligible_candidates for {target['shift_id']} before answering"]
    check = eligibility.check_candidates(conn, target["shift_id"], target["worker_id"])
    eligible = {c["worker_id"] for c in check["eligible"]}
    reasons = {x["worker_id"]: "; ".join(x["reasons"]) for x in check["excluded"]}
    if status == "no_eligible_candidate":
        return answer, [f"eligible workers exist: {', '.join(sorted(eligible))}"] if eligible else []

    problems = []
    for role, worker in (("ask_first", answer["ask_first"]), ("backup", answer["backup"])):
        if worker is None:
            if role == "ask_first":
                problems.append("ask_first is required")
        elif worker == target["worker_id"]:
            problems.append(f"{role} {worker} is the worker who called out")
        elif worker not in eligible:
            problems.append(f"{role} {worker} is not eligible: {reasons.get(worker, 'unknown worker')}")
    if answer["backup"] is not None and answer["backup"] == answer["ask_first"]:
        problems.append("backup must be a different worker")
    # The model must have seen soft context for each worker it names. This shows the facts were available to it;
    # it does not prove every sentence of the explanation.
    seen = task["context"].get(target["shift_id"], set())
    missing = [w for w in (answer["ask_first"], answer["backup"]) if w in eligible and w not in seen]
    if missing:
        problems.append(f"fetch soft context (preferences, coverage history, or nearby classes) for "
                        f"{', '.join(missing)} on {target['shift_id']} before recommending")
    if not (answer["explanation"] or "").strip():
        problems.append("explain the recommendation and its tradeoff")
    return answer, problems


# ---------- the agent loop ----------

def agent_steps(question, conn, provider, limits=None, rates=None, ask=None, clock=time.monotonic):
    """Run one replacement task, yielding events as they happen (actions, results, usage; no hidden reasoning).

    ask(question) -> reply lets the supervisor answer a clarification; None means no one can reply."""
    limits = {**LIMITS, **(limits or {})}
    usage = {"model_calls": 0, "tool_calls": 0, "clarifications": 0, "input_tokens": 0, "output_tokens": 0,
             "unmeasured_tokens": 0, "cost_usd": 0.0, "cost_unknown_calls": 0}
    started, waited = clock(), 0.0  # waited = time spent waiting for the supervisor (not counted)

    def time_left():
        return limits["run_seconds"] - (clock() - started - waited)

    def stop(status, message):
        return {"type": "stop", "status": status, "message": message,
                "usage": {**usage, "seconds": round(clock() - started - waited, 1)}}

    question = (question or "").strip()
    if not question or len(question) > limits["question_chars"]:
        yield stop("invalid_input", f"Ask one question of 1-{limits['question_chars']} characters.")
        return

    week = eligibility.evaluated_week(conn)
    workers = [dict(row) for row in conn.execute("SELECT id, name FROM workers")]
    task = {"texts": [question], "details": question_details(question, week, workers), "target": None,
            "names": {w["id"]: w["name"] for w in workers}, "shortlists": set(), "context": {}}
    messages = [{"role": "user", "content": question}]
    corrections = 0
    yield {"type": "start", "question": question, "limits": limits, "rates": rates}

    while True:
        # ----- limits checked before spending anything -----
        if time_left() < 1:
            yield stop("deadline", f"Stopped: the {limits['run_seconds']}-second execution deadline was reached.")
            return
        if usage["model_calls"] >= limits["model_calls"]:
            yield stop("call_limit", f"Stopped: all {limits['model_calls']} model calls used without a checked answer.")
            return
        # On the last allowed call, or with no tool budget left, the model must answer with what it has
        # (which may honestly be "incomplete").
        tools_allowed = usage["model_calls"] < limits["model_calls"] - 1 and usage["tool_calls"] < limits["tool_calls"]
        request = {"instructions": SYSTEM_PROMPT, "input": messages, "tools": tools.TOOL_SCHEMAS,
                   "text": ANSWER_FORMAT, "tool_choice": "auto" if tools_allowed else "none"}

        # Count the whole request, add a margin, and reserve the maximum output BEFORE paying.
        try:
            counted = provider.count_input_tokens(request, timeout=min(limits["request_seconds"], time_left()))
        except ProviderError as error:
            yield stop("provider_unavailable", f"Provider unavailable while counting tokens: {error}")
            return
        reserved_input = math.ceil(counted * INPUT_MARGIN)
        reserved = reserved_input + limits["output_tokens"]
        spent = usage["input_tokens"] + usage["output_tokens"]
        if spent + reserved > limits["total_tokens"]:
            yield stop("token_limit", f"Stopped before Step-{usage['model_calls'] + 1}: it could need {reserved:,} "
                       f"tokens, but only {limits['total_tokens'] - spent:,} of {limits['total_tokens']:,} remain.")
            return
        if time_left() < 1:
            yield stop("deadline", "Stopped: the execution deadline was reached while counting tokens.")
            return

        # ----- one paid model call -----
        usage["model_calls"] += 1
        call_started = clock()
        try:
            reply = provider.create(request, max_output_tokens=limits["output_tokens"],
                                    timeout=min(limits["request_seconds"], time_left()))
        except ProviderError as error:
            # Usage is unknown after a failed request: charge the whole reservation and label it unmeasured.
            usage["input_tokens"] += reserved_input
            usage["output_tokens"] += limits["output_tokens"]
            usage["unmeasured_tokens"] += reserved
            usage["cost_unknown_calls"] += 1
            yield stop("provider_unavailable", f"Provider request failed: {error}. Usage unknown; "
                       f"{reserved:,} tokens charged conservatively.")
            return
        # A count the provider did not report is charged at its reservation, never treated as free.
        for key, reported, fallback in (("input_tokens", reply.input_tokens, reserved_input),
                                        ("output_tokens", reply.output_tokens, limits["output_tokens"])):
            usage[key] += reported if reported is not None else fallback
            usage["unmeasured_tokens"] += 0 if reported is not None else fallback
        cost = pricing.estimate_cost(rates, reply.input_tokens, reply.output_tokens,
                                     reply.cached_tokens, reply.cache_write_tokens)
        if cost is None:
            usage["cost_unknown_calls"] += 1
        else:
            usage["cost_usd"] += cost
        yield {"type": "model_call", "step": usage["model_calls"], "reply": reply, "reserved": reserved,
               "cost": cost, "usage": dict(usage), "seconds": round(clock() - call_started, 1),
               "tools_allowed": tools_allowed}

        if time_left() < 0:
            yield stop("deadline", "Stopped: the model replied after the execution deadline; the reply was not used.")
            return
        if reply.status != "completed":
            if reply.incomplete_reason == "max_output_tokens":
                yield stop("truncated", f"Stopped: the reply was cut off at {limits['output_tokens']:,} output "
                           "tokens, so no partial answer is used.")
            else:
                yield stop("incomplete", f"Stopped: the reply was incomplete ({reply.incomplete_reason or 'no reason given'}).")
            return

        # ----- tool calls: run each registered tool and feed its result back -----
        if reply.tool_calls:
            if not tools_allowed:
                yield stop("protocol_error", "Stopped: the model asked for tools after tools were disabled.")
                return
            for call in reply.tool_calls:
                if usage["tool_calls"] >= limits["tool_calls"]:
                    result = {"error": f"tool limit ({limits['tool_calls']}) reached; not run. Give your final answer."}
                else:
                    usage["tool_calls"] += 1
                    result = tools.run_tool(conn, call.name, call.arguments)
                    record_retrieval(task, call.name, result)
                    if call.name == "find_shift" and result.get("status") == "resolved":
                        result["target_check"] = lock_target(conn, task, result["resolved"])
                yield {"type": "tool", "name": call.name, "arguments": call.arguments, "result": result}
                messages.append({"type": "function_call", "call_id": call.call_id, "name": call.name,
                                 "arguments": call.arguments})
                messages.append({"type": "function_call_output", "call_id": call.call_id,
                                 "output": json.dumps(result, separators=(",", ":"))})
            continue

        # ----- final answer: Python checks it -----
        answer, problems = answer_problems(conn, reply.text, task)
        if problems:
            if corrections >= limits["corrections"]:
                yield stop("invalid_answer", "Stopped: Python rejected the answer again: " + "; ".join(problems))
                return
            corrections += 1
            yield {"type": "rejected", "problems": problems}
            messages.append({"role": "assistant", "content": reply.text})
            messages.append({"role": "user", "content": "Python rejected that answer: " + "; ".join(problems)
                             + ". Give a corrected final answer using only facts from the tools."})
            continue

        # ----- a clarification the supervisor can answer (at most twice per task) -----
        if answer["status"] == "clarification" and ask is not None:
            if usage["clarifications"] >= limits["clarifications"]:
                yield stop("clarification_limit", f"Stopped: still unclear after {limits['clarifications']} replies. "
                           "Run again with the worker, hall, date, and shift.")
                return
            yield {"type": "clarify", "question": answer["question"]}
            paused = clock()
            try:
                supervisor = ask(answer["question"])
            except (EOFError, KeyboardInterrupt):
                supervisor = None
            waited += clock() - paused
            supervisor = (supervisor or "").strip()
            if not supervisor:
                yield stop("clarification_unanswered", "Stopped: no reply to the clarification question.")
                return
            supervisor = supervisor[:limits["question_chars"]]
            usage["clarifications"] += 1
            yield {"type": "reply", "text": supervisor}
            task["texts"].append(supervisor)
            task["details"] = merge_details(task["details"], question_details(supervisor, week, workers))
            if task["target"]:
                locked = eligibility.get_shift(conn, task["target"]["shift_id"])
                changed = shift_conflicts(task["details"], locked)
                if task["target"]["worker_id"] not in task["details"]["workers"]:
                    changed.append(f"the reply names {', '.join(sorted(task['names'][w] for w in task['details']['workers']))}")
                if changed:
                    yield stop("target_changed", f"Stopped: this task is locked to {locked['shift_id']}, and the reply "
                               f"points elsewhere ({'; '.join(changed)}). Run again for a different shift.")
                    return
            messages.append({"role": "assistant", "content": reply.text})
            messages.append({"role": "user", "content": f"Supervisor's reply to your question: {supervisor}"})
            continue

        yield {"type": "final", "answer": answer, "target": task["target"],
               "usage": {**usage, "seconds": round(clock() - started - waited, 1)}}
        return


# ---------- terminal display ----------

WIDTH = 76


def terminal_width():
    return max(20, min(WIDTH, shutil.get_terminal_size((WIDTH, 24)).columns))


def plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word[:-1] + 'ies' if word.endswith('y') else word + 's'}"


def wrap(lines, width):
    """Wrap (indent, text) pairs; continuation lines are indented two more spaces. ("", "") is a blank line."""
    out = []
    for indent, text in lines:
        out += textwrap.wrap(text, width, initial_indent=indent, subsequent_indent=indent + "  ",
                             break_on_hyphens=False) or [indent]
    return "\n".join(out)


def hours_text(c):
    return (f"{c['assigned_hours_now']:g}h now -> {c['hours_after_covering']:g}h after | "
            f"{c['hours_left_after_covering']:g}h left")


def shift_label(shift):
    return f"{shift['hall']} {shift['name']} | {shift['_start']:%a} {shift['time_range']}"


def money(amount, unknown_calls):
    if unknown_calls == 0:
        return pricing.format_usd(amount)
    return f"{pricing.format_usd(amount)} + unknown ({unknown_calls} call)" if amount else "unknown"


def arguments_text(raw):
    try:
        args = json.loads(raw)
        return " | ".join(f"{key} {', '.join(map(str, value)) if isinstance(value, list) else value}"
                          for key, value in args.items() if value is not None)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return str(raw)[:120]


EXCLUSION_LABELS = [("outgoing worker", "called out"), ("inactive", "inactive"),
                    ("class timetable not confirmed", "timetable not confirmed"), ("class overlap", "class overlap"),
                    ("approved leave", "approved leave"), ("overlaps own assignment", "own shift overlaps"),
                    ("already assigned", "already assigned"), ("weekly limit", "over 20h cap")]


def result_lines(conn, name, result):
    """A few readable lines built from a tool's result dict."""
    if "error" in result:
        return [f"error: {result['error']}"]
    if name == "find_shift":
        shifts = {s["shift_id"]: s for s in result["shifts"]}
        status = result["status"]
        if status == "resolved":
            s = shifts[result["resolved"]["shift_id"]]
            lines = [f"resolved | {s['hall']} {s['name']} | {s['time_range']} | {result['resolved']['name']}"]
        elif status == "ambiguous":
            lines = ["ambiguous"] + [f"{p['name']} ({p['worker_id']}) | {shifts[p['shift_id']]['hall']} "
                                     f"{shifts[p['shift_id']]['name']} | {shifts[p['shift_id']]['time_range']}"
                                     for p in result["possible"]]
        else:
            lines = [f"{status} | {plural(len(shifts), 'matching shift')}"]
            lines += [f"{s['hall']} {s['name']} | {s['time_range']} | "
                      f"{s['assigned_worker']['name'] if s['assigned_worker'] else 'unassigned'}"
                      for s in list(shifts.values())[:4]]
            lines += [f"{a['worker_id']} holds instead: {a['hall']} {a['name']} | {a['time_range']}"
                      for a in result.get("worker_assignments", [])]
        if "target_check" in result:
            lines.append(result["target_check"])
        return lines
    if name == "get_eligible_candidates":
        s = result["shift"]
        lines = [f"=> {len(result['eligible'])} Eligible | {len(result['excluded'])} Excluded | "
                 f"{s['hall']} {s['name']} {s['time_range']} | {eligibility.format_minutes(s['duration_minutes'])}"]
        if result["eligible"]:
            lines.append("Hours: assigned now -> assigned after covering | remaining under 20h")
        lines += [f"{c['name']} ({c['worker_id']}) | {hours_text(c)}" for c in result["eligible"]]
        groups = {}
        for x in result["excluded"]:
            for reason in x["reasons"]:
                label = next((label for start, label in EXCLUSION_LABELS if reason.startswith(start)), reason)
                if x["worker_id"] not in groups.setdefault(label, []):  # an overnight shift can repeat a reason
                    groups[label].append(x["worker_id"])
        if groups:
            lines.append("")
        lines += [f"Excluded: {label.title().replace('20H', '20h')}: {', '.join(ids)}" for label, ids in groups.items()]
        return lines
    if name == "get_preferences":
        return [" | ".join(f"{p['name'].split()[0]}: {p['level']}" for p in result["preferences"])]
    if name == "get_coverage_history":
        lines = []
        for w in result["workers"]:
            if lines:
                lines.append("")
            c = w["counts"]
            lines.append(f"{w['name']} | asked {c['asked']} | completed {c['completed']} | accepted {c['accepted']} | "
                         f"declined {c['declined']} | pending {c['pending']}")
            lines += [f"  asked {eligibility.parse_local(r['asked_at']):%b %d}: {r['outcome']} | {r['emergency_shift']}"
                      for r in w["requests"]]
        return lines
    if name == "get_nearby_classes":
        return [f"{w['name']} | " + (", ".join(f"{c['course']} {c['relation']} ({c['gap_minutes']} min)"
                                               for c in w["nearby_classes"]) or "none nearby")
                for w in result["workers"]]
    return [json.dumps(result)[:150]]


def totals_lines(usage, width):
    text = (f"Totals: {plural(usage['model_calls'], 'model call')} | {plural(usage['tool_calls'], 'tool call')} | "
            f"{plural(usage['clarifications'], 'clarification reply')} | {usage['input_tokens']:,} in + "
            f"{usage['output_tokens']:,} out tokens | {usage['seconds']}s")
    lines = [("", text)]
    if usage["unmeasured_tokens"]:
        lines.append(("", f"Includes {usage['unmeasured_tokens']:,} unmeasured tokens charged conservatively."))
    lines.append(("", f"Estimated API cost: {money(usage['cost_usd'], usage['cost_unknown_calls'])} "
                      "(estimate, not an invoice)"))
    return lines


def render(event, conn, width=WIDTH):
    kind = event["type"]
    if kind == "start":
        l = event["limits"]
        return wrap([("", ""), ("", "LIMITS:"), ("  ", f"{l['model_calls']} model calls | {l['tool_calls']} tool calls | "
                          f"{l['output_tokens']:,} output tokens/call | {l['total_tokens']:,} total tokens | "
                          f"{l['request_seconds']}s/request | {l['run_seconds']}s/run | "
                          f"{l['clarifications']} clarification replies"),
                     ("", ""), ("", "ESTIMATED PRICING:"),
                     ("  ", pricing.describe(event["rates"]) + ". Estimates only.")], width)
    if kind == "model_call":
        r, u = event["reply"], event["usage"]
        tokens = (f"in {r.input_tokens:,}" if r.input_tokens is not None else "in unknown") + " | " + \
                 (f"out {r.output_tokens:,}" if r.output_tokens is not None else "out unknown")
        cache = (f"read {r.cached_tokens:,} | write {r.cache_write_tokens:,}"
                 if r.cached_tokens is not None and r.cache_write_tokens is not None else "unreported")
        extra = f" | reasoning {r.reasoning_tokens:,} (in out)" if r.reasoning_tokens else ""
        forced = " | tools off: must answer" if not event["tools_allowed"] else ""
        return wrap([("", ""), ("", f"Step-{event['step']} | model call | {event['seconds']}s{forced}"),
                     ("  ", f"Tokens: {tokens}"),
                     ("  ", f"Cache: {cache}{extra}"),
                     ("  ", f"Cost: {pricing.format_usd(event['cost'])} this step | "
                            f"{money(u['cost_usd'], u['cost_unknown_calls'])} so far"),
                     ("  ", f"Tokens used: {u['input_tokens'] + u['output_tokens']:,}")], width)
    if kind == "tool":
        lines = [("", ""), ("", f"==> ACTION: {event['name']}"),
                 ("  ", arguments_text(event["arguments"])), ("", ""), ("", "RESULT:")]
        lines += [("  ", line) for line in result_lines(conn, event["name"], event["result"])]
        return wrap(lines, width)
    if kind == "rejected":
        return wrap([("  ", "Python rejected the answer: " + "; ".join(event["problems"]))], width)
    if kind == "clarify":
        return wrap([("", ""), ("", f"Question for you: {event['question']}")], width)
    if kind == "reply":
        return wrap([("", f"Your reply: {event['text']}")], width)
    if kind == "stop":
        return wrap([("", ""), ("", f"STOPPED ({event['status']}): {event['message']}")]
                    + totals_lines(event["usage"], width), width)

    # final answer
    answer, target = event["answer"], event["target"]
    status = answer["status"]
    lines = [("", ""), ("", {"recommendation": "RECOMMENDATION (checked by Python)",
                          "no_eligible_candidate": "NO ELIGIBLE REPLACEMENT (confirmed by Python)",
                          "clarification": "CLARIFICATION NEEDED",
                          "incomplete": "INCOMPLETE: NOT ENOUGH INFORMATION"}[status])]
    if status in ("recommendation", "no_eligible_candidate"):
        shift = eligibility.get_shift(conn, target["shift_id"])
        lines.append(("  ", f"Shift: {shift_label(shift)} | {shift['shift_id']}"))
        lines.append(("  ", f"Called out: {shift['assigned_worker']['name']} ({shift['assigned_worker']['worker_id']})"))
    if status == "recommendation":
        check = eligibility.check_candidates(conn, target["shift_id"], target["worker_id"])
        facts = {c["worker_id"]: c for c in check["eligible"]}  # weekly hours recomputed by Python
        for role, worker in (("Ask first", answer["ask_first"]), ("Backup", answer["backup"])):
            if worker:
                c = facts[worker]
                lines.append(("  ", f"{role}: {c['name']} ({worker}) | {hours_text(c)} of 20h"))
    if answer["question"]:
        lines.append(("  ", f"Question: {answer['question']}"))
        lines.append(("  ", "Run again with that detail in the question."))
    if answer["explanation"]:
        lines.append(("", ""))
        lines.append(("  ", f"Why: {answer['explanation']}"))
    lines.append(("  ", "Read-only: nothing was assigned and nobody was contacted."))
    return wrap(lines + [("", "")] + totals_lines(event["usage"], width), width)


def ask_supervisor(question):
    return input("Your reply> ")


def main(argv=None, ask=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    # Model text may contain characters such as "≤" or "→". Redirected Windows output defaults to cp1252 and
    # would crash, so write UTF-8 and replace anything that cannot be encoded.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    width = terminal_width()
    question = " ".join(argv)
    try:
        conn = database.connect(database.DB_PATH, read_only=True)
    except FileNotFoundError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    try:
        provider, model = provider_from_env()
    except ProviderError as error:
        print(wrap([("", f"STOPPED (provider_unavailable): {error}")], width))
        return 1
    # Ask for a reply only when a person can both see the question and type: if output is redirected, the
    # question would be invisible while the program waited (and waiting does not count toward the deadline).
    if ask is None and sys.stdin.isatty() and sys.stdout.isatty():
        ask = ask_supervisor
    print(wrap([("", f"ShiftOps advisor | model {model} | read-only database"),
                ("", ""), ("", "QUESTION:"), ("  ", question)], width))
    status = None
    for event in agent_steps(question, conn, provider, rates=pricing.rates_for(model), ask=ask):
        print(render(event, conn, width), flush=True)
        if event["type"] in ("final", "stop"):
            status = event["answer"]["status"] if event["type"] == "final" else event["status"]
    conn.close()
    return 0 if status in STATUSES else 2


if __name__ == "__main__":
    sys.exit(main())
