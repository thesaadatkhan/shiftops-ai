# ShiftOps AI - Project Specification

## 1. Overview

ShiftOps AI is a full-stack AI-assisted workforce scheduling and shift coverage application.

The application models a fictional university housing front-desk operation in which professional staff and student workers provide coverage across multiple residence halls.

The system is intended to demonstrate:

- Full-stack web application development
- REST API integration
- Relational workforce data management
- Constraint-based schedule optimization
- Workforce capacity analysis
- Natural-language AI interaction
- Cloud deployment

All application data and operational rules are synthetic.

---

## 2. Operational Scenario

The simulation contains five fictional residence halls:

1. Andromeda
2. Capella
3. Vega
4. Helix
5. Sirius

### Desk Operating Hours

**Andromeda and Capella**

Open 24 hours per day, seven days per week.

**Vega, Helix, and Sirius**

Open from 8:00 AM until 2:00 AM.

---

## 3. Professional Staff Coverage

Professional staff cover all five residence hall desks:

- Monday through Friday
- 8:00 AM to 5:00 PM

Student workers provide the remaining required coverage.

---

## 4. Student Worker Coverage

Student workers cover:

- Weekday evening hours beginning at 5:00 PM
- Overnight hours where required
- All required weekend desk hours

Based on the simulated operating schedule, student workers must collectively provide:

**489 desk-coverage hours per week.**

---

## 5. Student Workforce

The initial simulation contains:

- 30 synthetic student workers
- Maximum 20 scheduled work hours per worker per week

Therefore:

- Theoretical maximum workforce capacity: 600 hours/week
- Required desk coverage: 489 hours/week
- Theoretical excess capacity: 111 hours/week
- Average required assignment across 30 workers: 16.3 hours/week

These figures describe aggregate capacity only.

A schedule may still be infeasible because class conflicts, approved leave, assignment conflicts, and weekly hour limits restrict who can cover particular periods.

The workforce size should eventually be configurable for workforce-planning scenarios.

---

## 6. Worker Scheduling Constraints

A student worker may only be assigned to a shift when all applicable constraints are satisfied.

### Shift Preferences

Workers indicate preferred and low-preference shifts. Unspecified preferences are neutral. Preferences are soft: a low-preference shift remains assignable when all hard constraints are satisfied. Students do not freely declare themselves unavailable outside class; non-class absences require approved leave.

### Class Schedule

Any overlap with a class makes the worker ineligible for the entire shift as defined. For example, an 8:00-9:00 AM class blocks both an 8:00 AM-1:00 PM shift and an 8:00 AM-2:00 PM shift. The system must not silently split an existing shift to bypass a conflict.

Synthetic class schedules follow a fictional course-load convention, not a real institutional requirement: undergraduate workers are modeled with exactly 3 courses, each meeting twice per week for 1 hour 15 minutes (6 weekly meetings, 7.5 class hours); master's workers are modeled with at most 3 courses, each meeting once per week for 2 hours 45 minutes (up to 3 weekly meetings, up to 8.25 class hours).

### Approved Leave and Shift Changes

Under the synthetic simulation rules, students must request leave or a different shift at least one week in advance and obtain approval. Advance notice alone is not approval.

Approved leave blocks any shift overlapping the approved period. A pending request does not automatically block scheduling. A different-shift request requires an approved assignment change; it does not imply all-day leave. Approval cannot override class conflicts, assignment conflicts, or the weekly hour limit.

Store class schedules, shift preferences, and approved leave separately. Being outside class does not guarantee eligibility: all other hard constraints still apply.

Shift preferences and approved leave periods are stored. A request/approval interface, automated enforcement of the notice rule, pending-request tracking, and shift-change workflows are not implemented and are not required by the current data model. These are fictional rules, not claims about an actual institution's policies.

### Work Shift Durations

Every work shift must last a positive whole number of hours. Zero-length, negative, and fractional durations are invalid stored data. The system must reject them rather than rounding or truncating them into valid-looking values, because a silently corrected duration produces a capacity figure that is wrong without appearing wrong.

Because individual shifts are whole hours, each worker's assigned hours and remaining weekly capacity are also whole hours.

This rule governs **work** only. Class meetings are not work and legitimately last 75 minutes (undergraduate) or 165 minutes (master's). The workforce-wide figure of 16.3 hours per worker per week remains valid: it is an average across 30 workers, not an individual shift or assignment length.

### Assignment Conflicts

A worker cannot be assigned to overlapping shifts.

### Weekly Hour Limit

A worker cannot exceed 20 scheduled work hours within the applicable week.

### Coverage Requirement

Each required desk shift requires exactly one worker unless the simulation explicitly specifies otherwise.

The system should report uncovered shifts or an infeasible schedule rather than silently violating scheduling constraints.

---

## 7. Typical Shift Patterns

Shift lengths are flexible, but common simulated patterns include the following.

### Weekdays

- 5:00 PM - 10:00 PM
- 10:00 PM - 2:00 AM
- 10:00 PM - 3:00 AM
- 2:00/3:00 AM - 8:00 AM for 24-hour desks

### Weekends

Common daytime blocks may include:

- 8:00 AM - 1:00 PM
- 8:00 AM - 2:00 PM
- 1:00 PM - 5:00 PM
- 2:00 PM - 6:00 PM

Evening and overnight blocks continue as required by each desk's operating hours.

These patterns are templates rather than universal hard-coded shift lengths.

The application should support shifts of different durations where necessary.

Shift generation must collectively cover all required desk operating periods without unintended gaps or double coverage.

Shifts that cross midnight must be represented using correct start and end date-times.

---

## 8. Application Features

### Dashboard

Provide a high-level operational overview.

Potential metrics include:

- Number of active desks
- Number of student workers
- Required weekly coverage hours
- Maximum theoretical workforce capacity
- Scheduled hours
- Unfilled shifts
- Coverage percentage

When week selection is implemented, weekly dashboard metrics should use the same reporting week as Employees and Schedule. This alignment is planned for Phase 8.

### Employees

Allow supervisors to view synthetic workers and relevant scheduling information, including:

- Worker details
- Current scheduled hours
- Shift preferences
- Approved leave periods
- Class schedules
- Existing assignments

Implemented list controls (Phase 5B):

- Search by employee name or employee code, ignoring letter case.
- Sort by name, employee code, student type, course count, weekly class hours, or remaining weekly capacity, ascending or descending, with a stable employee-code tie-breaker. Numeric columns sort by value, so 10 follows 9 rather than preceding it.
- Filter by Active, Inactive, or All, showing active workers by default. Filtering changes only which rows are displayed; it does not alter status, eligibility, or records.
- Show the number of matching workers, a message when nothing matches, and a control that resets every list control at once.

Remaining employee management is planned scope (Phase 5B), not yet implemented:

- Add and edit worker details through forms backed by REST endpoints and SQLite. Keep the employee's internal identity stable when editing details and validate required fields and unique employee codes on the backend.
- Deactivate workers without deleting their classes, preferences, leave, or assignment history. Inactive workers must be excluded from new assignments and future schedule generation. Existing assignments must never silently disappear; if future assignments exist, block deactivation until they are explicitly resolved.
- Allow reactivation. (The Active/Inactive/All filter itself is implemented; the actions that change a worker's status are not.)
- Permanently delete an employee only after explicit confirmation and only when no assignment references that employee. Remove dependent class, preference, and leave records transactionally; preserve shared shifts. Workers with assignment history should be deactivated instead.

The database is the source of truth after explicit demo initialization. Normal startup must not seed or overwrite workers, and later seeding must not resurrect intentionally deleted demo employees. Provide an empty-workforce path for entering fictional workers through the interface. The 30-worker count, student-type mix, and course-load conventions constrain demo generation, not user-created records. The synthetic-only data policy and 20-hour weekly limit remain unchanged.

Phase 5B initially covers list controls and worker identity/status management. Editing class schedules, shift preferences, and approved leave through the interface is a separate follow-up; no leave approval workflow is implied.

Employees is a worker-management and summary view, not a full shift calendar. A simple reporting-week selector (previous/next week or a date picker) is planned alongside the Schedule interface in Phase 7. It will update assigned hours and remaining weekly capacity for the selected week; it must not change stored worker identity, student type, active status, or records. The currently displayed fixed sample week is sufficient for Phase 5B.

### Schedule

Display generated or existing assignments by:

- Residence hall
- Date
- Time
- Employee

The full shift calendar/timetable belongs in Schedule, showing assignments and uncovered shifts for the selected week. Employees and Schedule should share one reporting-week selection so moving between them preserves the reporting period. Backend requests must use that same week and return clear reporting dates, retaining D025's start-week accounting for cross-midnight assignments. Week selection changes the view and calculations; it does not create shifts, generate assignments, or edit data. Implement this in Phase 7, not during Phase 5B.

### Coverage

Allow the user to inspect a shift and determine which employees are eligible to cover it.

Where useful, the application should explain why a worker is eligible or ineligible.

Examples include:

- Eligible
- Approved leave conflict
- Class conflict
- Existing shift conflict
- Weekly hour limit exceeded

### Schedule Generation

Use constraint-based optimization to assign eligible workers to required shifts.

The scheduling engine should respect all hard constraints and identify cases where complete coverage cannot be achieved. Within those constraints, prioritize coverage and favor preferred shifts over low-preference shifts. A low preference alone must not make a shift uncovered or a worker ineligible.

### Workforce Planning

Provide capacity analysis and allow workforce-size scenarios to be evaluated.

The application should distinguish between:

1. Theoretical staffing capacity
2. Actual staffing feasibility under class schedules, approved leave, and other hard scheduling constraints

For example, `ceil(required_hours / weekly_hour_limit)` provides a theoretical lower bound, but does not prove that a feasible schedule exists.

#### Remaining Weekly Capacity

Remaining weekly capacity for a worker is defined as:

`max(0, weekly_hour_limit - assigned hours for the selected week)`

Assigned hours are calculated from stored assignments and the start/end date-times of the shifts they reference. Because work shifts are whole-hour blocks, assigned hours and remaining capacity are whole hours; invalid stored durations cause a controlled API error rather than an approximate figure. A shift counts entirely towards the week containing its start, so a shift running from Sunday night into Monday morning contributes all of its hours to the week the Sunday belongs to, and none to the following week. Hours are never split across two weeks.

This figure is theoretical unused capacity only. It is explicitly **not** shift eligibility and **not** personal availability. Class hours and approved leave are **not** subtracted from the weekly limit, because neither consumes any part of a worker's 20 scheduled work hours. A worker may show remaining capacity and still be ineligible for a particular shift because of a class conflict, approved leave, an assignment conflict, or the weekly hour limit itself.

Active status is a separate concept from capacity. An inactive worker may still show remaining capacity; excluding inactive workers from scheduling is a matter of their status, not their capacity.

### AI Assistant

Provide a natural-language interface for operational questions.

Example questions may include:

- Who can cover this shift?
- How many hours is a particular worker scheduled?
- Which shifts are currently uncovered?
- Which periods are hardest to staff?
- How much unused workforce capacity remains?
- Can a particular employee cover a particular shift?

The language model should interpret the user's request while scheduling facts and calculations come from application data, deterministic business logic, and the optimization engine.

---

## 9. Planned Architecture

### Frontend

React with JavaScript and Vite.

Responsibilities include:

- User interface
- Navigation
- Dashboard visualization
- Schedule display
- Coverage interactions
- AI chat interface

### Backend

Python with FastAPI.

Responsibilities include:

- REST endpoints
- Business logic
- Database access
- Eligibility calculations
- Optimization
- AI integration

### API

Frontend and backend communicate using REST requests and JSON responses.

### Database

SQLite is used during initial development.

Structured entities are expected to include:

- Employees
- Class schedules
- Shift preferences
- Approved leave periods
- Shifts
- Assignments

### Optimization

Google OR-Tools is planned for constraint-based schedule generation.

### AI

An LLM API will provide the natural-language interface.

The API key must remain server-side and must never be exposed in frontend code or committed to Git.

### Optional RAG

Retrieval-Augmented Generation may later be added using synthetic policy or reference documents.

RAG is not the source of truth for structured scheduling data.

### Deployment

The completed application is intended to be deployed using Microsoft Azure services.

---

## 10. Architecture Principle

Structured operational truth should flow through:

**Database -> deterministic application logic / optimizer -> API -> user interface**

The LLM should provide a natural-language interface to reliable application capabilities rather than independently inventing scheduling facts.

---

## 11. Data Disclaimer

This project does not use real employee records, private university data, internal operational documents, or actual institutional scheduling policies.

The residence hall names, employee records, class schedules, shift preferences, approved leave, staffing requirements, and operational rules used by the application are synthetic.
