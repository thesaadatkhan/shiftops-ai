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

- A React application shell (Vite + JavaScript) with sidebar navigation covering all planned sections (Dashboard, Employees, Schedule, Coverage, Generate Schedule, Workforce Planning, AI Assistant). Dashboard and Employees show real data; the remaining five sections are still placeholders.
- A FastAPI backend covering the whole employee lifecycle across seven endpoints:

  | Endpoint | Purpose |
  |---|---|
  | `GET /api/health` | backend connection check |
  | `GET /api/employees` | read the workforce with weekly capacity figures |
  | `POST /api/employees` | create a worker |
  | `PUT /api/employees/{employee_code}` | edit a worker's name and student type |
  | `POST /api/employees/{employee_code}/deactivate` | make a worker inactive |
  | `POST /api/employees/{employee_code}/reactivate` | make a worker active again |
  | `DELETE /api/employees/{employee_code}` | permanently delete a worker |

  Every write returns errors in one consistent `{"detail": ...}` shape.
- Frontend/backend integration: when the Dashboard opens it calls `GET /api/health` once and displays the resulting connection status (loading, connected, or unavailable); the Employees section loads the workforce from `GET /api/employees` with the same loading and error handling. CORS is configured on the backend for the local frontend origin.
- An Employees view listing one summary row per worker: employee ID, name, student type, course count, class-meeting count, weekly class hours, weekly hour limit, and a count of approved leave periods. The individual class meetings, shift preferences and leave periods behind those figures are stored in the database but are not yet displayed anywhere in the interface.
- Adding and editing workers from the Employees view: forms for employee ID, name and student type, backed by `POST /api/employees` and `PUT /api/employees/{employee_code}`. The backend validates required fields, whitespace-only input, supported student types and unique employee codes, returning clear validation, duplicate-code and not-found errors. New workers start active with the 20-hour weekly limit, and no classes, preferences, leave or assignments are invented for them. A worker can be added to a completely empty database without seeding.
- **An employee ID cannot be changed once the worker exists.** Editing shows the ID read-only and offers the name and student type instead; the backend refuses any edit that submits a different ID, including one that differs only in capitalisation. The ID is what identifies a worker in schedules and reports, so renaming it would make one person appear to be two and could leave the old ID free for accidental reuse. Editing a worker's name or student type keeps their ID, their internal database identity, their active status and all of their classes, preferences, leave and assignments exactly as they were.
- Employee IDs are still typed in by hand when adding a worker, which is temporary: Phase 5C will have the backend allocate them. While they are typed, an ID may contain only letters, digits, hyphens and underscores, so that every saved ID can still be addressed by the edit request. Existing stored IDs are left exactly as they are.
- Deactivating and reactivating workers from the Employees view, one button per row. Deactivating preserves everything about the worker: their identity, classes, shift preferences, approved leave and past assignments are all left in place, and reactivating restores the same record rather than creating a new one. Deactivation is refused while the worker still has an assigned shift that has not finished — including one already in progress, not only shifts that have yet to start — and the refusal names those shifts and explains that nothing was cancelled. Shifts that have already ended are history and never block. Because the list defaults to the Active filter, a worker you have just deactivated disappears from the table; the confirmation message says to switch the Status filter to Inactive or All to find them again. Search, sorting and the status filter are left exactly as you set them when the list refreshes. Permanent deletion does not exist yet.
- Permanently deleting a worker, behind an explicit confirmation that names them, says what will be removed, and warns that it cannot be undone. Deletion removes the worker's profile, courses, class meetings, shift preferences and approved leave in a single transaction; if any step fails, nothing is removed at all. Shifts are never deleted, because a shift is a residence hall's staffing requirement shared by everyone, and no other worker's records are touched. A worker who has **any** assignment on record cannot be deleted, historical assignments included: that history would otherwise refer to nobody. The backend enforces this, not just the interface, and it checks inside the same transaction that does the deleting, so a shift assigned at the same moment cannot slip past the check. Workers with shift history are deactivated instead. A deleted employee ID is recorded as retired and will not be issued to anyone else.
- While a delete confirmation is open, it is the only thing you can act on: adding, editing, deactivating and deleting anyone else are all unavailable until you cancel or the request finishes. Searching, sorting and filtering still work, because they only change what the table shows.
- List controls on the Employees view: case-insensitive search across worker name and employee ID; sorting by employee ID, name, student type, course count, weekly class hours or remaining capacity, in either direction, with numeric columns sorted by value; an Active / Inactive / All status filter defaulting to Active; a count of matching workers; a no-results message; and a Reset control. Search, filtering and sorting all combine. They run in the browser over the already-loaded list rather than querying the backend.
- Remaining weekly capacity per worker, calculated in the backend as `max(0, weekly hour limit - assigned hours for the reporting week)`. Work shifts must be a positive whole number of hours, so assigned hours and remaining capacity are whole hours; a stored shift that breaks that rule produces a clear API error instead of an approximate figure. (Class meetings are not work and keep their 75- and 165-minute lengths.) A shift counts entirely towards the week containing its start, so a Sunday-night-into-Monday shift is not split across two weeks. This is theoretical unused capacity, not shift eligibility or availability: class hours and approved leave are deliberately not subtracted. With no assignments stored yet, every worker shows the full 20 hours.
- A SQLite database holding the synthetic dataset, created and populated by scripts in `backend/` so it can be rebuilt from scratch at any time. It stores employees, courses, class meetings, shifts, shift preferences, approved leave, and assignments.
- Synthetic data for the five residence halls: 30 student workers with class schedules, shift preferences and approved leave records, plus the 99 required weekly shifts derived from the halls' operating hours. The derivation is checked by a script that confirms the 489 weekly student-coverage hours with no gaps and no double coverage.

**Not yet implemented:**

- Shift-eligibility logic. The database records class schedules, approved leave and shifts, but nothing yet evaluates whether a given worker may take a given shift.
- Constraint-based schedule optimization, and therefore any actual shift assignments
- Workforce capacity analytics
- The AI scheduling agent: multi-step investigation, replacement proposals, supervisor-approved execution, audit history and result verification
- Cloud deployment
- Detailed employee setup. Class schedules, shift preferences and approved leave are stored and counted, but there is no interface for entering or editing them; the only ones that exist came from the demo data. Semester dates and recurring class blocks are planned for Phase 5C.
- Automatic employee IDs. Codes are still typed by hand when adding a worker. Deleted IDs are already recorded as retired so a future automatic allocator can avoid reissuing them, but that allocator is planned (Phase 5C), not built - nothing currently stops you re-entering a retired ID yourself.
- Anything that acts on a worker's active status. Deactivating a worker records that status and hides them from the default list view, but no other part of the application reads it yet, because neither the eligibility logic nor the schedule generator exists. Excluding inactive workers from new assignments is a requirement for those later phases, not current behaviour.

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

Starting the backend never seeds. It creates missing tables and applies schema migrations; `CREATE TABLE IF NOT EXISTS` alone cannot update an existing table. Current migrations add fields and backfill metadata. Future semester-model migrations must preserve worker records and relationships while transforming their representation; initialization is not a migration mechanism. Records live in `backend/shiftops.db` and persist across restarts. That file is deliberately not committed, so a fresh clone starts empty and can be populated manually or explicitly initialized with demo data.

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
```

Each script uses its own in-memory or temporary database and never opens
`backend/shiftops.db`, so they all work on a fresh checkout before any
database exists.

## Planned Capabilities

Development order: Phase 5B employee lifecycle controls are complete; next is
Phase 5C automatic IDs and complete semester-based worker setup, then
eligibility, optimization, reporting and AI. None of those exist yet. Later phases must use the managed workforce
and confirmed semester data, not fixed demo counts. Full checklists live in
the project context; public cross-phase requirements are in
`docs/PROJECT_SPEC.md` section 10.

Goals for the finished application. Employee records, shift preferences and approved leave are now stored, and worker summaries are viewable; everything below that depends on displaying those details, or on evaluating or generating a schedule, is still future work.

- Workforce and coverage dashboard
- Complete employee setup: automatic read-only employee IDs; supervisor entry of semester dates and recurring class times without course names; editing shift preferences and already-approved leave. Incomplete class information will block scheduling until confirmed. This is planned Phase 5C work, not part of the current basic Add/Edit form.
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
- **API:** REST/JSON (health and employee reads, plus employee create and update)
- **Version Control:** Git and GitHub

**Planned:**

- **Optimization:** Google OR-Tools
- **AI:** LLM API integration
- **Deployment:** Microsoft Azure

## Data and Scenario Disclaimer

ShiftOps AI uses a fictional workforce scheduling scenario inspired by general operational scheduling challenges in university housing.

All employee records, schedules, shift preferences, leave records, staffing requirements, residence hall names, policies, and operational rules used by the application are synthetic and should not be interpreted as actual University of Texas at Dallas data or policies.

See [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md) for the detailed project specification.
