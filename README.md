# ShiftOps AI

A terminal agent that answers one question for a residence-hall supervisor:

> **A student worker just called out. Who should I ask first to cover the shift, and why?**

Python works out who **can** cover. The model decides who **should** be asked first among those workers by weighing preferences, recent emergency coverage, nearby classes, and weekly hours. It then explains the tradeoff. The supervisor reviews the recommendation before acting.

The agent is read-only: it never assigns a shift or contacts anyone.

## How it works

```
question ─▶ agent_steps() ──▶ model: asks for tools, or gives a final answer
                 ▲                    │
                 │  tool results      ▼
                 └──────────── tools.run_tool() ──▶ SQLite (read-only)
final answer ─▶ Python checks it ─▶ printed only if it passes
```

The loop in `agent.py` is written by hand. No agent framework is used; the OpenAI SDK only sends requests.

1. Send the conversation and the five tool schemas to the model.
2. If the model asks for tools, run them in Python and append each result to the conversation.
3. If the model gives a final answer, Python checks it. A rejected answer gets one correction attempt.
4. If the model needs a detail (for example, which Alex), the supervisor can reply. The task continues with the same budget.
5. Stop at a checked answer, or when a limit is reached.

**Python fetches and checks:**
- **Hard rules**, applied in `eligibility.py` to the whole shift:
  - the worker is active and has a confirmed class timetable;
  - no overlap with a class, approved leave, or another assignment;
  - an absolute **20-hour weekly cap**.
- **Shift length** comes from the stored start and end times. Nobody can split or shorten a shift to fit.
- **The target shift:** Python locks the task to the one shift and outgoing worker the request describes, and never switches silently.
- **The final answer** must be about that shift, and the chosen workers must pass a fresh eligibility check.
- **Facts first:** the model must have received the eligible list, and some soft context for each worker it names, before it may recommend.
- **Display facts:** the shift time ranges and weekly hours shown at the end are computed by Python, not taken from the model's wording.

**The model decides:**
- which context to look up after the eligibility check;
- whom to ask first, and an optional backup;
- how to explain the tradeoff.

Several eligible workers can be defensible choices, and different runs may weigh the tradeoffs differently. Python does **not** verify every sentence of the explanation. It checks the shift, eligibility, and that the facts were retrieved. The supervisor makes the final call.

## Files

| File | Purpose |
| --- | --- |
| `agent.py` | Limits, system prompt, OpenAI calls, correct-shift check, final-answer check, the `agent_steps()` loop, terminal output, and `main()`. There is no separate `main.py` |
| `tools.py` | The five read-only tools, their schemas, and `run_tool()`, which allows only these tools and validates arguments |
| `eligibility.py` | Time arithmetic, time-range labels, weekly hours, and the hard eligibility rules |
| `database.py` | Builds the SQLite database from the seed and validates it |
| `pricing.py` | Verified model rates and the cost estimate |
| `data/seed.json` | The saved synthetic dataset (workers, shifts, classes, leave, preferences, coverage history) |
| `tests/` | Offline tests; the model is replaced by pre-written replies in `tests/scripted.py` |

## Tools

| Tool | Returns |
| --- | --- |
| `find_shift` | The dated shift and whether the named worker holds it: resolved, ambiguous, not assigned, or not found |
| `get_eligible_candidates` | Eligible workers with hours assigned now, hours after covering, and hours left under the cap; plus every exclusion reason |
| `get_preferences` | Preferred / neutral / low for this shift; a missing preference counts as neutral |
| `get_coverage_history` | Emergency requests asked before the shift, with each outcome only as it was known at that time |
| `get_nearby_classes` | Classes ending shortly before or starting shortly after the shift, with exact gaps |

The three context tools accept only workers who are eligible for that shift. No tool ranks workers or writes the explanation.

## Demo data

- 30 synthetic workers; one (W30) is inactive.
- Capella, Helix, Vega, and Sirius run 8 AM–6 PM. Andromeda runs 24 hours.
- One fixed demo week, **Monday Oct 12 to Sunday Oct 18, 2026**, on a fictional America/Chicago clock.
- The week has 91 shifts and 448 hours. `database.py` checks that every operating hour is covered exactly once, with no conflicts and nobody over 20 hours.
- Weekly hours are charged to the week in which a shift starts.
- Shift IDs such as `AND-1012-EVE` use the operating date. So `AND-1012-EM` runs Oct 13, 4 AM–8 AM, and screens always show the actual dated time range.

| Scenario | What the data sets up |
| --- | --- |
| Andromeda Oct 12, 6 PM–11 PM (Jordan out) | Four eligible workers. Riley prefers the shift but covered three recent emergencies. Jamie's class ends 15 minutes before it. Skyler marked it low but has the most hours left. Dakota would reach exactly 20h |
| Helix Oct 13, 8 AM–1 PM (Morgan out) | Elliot prefers it, but a class starts 10 minutes after it ends. Dakota marked it low. Skyler recently declined requests |
| Capella Oct 16, 1 PM–6 PM (Drew out) | Nobody is eligible: fall-break leave, Friday labs, or the hour cap rule everyone out |
| "Alex" on Andromeda, Sat Oct 17 | Alex Chen and Alex Rivera both work Andromeda that day, so the agent asks which one |
| Sam, Capella Oct 13, 1 PM–6 PM | Sam doesn't hold that shift, so the agent asks whether you meant the worker who does |

All people, schedules, and policies are fictional.

## Setup

You need **Python 3.10 or newer** (tested with 3.12). Install Python yourself; pip installs the three dependencies from `requirements.txt`.

```powershell
# Windows PowerShell. On macOS/Linux use: python3 -m venv .venv  and  source .venv/bin/activate
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt

copy .env.example .env           # then put your own OPENAI_API_KEY and OPENAI_MODEL in .env

python database.py --init-demo   # build shiftops.db once from data/seed.json
python database.py --check       # optional: validate the database again
python -m pytest -q              # offline tests: no API key or network needed
```

`data/seed.json` is the saved synthetic dataset. `database.py --init-demo` turns it into `shiftops.db` and validates it. You only do this once; every agent run reuses the same file through a read-only connection.

`--init-demo` refuses to overwrite an existing database. Use `--init-demo --reset` only when you deliberately want to rebuild it.

The project was configured and tested with `gpt-5.6-luna`. Another model must support tool calling and structured output. Cost estimates show `unknown` for a model without verified rates in `pricing.py`.

## Running it

Ask one replacement question in quotes:

```powershell
python agent.py "Jordan called out for Andromeda on October 12, 2026, from 6 PM to 11 PM. Who should I ask first, and why?"
```

The terminal prints each step as it happens:
- **`Step-N`** is one paid model call. It shows the input/output tokens, cache reads and writes, and estimated cost.
- **`action:`** is a tool call with its arguments; several can sit under one step.
- **Below each action**, a short summary of the tool result.
- **At the end**, the checked answer, Python-computed weekly hours, and the totals.

Hidden reasoning is never printed. Output stays within 76 columns, or narrower on narrow terminals.

Each command makes a few paid API calls, bounded by the limits below.

### Example questions

| Situation | Question |
| --- | --- |
| Clear request | `Morgan called out for Helix's morning shift on October 13, 2026. Who should we ask first?` |
| Ambiguous worker | `Alex called out of Andromeda on Saturday. Who should I ask first?` The agent asks "Alex Chen or Alex Rivera?"; reply `Alex Chen` |
| No eligible replacement | `Drew called out for Capella's afternoon shift on Friday, October 16, 2026. Who can cover?` |
| Wrong worker for the shift | `Sam called out for Capella's afternoon shift on October 13, 2026.` Sam doesn't hold that shift, so the agent asks whether you meant the worker who does |
| Overnight shift | `Reese can't make the Andromeda overnight shift from 11 PM to 4 AM on Thursday, October 15, 2026. Who should we ask first?` |

End of a live run of the overnight example:

```
RECOMMENDATION (checked by Python)
  Shift: Andromeda Overnight | Thu Oct 15, 11 PM–Oct 16, 4 AM | AND-1015-OVN
  Called out: Reese Wallace (W13)
  Ask first: Jesse Park (W27) | 0h now -> 5h after | 15h left of 20h
  Backup: Riley Nguyen (W05) | 10h now -> 15h after | 5h left of 20h
```

### Clarification

When a detail is missing or ambiguous, the agent asks one short question.
- **At a terminal**, you can reply up to **two** times within the same task. Your reply only replaces what it mentions: answering `Alex Chen` keeps the original hall and day.
- **Shared budget:** replies share the task's budget for model calls, tools, and tokens, and nothing is reset.
- **Typing time** doesn't count toward the execution deadline.
- **When a reply is offered:** only when both input and output are a terminal. If you redirect output to a file or run non-interactively, the agent prints its question and finishes.
- **Stopping:** an empty reply, Ctrl+C, or end of input stops the task honestly. So does a third question.

Each run handles one call-out. There is no general chat, schedule reporting, or saved conversation history.

## What the agent understands

Python reads these explicit forms in a question and refuses to lock onto a shift that contradicts them:

| Kind | Supported forms |
| --- | --- |
| Worker | ID (`W02`), full name (`Alex Chen`), or first name (`Alex`; several workers may match) |
| Hall / shift ID | `Andromeda`, `Capella`, `Helix`, `Vega`, `Sirius`; IDs such as `AND-1012-EVE` |
| Date | `2026-10-12`; `Oct 12`, `October 12th`, `October 12, 2027`; `10/12`, `10/12/2027`; weekday names (`Monday`... `Sunday`, `Mon`–`Fri`) meaning that day of the demo week |
| Period | morning, afternoon, evening, overnight, early morning, night (evening or overnight) |
| Time | `6 PM`, `6:30 p.m.`, `18:00`, noon, midnight; ranges such as `6-11 PM`, `6 PM to 11 PM`, `18:00-23:00`; `starting at 6 PM`, `until 11 PM` |

How these forms are used:
- A date matches a shift's start date or its operating day. Andromeda runs 24 hours, so "October 12 early morning" can mean either of two shifts.
- A range must match both ends of the shift.
- A first name is enough when only one assignment fits the request.

These need a clarification instead of a guess:
- vague dates ("tonight", "tomorrow");
- two different dates;
- a range without AM/PM ("7 to 11");
- a request that fits more than one assignment;
- an unrelated time in the question, such as "has class at 5 PM".

## Limits

All limits live in one `LIMITS` dictionary in `agent.py`, and the terminal header is printed from it.

| Limit | Value |
| --- | --- |
| Model calls per task (including the final answer and any correction) | 10 |
| Tool executions per task | 12 |
| Output tokens per model call | 1,000 |
| Input + output tokens per task | 50,000 |
| Provider request timeout | 30 s |
| Execution deadline (waiting for your reply excluded) | 120 s |
| Clarification replies | 2 |
| Correction attempts after a rejected answer | 1 |
| Automatic SDK retries | none |

Before each paid request:
1. The agent counts the full request with the provider's token counter.
2. It adds a 10% margin and reserves the maximum output.
3. It stops if that total would exceed what's left.

On the last allowed call, or once the tool budget is used, tools are turned off and the model must answer. It may honestly answer `incomplete`. If the provider doesn't report usage, the agent charges the reservation, labels it as unmeasured, and shows the cost as `unknown`.

Every run ends with a clear status:
- **Answers:** `recommendation`, `clarification`, `no_eligible_candidate`, or `incomplete`.
- **Stops:** a limit; an unavailable provider; truncated output; a rejected answer; an unanswered clarification; or a reply that points to a different shift.

## Estimated cost

`pricing.py` calculates an **estimated API cost** for each model call, and a running total, from the token usage the provider reports.

| | |
| --- | --- |
| Source | [OpenAI API pricing](https://developers.openai.com/api/docs/pricing), verified 2026-10-10 |
| Model / tier | `gpt-5.6-luna`, Standard tier (the agent sets no service tier) |
| Per 1M tokens (USD) | input $0.20, cached input $0.02, cache write $0.25, output $1.20 |
| Over 272K input tokens | $0.40 / $0.04 / $0.50 / $1.80; the 50K-token cap keeps runs far below this |

How the estimate is calculated:
- **Cache tokens:** cached reads and cache writes are parts of the input count and are priced at their own rates.
- **Reasoning tokens** are already included in output tokens, so they are not counted twice.
- **Local tools** have no API charge. Their results cost money only as input to later model calls.
- **Unknown:** if the price or a needed usage number is missing, the cost shows `unknown` rather than a guess.

Live test runs cost about $0.0003–$0.002 each. This is an estimate, not an invoice. Your account's tier and current prices decide the actual bill, and OpenAI reports billed costs only as account-level aggregates.

## Data and security

All workers, schedules, classes, preferences, and coverage history are synthetic; the policies are fictional.

Keep your API key only in the local `.env` file, which Git ignores; only the blank `.env.example` is published. The key is never printed or passed to tools. The generated database, virtual environment, caches, and saved traces are also ignored.

See [docs/PROJECT_SPEC.md](docs/PROJECT_SPEC.md) for the detailed specification.
