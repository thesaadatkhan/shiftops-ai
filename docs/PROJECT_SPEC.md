# ShiftOps AI - Project Specification

## 1. Overview

ShiftOps AI is a full-stack AI-assisted workforce scheduling and shift coverage application.

Its target product is a housing-operations scheduling agent: a supervisor can
ask it to investigate staffing gaps, find replacements, propose a feasible
change, and carry out that specific change after approval. Employee management,
eligibility rules and optimization supply its reliable operational tools.
This agent is planned Phase 9 work, not an implemented capability today.

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

The planned Phase 6/7 rules require active status and confirmed semester
timetable coverage for the full shift. Missing, unconfirmed or expired class
information is not unrestricted availability; explicitly confirmed no classes
is valid. These checks are not implemented by the current lifecycle endpoints.

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

This rule governs **work** only. Demo class meetings last 75 minutes (undergraduate) or 165 minutes (master's); these lengths do not constrain manually entered semester blocks. The workforce-wide figure of 16.3 hours per worker per week remains valid for the original demo: it is an average across 30 workers, not an individual shift or assignment length.

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
- Sort by name, employee code, student type, class-block count, weekly class hours, or remaining weekly capacity, ascending or descending, with a stable employee-code tie-breaker. Numeric columns sort by value, so 10 follows 9 rather than preceding it.
- Filter by Active, Inactive, or All, showing active workers by default. Filtering changes only which rows are displayed; it does not alter status, eligibility, or records.
- Show the number of matching workers, a message when nothing matches, and a control that resets every list control at once.

- Add and edit worker details through forms backed by REST endpoints and SQLite. The backend validates required fields, rejects whitespace-only values, accepts only supported student types, and rejects an employee code already used by another worker (compared without regard to letter case). New workers start active with the 20-hour weekly limit and receive no generated classes, preferences, leave, or assignments. A worker can be added to an empty database without seeding.
- An existing employee code cannot be edited. The Edit interface displays the stored code read-only, and the update operation rejects any submitted code that differs from it, including a change of letter case only, returning the standard validation error with an explanation. An unknown code in the request path is still reported as not found rather than as an immutability failure. Editing a worker's name or student type preserves their employee code, internal database identity, active status, semester schedules, class blocks, any legacy course records, shift preferences, approved leave and assignments. There is no rename workflow, and editing never retires a code; retirement applies to permanent deletion only.
- Employee codes are assigned by the backend when a worker is created. The create request carries the worker's name and student type only; a request that supplies a code is rejected with a validation error explaining that codes are assigned automatically, rather than having the supplied value silently ignored. The response reports the code that was issued.
- Codes follow a single sequence: `SW-` and at least three digits, zero-padded, continuing to more digits when the sequence passes 999. A database that has never issued a code starts at SW-001. Otherwise the sequence continues above the highest number already reserved by a live or retired code, and never fills a gap left by a deletion. Allocation progress is stored, so it survives restarts and the deletion of the highest-numbered worker.
- Codes are allocated and the worker created in one transaction, serialized by the database's own write locking, so simultaneous creates cannot receive the same code and a failure leaves neither a partially created worker nor an advanced counter.
- A stored code counts towards the sequence when it is `SW-` followed entirely by digits, regardless of letter case or zero padding. Any other stored code is preserved and never interpreted as a sequence value. No existing worker is ever renumbered, and no stored code is rewritten.
- Demo initialization reserves the numbers its codes occupy, so a worker added afterwards continues above them. A database in which codes have already been issued counts as in use, and initialization refuses, even if every worker has since been deleted.

- While a permanent-deletion confirmation is open it is the only employee action available. Adding, editing, deactivating, reactivating and deleting any other worker are all unavailable until the supervisor cancels or the request resolves, enforced in the application's own action handlers and not only by disabling controls. Cancelling remains available throughout. Searching, sorting and status filtering stay usable, because they change only what is displayed.
- Deactivate and reactivate workers, one control per row, backed by REST endpoints. Deactivating changes only the active flag: the worker's internal identity, classes, shift preferences, approved leave and assignment history are all preserved, and reactivation restores the same record rather than creating a replacement. Deactivation is blocked while the worker holds an assigned shift that has not finished — a shift already in progress counts, not only shifts whose start is still ahead — and the refusal names those shifts and states that deactivating would not have cancelled them. Nothing is deleted, cancelled or reassigned. Shifts that have already ended are history and never block. "Has not finished" is judged against the project's single local simulation clock - one wall-clock timeline with no timezone or daylight-saving conversion - comparing the shift's end datetime with the reference time; the reference time is injectable so that tests use fixed values rather than depending on when they run.
- After a successful action the list reloads with the supervisor's search text, sort selection and status filter unchanged. Because the list defaults to Active, a worker who was just deactivated is no longer shown, and the confirmation says so and directs the supervisor to the Inactive or All filter. A confirmed action and a failed list reload are reported as separate outcomes, and retrying the reload only re-reads the list.

- Permanently delete an employee, behind an explicit confirmation identifying them by name and employee code and stating that their profile, classes, shift preferences and approved leave will be removed permanently. Cancelling performs no change of any kind. Deletion is allowed only when no assignment references that employee, historical assignments included, because shift history must continue to refer to a real worker; the backend enforces this rather than relying on the interface. The refusal explains that history is preserved and that deactivation is the appropriate alternative, subject to its own safeguards. The employee and their dependent class, preference and leave records are removed in one transaction, with the assignment check made inside that same transaction so a concurrent write cannot invalidate it; any failure removes nothing. Shared shifts and every other employee's records are preserved.
- Open one worker from the list to inspect everything stored about them, through a read-only details request addressed by employee code. An unknown code is reported as not found in the standard error shape. Loading details performs no writes. The screen also offers explicit semester and class editing, explicit timetable confirmation, shift-preference editing and approved-leave editing, each through its own mutation requests. Its contents are specified under Complete Employee Setup below.
- A deleted employee code is retained as a retired code, holding the code and the time only and no copy of the deleted worker's details. Automatic employee-code allocation reads this record when working out the next code, so a code that has already been used is never reissued. Codes can no longer be entered by hand, so there is no other route to reusing one either.

Remaining employee management is planned scope, not yet implemented:
- Excluding inactive workers from new assignments and from schedule generation. The active flag is now stored and editable, but nothing consumes it beyond the employee list's own filter: neither the eligibility logic (Phase 6) nor the schedule generator (Phase 7) exists yet. Both must exclude inactive workers while leaving their historical assignments attached.

The database is the source of truth after explicit demo initialization. Demo data is initialized once, into a database that holds no workforce or scheduling records, and that initialization is atomic: it either writes the whole dataset or nothing. If any such record already exists, initialization refuses and changes nothing, so it cannot overwrite edits, restore intentionally deleted workers, or add demo workers to a manually managed database. There is no repair, reset or regeneration workflow, and neither application startup nor any employee-management action performs seeding. Schema changes are applied through repeatable migrations that preserve existing records and relationships. Most are purely additive; where a change must reshape existing data, it is carried out transactionally with the information preserved, never by resetting or reseeding the database. Provide an empty-workforce path for entering fictional workers through the interface. The 30-worker count, student-type mix, and course-load conventions constrain demo generation, not user-created records. The synthetic-only data policy and 20-hour weekly limit remain unchanged.

Phase 5B initially covers list controls and worker identity/status management. Phase 5C, before eligibility implementation, is a required milestone for complete employee setup. **Every requirement below is now implemented: Phase 5C is functionally complete.**

#### Complete Employee Setup (Phase 5C)

All parts of this section are implemented: automatic employee-code
allocation and the semester/class-block data model with its migration, both
described above; the employee-details view described immediately below;
supervisor entry and editing of semester dates and recurring class blocks;
explicit timetable confirmation, including a deliberate confirmed-no-classes
choice; editing shift preferences on any existing shift; and adding, editing
and removing approved leave.

**Supervisors can enter and edit a worker's semester timetables.** From the
details view a supervisor can add a semester with inclusive start and end
dates, change those dates, delete a semester behind an explicit confirmation,
and add, edit and remove the recurring weekly classes within it. Entry is
structured throughout: a semester start and end date, and a weekday plus a
start and end time for each class. No course name, course identifier,
document upload or free text is accepted anywhere in this workflow.

The application validates every submission, and does so authoritatively in
the backend rather than relying on the interface:

- Dates must be real calendar dates in the stored form, with the start on or
  before the end.
- A worker's semester periods must not overlap. Because the dates are
  inclusive, two periods sharing even a single calendar date overlap; two
  periods on consecutive days do not.
- A weekday must be a whole number identifying one of the seven days.
- Times must be valid normalized local times.
- A class must end after it starts on the same day. Classes running past
  midnight are not supported and are refused rather than reinterpreted.
- A class may not duplicate or overlap another class in the same semester. A
  class beginning exactly when another ends does not overlap, matching the
  rule used everywhere else in the application. Identical classes in
  *different* semesters are allowed.
- Every operation identifies both the worker and the record within that
  worker, so a record belonging to somebody else is reported as not found.
  Unknown workers, semesters and classes use the same not-found reporting.
- Each operation is applied as a single unit: if any part fails, nothing is
  changed.

The demo dataset's course counts and 75- and 165-minute class lengths do not
constrain manually entered classes; any positive same-day duration is
accepted.

**Editing a timetable withdraws its confirmation; confirming is a separate, deliberate action.** A new semester is
unconfirmed. Changing a semester's dates, and adding, editing or removing any
class within it, all clear that semester's confirmation, because a supervisor
confirmed the timetable they were shown rather than the one it has since
become. Deleting a semester removes its confirmation with it. Creating a
semester, and adding or removing its classes, never confirms it on their own.

**Explicit confirmation.** A supervisor confirms a semester by submitting its
current dates and the exact classes it holds (not merely how many) back; the
backend checks that submission against what is actually stored before
granting confirmation, so a stale or malformed request is refused rather than
silently confirming content the supervisor has not actually seen refreshed. A
matching count alone is not sufficient - a class that moved to a different
day or time while the count stayed the same must still be caught as stale.
Confirming a semester whose stored class list is empty additionally requires
an explicit, separate acknowledgement that the worker genuinely has no
classes - "this timetable is complete" and "this employee has no classes" are
two distinct, deliberate submissions, worded apart so neither can be
confirmed by a generic or malformed request falling through a default.
Confirming also accepts the semester's currently displayed dates as correct,
clearing a provisional-dates notice the same way editing the dates does, so a
supervisor need not retype dates that were already right. Confirming the
same, unchanged content again is accepted rather than refused, and records
the current confirmation time at the application's existing (minute)
precision - it does not promise a visibly different timestamp on every call.

**Supervisors can inspect one worker's stored setup.** Opening a worker from
the employee list shows:

- Their basic details: employee code, name, student type, active status and
  weekly hour limit, together with assigned hours and remaining capacity for
  the identified reporting period and the existing explanation that capacity
  is not eligibility. The employee code is displayed, never offered for
  editing.
- Every semester schedule stored for them, in a predictable order, with its
  inclusive start and end dates, its own confirmation state and timestamp,
  and its recurring class blocks as readable weekday, start and end times,
  also in a predictable order. Each schedule is also described by how much of
  the displayed reporting period **its dates** cover, in three states:
  *outside* it, *partial* — some but not all of its days — or *full*. A
  schedule that begins part-way through the period overlaps it without
  covering it, and must not be described as though it covered it. Schedules
  lying wholly outside the period are shown, not hidden.
- Their stored shift preferences, each naming the hall and the full dated
  start and end of the shift so that an overnight shift shows both of its
  dates, with the explanation that unspecified shifts are neutral and that
  preferences are soft rather than declarations of unavailability. Neutral
  shifts are not listed, because neutral is the absence of a stored
  preference.
- Their approved leave periods with actual start and end datetimes, and a
  clear statement when none is stored. No pending requests, approval actions
  or inferred leave appear, because none exist.

**Supervisors can set a preference for any existing shift.** A picker lists
every existing shift - not only ones already preferred or low - and a
preferred/low/neutral choice; choosing neutral deletes the stored preference
row rather than writing a third value, and setting preferred or low replaces
any existing preference for that shift rather than adding a second row. This
never creates a shift or a recurring preference template, and a low
preference is never treated as ineligibility or a hard restriction. The
backend validates the preference value and that the shift exists; an unknown
shift or an unknown employee is reported as not found.

**Supervisors can add, edit and remove approved leave.** Leave is entered as
an actual start and end datetime in the project's plain wall-clock form; the
end must be strictly after the start. Editing and removing address a specific
leave period by its own id and the owning employee's code, following the same
not-found-not-forbidden ownership rule as semesters and classes: another
worker's real leave id is reported not found. This is leave that has already
been approved, not a request - there is no pending-request state or approval
workflow - and saving or removing a leave period never changes or removes an
assignment; reconciling leave against assignments is Phase 6 work.

A semester's own confirmation, its coverage of the displayed period, and the
worker's combined five-state readiness across all their semesters are three
separate facts and are presented separately. They answer different questions:
a semester confirmed for an earlier period is genuinely confirmed and still
tells the reader nothing about the period on screen; an unconfirmed semester
can cover the whole period; and two semesters that each cover only part of it
can leave the worker fully ready between them while neither is individually
full.

A missing schedule, an unconfirmed schedule with no class blocks, and a
confirmed schedule with no class blocks are described in three different
ways, so that "nobody has told us" is never presented as "this worker has no
classes".

Where a timetable was produced by migrating the older course-linked records,
its semester dates are identified as provisional and needing review, because
the old model stored no semester dates and the migration had to assume them.
That fact is recorded on the semester period itself, not inferred from the
classes inside it, so editing or removing those classes - including removing
every one of them - cannot erase it. Supplying the semester's dates clears
it, because that is a supervisor stating what the dates are. A semester
created by a supervisor is never provisional. The migrated class days and
times are unchanged.

The provenance the application records internally is not published. The
supervisor is told that the dates are assumed and should be checked, and why;
the internal text the migration wrote against each class is not part of the
response and is not displayed. Product wording is therefore independent of
whatever those internal records happen to say.

Legacy course records are not presented as a second timetable. They are
retained as migration provenance only, and the details view does not expose
them.

Viewing a worker's details creates, updates, confirms and deletes nothing;
only the explicit save and delete controls change anything. While the details
view is open no employee-list write can be started, and while a form or a
deletion confirmation is open the details view cannot be opened, under the
same one-action-at-a-time rule the write controls already follow. Within the
details view that rule holds again: one form or confirmation is open at a
time, and the others are unavailable until it is saved or cancelled.

Opening and closing details leaves the list's search text, sort selection and
status filter unchanged. When a timetable has been edited, the list's
summaries for that worker - class-block count, weekly class hours and
timetable readiness - are re-read on returning to it, so they cannot be left
describing the timetable as it was before the edit. A confirmed save followed
by a failed reload is reported as two separate outcomes: the save stands, and
the reload failure offers a retry that only re-reads.

**Class timetables are stored as semester schedules owned by a worker.** A
schedule records inclusive semester start and end dates and whether a
supervisor has confirmed the timetable is complete. Each schedule owns
recurring weekly class blocks, each a weekday plus a start and end time in
the project's local clock. Course names and course entities are not required
to operate the application.

**Timetable readiness is reported for the displayed reporting period**, not
as a permanent property of the worker. A semester confirmed for an earlier or
later period does not make a worker ready for the period on screen, and must
not be reported as if it did. Five states are distinguished:

- *Missing* - no semester schedule exists for this worker at all.
- *Outside the period* - schedules exist, but none covers any day of the
  displayed period.
- *Unconfirmed* - a schedule covers part of the period, but no confirmed
  schedule covers any of it.
- *Partial* - confirmed schedules cover some days of the period but not all.
- *Confirmed* - confirmed schedules cover the whole period.

A confirmed timetable with no class blocks means the worker deliberately has
no classes in that period, and must remain distinguishable from missing
information: eligibility work must be able to tell "we do not know" from "we
know there are none". Readiness is separate from active status, and neither
is shift eligibility, which is not implemented.

**Migrating an existing database preserves everything.** Each stored class
meeting becomes exactly one class block with its weekday and times unchanged,
including meetings that look identical to one another; nothing is discarded,
deduplicated or rewritten, and the legacy course records are retained. The
transformation is repeatable and all-or-nothing: a failure leaves the
database exactly as it was, and repeating it moves nothing. A migrated
timetable is recorded as confirmed only where the stored class information is
proved to be a validated generated timetable: its provenance marker must name
a generated worker, and the stored meetings must match that worker's expected
timetable exactly, compared with multiplicity so that a missing, extra,
duplicated or altered meeting cannot pass. A provenance marker alone is not
accepted as proof, because an earlier migration recorded one for every worker
and stored data may since have been edited. Anything that does not match
exactly is migrated unconfirmed with its records preserved untouched.

The fictional demo semester runs from 2026-08-24 to 2026-12-11 inclusive,
which contains the sample reporting week of 5-11 October 2026.
Legacy timetables without known semester dates also receive these provisional
dates during migration and remain unconfirmed unless validated as intact demo
data. Supervisor editing must require review of these assumed dates; they are
not evidence of the worker's actual semester period.

Class-block counts and weekly class hours count only the classes that
actually occur within the reporting period: each recurring class is resolved
to its date in that period and counted only when that date falls inside its
semester's inclusive start and end dates. A semester that begins midway
through the period therefore excludes the earlier weekdays of that period,
and one that ends midway excludes the later ones. Class durations remain
fractional; the whole-hour rule applies to work shifts only.

- The backend assigns the next employee code automatically on save, using the SW-001 sequence with at least three digits. Codes are read-only in the interface and cannot be changed through the management API. Existing codes and internal identities are preserved. Allocation must be unique under concurrent requests, persist across restarts, and never reuse an issued code after deletion. Demo initialization must respect the same namespace without overwriting manual workers; seed identity remains separate.
- Supervisors enter a fictional worker's name and student type, then their semester class timetable, shift preferences, and any already-approved leave. The app handles internal keys, relationships, default active status, the 20-hour limit and calculated summaries. Saving an incomplete profile is allowed, but active status alone does not establish scheduling readiness.
- Class timetables are entered manually from the semester schedule supplied to the supervisor. Record inclusive semester start/end dates and recurring weekly blocks containing weekday, start time and end time. For example, Tuesday 9:00 AM-10:15 AM is stored as weekday 1 (Monday = 0), start 09:00 and end 10:15, linked to that employee's semester schedule. Tuesday and Thursday meetings are two blocks. No course name or course entity is needed in this workflow; no document upload or AI extraction is required.
- Validate dates, weekdays, times, positive same-day class durations, duplicate blocks and overlapping class blocks. Initially permit non-overlapping semester periods per worker. The demo's course-load conventions and 75/165-minute meetings do not restrict manually entered class blocks. Class durations can be fractional hours; work shifts remain whole hours.
- Supervisors explicitly confirm that the semester timetable is complete, including a deliberate confirmation when the worker has no classes. Missing, unconfirmed or expired class information must not be interpreted as unrestricted availability. Changes to class blocks or semester dates require reconfirmation. Phase 6 eligibility and Phase 7 generation must require confirmed timetable coverage for the full candidate shift and explain missing coverage.
- Expand recurring class blocks into actual occurrences only within their semester dates. Any positive overlap with a class blocks the whole shift; touching endpoints do not overlap. Preserve the current local-clock convention and keep the full calendar and shared reporting-week selector in Phase 7.
- Preferences remain preferred/low/neutral against existing dated shifts (hall and start/end shown); unspecified is neutral. Approved leave uses actual start/end datetimes and blocks overlaps. There is no general self-declared unavailability field or new request/approval workflow.
- Existing and newly added workers use the same details sections to view, add, edit and remove class blocks, preferences and approved leave. Viewing is implemented for all three; adding, editing and removing are implemented for semesters and class blocks only. Weekly class hours reflect semester applicability in the displayed week. The course-count column and sort have been replaced by class-block summaries, as required once the new model shipped.
- Migrate existing course-linked meetings to employee semester schedules without losing their times or relationships, resetting the database, or changing unrelated leave, preferences or assignments. Document fictional demo semester dates containing the sample week. Adapt seeding and tests to the new model; historical course labels may remain in example documentation but are not scheduling inputs.

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

### AI Scheduling Agent (Required Phase 9)

Provide a supervisor-directed agent that uses backend tools across multiple
steps to investigate a scheduling problem, propose an action, execute the
approved change and verify the result. Answering questions remains supported,
but a question-answer interface alone does not complete this milestone.

The primary workflow is: "Jordan called out for tonight's Capella shift.
Find a replacement." The agent resolves the exact worker and dated shift,
checks current assignments and eligibility, ranks eligible replacements using
documented deterministic preference/workload criteria, and explains a proposal.
The supervisor explicitly approves or rejects the specific assignment change.
The backend revalidates and applies an approved replacement atomically; the
agent then reads back the assignment and coverage to confirm the outcome.
Investigating and proposing a worker for an uncovered shift is also required.

A call-out statement by itself does not remove an assignment or create leave.
Ambiguous names/dates require clarification. If no eligible worker exists,
report the blocker without relaxing constraints or changing records.

Proposals identify the hall, dated shift, affected workers, before/after
assignment and hours impact. Store proposal and approval state on the backend;
model-generated approval claims do not authorize a write. Reject stale target
state, recheck all current hard restrictions at execution, require fresh
approval for changed proposals, and prevent duplicate application on retries.
Record proposals, approval/rejection and execution results with assignment
before/after facts in an audit trail. Preserve history and unrelated assignments.
If execution succeeds but verification fails, report those outcomes separately.

Phase 7 must supply reusable validated assignment-change operations, draft
versus persisted schedule separation, and assignment-change history. Phase 9
adds the bounded tool-calling loop, task/proposal state, approval interface and
verified multi-step workflow. The agent cannot directly run arbitrary SQL or
use this workflow to delete employees or approve leave. External notifications
and background autonomous operation are outside the required milestone.

Example questions may include:

- Who can cover this shift?
- How many hours is a particular worker scheduled?
- Which shifts are currently uncovered?
- Which periods are hardest to staff?
- How much unused workforce capacity remains?
- Can a particular employee cover a particular shift?

The language model should interpret the user's request while scheduling facts and calculations come from application data, deterministic business logic, and the optimization engine.

Use backend function calling initially. RAG remains optional for fictional
policy/procedure documents in Phase 10, never operational availability or
employee hours. MCP is an optional post-deployment adapter exposing selected
existing tools to compatible clients; it is not required for the in-app agent
and must preserve the same approval and validation rules.

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

An LLM API will drive the bounded scheduling-agent tool loop through FastAPI.
Structured tools query operational facts and propose changes; backend-enforced
supervisor approval gates execution. The model never replaces deterministic
eligibility checks or OR-Tools optimization.

The API key must remain server-side and must never be exposed in frontend code or committed to Git.

### Optional RAG

Retrieval-Augmented Generation may later be added using synthetic policy or reference documents.

RAG is not the source of truth for structured scheduling data.

### Deployment

The completed application is intended to be deployed using Microsoft Azure services.

---

## 10. Architecture Principle

### Integration of Employee Management With Later Phases

Phase 5B manages identity/status/history; Phase 5C provides automatic codes
and confirmed semester timetables. These feed Phase 6 eligibility and Phase 7
optimization, followed by Phase 8 reporting and Phase 9 AI. The following are
planned integration requirements, not claims of existing scheduling behavior:

- Retired employee-number information is already preserved by Phase 5B
  deletion, so Phase 5C's allocator must consult it and must not reuse a
  deleted number. Existing identifiers and internal relationships remain
  stable during migration.
- Evaluate current database records, not fixed demo populations or course
  counts. Apply identical status, semester readiness, overlap and hour-limit
  rules to eligibility and generation; revalidate before saving assignments.
- Once assignments exist, class/leave edits must expose newly created
  conflicts for explicit resolution. Do not silently remove assignments or
  treat previously computed eligibility as permanently valid.
- Apply the shared reporting week to class summaries, coverage and assigned
  hours. Employee status is current, while historical assignments remain
  history. Preserve D025 start-week work-hour accounting.
- Preferences refer to concrete dated shifts. Another week's shifts require
  explicit preparation, separate from one-time workforce initialization;
  browsing a week must not generate records or copy preferences.
- Calculate aggregate active capacity from actual worker limits, distinguish
  it from timetable readiness and usable eligibility, and retain historical
  coverage/hours for workers now inactive. The 30-worker/600-hour numbers
  describe only the original demo.
- AI tools must use the requested worker/week and return the deterministic
  results, including incomplete/expired timetable explanations.
- Deployment must preserve SQLite and ID-allocation state, support backup and
  restore, migrate existing data rather than reinitialize it, and align the
  runtime clock with the documented local simulation clock.

### Source of Truth

Structured operational truth should flow through:

**Database -> deterministic application logic / optimizer -> API -> user interface**

The LLM interprets requests and orchestrates reliable tools. The required
agent workflow is investigate -> propose -> approve -> apply -> verify, with
backend validation and audit history. Scheduling facts are never invented by
the model. End-to-end tests and deployment checks must cover this workflow,
including rejection, stale data, no eligible candidate and failed verification.

---

## 11. Data Disclaimer

This project does not use real employee records, private university data, internal operational documents, or actual institutional scheduling policies.

The residence hall names, employee records, class schedules, shift preferences, approved leave, staffing requirements, and operational rules used by the application are synthetic.
