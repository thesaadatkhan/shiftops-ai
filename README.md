# ShiftOps AI

ShiftOps AI is an AI-assisted workforce scheduling and shift coverage platform built as a full-stack web application.

The application demonstrates how structured workforce data, constraint-based schedule optimization, and a natural-language AI interface can be combined to support operational scheduling decisions.

## Project Status

ShiftOps AI is in early active development. It currently runs locally only and is not deployed.

**Implemented:**

- A React application shell (Vite + JavaScript) with sidebar navigation covering all planned sections (Dashboard, Employees, Schedule, Coverage, Generate Schedule, Workforce Planning, AI Assistant). Dashboard and Employees show real data; the remaining five sections are still placeholders.
- A FastAPI backend with two read-only endpoints: `GET /api/health` and `GET /api/employees`.
- Frontend/backend integration: when the Dashboard opens it calls `GET /api/health` once and displays the resulting connection status (loading, connected, or unavailable); the Employees section loads the workforce from `GET /api/employees` with the same loading and error handling. CORS is configured on the backend for the local frontend origin.
- An Employees view listing one summary row per worker: employee ID, name, student type, course count, class-meeting count, weekly class hours, weekly hour limit, and a count of approved leave periods. The individual class meetings, shift preferences and leave periods behind those figures are stored in the database but are not yet displayed anywhere in the interface.
- A SQLite database holding the synthetic dataset, created and populated by scripts in `backend/` so it can be rebuilt from scratch at any time. It stores employees, courses, class meetings, shifts, shift preferences, approved leave, and assignments.
- Synthetic data for the five residence halls: 30 student workers with class schedules, shift preferences and approved leave records, plus the 99 required weekly shifts derived from the halls' operating hours. The derivation is checked by a script that confirms the 489 weekly student-coverage hours with no gaps and no double coverage.

**Not yet implemented:**

- Shift-eligibility logic. The database records class schedules, approved leave and shifts, but nothing yet evaluates whether a given worker may take a given shift.
- Constraint-based schedule optimization, and therefore any actual shift assignments
- Workforce capacity analytics
- The natural-language AI assistant
- Cloud deployment
- Employee search and sorting, active/inactive filters, and add/edit/deactivate/reactivate/delete controls. These are planned for Phase 5B, not implemented. Deactivation preserves history; permanent deletion will be restricted to workers without assignments.

## Running Locally

Requires Python 3.12+ and Node.js. Two terminals.

**Backend** (from `backend/`):

```
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # PowerShell; use source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
python seed.py                       # first run only: creates and populates the database
uvicorn main:app --reload --port 8000
```

**Frontend** (from `frontend/`):

```
npm install
npm run dev
```

Then open `http://localhost:5173/`. The backend's interactive API documentation is at `http://127.0.0.1:8000/docs`.

### About the demo data

The synthetic workforce is **seeded explicitly, never automatically**. Starting the backend only creates any missing tables; it does not generate or regenerate workers. Records live in a SQLite file (`backend/shiftops.db`) and persist across restarts. That file is deliberately not committed, so a fresh clone starts empty until you run the seed script.

```
python seed.py            # ordinary seeding - safe to run at any time
python seed.py --repair   # destructive - overwrites the seeded workers
```

Ordinary `python seed.py` only fills in what is missing: it creates seed workers that are absent, and skips workers that already exist without touching their details, classes, leave or preferences. Running it repeatedly is harmless and creates no duplicates.

`python seed.py --repair` is the destructive counterpart. It rewrites the seed workers' names, courses, class meetings, approved leave and shift preferences from the generator, **deleting any rows the generator does not produce** — including records you added yourself. It prints exactly what it will replace before running. Use it only when you have changed the generator and want its values to overwrite what is stored. It never touches shift assignments, the shift list, or workers the generator does not produce.

### Verification scripts

Run from `backend/` with the virtual environment active:

```
python verify_coverage.py      # 99 shifts totalling 489 student-coverage hours, no gaps or double coverage
python verify_preferences.py   # shift preferences match whole shifts, checked against independently listed expectations
python verify_seeding.py       # seeding safety, run against a temporary in-memory database
```

## Planned Capabilities

Goals for the finished application. Employee records, shift preferences and approved leave are now stored, and worker summaries are viewable; everything below that depends on displaying those details, or on evaluating or generating a schedule, is still future work.

- Workforce and coverage dashboard
- Class schedule conflict detection
- Shift eligibility analysis
- Automated schedule generation
- Coverage-gap identification
- Workforce capacity analysis
- Natural-language scheduling assistant

## Technology Stack

**In use:**

- **Frontend:** React, JavaScript, Vite
- **Backend:** Python, FastAPI, Uvicorn
- **Database:** SQLite, accessed with Python's standard-library `sqlite3` and plain SQL (no ORM)
- **API:** REST/JSON (read-only health and employee endpoints)
- **Version Control:** Git and GitHub

**Planned:**

- **Optimization:** Google OR-Tools
- **AI:** LLM API integration
- **Deployment:** Microsoft Azure

## Data and Scenario Disclaimer

ShiftOps AI uses a fictional workforce scheduling scenario inspired by general operational scheduling challenges in university housing.

All employee records, schedules, shift preferences, leave records, staffing requirements, residence hall names, policies, and operational rules used by the application are synthetic and should not be interpreted as actual University of Texas at Dallas data or policies.

See [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md) for the detailed project specification.
