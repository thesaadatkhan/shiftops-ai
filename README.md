# ShiftOps AI

A terminal agent that helps a housing supervisor decide whom to ask first when a student worker calls out.

Python works out who is allowed to cover the shift. The model weighs the eligible workers' preferences, recent emergency coverage, and nearby classes, then explains its recommendation so the supervisor can review it. The agent never assigns shifts or contacts anyone.

## Status

| Part | State |
| --- | --- |
| Synthetic data, eligibility rules, replacement tools | Implemented and tested offline |
| Interactive context and factual schedule lookup | Tested offline and in user-run live sessions: ambiguous Alex clarification, weekly Capella assignments, and an afternoon follow-up. Known query limitations are listed below |
| Agent loop, guardrails, final validation | Implemented; tested offline with scripted model replies |
| Live model runs | **Verified 2026-10-10** with `gpt-5.6-luna`. Seven capped runs covered the five demo scenarios, one unfamiliar question, and one repeat. All seven produced Python-validated answers in 2-4 model calls and 5-13 s, and left the database unchanged |
| Time-range labels, weekly-hours wording, estimated cost | Tested offline (180 tests with scripted replies) and in a fresh virtual environment. **One live smoke run (2026-10-10)** showed time ranges, Python-computed weekly hours, cached-token counts, and numeric per-call and cumulative cost; it reported $0.001983 for 4 model calls. One run is a smoke test, not proof of general reliability |

## How it works

```
question ─▶ agent_steps() ──▶ model: request tools or give a final answer
                 ▲                    │
                 │   observations     ▼
                 └──────────── tools.dispatch() ──▶ SQLite (read-only)
final JSON ─▶ validate_final() in Python ─▶ printed only if it passes
```

**Python fetches and checks:**
- `eligibility.py` applies the hard rules for the whole shift: class, approved-leave, and assignment overlaps; a confirmed class timetable; and an absolute 20-hour weekly cap. Shift lengths come from the stored start and end times, so 4-, 5-, or 7-hour shifts are all handled exactly.
- `tools.py` provides five replacement fact tools and one read-only schedule tool, with evidence IDs. None ranks workers.
- `agent.py` binds the run to the shift and worker named in the question. It enforces the limits and checks the final answer against freshly computed eligibility. It also confirms that every cited evidence ID came back from a tool during this run.
- `pricing.py` estimates cost from the token usage the provider reports.

**The model decides:**
- which context to look up after the eligibility check;
- whom to ask first among the eligible workers, and who the backup is;
- how to explain the tradeoff.

The same question can produce different defensible choices on different runs. Three live runs of the Andromeda Oct 12 question picked Riley first twice and Dakota first once.

Facts and hard eligibility must be the same on every run: Python checks that the chosen workers are eligible and that cited evidence was retrieved. Only the weighting of soft tradeoffs may vary, such as preference versus recent emergency burden versus remaining hours. These few runs show the behavior; they don't prove the model is reliable in general.

## Files

| File | Responsibility |
| --- | --- |
| `agent.py` | `agent_steps()` loop, OpenAI provider, target binding, final validation, terminal trace |
| `tools.py` | Five replacement fact tools, schedule lookup, and validated dispatch |
| `eligibility.py` | Time arithmetic, time-range labels, hard eligibility, weekly hours, baseline validation |
| `database.py` | SQLite schema, seed loading, and safe one-time initialization |
| `pricing.py` | Verified model rates and the cost-estimate calculation |
| `data/seed.json` | Synthetic workers, shifts, classes, leave, preferences, and coverage history |
| `tests/` | Offline tests (`scripted.py` holds the pre-written model replies) |

## Tools

| Tool | Returns |
| --- | --- |
| `find_shift` | Matching shifts with their time ranges, and whether the named worker resolves, is ambiguous, or isn't assigned to that shift |
| `get_eligible_candidates` | Eligible workers in ID order, each with hours assigned now, hours after covering the whole shift, and hours left under the 20h cap; plus every exclusion reason |
| `get_preferences` | Stored preference levels; a missing preference counts as neutral |
| `get_coverage_history` | Requests made before the target shift, within a lookback of at most 30 days; outcomes shown only as known at that time |
| `get_nearby_classes` | Classes ending shortly before or starting shortly after the shift, with exact gaps in minutes |
| `get_schedule` | Dated assignments in the complete demo week, optionally filtered by hall, period, start time, or actual start date |

The three context tools accept only workers who are eligible for that shift.

## Demo data

- 30 synthetic workers; one (W30) is inactive.
- Capella, Helix, Vega, and Sirius run 8 AM–6 PM. Andromeda runs 24 hours.
- The evaluated week is Mon Oct 12 to Sun Oct 18, 2026, on one fictional America/Chicago clock. It contains 91 shifts and 448 hours. `database.py` checks that every operating hour is covered exactly once, with no conflicts and nobody over 20 hours.
- Shifts are displayed with time ranges generated from their stored timestamps, for example `Oct 12, 6 PM–11 PM`. A shift that crosses midnight shows both dates: `Oct 12, 11 PM–Oct 13, 4 AM`.
- IDs such as `AND-1012-EVE` stay the internal identity. The ID uses the operating date, so `AND-1012-EM` runs `Oct 13, 4 AM–8 AM`.
- Weekly hours are charged to the week in which a shift starts.

| Scenario | What the data sets up |
| --- | --- |
| Andromeda Oct 12, 6 PM–11 PM (Jordan out) | Four eligible workers. Riley prefers the shift but has covered three recent emergencies. Jamie's class ends 15 minutes before the start. Skyler marked it low but has the most spare hours. Dakota would reach exactly 20h. Casey is excluded (18h assigned + 5h = 23h) |
| Helix Oct 13, 8 AM–1 PM (Morgan out) | Elliot prefers it, but a class starts 10 minutes after it ends. Dakota marked it low. Skyler has recently declined requests |
| Capella Oct 16, 1 PM–6 PM (Drew out) | No eligible replacement: fall-break leave, Friday labs, or the hour cap rule out everyone |
| "Alex" on Andromeda, Sat Oct 17 | Two different Alexes work Andromeda that day, so the agent asks which one |
| Sam, Capella Oct 13, 1 PM–6 PM | Sam isn't assigned to that shift, so the agent reports the mismatch |

All people, schedules, and policies are fictional.

## Setup (Windows PowerShell)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then set OPENAI_API_KEY and OPENAI_MODEL locally
.venv\Scripts\python database.py --init-demo
.venv\Scripts\python -m pytest -q
```

Initialization happens once: `database.py --init-demo` builds `shiftops.db` from `data/seed.json` and validates it. Every agent run after that reuses the same SQLite file through a read-only connection, so runs never change it.

- `--init-demo` refuses to overwrite an existing database. Use `--init-demo --reset` only to rebuild it on purpose.
- `--check` validates an existing database again.
- The tests build their own temporary databases and use pre-written replies, so they need no key or network.

These steps were verified on 2026-10-10 in a separate temporary copy with a new virtual environment: install, one-time initialization, refusal to overwrite, `--check`, and the full offline suite (176 tests at that time).

## Run

```powershell
.venv\Scripts\python agent.py "Jordan called out for Andromeda's October 12 evening shift. Who should I ask first?"
.venv\Scripts\python agent.py "Morgan can't make the Helix morning shift on October 13. Who should cover?"
.venv\Scripts\python agent.py "Drew called out of Capella on Friday afternoon. Who can cover?"
.venv\Scripts\python agent.py "Alex called out of Andromeda on Saturday."
.venv\Scripts\python agent.py "Sam called out for Capella's October 13 afternoon shift."
```

Each command makes paid API calls, bounded by the limits below.

## The trace

For follow-up questions, start an interactive session:

```powershell
.\.venv\Scripts\python.exe .\agent.py --chat
```

Ask `Alex called out of Andromeda on Saturday`, then answer `Alex Chen` at the next `You>` prompt. You can also ask `Why that worker rather than Riley?` after a recommendation. Context exists only while this process is open; it is not saved to disk. `/new` clears the topic, and `/quit` exits. Steps, tools, tokens, and calculated costs accumulate across the session; `/new` does not reset the spent budget. Each turn has its own request/run timeout, excluding time you spend typing.

Factual schedule questions are supported too: `Which workers are covering all morning shifts at Capella this week?` The schedule tool reads SQLite, and Python displays the complete retrieved assignment list. This is factual lookup, not model judgment. `This week` refers to the synthetic October 12-18 demo week. Broad requests returning more than 16 shifts must be narrowed. A quoted one-shot command still starts an independent conversation.

The terminal prints what happens as it happens:
- `Step-1`, `Step-2`, and so on (one step per model call);
- each tool action with its arguments;
- a concise observation for each tool result;
- the final answer validated by Python.

It never shows hidden reasoning.

Output stays left-aligned in a column at most 76 characters wide and adapts to narrower terminals. Each step labels its action and tools. A step may request several tools; each tool has its own arguments and result block. Candidates and evidence appear on separate lines rather than long horizontal lists. The command is unchanged.

Related metrics and arguments now share lines using `|`, with automatic wrapping. For example: `Tokens: in 1,398 / out 85 | Time: 2.0s`. No usage or eligibility facts are removed.

Format example, produced from **scripted** replies (the numbers are illustrative):

```
Step-1
------------------------------------
Action: Call tools
Tools: find_shift
Tokens: in 800 / out 60
Cache: (cache read 0 / write 0)
Reserved: 3,138 | Used so far: 860/50,000 tokens
Time: 0.0s
est. cost: $0.000232 this call | $0.000232 so far

  Action: Identify the shift and outgoing worker
  Tool: find_shift
  Arguments:
    date: 2026-10-12
    hall: Andromeda
    worker: Jordan
  Result: resolved
    Andromeda | Oct 12, 6 PM–11 PM
    Shift ID: AND-1012-EVE
    Assigned: Jordan Lee (W01)

...

FINAL (recommendation, validated by Python)
  Shift: Andromeda Evening, Oct 12, 6 PM–11 PM (AND-1012-EVE)
  Outgoing: W01 Jordan Lee
  Ask first: W14 Skyler Moreno
  Backup: W21 Dakota Lin
  Weekly hours, W14 Skyler Moreno: 5h assigned now -> 10h after covering,
  10h left of the 20h cap

Why: [The model's explanation, wrapped in the same narrow column.]

Run totals
  Steps: 4 | Tool actions: 5
  Tokens: 3,900 input + 330 output
  Time: 0.0s
Estimated cost: $0.001176 (estimate, not a bill)
```

The weekly-hours lines in the final answer are recomputed by Python; they don't come from the model's wording.

## Estimated cost

`pricing.py` computes an estimate for each model call and a running total from the token usage the provider reports.

The counts are measured, not guessed: the calculation applies the recorded rates to ordinary input, cached reads, cache writes, and output. It remains labeled an estimate because it assumes Standard-tier pricing rather than retrieving your final account bill. OpenAI's Costs API supplies aggregated account costs, not a live billed amount attached to each response.

| | |
| --- | --- |
| Source | [OpenAI API pricing](https://developers.openai.com/api/docs/pricing), verified 2026-10-10 |
| Model / tier | `gpt-5.6-luna`, Standard tier (the agent sets no service tier) |
| Currency | USD per 1M tokens |
| Input | $0.20 |
| Cached input | $0.02 |
| Cache write | $0.25 |
| Output | $1.20 |
| Long context (over 272K input tokens) | $0.40 / $0.04 / $0.50 / $1.80 for the whole request; the 50K-token cap keeps runs far below this |

How the estimate is calculated:
- Cached and cache-write tokens are parts of the input count and are priced at their own rates.
- Reasoning tokens are billed as output and are already included in the output count, so they aren't added again.
- Local Python/SQLite tools have no API charge. Their results are paid for only as input to later model calls.
- If the price or any needed usage number is missing or inconsistent, the cost shows as `unknown`, never a guess. That includes a missing cache breakdown, a failed request, or an unknown model.
- Tokens charged conservatively when usage wasn't reported are labeled separately from measured usage.
- The estimate is display only. Prices can change, so treat it as an estimate rather than a bill. Budgets are always enforced on tokens and calls.

Measured example (live smoke run, 2026-10-10): 4 model calls, 9,946 input tokens (5,845 of them cached reads) and 701 output tokens, estimated at $0.001983.
- Every call reported both cache counts, which is why the estimates are numbers rather than "unknown".
- That run happened before the trace printed cache writes. Re-deriving the four printed costs implies the writes equaled each call's uncached input; the trace now shows both reads and writes.
- Earlier runs used 3,000-12,000 tokens, roughly $0.001-$0.003 each at these rates.

## Guardrails

| Limit | Default |
| --- | --- |
| Model calls per run (including repairs) | 10 |
| Tool executions per run | 12 |
| Output tokens per call | 1,000 |
| Total input + output tokens per one-shot run or entire chat session | 50,000 |
| Request timeout / run deadline | 30 s / 120 s |
| SDK automatic retries | Disabled |
| Question / tool arguments / tool result size | 500 / 1,000 / 8,000 characters |

Before every paid request, the agent asks the provider's token counter for the size of the full request. It adds a 10% margin, reserves the maximum output, and stops if that total won't fit in the remaining allowance. On the last allowed call, or once the tool budget is spent, tools are disabled so the model has to answer.

A run stops with an explicit status for any of these: truncated output, unavailable provider, call/tool/token/deadline limits, repeated calls, or a final answer that fails validation after one repair attempt.

Earlier one-shot acceptance used at most 11,991 tokens with a 20,000-token cap. The current allowance is 50,000: a user-run chat resolved an ambiguous Alex call-out in six steps and 21,235 tokens, then reached the separate 10-step cap after further queries. Clear weekly schedule lookup took two steps and 4,367 tokens. These are observed examples, not guarantees for other inputs.

## Limitations

- The primary task is replacement advice for one call-out. Factual schedule lookup is secondary; general workforce-hours reporting is not supported.
- Ambiguous day-versus-week wording can cause a validation repair or a narrower answer. Use an explicit date or "the week beginning October 12, 2026". Multiple-date ranges require clarification with the current parser.
- A follow-up asking for each listed worker's total assigned weekly hours lacks an exposed reporting tool and may fail validation rather than produce an hours report. Existing replacement eligibility still calculates weekly hours correctly.

- Evidence checking confirms that cited records were retrieved. It can't prove every free-text claim in the rationale is accurate.
- The question parser is deliberately limited. It rejects contradictions in supported dates, weekdays, shift names, shift IDs, and time ranges.
  - Relative dates such as "tonight" require clarification.
  - Unrelated times such as "has class at 5 PM" may also trigger clarification.
  - Use an explicit date and shift name, or a shift ID, in demo questions. Class facts come from the tools.
- The data covers one demo week. Weekday names in questions refer to that week.
- Not exercised live: the largest eligible pools (up to 16 workers) and a repair turn after a rejected answer.
- User-run transcripts now show actual cache reads/writes and verified arithmetic. If a provider response omits either cache count, the trace shows `cache breakdown unreported` and the cost shows `unknown`.
- If the provider reports only one of input or output usage, the trace shows the missing count as unknown, charges its reservation conservatively, and marks the cost unknown.
- Cost assumes Standard-tier billing; actual account or tier settings can differ.
- No partial or split coverage, writes, notifications, or saved chat history. Use `--chat` for clarification replies in the same process; separate one-shot invocations have no shared context.

## Security

Keep the API key in the local `.env`. Only the blank `.env.example` is tracked. The key is never printed, logged, or sent to tools. Generated databases, environments, traces, and private course materials are ignored by Git.

See [the project specification](docs/PROJECT_SPEC.md) for the full scope and acceptance criteria.
