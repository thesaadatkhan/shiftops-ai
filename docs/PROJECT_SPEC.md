# ShiftOps AI specification

## Problem and scope

When a student worker calls out, a housing supervisor needs to decide whom to ask first. Several workers may be able to cover, but preferences, recent emergency coverage, nearby classes, and weekly hours can make one a more considerate choice.

ShiftOps is one small, read-only terminal agent. It identifies the outgoing worker and the exact shift, checks hard eligibility through tools, gathers relevant soft context, and recommends an eligible worker with an explanation and an optional eligible backup.

Python establishes the facts and refuses invalid answers. The model makes the judgment among eligible workers. No assignment changes and no messages are sent; the supervisor reviews the recommendation.

## Status

Implemented, and tested offline with scripted model replies and temporary databases.

Live acceptance with `gpt-5.6-luna` (six capped runs) covered:
- two recommendations with competing tradeoffs;
- a no-candidate case;
- an ambiguous worker resolved by a clarification reply;
- an outgoing-worker mismatch;
- an unfamiliar overnight case.

The prompt guidance on weekly-hour fields and preference wording was added after those runs and is verified offline only. Interactive keyboard clarification needs a manual check; live runs supplied the reply through `main(ask=...)`.

## Interface and loop

`python agent.py "question"` runs one replacement task; `--help` prints usage. There is no separate `main.py`.

`agent_steps()` is a generator that yields events as they happen; `main()` prints them. The loop:
1. Counts the full request with the provider's token counter and reserves the maximum output within the remaining allowance.
2. Sends the instructions, conversation, and five tool schemas to the model (structured tool calls through the OpenAI Responses API; `store=False`; no automatic retries).
3. Runs only registered tools, validates their arguments, and appends each tool call with its result.
4. Repeats until Python accepts a final answer or a limit stops the run.

Model text is never evaluated or executed. The model chooses which context tools to call and in what order.

### Final answer

The final answer is a strict JSON object with six fields: `status`, `shift_id`, `ask_first`, `backup`, `explanation`, `question`. The statuses are:
- `recommendation`: the locked shift, a first choice, an optional backup, and an explanation that names the tradeoff;
- `clarification`: one short question, used when the worker or shift is ambiguous or the named worker doesn't hold the shift;
- `no_eligible_candidate`: nobody can cover the whole shift;
- `incomplete`: the facts available are not enough to recommend responsibly.

### Clarification within one task

- A clarification can be answered by the supervisor up to two times within the same task.
- Replies share the task's budgets; nothing is reset. Time spent waiting for a reply is excluded from the execution deadline.
- A reply replaces only the kinds of detail it mentions (worker, hall, date, period, time, shift ID), so the original constraints are kept.
- A reply that contradicts an already locked shift or worker stops the task.
- Replies are requested only when both stdin and stdout are terminals. Otherwise the clarification is printed as the final answer.
- An empty reply, end of input, Ctrl+C, or a third question stops honestly.
- There is no general chat, schedule reporting, or saved conversation.

## Synthetic data

SQLite is generated once from the tracked `data/seed.json` by `python database.py --init-demo`, which refuses to overwrite an existing database without `--reset`. Agent runs reuse it through a read-only connection.

The data models 30 synthetic student workers (one inactive) and five halls, with:
- a fully covered baseline week;
- dated classes, approved leave, and preferences;
- recent emergency coverage history.

### Halls and usual shift patterns

Each shift requires one worker. Capella, Helix, Vega, and Sirius operate daily from 8 AM to 6 PM. Andromeda operates continuously; each operating day runs from 8 AM to 8 AM the next day.

| Halls | Usual shift name | Start | End | Duration |
| --- | --- | --- | --- | --- |
| Capella, Helix, Vega, Sirius | Morning | 8 AM | 1 PM | 5 hours |
| Capella, Helix, Vega, Sirius | Afternoon | 1 PM | 6 PM | 5 hours |
| Andromeda | Morning | 8 AM | 1 PM | 5 hours |
| Andromeda | Afternoon | 1 PM | 6 PM | 5 hours |
| Andromeda | Evening | 6 PM | 11 PM | 5 hours |
| Andromeda | Overnight | 11 PM | 4 AM next day | 5 hours |
| Andromeda | Early Morning | 4 AM | 8 AM | 4 hours |

These are usual patterns, not duration rules. All calculations use the exact stored start and end times. Displays show the actual dated range generated from those timestamps (for example "Oct 12, 11 PM–Oct 13, 4 AM"); shift IDs such as `AND-1012-EVE` remain the internal identity and use the operating date.

The demo week, Monday 2026-10-12 to Sunday 2026-10-18, needs 448 worker-hours. `database.py` validates that every operating interval is covered exactly once, with no class, leave, or assignment conflicts and nobody over 20 hours.

All timestamps share one fictional America/Chicago local clock, away from daylight-saving changes. Timezone suffixes are rejected.

Tables:
- **workers**, **timetable_coverage** (confirmed complete dates), **shifts**, **assignments**, **classes**, **approved_leave**.
- **preferences**: worker, shift, and level preferred/neutral/low.
- **coverage_requests**: worker, emergency shift, asked_at, outcome (pending/declined/accepted/completed), and resolved_at, the time the outcome became known.

Database validation enforces:
- foreign keys, unique assignments, interval order, and allowed values;
- accepted/completed requests that match real assignments;
- completed outcomes known no earlier than the shift end.

## Hard eligibility

A candidate must:
- be active and not the outgoing worker;
- have a confirmed class timetable for every date the shift touches;
- have no class, approved leave, or other assignment overlapping the shift;
- stay at or below the absolute 20-hour weekly cap after adding the whole shift. A stored limit above 20 cannot raise it.

Intervals are half-open (touching endpoints don't overlap), arithmetic uses integer minutes, and a whole assignment counts toward the week in which it starts. One replacement must cover the entire shift: no splitting or shortening. Eligible workers are listed in ID order, never ranked, with every exclusion reason returned.

## Tools

| Tool | Inputs | Output |
| --- | --- | --- |
| find_shift | shift ID, or date with optional hall/shift name/start time; outgoing worker name or ID | Matching dated shifts and status: resolved, ambiguous, not_assigned (with the worker's actual nearby shifts), no_worker_match, no_shift_match, or worker_needed |
| get_eligible_candidates | shift ID (the outgoing worker is whoever holds it) | Eligible workers with assigned_hours_now, hours_after_covering, hours_left_after_covering; excluded workers with reasons |
| get_preferences | shift ID, 1–16 eligible worker IDs | Level per worker; missing means neutral |
| get_coverage_history | shift ID, eligible worker IDs, lookback 1–30 days | Requests asked before the shift; outcomes only if known by then, otherwise pending; counts per outcome |
| get_nearby_classes | shift ID, eligible worker IDs, window 1–240 minutes | Classes ending before or starting after the shift, with gaps |

Tools return small dictionaries. The model receives them as compact JSON, and the terminal shows a summary of the same data. Unknown tools, malformed arguments (including wrongly typed worker lists), and controlled data errors become error observations. No tool ranks workers, scores suitability, infers wellbeing, or writes the explanation.

## Python checks

**Correct shift.** Python reads explicit details from the question and any replies, in a limited set of supported forms:
- worker ID, full name, or first name;
- hall and shift ID;
- dates: ISO, month-name with an optional year, M/D with an optional year, and weekday names in the demo week;
- periods;
- times, time ranges, and "starting at"/"until".

The task locks to a shift only when all of these hold:
- the resolved worker is one of the named workers;
- the shift contradicts none of these details;
- exactly one assignment in the demo week fits.

Unclear input means asking instead of guessing. Examples: relative days, two dates, or a range without AM/PM. A locked target never changes.

**Final answer.** A recommendation is accepted only when all of these hold:
- it concerns the locked shift;
- the eligible list for that shift was returned to the model;
- soft context was returned for the first choice and any backup;
- both pass a fresh eligibility check;
- neither is the outgoing worker, and they are different people.

A no-candidate answer is accepted only if the eligible list was retrieved and is truly empty. Malformed or missing fields are rejected without crashing. A rejected answer gets one correction message within the normal budgets, then the run stops honestly.

These checks show that the facts were available and the choice is allowed. They do not verify every sentence of the explanation. The prompt defines the weekly-hour fields and preference levels and asks the model to use only observed facts. The final display recomputes shift times and weekly hours in Python.

## Limits and observability

All limits are defined once in `LIMITS` in `agent.py`:
- 10 model calls per task (including final answers and corrections);
- 12 tool executions;
- 1,000 output tokens per call;
- 50,000 cumulative input and output tokens;
- 30-second request timeout;
- 120-second execution deadline, excluding waiting for a reply;
- 2 clarification replies and 1 correction attempt;
- no automatic retries.

Question and reply length is bounded, and so are tool arguments and tool result size.

Before every paid call, the full request is counted by the provider and multiplied by 1.10, and the maximum output is reserved. The run stops if that won't fit. On the last call, or with no tool budget left, tools are disabled and the model may answer `incomplete`. Truncated output, provider failures, late replies, and exhausted limits end with explicit statuses.

The trace prints:
- `Step-N` for each model call, with input/output tokens, cache reads and writes, and estimated cost;
- each tool action with readable arguments and a concise result summary, including grouped exclusion reasons and dated history;
- the checked final answer and totals.

Output wraps at 76 columns or the terminal width if narrower. Redirected output is written as UTF-8. Hidden reasoning is never printed. Missing usage is charged at its reservation, labeled unmeasured, and the cost is shown as unknown.

Estimated cost is computed in `pricing.py` from reported usage and verified rates:
- `gpt-5.6-luna`, Standard tier, verified 2026-10-10 at https://developers.openai.com/api/docs/pricing;
- USD per 1M tokens: input $0.20, cached input $0.02, cache write $0.25, output $1.20;
- long-context rates over 272K input tokens.

Cached reads and writes are priced separately, and reasoning tokens are not double-counted. Estimates are display only, not invoices.

## Demo cases

1. Multiple eligible workers with competing soft considerations (Andromeda Oct 12, 6 PM–11 PM).
2. A nearby class and a preference change the tradeoff (Helix Oct 13 morning).
3. No eligible replacement (Capella Fri Oct 16 afternoon).
4. An ambiguous worker resolved by a clarification reply (Alex, Andromeda Saturday).
5. An outgoing worker not assigned to the requested shift (Sam, Capella Oct 13 afternoon).
6. An overnight shift crossing midnight (Andromeda Oct 15, 11 PM–Oct 16, 4 AM).

Offline tests use scripted model replies and temporary databases. Live checks are few, bounded, and reviewed step by step. Several eligible recommendations can be defensible.

## Excluded scope

No frontend, web server, scheduling optimizer, database writes during a run, notifications, approvals, deployment, RAG, MCP, agent frameworks, multiple agents, general chat, schedule or workforce reporting, citation-ID frameworks, or splitting shifts between replacements.
