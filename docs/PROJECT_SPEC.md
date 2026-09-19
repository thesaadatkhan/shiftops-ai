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

A schedule may still be infeasible because individual workers may not be available during the specific periods in which coverage is required.

The workforce size should eventually be configurable for workforce-planning scenarios.

---

## 6. Worker Scheduling Constraints

A student worker may only be assigned to a shift when all applicable constraints are satisfied.

### Personal Availability

The worker must have indicated that they are available during the complete shift.

### Class Schedule

The shift cannot conflict with the worker's class schedule.

Class schedules and personal availability are modeled separately.

Being outside of class does not automatically imply that a worker is available.

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

### Employees

Allow supervisors to view synthetic workers and relevant scheduling information, including:

- Worker details
- Current scheduled hours
- Personal availability
- Class schedules
- Existing assignments

### Schedule

Display generated or existing assignments by:

- Residence hall
- Date
- Time
- Employee

### Coverage

Allow the user to inspect a shift and determine which employees are eligible to cover it.

Where useful, the application should explain why a worker is eligible or ineligible.

Examples include:

- Available
- Personal availability conflict
- Class conflict
- Existing shift conflict
- Weekly hour limit exceeded

### Schedule Generation

Use constraint-based optimization to assign eligible workers to required shifts.

The scheduling engine should respect all defined constraints and identify cases where complete coverage cannot be achieved.

### Workforce Planning

Provide capacity analysis and allow workforce-size scenarios to be evaluated.

The application should distinguish between:

1. Theoretical staffing capacity
2. Actual staffing feasibility under worker availability and scheduling constraints

For example, `ceil(required_hours / weekly_hour_limit)` provides a theoretical lower bound, but does not prove that a feasible schedule exists.

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
- Personal availability
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

The residence hall names, employee records, class schedules, availability, staffing requirements, and operational rules used by the application are synthetic.