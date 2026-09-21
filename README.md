# ShiftOps AI

ShiftOps AI is an AI-assisted workforce scheduling and shift coverage platform built as a full-stack web application.

The application demonstrates how structured workforce data, constraint-based schedule optimization, and a natural-language AI interface can be combined to support operational scheduling decisions.

The planned AI product is a scheduling agent: ask it to find a replacement
for a call-out, have it investigate eligible workers and propose a change,
then approve the change and receive a verified result. Reliable backend rules
validate every assignment. This workflow is required future work; the agent
is not implemented yet.

## Project Status

ShiftOps AI is in early active development. It currently runs locally only and is not deployed.

**Implemented:**

- A React application shell (Vite + JavaScript) with sidebar navigation covering all planned sections (Dashboard, Employees, Schedule, Coverage, Workforce Planning, AI Assistant). Dashboard, Employees, Coverage, Schedule and Workforce Planning all show real data, sharing one reporting-week selection between Dashboard, Employees, Schedule and Workforce Planning; only AI Assistant remains a placeholder.
- Deterministic shift coverage (Phase 6) and a full schedule-generation pipeline (Phase 7): a read-only eligibility engine and Coverage screen; a Google OR-Tools draft optimizer; and a stored proposal/approval workflow so a supervisor must explicitly approve a generated draft, with full revalidation against current data, before any assignment becomes real. Every generated proposal persists its full coverage review (existing/proposed assignments, totals, uncovered reasons) and can be recovered by week after a refresh or navigation, not only for the browser session that generated it. An explicit worker-replacement operation and an append-only assignment-change audit trail are also implemented. The Schedule screen surfaces all of this: preparing a week's required shifts, generating and reviewing a proposal with an explicit approval-confirmation step, approving or rejecting it, and replacing an individual assignment.
- A FastAPI backend covering the employee lifecycle and semester timetables across fourteen endpoints:

  | Endpoint | Purpose |
  |---|---|
  | `GET /api/health` | backend connection check |
  | `GET /api/employees` | read the workforce with weekly capacity figures |
  | `GET /api/employees/{employee_code}` | read one worker's stored details, semesters, preferences and leave |
  | `POST /api/employees` | create a worker |
  | `PUT /api/employees/{employee_code}` | edit a worker's name and student type |
  | `POST /api/employees/{employee_code}/deactivate` | make a worker inactive |
  | `POST /api/employees/{employee_code}/reactivate` | make a worker active again |
  | `DELETE /api/employees/{employee_code}` | permanently delete a worker |
  | `POST /api/employees/{code}/semesters` | add a semester with inclusive start and end dates |
  | `PUT /api/employees/{code}/semesters/{id}` | change a semester's dates |
  | `DELETE /api/employees/{code}/semesters/{id}` | delete a semester and its classes |
  | `POST /api/employees/{code}/semesters/{id}/blocks` | add a recurring weekly class |
  | `PUT /api/employees/{code}/semesters/{id}/blocks/{id}` | change a class's day or times |
  | `DELETE /api/employees/{code}/semesters/{id}/blocks/{id}` | remove a class |

  Every write returns errors in one consistent `{"detail": ...}` shape.
- Frontend/backend integration: the Employees section loads the workforce from `GET /api/employees`; the Dashboard and Workforce Planning sections load `GET /api/analytics/weeks/{week_start}` (see Phase 8 below). All three share the same loading, retry-on-error, and malformed-response handling. CORS is configured on the backend for the local frontend origin.
- **Phase 8 - Dashboard and Workforce Planning analytics**, read-only throughout, built on one endpoint: `GET /api/analytics/weeks/{week_start}`, validated the same way every other week-scoped endpoint validates a Monday. Reading analytics never prepares a week's shifts. It reuses the existing eligibility/reporting rules rather than re-deriving them - the same accepted (confirmed, non-provisional) timetable-readiness rule `eligibility.py` requires, and the same whole-hour, start-week assigned-hours accounting `reporting.py` already established.
  - **Coverage metrics**: stored shift count; required and filled positions (filled capped per shift at `required_staff`, so overstaffing can never inflate it); uncovered positions; unfilled shift count; excess assignments (the overstaffed remainder, reported rather than dropped); required coverage hours (`sum(duration x required_staff)`); scheduled coverage hours (`sum(duration x min(assigned_count, required_staff))`); recorded assignment hours (uncapped); and a coverage percentage defined as exactly 100% when required hours are zero, rather than an undefined division. Historical coverage is preserved even for an assignment held by a worker who is now inactive.
  - **Workforce metrics**: total, active, timetable-ready and active-and-timetable-ready worker counts; active theoretical capacity (the sum of active workers' own weekly hour limits - never equated with eligibility, and never reduced by class or leave hours); active assigned hours; theoretical remaining active capacity (`max(0, active capacity - active assigned hours)`); recorded assigned hours across all workers, including inactive ones; and a per-worker utilization row (identity, active/readiness state, weekly limit, assigned hours, remaining capacity, utilization percentage).
  - **The Dashboard screen** shows both sets of metrics as cards, a coverage progress bar, and a worker-utilization table, for the same App-owned reporting week Employees and Schedule already share. An unprepared week is shown honestly (zero stored shifts, an explicit note), never as an error and never as a reason to prepare one on its own.
  - **The Workforce Planning screen** shows the current week's actual operational feasibility (coverage percentage and uncovered positions from the real stored schedule) separately from a read-only, aggregate workforce-size scenario calculator: given an explicit hypothetical worker count and weekly hours per worker (both supervisor-entered, never inferred), it reports hypothetical theoretical capacity, the capacity surplus or shortfall against required coverage hours, a theoretical minimum worker count (`ceil(required coverage hours / weekly hours per worker)`, undefined rather than zero when weekly hours is zero), and whether aggregate capacity is sufficient - with a permanent, prominent disclaimer that this is an aggregate lower bound only, never a proof that a feasible schedule exists, and never a hiring/firing recommendation. The scenario math runs entirely client-side against the one already-fetched `required_coverage_hours` figure; it mirrors `backend/analytics.py`'s `workforce_scenario` formula exactly and needs no second network round trip per keystroke.
- An Employees view listing one summary row per worker: employee ID, name, student type, class-block count, weekly class hours, timetable status, weekly hour limit, and a count of approved leave periods. The details screen shows the records behind those figures and offers semester and class editing, timetable confirmation, shift-preference editing and approved-leave editing.
- Class timetables are stored as **semester schedules owned by a worker**, each holding recurring weekly class blocks (a weekday plus a start and end time). Course names are no longer needed to run the application. Class-block counts and class hours count only the classes that actually fall inside the displayed week: a Monday class counts only if that Monday is within the worker's semester, so a semester starting on the Wednesday does not retrospectively add the Monday and Tuesday. Class hours stay fractional, because a class is 75 or 165 minutes rather than a whole number of hours.
- Timetable status describes **the week on screen**, not whether a worker ever had a confirmed timetable. A semester confirmed last spring says nothing about October, so it is not reported as confirmed while October is displayed. There are five states:

  | Status | Meaning |
  |---|---|
  | Not set up | No semester has been entered for this worker at all |
  | Other semester only | They have a timetable, but for a different period |
  | Awaiting confirmation | Something covers this week, but nobody has checked it |
  | Part of week | Confirmed for some days of this week, not all seven |
  | Confirmed | Confirmed for the whole week |

  **Confirmed with no class blocks** means a deliberate "no classes this week", which stays distinct from **Not set up**. This describes timetable readiness only. It is not shift eligibility - that is a separate backend calculation (see below) - and it is separate from whether a worker is active.
- **A details view for one worker**, opened with the View details button on their row and closed with Back to employee list. It shows their basic details, every semester timetable stored for them with its recurring class blocks, their shift preferences and their approved leave. Semester dates and class times can be **edited** here, a semester's timetable can be **confirmed**, shift preferences can be **set** on any existing shift, and approved leave can be **added, edited and removed**. This completes Phase 5C's employee-setup editing.
- **Supervisor entry and editing of semester timetables.** From the details view a supervisor can add a semester with inclusive start and end dates, change those dates, delete a semester behind an explicit confirmation, and add, edit and remove the recurring weekly classes inside it. Entry is entirely structured &mdash; a date picker for the semester, and a weekday plus start and end times for each class. There are no course names, no course IDs, no document uploads and no text for an AI to interpret.
  - The backend is authoritative and validates everything again: real ISO dates with the start on or before the end; no two of a worker's semesters sharing even one calendar date, because the dates are inclusive; a weekday from Monday to Sunday; real 24-hour times; a class that ends after it starts on the same day; and no class duplicating or overlapping another in the same semester, though one class may start exactly when another ends. Each operation runs in a single transaction, so a failure part-way leaves the database exactly as it was.
  - **Editing a timetable withdraws its confirmation, and confirming is a separate, deliberate action.** Adding, changing or removing a class, or changing a semester's dates, all clear that semester's confirmation, because a supervisor confirmed the timetable they saw rather than the one it has become; none of those edits, and none of creating a semester or emptying it, ever confirms anything on their own. A "Confirm timetable" control lets a supervisor confirm a populated semester as complete, and a separate, explicitly checked acknowledgement lets them confirm that a semester genuinely has no classes &mdash; the two are worded apart so neither can be mistaken for the other, and a plain or malformed submission cannot confirm either. Confirming also accepts the semester's current dates as correct, clearing a provisional-dates notice the same way editing the dates does. Confirming the same, unchanged content again is accepted, not refused, and simply records that a supervisor checked again.
  - Semester timetables are listed in date order, with inclusive start and end dates, each semester's **own** confirmation state and timestamp, and its classes as readable weekday, start and end times. A semester's own confirmation is a different question from the Timetable readiness line above it, which describes only the displayed reporting week — a semester confirmed for the spring is genuinely confirmed, and still says nothing about October.
  - Each semester also says exactly how much of the displayed week **its dates** reach: outside it, part of it, or all seven days of it. That is deliberately three states rather than a yes/no, because a semester beginning on the Wednesday overlaps the week without covering it, and two half-semesters can cover the week between them while neither covers it alone. It is a statement about dates only — separate from whether that semester was confirmed, and separate again from the worker's combined readiness across every semester they have.
  - Three easily confused situations are worded apart: **no semester at all** (missing information, not a statement that the worker has no classes), **a semester with no classes that nobody has confirmed** (still missing information), and **a confirmed semester with no classes** (a deliberate "this worker has no classes").
  - Where a timetable was moved across from the older course-based records, the semester dates are labelled **provisional**, because the old records stored no semester dates and the migration had to assume them. That fact is stored on the semester itself, so editing or even deleting every migrated class leaves it standing: losing the last migrated class must not quietly erase the knowledge that nobody chose those dates. Saving the semester's dates is what clears it, because that is a supervisor supplying them. The class days and times themselves were carried across unchanged.
  - Shift preferences list the hall and the full dated start and end of each shift, so an overnight shift shows both of its dates. Only shifts the worker has a stored preference for are listed: every other shift is neutral, which is stored as the absence of a preference rather than as a third value, and the view says so rather than listing 99 neutral shifts. Preferences are soft — a low-preference shift is still one the worker can be assigned to. **Set a shift preference** opens a picker over every existing shift and a preferred/low/neutral choice; choosing neutral deletes the stored row rather than writing a third value, and each stored row also offers **Change** to revisit it. Nothing here creates a shift or a recurring template.
  - Approved leave shows the actual dates and times each period covers, with a clear message when none is stored, and can be **added, edited and removed**. A period must end after it starts. This is leave that has already been approved, not a request; there is no approval workflow, and saving does not change or remove any assignment — reconciling leave against assignments is Phase 6 work.
  - Opening details does not disturb the list: the search text, sort selection and status filter are all exactly as you left them when you go back. While the details view is open it is the only thing you can act on, and while a form or delete confirmation is open you cannot open details — the same one-action-at-a-time rule the write controls already follow.
- Adding and editing workers from the Employees view: Add asks for a name and student type, Edit also shows the read-only employee ID, backed by `POST /api/employees` and `PUT /api/employees/{employee_code}`. The backend validates required fields, whitespace-only input and supported student types, returning clear validation and not-found errors. Duplicate IDs cannot arise on Add, because the ID is issued by the backend rather than typed. New workers start active with the 20-hour weekly limit, and no classes, preferences, leave or assignments are invented for them. A worker can be added to a completely empty database without seeding.
- **An employee ID cannot be changed once the worker exists.** Editing shows the ID read-only and offers the name and student type instead; the backend refuses any edit that submits a different ID, including one that differs only in capitalisation. The ID is what identifies a worker in schedules and reports, so renaming it would make one person appear to be two and could leave the old ID free for accidental reuse. Editing a worker's name or student type keeps their ID, their internal database identity, their active status and all of their classes, preferences, leave and assignments exactly as they were.
- **Employee IDs are assigned by the backend**, not typed in. Adding a worker asks only for their name and student type; the ID is issued when the record is saved and shown in the confirmation. IDs run SW-001, SW-002 and so on, always at least three digits, and simply grow a digit past SW-999. The sequence continues above the highest ID ever used and never fills a gap, so a deleted or edited worker's ID is never handed to anyone else. Existing stored IDs, including any that predate this scheme, are left exactly as they are.
- Deactivating and reactivating workers from the Employees view, one button per row. Deactivating preserves everything about the worker: their identity, classes, shift preferences, approved leave and past assignments are all left in place, and reactivating restores the same record rather than creating a new one. Deactivation is refused while the worker still has an assigned shift that has not finished — including one already in progress, not only shifts that have yet to start — and the refusal names those shifts and explains that nothing was cancelled. Shifts that have already ended are history and never block. Because the list defaults to the Active filter, a worker you have just deactivated disappears from the table; the confirmation message says to switch the Status filter to Inactive or All to find them again. Search, sorting and the status filter are left exactly as you set them when the list refreshes.
- Permanently deleting a worker, behind an explicit confirmation that names them, says what will be removed, and warns that it cannot be undone. Deletion removes the worker's profile, semester schedules, class blocks, shift preferences, approved leave and any legacy course rows they still carry, in a single transaction; if any step fails, nothing is removed at all. Shifts are never deleted, because a shift is a residence hall's staffing requirement shared by everyone, and no other worker's records are touched. A worker who has **any** assignment on record cannot be deleted, historical assignments included: that history would otherwise refer to nobody. The backend enforces this, not just the interface, and it checks inside the same transaction that does the deleting, so a shift assigned at the same moment cannot slip past the check. Workers with shift history are deactivated instead.
- A deleted employee ID is recorded in a retired-ID ledger, and the allocator reads that ledger when working out the next ID. A deleted worker's ID is therefore never reissued, and since IDs can no longer be typed in, there is no way to re-enter one by hand either.
- While a delete confirmation is open, it is the only thing you can act on: adding, editing, deactivating and deleting anyone else are all unavailable until you cancel or the request finishes. Searching, sorting and filtering still work, because they only change what the table shows.
- List controls on the Employees view: case-insensitive search across worker name and employee ID; sorting by employee ID, name, student type, course count, weekly class hours or remaining capacity, in either direction, with numeric columns sorted by value; an Active / Inactive / All status filter defaulting to Active; a count of matching workers; a no-results message; and a Reset control. Search, filtering and sorting all combine. They run in the browser over the already-loaded list rather than querying the backend.
- Remaining weekly capacity per worker, calculated in the backend as `max(0, weekly hour limit - assigned hours for the reporting week)`. Work shifts must be a positive whole number of hours, so assigned hours and remaining capacity are whole hours; a stored shift that breaks that rule produces a clear API error instead of an approximate figure. (Class meetings are not work and keep their 75- and 165-minute lengths.) A shift counts entirely towards the week containing its start, so a Sunday-night-into-Monday shift is not split across two weeks. This is theoretical unused capacity, not shift eligibility or availability: class hours and approved leave are deliberately not subtracted. With no assignments stored yet, every worker shows the full 20 hours.
- A SQLite database holding the synthetic dataset, created and populated by scripts in `backend/` so it can be rebuilt from scratch at any time. It stores employees, semester schedules, recurring class blocks, shifts, shift preferences, approved leave, assignments, retired employee IDs and the ID-allocation counter. **Class timetables live in the semester schedules and class blocks.** A database upgraded from the older model also still holds its `courses` and `class_meetings` rows: those are kept as evidence of where the migrated class blocks came from, and nothing reads them to run the application.
- Synthetic data for the five residence halls: 30 student workers with class schedules, shift preferences and approved leave records, plus the 99 required weekly shifts derived from the halls' operating hours. The derivation is checked by a script that confirms the 489 weekly student-coverage hours with no gaps and no double coverage.
- **Deterministic shift-eligibility logic**, with a Coverage screen in the interface: given a stored shift and worker, it decides whether that worker is eligible using only current stored records, and if not, exactly why. It checks the worker is active; that a CONFIRMED AND ACCEPTED (non-provisional) semester timetable covers every calendar date the shift touches (missing, unconfirmed, provisional/unaccepted or expired information does not establish availability - a schedule migrated with assumed dates is not coverage until a supervisor accepts them by confirming, and a deliberately confirmed, non-provisional "no classes" semester counts as coverage); that no recurring class, approved leave period or existing assignment overlaps the shift; and that adding the shift would not exceed the worker's weekly hour limit for the week its start falls in. Every applicable reason is returned, not just the first, and a preferred/low/neutral preference is reported as a fact, never as a reason a shift is unavailable. A read-only `GET /api/shifts/{shift_id}/coverage` answers "who can cover this shift", returning every worker's result plus the eligible subset, with an optional worker excluded from that subset (for replacing whoever is calling out); `GET /api/shifts` lists every stored shift for the screen's picker.
- **The Coverage screen itself**, in the sidebar: pick a stored shift through a progressive selector - hall, then a readable weekday/date, then the matching shift's time - rather than scanning or typing a raw id. Times are friendly and 12-hour, a cross-midnight shift shows both of its dates, the stable id stays secondary, and the number of matching shifts is shown once a hall and date are chosen. Choosing a shift shows its eligible and ineligible workers in two separate tables. Ineligible rows show every reason the backend returned, in its own words; eligible rows show preference, assigned hours this week, projected hours if assigned, and the weekly limit, so a low-preference worker who clears every hard rule reads as plainly eligible. An "Exclude worker" picker (for a replacement or call-out) re-requests coverage naming a worker to leave out of the eligible list - it changes no stored record and makes no assignment, and an eligible-but-excluded worker is called out with a note rather than silently disappearing. If the excluded worker no longer exists, the screen offers "Clear exclusion and retry" rather than only a plain Retry that would resend the same refused request. The same hall/day/shift picker is used by Employee Details' general "Set a shift preference" action.

**Not yet implemented:**

- The AI scheduling agent: multi-step investigation, replacement proposals, supervisor-approved execution, audit history and result verification
- Cloud deployment
- Active status affects only future eligibility, never past records. Deactivating a worker records that status and hides them from the default list view, and the eligibility logic above excludes inactive workers from eligible candidates for new assignments; it does not touch anything already recorded. Phase 7's schedule generator, proposal approval, and replacement workflow are implemented (see above), and a worker's historical assignments and coverage figures remain visible even after that worker later becomes inactive.

## Running Locally

Requires Python 3.12+ and Node.js. Two terminals.

**Backend** (from `backend/`):

```
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # PowerShell; use source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
python seed.py                       # once, on an empty database: loads the demo data
uvicorn main:app --reload --port 8000
```

**Frontend** (from `frontend/`):

```
npm install
npm run dev
```

Then open `http://localhost:5173/`. The backend's interactive API documentation is at `http://127.0.0.1:8000/docs`.

If uvicorn reports that port 8000 is already in use, an earlier backend is still running. Find and stop that one rather than picking a different port, because the frontend calls `http://127.0.0.1:8000` directly:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  ForEach-Object { Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)" } |
  Select-Object ProcessId, CommandLine
```

Confirm the command line is this project's `uvicorn main:app`, then stop it with `Stop-Process -Id <ProcessId> -Force`. When it was started with `--reload` there are two processes: a reloader parent and a worker child. Stopping only the child makes the parent immediately respawn it, so stop the **parent**, or press Ctrl+C in the terminal running it.

### About the demo data

The synthetic workforce is **initialized once, explicitly, into an empty database**:

```
python seed.py
```

After that, workers are managed through the application. The records in the database are the source of truth.

Initialization only runs when the database holds no workforce or scheduling records at all. If anything is already there, it refuses, explains what it found, and changes nothing. There is deliberately no repair, reset or force mode, and no "add the demo workers that are missing" behaviour — so re-running it can never overwrite your edits, restore a worker you deleted, or drop demo workers into a database you are managing yourself.

An existing database with the tables created but no rows is still eligible. Emptiness is judged across every workforce table, not just the employee count: a database can hold shifts or assignments with no employees, and that still counts as in use.

Everything is written in one transaction. If any part fails, the whole dataset is rolled back, so there is no half-initialized state.

Starting the backend never seeds. It creates missing tables and applies schema migrations; `CREATE TABLE IF NOT EXISTS` alone cannot update an existing table. Migrations add fields, backfill metadata and, in the case of the semester model, transform existing data. **That semester migration is implemented**: on an upgraded database it moves each stored class meeting into a semester schedule and a recurring class block, keeping the weekday and times unchanged and leaving the legacy rows in place as provenance. It runs inside one transaction, so it either completes or changes nothing, and repeating it moves nothing. Initialization is not a migration mechanism. Records live in `backend/shiftops.db` and persist across restarts. That file is deliberately not committed, so a fresh clone starts empty and can be populated manually or explicitly initialized with demo data.

To start over with fresh demo data, delete `backend/shiftops.db` and run `python seed.py` again. That is the only way to regenerate, and it is a deliberate manual act.

### Verification scripts

Run from `backend/` with the virtual environment active:

```
python verify_coverage.py      # 99 shifts totalling 489 student-coverage hours, no gaps or double coverage
python verify_preferences.py   # shift preferences match whole shifts, checked against independently listed expectations
python verify_seeding.py       # demo initialization: empty-only, atomic, refuses to re-run
python verify_migration.py     # schema migrations add columns without losing existing records
python verify_capacity.py      # assigned hours and remaining capacity, including week boundaries
python verify_employee_writes.py     # creating and editing workers, validation, and the immutable employee ID
python verify_employee_status.py     # deactivation and reactivation, and the shifts that block deactivation
python verify_employee_deletion.py   # permanent deletion, assignment protection, rollback, and retired IDs
python verify_code_allocation.py     # automatic employee IDs: sequence, reservation, concurrency, rollback
python verify_employee_api.py        # the endpoints' request/response contract, against a throwaway database
python verify_semester_migration.py  # migrating class meetings into semester schedules and class blocks
python verify_timetable_reporting.py # class summaries and timetable readiness for the reporting week
python verify_employee_details.py    # the details payload: ownership, semesters, preferences, leave
python verify_details_http.py        # the details route over a real socket: status codes, JSON, CORS
python verify_timetable_editing.py   # entering, editing and confirming semesters and classes: validation, clashes, rollback, confirmation
python verify_timetable_editing_http.py # timetable, preference and leave mutation routes over HTTP, including rejected request bodies
python verify_preference_leave_editing.py # setting shift preferences and editing approved leave: validation, ownership, rollback
python verify_eligibility.py         # deterministic shift eligibility: every hard rule, boundaries, and the Coverage query
python verify_eligibility_http.py    # the Coverage route over a real socket: status codes, JSON, CORS
```

Each script uses its own in-memory or temporary database and never opens
`backend/shiftops.db`, so they all work on a fresh checkout before any
database exists.
The employee API, timetable-reporting, employee-details and timetable-editing
scripts call endpoint functions against disposable databases; they do not
exercise HTTP routing, wire serialization, or CORS. `verify_details_http.py`
and `verify_timetable_editing_http.py` start real Uvicorn servers on
operating-system-assigned free ports against temporary databases. They cover
routing, status codes, JSON serialization and CORS for the details route and
the timetable, confirmation, preference and leave mutation routes. The
editing checks also verify that non-object JSON and missing bodies receive
400 with a string `detail` on the create/update routes, without changing
stored records. Malformed JSON syntax and invalid path-parameter types remain
FastAPI validation errors (422).
Migration concurrency checks use separate SQLite connections in threads;
they cover concurrent execution without guaranteeing a particular interleaving.

## Planned Capabilities

Development order: Phase 5B employee lifecycle controls are complete, and
**Phase 5C - complete employee setup - is functionally complete**: automatic
employee IDs, the semester/class-block migration, the employee-details view,
semester/class editing, timetable confirmation, shift-preference editing and
approved-leave editing are all built. **Phase 6 (deterministic eligibility
and Coverage) and Phase 7 (schedule optimization, proposal/approval,
replacement, and the Schedule frontend) are both complete
as well** - see the bullet list below. **Phase 8 (Dashboard and Workforce
Planning analytics) is also complete.** Later phases must use the managed
workforce and confirmed semester data, not fixed demo counts. Full
checklists live in the project context; public cross-phase requirements are
in `docs/PROJECT_SPEC.md` section 10.

Phase 9 (the approved AI scheduling agent) is next; it does not exist yet.

Goals for the finished application are listed below. Employee records, shift preferences and approved leave are stored and editable, and the workforce summary and individual details are viewable. Semester and class editing, timetable confirmation, and preference and leave editing are all implemented. Deterministic eligibility/coverage and schedule evaluation/generation (optimizer draft, stored proposal, explicit approval, and explicit replacement) are also implemented. Dashboard and Workforce Planning analytics are implemented; only the AI-agent goal below remains planned.

- Workforce and coverage dashboard
- Complete employee setup, **built**: supervisor entry of semester dates and recurring class times without course names; explicit confirmation that a timetable is complete, including a deliberate "no classes"; editing shift preferences on any existing shift; and adding, editing and removing approved leave. Incomplete class information will block scheduling until confirmed.
- Class schedule conflict detection
- Shift eligibility analysis
- Automated schedule generation
- Coverage-gap identification
- Workforce capacity analysis
- AI scheduling agent with an approved call-out replacement workflow; optional RAG for fictional reference documents and optional MCP integration after core deployment

## Technology Stack

**In use:**

- **Frontend:** React, JavaScript, Vite
- **Backend:** Python, FastAPI, Uvicorn
- **Database:** SQLite, accessed with Python's standard-library `sqlite3` and plain SQL (no ORM)
- **API:** REST/JSON (health, workforce and single-worker reads, plus employee create, update, status changes and deletion)
- **Version Control:** Git and GitHub

**Planned:**

- **Optimization:** Google OR-Tools
- **AI:** LLM API integration
- **Deployment:** Microsoft Azure

## Data and Scenario Disclaimer

ShiftOps AI uses a fictional workforce scheduling scenario inspired by general operational scheduling challenges in university housing.

All employee records, schedules, shift preferences, leave records, staffing requirements, residence hall names, policies, and operational rules used by the application are synthetic and should not be interpreted as actual University of Texas at Dallas data or policies.

See [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md) for the detailed project specification.
