# ShiftOps AI

ShiftOps AI is an AI-assisted workforce scheduling and shift coverage platform built as a full-stack web application.

The application demonstrates how structured workforce data, constraint-based schedule optimization, and a natural-language AI interface can be combined to support operational scheduling decisions.

## Project Status

ShiftOps AI is in early active development. It currently runs locally only and is not deployed.

**Implemented:**

- A static React application shell (Vite + JavaScript) with sidebar navigation covering all planned sections (Dashboard, Employees, Schedule, Coverage, Generate Schedule, Workforce Planning, AI Assistant). Each section currently shows placeholder content only.
- A FastAPI backend with a single working endpoint, `GET /api/health`, used to verify the backend runs correctly.

**Not yet implemented:**

- Frontend/backend integration (the React app does not call the backend yet)
- A structured database and synthetic workforce data
- Coverage and shift-eligibility logic
- Constraint-based schedule optimization
- Workforce capacity analytics
- The natural-language AI assistant
- Cloud deployment

## Planned Capabilities

The following are goals for the finished application; none are implemented yet.

- Workforce and coverage dashboard
- Employee availability management
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
- **API:** REST/JSON (implemented on the backend; not yet consumed by the frontend)
- **Version Control:** Git and GitHub

**Planned:**

- **Database:** SQLite
- **Optimization:** Google OR-Tools
- **AI:** LLM API integration
- **Deployment:** Microsoft Azure

## Data and Scenario Disclaimer

ShiftOps AI uses a fictional workforce scheduling scenario inspired by general operational scheduling challenges in university housing.

All employee records, schedules, availability, staffing requirements, residence hall names, policies, and operational rules used by the application are synthetic and should not be interpreted as actual University of Texas at Dallas data or policies.

See [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md) for the detailed project specification.
