# ShiftOps AI mini-project specification

## Problem and scope

When a student worker calls out, a housing supervisor needs to decide whom to ask first. Several workers may be able to cover, but preferences, recent emergency coverage, and nearby classes can make one a more considerate choice.

Build one small, read-only terminal agent. It identifies the worker/shift, checks hard eligibility through tools, gathers relevant contextual facts, and recommends an eligible worker with a grounded explanation and optional eligible backup.

The model makes the judgment. Python establishes the facts and refuses invalid recommendations. No contact or assignment change occurs.

## Implementation status

Phases 1-3 are implemented. Phase 3 live acceptance (2026-10-10) used seven capped runs with gpt-5.6-luna: the five demo scenarios, one unfamiliar input, and one repeat. All seven produced Python-validated finals, and the database was unchanged.

A later finalization pass added time-range labels, clearer weekly-capacity fields, and estimated cost, followed by compact terminal formatting, in-process chat, and factual schedule lookup. These have offline coverage (214 passing tests at the Git checkpoint), with user-run live evidence for clarification replies, replacement selection, and weekly schedule follow-ups. Fresh setup was tested in an isolated virtual environment. Day/week ambiguity and general weekly-hours reports remain known limitations; replacement advice is the primary scope. The README records tested behavior and limits.

## Interface and loop

Use `python agent.py "question"`; also support `--help`. A separate main.py is unnecessary.

Implement `agent_steps()` as a generator yielding events as they happen. The terminal entry point prints the events. The loop:
1. Sends the user request, conversation, instructions, and tool schemas to the model.
2. Receives requested tool calls or a final result.
3. Validates and dispatches only registered tools.
4. Appends the assistant tool-call message and matching tool observations.
5. Calls the model again until it returns a valid final result or a guardrail stops the run.

Use structured function calling through the provider SDK. Do not eval/exec model text. Do not hardcode a mandatory sequence of context tools. Required evidence and final validation may be enforced without predetermining the substantive choice.

A final result distinguishes recommendation, clarification, no eligible candidate, factual schedule answer, and incomplete/error. For a recommendation, identify the exact shift, outgoing worker, chosen worker, optional backup, concise rationale, tradeoff, and factual evidence references. Use a small validated JSON contract.

One-shot invocations are independent. `--chat` preserves conversation, retrieved evidence, and topic constraints only in the current process. A clarification reply may supply a name or missing interval details; Python checks those details before binding a target. Follow-ups retain context and revalidate recommendation eligibility. `/new` clears context without resetting spent session budget; `/quit` exits. Existing 10-model-step, 12-tool, 50,000-token limits apply across all turns, with a fresh per-turn deadline that excludes user typing time. No on-disk conversation persistence.

## Synthetic data

Use SQLite generated from tracked `data/seed.json`. Model 30 synthetic student workers and the five halls below. Seed a fully covered baseline week, relevant dated classes/leave/preferences, and recent coverage history. Build the baseline explicitly in the seed; do not add an optimizer. Use explicit dated class intervals for this bounded demo instead of semester management.

### Halls and usual shift patterns

Each shift requires one worker. Capella, Helix, Vega, and Sirius operate daily from 8 AM to 6 PM. Andromeda operates continuously, with each operating day running from 8 AM to 8 AM the next day.

| Halls | Usual shift name | Start | End | Duration |
| --- | --- | --- | --- | --- |
| Capella, Helix, Vega, Sirius | Morning | 8 AM | 1 PM | 5 hours |
| Capella, Helix, Vega, Sirius | Afternoon | 1 PM | 6 PM | 5 hours |
| Andromeda | Morning | 8 AM | 1 PM | 5 hours |
| Andromeda | Afternoon | 1 PM | 6 PM | 5 hours |
| Andromeda | Evening | 6 PM | 11 PM | 5 hours |
| Andromeda | Overnight | 11 PM | 4 AM next day | 5 hours |
| Andromeda | Early Morning | 4 AM | 8 AM | 4 hours |

These are baseline patterns, not fixed-duration eligibility rules. Actual stored shifts may have other positive durations. Use their exact start/end datetimes in all tools and calculations; never assume five hours, round durations, or impose a separate five-hour daily limit. Display exact dates/times with shift names, since a name alone is not a unique identity. Labels are generated from the stored timestamps, for example "Oct 12, 6 PM–11 PM" or, across midnight, "Oct 12, 11 PM–Oct 13, 4 AM". Shift IDs remain the internal identity. The early-morning shift starts on the calendar date following its operating day's 8 AM start.

This operation requires 64 worker-hours per day and 448 per week. Thirty workers at 20 hours each provide 600 theoretical hours, which does not prove feasibility. Validate that the seeded baseline actually covers every operating interval without gaps, duplicate coverage, class/leave/assignment conflicts, or exceeded weekly limits. Include adjacent-week assignments where needed for correct overnight accounting and overlap checks.

The ordinary schedule is staffed; the pain point is a subsequent call-out, not unexplained widespread vacancies. Keep the outgoing worker's baseline assignment intact during the read-only investigation and exclude them as a replacement. Include several scenarios with multiple eligible alternatives and meaningful soft tradeoffs. A no-candidate scenario may leave the original assignment in place while showing that no replacement can cover it.

Include a variable-duration coverage example alongside the usual patterns, in a separate test fixture or clearly identified scenario that does not corrupt the fully covered baseline. A worker at 13 scheduled hours can cover a seven-hour interval if every other rule passes; a worker at 18 hours cannot cover an entire five-hour interval.

Suggested tables:
- workers: stable ID/code, name, active status, weekly hour limit fixed at 20 hours for this demo.
- timetable_coverage: worker, inclusive coverage dates, explicitly confirmed completeness. Confirmed no classes is valid; absent/unconfirmed coverage is unknown.
- shifts: ID, hall, readable shift name, local start/end datetime, required staffing of one.
- assignments: worker and shift.
- classes: worker and exact start/end datetime.
- approved_leave: worker and exact start/end datetime.
- preferences: worker, target shift, preferred/neutral/low.
- coverage_requests: worker, referenced emergency shift, asked_at, outcome (pending/declined/accepted/completed). One record describes one request's current outcome.

Store facts, not inferred feelings or a predetermined winning worker. Record being asked, accepting, and completing distinctly. Use only events before the target shift for historical context; never let future seed records inform a historical decision. Completed emergency shifts inside the evaluated week must also appear in assignments and count once toward hours.

All timestamps share an explicitly documented fictional America/Chicago local clock. Choose demo dates away from daylight-saving transitions and reject timezone-ambiguous inputs rather than silently mixing clocks. Prefer explicit dates to "tonight"; do not depend on the host's current date.

Validate foreign keys, unique assignments, interval order, preference/outcome values, and nonnegative limits. Reinitialization must refuse an existing database unless an explicit reset flag is provided. Initialization happens once; normal agent runs reuse the existing SQLite file through a read-only connection.

## Hard eligibility

A candidate must:
- Be active and different from the outgoing worker.
- Have confirmed complete class information covering every date touched by the shift.
- Have no class, approved leave, or other assignment overlap.
- Remain at or below the absolute 20-hour weekly work limit after the full shift is added. This is the primary work-hours restriction and cannot be relaxed by the model or by a stored limit above 20.
- Not already be assigned to the target shift.

Use half-open intervals: touching endpoints do not overlap. Calculate durations and time gaps in Python, preferably integer minutes. Reject invalid durations instead of rounding them into validity.

The 20-hour ceiling is the primary cap; actual class overlaps, approved leave, and assignment overlaps are also mandatory exclusions. There is no priority rule allowing spare hours to override a class conflict, or allowing an appealing candidate to override any hard restriction. Preferences, nearby non-overlapping classes, recent emergency burden, and remaining capacity among eligible workers inform the model's recommendation.

Recommend one worker who can cover the entire requested interval. Do not shorten the interval to fit remaining hours, split it between workers, or stop coverage when the worker reaches 20. Partial or shared coverage is outside scope; if no worker can cover the whole interval, report that honestly.

Weeks run Monday through Sunday. Charge a whole assignment to the week containing its start, including overnight shifts; document this fictional accounting rule. Class and leave hours do not consume weekly work-hour allowance.

Check the outgoing worker is actually assigned to the resolved target. If not, report the mismatch; do not invent a call-out or silently fill an unrelated uncovered position.

No separate unrestricted personal availability or qualification system is needed for this mini-project. All active demo workers share the desk role; non-class absences are represented by approved leave. Preferences are soft and never make a worker ineligible.

Return all applicable exclusion reasons. Do not rank the eligible list by suitability; use a stable ID order for reproducibility.

## Tools

| Tool | Inputs | Deterministic output |
| --- | --- | --- |
| find_shift | hall/date/time or shift ID; outgoing worker name/code as appropriate | Matching shifts, assignments, resolved worker or explicit ambiguity/mismatch |
| get_eligible_candidates | shift ID and outgoing worker ID | Eligible workers with hours assigned now, hours after covering the whole shift, and hours left under the cap; exclusion reasons |
| get_preferences | shift ID and bounded candidate IDs | Stored preferences; absent preference is neutral |
| get_coverage_history | shift ID, bounded candidate IDs, bounded lookback days | Dated requests/outcomes and counts before the target shift |
| get_nearby_classes | shift ID, bounded candidate IDs, bounded time window | Nearby classes and exact gaps, distinct from overlap exclusions |
| get_schedule | demo week, optional actual start date, hall, period, start time | Complete matching dated assignments in start-time order, capped at 16 records; broader requests require narrowing |

Factual schedule answers cite exactly one retrieved SCHEDULE record. Python re-reads it for freshness, checks its filters against explicit hall/date/period/time constraints, and displays every matched assignment from SQLite. The model does not invent the displayed list or treat this factual lookup as judgment. Historical seed fragments cannot be presented as complete weeks.

Choose clear argument schemas during implementation. Candidate-context tools should be restricted to the relevant eligible candidates and return compact bounded results. Unknown tool names, malformed arguments, and controlled tool errors become observations the model can handle within the existing budget.

Do not add a best_candidate tool, decisive fairness score, or generated "burnout" label. The model may weigh recorded emergency workload but cannot assert a worker is exhausted or likely to accept without supporting records.

## Final validation

Python automatically checks the selected worker and optional backup against current deterministic eligibility for the exact target. Reject unknown or ineligible identities; never silently substitute another worker.

Require that supporting context in the rationale was actually retrieved. Use simple evidence IDs/structured references to validate cited records; do not promise perfect automatic verification of every free-text claim. Test factual grounding in real-run review too.

If the model invents a worker or fails validation, either make a bounded correction request charged to the same run limits or finish with an explicit invalid/incomplete result. No extra hidden retry budget.

## Guardrails and observability

Initial configurable targets, to be tuned with measured traces:
- At most 10 model calls per run, including final answers and repairs.
- At most 12 tool executions total.
- At most 1,000 generated tokens per model call.
- A 50,000-token aggregate allowance across repeated input and generated output (raised by the user after the 20,000-token chat cap blocked a legitimate clarification flow).
- A 30-second provider request timeout and 120-second overall deadline.
- Disable automatic provider retries initially.
- Bound user input, tool arguments, result sizes, lookback periods, and conversation size.

A cumulative after-the-fact usage check is insufficient. Before each paid request, account for the full accumulated messages/tool schemas and reserve its maximum output within the remaining allowance. Use model-appropriate token counting with a documented conservative margin, and state any limitations. Stop before a request that will not fit. Never silently truncate away tool-call pairing or needed evidence.

Print Step-1, Step-2, etc. for model calls, with an explicit action and requested tools. Each tool action has readable arguments and a result block; list candidate facts individually. Keep output left-aligned, wrap at at most 76 characters (narrower in small terminals), and preserve all displayed facts. Include actual input/output usage, cumulative usage, per-call/cumulative cost estimates, and final status. Show available usage even on invalid responses. Handle output truncation explicitly using provider finish metadata.

Do not print hidden chain-of-thought. The trace contains observable actions, results, and concise visible explanations. Any saved trace is ignored by Git.

Estimated dollar cost is shown per model call and cumulatively. Python computes it in pricing.py from provider-reported usage and explicitly verified rates:
- Source: https://developers.openai.com/api/docs/pricing, verified 2026-10-10.
- gpt-5.6-luna, Standard tier, USD per 1M tokens: input $0.20, cached input $0.02, cache write $0.25, output $1.20.
- Long-context rates apply to prompts over 272K input tokens.

Accounting rules:
- Cached and cache-write tokens are priced separately as parts of input.
- Reasoning tokens are already inside output tokens and are not added again.
- Local tools have no API charge; their results are paid for only as later model input.
- If the price or any required usage number is missing or inconsistent, the cost is shown as "unknown".
- Conservatively charged tokens stay labeled separately from measured usage.

Cost is display only; token and call caps remain the enforcement controls.

## Demo cases and acceptance

Use at least these scenarios:
1. Multiple eligible workers with conflicting soft considerations, including frequent recent emergency coverage.
2. Another case where a nearby class or preference changes the tradeoff.
3. No eligible replacement.
4. Ambiguous worker or shift requiring clarification.
5. Outgoing worker not assigned to the requested shift.

Test baseline coverage/feasibility, exact overlap/hour boundaries, variable durations (including seven hours at 13 already scheduled), rejection of partial coverage, overnight and week-boundary shifts, missing timetable information, historical cutoff, invalid tool inputs, unavailable provider, invalid/ineligible final selection, truncated output, repeated calls, and cap exhaustion.

Use scripted provider responses for deterministic loop/failure tests with no key/network. Clearly label replay/demo fixtures as scripted. Run a few bounded live calls for genuine model judgment and inspect the trace; do not require one exact candidate when multiple choices are defensible.

The finished project must run from a fresh clone using documented setup and deterministic seed initialization. README must distinguish tested behavior and remaining limitations.

## Presentation

Five minutes: state the pain point, point to fetching/checking versus deciding code, demonstrate the terminal trace, explain one substantive tradeoff, and discuss caps and limitations. Run an unfamiliar input and read the steps before rehearsing.

A rules-only system could impose a scoring scheme. Explain honestly that this implementation uses a model to weigh competing context and explain a choice; do not claim that code cannot calculate history or trends.

## Excluded scope

No frontend, FastAPI, scheduling optimizer, writes during an agent run, notifications, approval persistence, deployment, RAG, MCP, frameworks, multiple agents, full-semester employee management, or splitting shifts between replacements.
