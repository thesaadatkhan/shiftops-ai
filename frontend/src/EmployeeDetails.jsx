import { useEffect, useState } from 'react'

import {
  EMPLOYEES_URL,
  studentTypeLabel,
  timetableLabel,
  weekdayName,
} from './employees.js'

const PREFERENCE_LABELS = {
  preferred: 'Preferred',
  low: 'Low preference',
}

// How much of the displayed reporting week this semester's DATES reach.
// Three states rather than a yes/no, because a semester that begins on the
// Wednesday overlaps the week without covering it, and saying it "includes
// the reporting week" would be a stronger claim than the dates support.
// None of these say anything about whether the timetable was confirmed.
const COVERAGE_SENTENCES = {
  outside: 'These dates fall outside the reporting week shown above.',
  partial:
    'These dates cover part of the reporting week shown above, not all seven days.',
  full: 'These dates cover the whole reporting week shown above.',
}

// Response validation.
//
// Checked before anything reaches state, so a malformed body shows the error
// state instead of crashing the render. The first version only checked the
// top level, which review showed was not enough: `semesters: [{}]` passed,
// and then `semester.class_blocks.length` threw part way down the page,
// leaving a blank screen with no explanation.
//
// So every field the component actually reads is checked, at every depth. The
// rule is deliberately one-directional: a missing or wrong-typed REQUIRED
// field fails the whole response, but an unrecognised extra field is ignored,
// so the backend can add one without breaking a browser tab that has not been
// reloaded.
//
// Nothing here substitutes a default. A record that cannot be trusted is not
// quietly replaced with an invented one - the supervisor is told the response
// could not be read.

function isObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function hasStrings(value, keys) {
  return keys.every((key) => typeof value[key] === 'string')
}

function isValidEmployee(employee) {
  return (
    isObject(employee) &&
    hasStrings(employee, [
      'employee_code',
      'full_name',
      'student_type',
      'timetable_status',
    ]) &&
    typeof employee.is_active === 'boolean' &&
    // Number.isFinite rejects NaN and Infinity as well as non-numbers; both
    // would render as "NaN hours".
    Number.isFinite(employee.weekly_hour_limit) &&
    Number.isFinite(employee.assigned_hours) &&
    Number.isFinite(employee.remaining_capacity_hours)
  )
}

const COVERAGE_STATES = ['outside', 'partial', 'full']

function isValidClassBlock(block) {
  return (
    isObject(block) &&
    Number.isInteger(block.day_of_week) &&
    block.day_of_week >= 0 &&
    block.day_of_week <= 6 &&
    hasStrings(block, ['start_time', 'end_time']) &&
    Number.isFinite(block.hours)
  )
}

function isValidSemester(semester) {
  return (
    isObject(semester) &&
    hasStrings(semester, ['start_date', 'end_date']) &&
    // Null is meaningful here - it is what "not confirmed" looks like - so it
    // is accepted, and anything that is neither a string nor null is not.
    (semester.confirmed_at === null ||
      typeof semester.confirmed_at === 'string') &&
    COVERAGE_STATES.includes(semester.reporting_week_coverage) &&
    typeof semester.dates_provisional === 'boolean' &&
    Array.isArray(semester.class_blocks) &&
    semester.class_blocks.every(isValidClassBlock)
  )
}

function isValidPreference(row) {
  return (
    isObject(row) &&
    hasStrings(row, ['preference', 'hall', 'start_datetime', 'end_datetime'])
  )
}

function isValidLeave(row) {
  return isObject(row) && hasStrings(row, ['start_datetime', 'end_datetime'])
}

function isValidDetailResponse(data) {
  return (
    isObject(data) &&
    hasStrings(data, ['week_start', 'week_end']) &&
    isValidEmployee(data.employee) &&
    Array.isArray(data.semesters) &&
    data.semesters.every(isValidSemester) &&
    Array.isArray(data.shift_preferences) &&
    data.shift_preferences.every(isValidPreference) &&
    Array.isArray(data.approved_leave) &&
    data.approved_leave.every(isValidLeave)
  )
}

function SemesterPanel({ semester }) {
  const blocks = semester.class_blocks
  const confirmed = semester.confirmed_at !== null

  return (
    <div className="detail-panel">
      <h5>
        {semester.start_date} to {semester.end_date}
      </h5>

      <p className="table-note">
        {/* This semester's OWN confirmation. It is not the readiness figure
            for the displayed week shown above, and the two can legitimately
            disagree: a spring semester can be confirmed while October has
            nothing covering it at all. */}
        {confirmed
          ? `Confirmed on ${semester.confirmed_at}.`
          : 'Not confirmed. Nobody has checked that this timetable is complete.'}{' '}
        {/* Dates only, and precise about how much of the week they reach.
            This used to say "include the reporting week" whenever the
            semester touched it at all, which claimed full coverage for a
            semester that starts on the Wednesday. */}
        {COVERAGE_SENTENCES[semester.reporting_week_coverage]}
      </p>

      {semester.dates_provisional && (
        <p className="table-note">
          <strong>These dates are provisional.</strong> They were filled in
          when this worker&rsquo;s classes were carried over from the older
          way of storing them, which recorded no semester dates at all. Nobody
          has confirmed that this is really when their semester runs, so check
          the dates before relying on them. The class days and times
          themselves came across unchanged.
        </p>
      )}

      {blocks.length === 0 ? (
        <p className="backend-status backend-status-loading">
          {/* Three different things, deliberately worded apart. */}
          {confirmed
            ? 'Confirmed with no classes: a supervisor has confirmed that this worker has no classes in this semester.'
            : 'No class blocks have been entered, and this timetable has not been confirmed. That is missing information, not a statement that the worker has no classes.'}
        </p>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
            <caption className="table-caption">
              Recurring weekly classes, {semester.start_date} to{' '}
              {semester.end_date}
            </caption>
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col">Starts</th>
                <th scope="col">Ends</th>
                <th scope="col">Hours</th>
              </tr>
            </thead>
            <tbody>
              {blocks.map((block, index) => (
                // Two identical blocks are legitimate - the old model let one
                // worker hold the same meeting under two courses, and the
                // migration kept both - so the position in the ordered list
                // is the only thing that distinguishes them.
                <tr key={index}>
                  <td>{weekdayName(block.day_of_week)}</td>
                  <td>{block.start_time}</td>
                  <td>{block.end_time}</td>
                  <td>{block.hours}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function EmployeeDetails({ employeeCode, onClose }) {
  const [status, setStatus] = useState('loading')
  const [detail, setDetail] = useState(null)
  // Bumped by Try again. Changing it re-runs the effect below, which is the
  // whole retry mechanism - there is no second code path that could get it
  // wrong. The effect deliberately does not reset `status` itself: the
  // parent gives this component a key of the employee code, so opening a
  // different worker mounts a fresh one that starts at 'loading' with no
  // state left over from the last.
  const [attempt, setAttempt] = useState(0)

  function tryAgain() {
    setStatus('loading')
    setDetail(null)
    setAttempt(attempt + 1)
  }

  useEffect(() => {
    // `cancelled` is the guard against a late response. The cleanup runs both
    // when employeeCode changes and when this view closes and unmounts, so a
    // slow reply for the worker you have just navigated away from is thrown
    // away instead of being displayed under the new worker's name - and a
    // reply arriving after Close cannot reopen the view, because there is no
    // longer a component to set state on.
    let cancelled = false

    async function loadDetail() {
      let response

      try {
        response = await fetch(
          `${EMPLOYEES_URL}/${encodeURIComponent(employeeCode)}`,
        )
      } catch {
        if (!cancelled) {
          setStatus('error')
        }
        return
      }

      if (cancelled) {
        return
      }

      if (response.status === 404) {
        // Somebody else deleted them, or the list is stale. Distinct from a
        // failure, because retrying will not help.
        setStatus('notfound')
        return
      }

      if (!response.ok) {
        setStatus('error')
        return
      }

      let data
      try {
        data = await response.json()
      } catch {
        if (!cancelled) {
          setStatus('error')
        }
        return
      }

      if (cancelled) {
        return
      }

      // The second half of this test is belt and braces: the response must be
      // about the worker that was actually asked for, whatever the timing.
      if (
        !isValidDetailResponse(data) ||
        data.employee.employee_code !== employeeCode
      ) {
        setStatus('error')
        return
      }

      setDetail(data)
      setStatus('success')
    }

    loadDetail()

    return () => {
      cancelled = true
    }
  }, [employeeCode, attempt])

  const backButton = (
    <div className="list-controls">
      <button type="button" onClick={onClose}>
        Back to employee list
      </button>
    </div>
  )

  if (status === 'loading') {
    return (
      <section aria-label={`Details for ${employeeCode}`}>
        {backButton}
        <p className="backend-status backend-status-loading" aria-live="polite">
          Loading details for {employeeCode}...
        </p>
      </section>
    )
  }

  if (status === 'notfound') {
    return (
      <section aria-label={`Details for ${employeeCode}`}>
        {backButton}
        <p className="backend-status backend-status-error" aria-live="polite">
          {employeeCode} is no longer in the database. They may have been
          deleted since this list was loaded. Go back to refresh the list.
        </p>
      </section>
    )
  }

  if (status === 'error') {
    return (
      <section aria-label={`Details for ${employeeCode}`}>
        {backButton}
        <p className="backend-status backend-status-error" aria-live="polite">
          Could not load details for {employeeCode}. The backend may be offline
          or returned an unexpected response.{' '}
          <button type="button" onClick={tryAgain}>
            Try again
          </button>
        </p>
      </section>
    )
  }

  const { employee, semesters, shift_preferences, approved_leave } = detail

  return (
    <section aria-label={`Details for ${employee.full_name}`}>
      {backButton}

      <h3 className="detail-title">
        {employee.full_name} ({employee.employee_code})
      </h3>
      <p className="table-note">
        Everything here is read-only. Entering and editing semester dates,
        class blocks, shift preferences and approved leave is not built yet,
        so this view shows what is stored rather than offering controls that
        would not work.
      </p>

      <h4>Details</h4>
      <dl className="detail-list">
        <dt>Employee ID</dt>
        <dd>
          {employee.employee_code}{' '}
          <span className="detail-hint">
            assigned by the application and never changed
          </span>
        </dd>

        <dt>Name</dt>
        <dd>{employee.full_name}</dd>

        <dt>Student type</dt>
        <dd>{studentTypeLabel(employee)}</dd>

        <dt>Status</dt>
        <dd>{employee.is_active ? 'Active' : 'Inactive'}</dd>

        <dt>Weekly limit</dt>
        <dd>{employee.weekly_hour_limit} hours</dd>

        <dt>Assigned, {detail.week_start} to {detail.week_end}</dt>
        <dd>{employee.assigned_hours} hours</dd>

        <dt>Remaining capacity, {detail.week_start} to {detail.week_end}</dt>
        <dd>{employee.remaining_capacity_hours} hours</dd>

        <dt>Timetable, {detail.week_start} to {detail.week_end}</dt>
        <dd>{timetableLabel(employee.timetable_status)}</dd>
      </dl>

      <p className="table-note">
        Remaining capacity is the unused part of the 20-hour weekly limit for{' '}
        {detail.week_start} to {detail.week_end}, after the shifts already
        assigned. It is theoretical spare capacity only &mdash; it does not
        mean the worker is eligible or available for a given shift, and class
        hours and approved leave are not subtracted from it.
      </p>

      <h4>Semester timetables</h4>
      <p className="table-note">
        Every semester stored for this worker, including ones outside{' '}
        {detail.week_start} to {detail.week_end}. Each semester carries its own
        confirmation, which is a different question from the Timetable line
        above: that line describes only the reporting week on screen, so a
        semester can be confirmed for its own dates while the displayed week
        has nothing covering it.
      </p>

      {semesters.length === 0 ? (
        <p className="backend-status backend-status-loading">
          No semester timetable has been entered for this worker. That is
          missing information &mdash; it does not mean they have no classes.
        </p>
      ) : (
        semesters.map((semester) => (
          <SemesterPanel
            key={`${semester.start_date}-${semester.end_date}`}
            semester={semester}
          />
        ))
      )}

      <h4>Shift preferences</h4>
      <p className="table-note">
        Only the shifts this worker has expressed a preference about. Every
        other shift is neutral, which is stored as the absence of a preference
        rather than as a third value. Preferences are soft: a low-preference
        shift is still one the worker can be assigned to, and a preferred one
        is not a claim on it.
      </p>

      {shift_preferences.length === 0 ? (
        <p className="backend-status backend-status-loading">
          No shift preferences are stored. Every shift is neutral for this
          worker.
        </p>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
            <caption className="table-caption">
              Stored shift preferences
            </caption>
            <thead>
              <tr>
                <th scope="col">Preference</th>
                <th scope="col">Hall</th>
                <th scope="col">Starts</th>
                <th scope="col">Ends</th>
              </tr>
            </thead>
            <tbody>
              {shift_preferences.map((row, index) => (
                <tr key={index}>
                  <td>{PREFERENCE_LABELS[row.preference] ?? row.preference}</td>
                  <td>{row.hall}</td>
                  {/* Full dates on both ends, so an overnight shift reads as
                      finishing the next morning rather than looking as if it
                      ends before it starts. */}
                  <td>{row.start_datetime}</td>
                  <td>{row.end_datetime}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h4>Approved leave</h4>
      <p className="table-note">
        Leave that has already been approved, with the dates and times it
        actually covers. Pending requests are not part of this application.
      </p>

      {approved_leave.length === 0 ? (
        <p className="backend-status backend-status-loading">
          No approved leave is stored for this worker.
        </p>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
            <caption className="table-caption">Approved leave periods</caption>
            <thead>
              <tr>
                <th scope="col">Starts</th>
                <th scope="col">Ends</th>
              </tr>
            </thead>
            <tbody>
              {approved_leave.map((row, index) => (
                <tr key={index}>
                  <td>{row.start_datetime}</td>
                  <td>{row.end_datetime}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {backButton}
    </section>
  )
}

export default EmployeeDetails
