# ShiftOps AI

A terminal agent that answers one question for a residence-hall supervisor:

> **A student worker just called out. Who should I ask first to cover the shift, and why?**

Python works out who **can** cover. The model decides who **should** be asked first among those workers by weighing preferences, recent emergency coverage, nearby classes, and weekly hours. It then explains the tradeoff. The supervisor reviews the recommendation before acting.

The agent is read-only: it never assigns a shift or contacts anyone.

## How it works

The agent uses a handwritten Python loop with OpenAI tool calling. Given a call-out question, the model requests tools, receives their results, and decides what to investigate next. It continues until it can recommend a replacement, ask for clarification, or explain why nobody can cover.

Eligibility is determined by Python rules, not the model. The rules produce an unranked shortlist; the model weighs the softer considerations. For example, one worker may prefer the shift but have covered several recent emergencies, while another has not been asked recently but has a tight transition from class. The recommendation explains that tradeoff, and Python checks the selected workers again before displaying it.

```text
question ─▶ agent_steps() ──▶ model: asks for tools, or gives a final answer
                 ▲                    │
                 │  tool results      ▼
                 └──────────── tools.run_tool() ──▶ SQLite (read-only)
final answer ─▶ Python checks it ─▶ printed only if it passes
```

The loop stops with an explicit status if information is insufficient or a call, tool, token, or time limit is reached.

| Stage | What happens |
| --- | --- |
| Question | The supervisor describes the call-out: worker, hall, date, and shift. |
| Model | Reads the question and any results already returned, then chooses a tool, asks for clarification, or proposes a final answer. |
| ACTION / Python tool | Python runs only an allowed tool. The tools identify the shift, calculate eligibility, or retrieve preferences, coverage history, and nearby classes. |
| RESULT / feedback | The tool's facts go back into the conversation so the model can decide what to do next. This repeats as needed within the task's limits. |
| Final Python check | Ordinary **Python comparisons and database checks**, not another AI call.<br><br>**Correct shift?** `answer["shift_id"] == locked_shift_id`<br>**Eligible choice?** `answer["ask_first"] in eligible_worker_ids`<br>**Different backup, if provided?** `answer["backup"] is None or answer["ask_first"] != answer["backup"]`<br><br>These are simplified examples. Python reruns class, leave, assignment, active-status, timetable, and 20-hour checks for the proposed workers. It confirms that the required tools returned facts and that the answer has the expected fields. It rejects the outgoing worker and checks any no-candidate claim against an empty eligible list. **An invented worker, ineligible choice, or wrong-shift answer is rejected before a recommendation is printed.** |
| Print or correct | A valid result is printed with Python-calculated shift times and hours. If rejected, the program prints the reason and sends it back to the model for **one correction attempt** within the same call, token, and time budget. Python checks the corrected answer again. If it still fails, or a limit prevents correction, the run stops with an explicit status. Python does not silently pick a different worker. |

The final check verifies eligibility and that supporting facts were retrieved; it does not verify every sentence of the explanation or guarantee the best possible choice.

## Tools

All five tools read SQLite and return facts. None chooses a winner or writes the recommendation.

| Tool | What it does |
| --- | --- |
| `find_shift` | Finds the exact dated shift and confirms that the named worker holds it. Returns ambiguity or a mismatch when the question cannot be resolved safely. |
| `get_eligible_candidates` | Checks active status, complete class information, class/leave/assignment overlaps, and the absolute 20-hour weekly cap after adding the whole shift. Returns eligible workers with current/projected hours and exclusion reasons for everyone else. |
| `get_preferences` | Looks up whether each eligible worker marked this particular shift preferred, neutral, or low. A missing preference is neutral; preferences do not affect hard eligibility. |
| `get_coverage_history` | Returns recent emergency requests and their pending, declined, accepted, or completed outcomes, using only information known before the target shift starts. |
| `get_nearby_classes` | Returns non-overlapping classes shortly before or after the shift and the exact time gaps, helping the model consider tight transitions. |

## Synthetic data

The saved dataset in `data/seed.json` models **30 student workers and five residence halls** for **October 12–18, 2026**. One worker is inactive. Each desk shift has one assigned worker, and the baseline is fully staffed: **91 shifts covering 448 hours**.

### The halls and their shifts

| Hall | Desk hours | Usual shifts |
| --- | --- | --- |
| Capella | 8 AM–6 PM daily | 8 AM–1 PM; 1 PM–6 PM |
| Helix | 8 AM–6 PM daily | 8 AM–1 PM; 1 PM–6 PM |
| Vega | 8 AM–6 PM daily | 8 AM–1 PM; 1 PM–6 PM |
| Sirius | 8 AM–6 PM daily | 8 AM–1 PM; 1 PM–6 PM |
| Andromeda | 24 hours, from 8 AM to the next day's 8 AM | 8 AM–1 PM; 1 PM–6 PM; 6 PM–11 PM; 11 PM–4 AM; 4 AM–8 AM |

Most shifts are five hours; Andromeda's 4 AM–8 AM shift is four hours. These are schedule patterns, not fixed-duration eligibility rules: Python uses each stored shift's actual dates and times, including midnight crossings.

The story starts with a normal, covered schedule. A worker then calls out, and the supervisor asks for a replacement. Several workers may pass the hard checks, but their preferences, recent emergency coverage, and class transitions differ. The agent investigates those facts and proposes whom to ask first. The original assignment remains intact while this read-only investigation runs.

### What SQLite stores

| Table | Facts stored |
| --- | --- |
| `workers` | Worker IDs, names, active status, and weekly hour limits |
| `timetable_coverage` | Dates for which a worker's class timetable is confirmed complete |
| `shifts` | Hall, shift name, and exact start/end timestamps |
| `assignments` | Which worker is scheduled for each shift; used to calculate weekly hours |
| `classes` | Each worker's dated class intervals |
| `approved_leave` | Dated leave intervals that prevent coverage |
| `preferences` | Preferred, neutral, or low preference for a particular shift |
| `coverage_requests` | Emergency requests, when they were made, outcomes, and when outcomes became known |

Supporting historical and adjacent-week records allow coverage-history and overnight checks. All people, schedules, and policies are fictional.

`database.py --init-demo` builds `shiftops.db` from this fixed seed once. Normal runs reuse it through a read-only connection; they do not regenerate synthetic data. Shift durations come from actual timestamps, and a replacement must cover the entire shift without exceeding 20 weekly hours. Exactly 20 hours is allowed.

## Setup

Install **Python 3.10+** (tested with 3.12). Live runs also require internet access and your own OpenAI API key with access to the configured model. SQLite comes with Python; the SDK, dotenv, and test dependencies are pinned in `requirements.txt`.

Run these steps in **Windows PowerShell**. The example uses Python 3.12; substitute another supported interpreter if needed. No environment activation is required.

```powershell
# 1. Download the project. If using a ZIP, extract it and open its folder instead.
git clone https://github.com/thesaadatkhan/shiftops-ai.git
cd shiftops-ai

# 2. Create a local Python environment and install the pinned dependencies.
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. First-time configuration: copy the blank example and edit your local .env.
# Set OPENAI_API_KEY to your own key and OPENAI_MODEL to gpt-5.6-luna.
# Leave OPENAI_REASONING_EFFORT blank for the model default. Save and close Notepad.
# Skip the copy if you already have .env; never overwrite your existing key.
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env

# 4. Build the synthetic SQLite database once, then validate it.
# Skip initialization if shiftops.db already exists; it refuses to overwrite it.
.\.venv\Scripts\python.exe .\database.py --init-demo
.\.venv\Scripts\python.exe .\database.py --check

# 5. Optional: check that the program's rules and safeguards work.
# These tests try situations where we already know the correct outcome:
# - 18 hours already assigned + a 5-hour shift must be rejected.
# - A class overlapping the shift must prevent that worker from covering.
# - An invalid AI choice must be caught before a recommendation is printed.
# The tests use temporary data and pre-written AI replies, not paid AI calls.
# They need no API key or internet and do not change your demo database.
# "Passed" means the program behaved as expected in the tested situations.
# It does not prove the real AI will always make the best recommendation.
# You can skip this step and still run the agent.
.\.venv\Scripts\python.exe -m pytest -q
```

For macOS/Linux, create the environment with `python3 -m venv .venv`, use `.venv/bin/python` in place of the Windows interpreter, copy the example with `cp .env.example .env`, and edit it in your preferred text editor. Keep your key local; `.env` is ignored by Git.

## Run

```powershell
# 1. From the project folder, ask one call-out question in quotes.
# This makes paid model calls and prints the steps, tool results, and recommendation.
.\.venv\Scripts\python.exe .\agent.py "Morgan called out for Helix's morning shift on October 13, 2026. Who should we ask first?"

# 2. If the agent asks a clarification, type your reply at 'Your reply>' and press Enter.
# Keep output in the terminal for interactive replies; do not redirect it to a file.

# Optional: show usage without making an API call.
.\.venv\Scripts\python.exe .\agent.py --help
```

On macOS/Linux, use `.venv/bin/python agent.py` with the same question.

The terminal prints numbered steps, tool actions/results, token usage, estimated cost, and the final recommendation. If the request is ambiguous, you can answer up to two clarification questions when both input and output are terminals. Redirected runs print the question and finish.

## Limits

Each task is capped at 10 model calls, 12 tool executions, and 50,000 total tokens. Defaults are in `LIMITS` in `agent.py`. Live calls are paid; displayed costs are estimates.

Use explicit demo dates and one call-out per run. Python validates the target, eligibility, and required fact retrieval, but does not verify every sentence of the explanation. Different eligible choices can be defensible. All data and policies are fictional.
