import { useEffect, useState } from 'react'

import {
  EMPLOYEES_URL,
  WEEKDAY_NAMES,
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

const FEEDBACK_CLASSES = {
  success: 'backend-status backend-status-success',
  error: 'backend-status backend-status-error',
  notice: 'backend-status backend-status-loading',
}

const EMPTY_SEMESTER = { start_date: '', end_date: '' }
const EMPTY_BLOCK = { day_of_week: '0', start_time: '', end_time: '' }
const EMPTY_PREFERENCE = { shift_id: '', preference: 'preferred' }
const EMPTY_LEAVE = { start_datetime: '', end_datetime: '' }

// Which action kinds render in the semester section's shared panel slot, so
// a preference or leave form (rendered in their own sections below) is never
// also rendered here and duplicated.
const SEMESTER_ACTION_KINDS = [
  'add-semester',
  'edit-semester',
  'delete-semester',
  'add-block',
  'edit-block',
  'delete-block',
  'confirm-semester',
]

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
    // The id addresses this block when editing or removing it, so a response
    // without one cannot be acted on and is not rendered.
    Number.isInteger(block.id) &&
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
    Number.isInteger(semester.id) &&
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
    Number.isInteger(row.shift_id) &&
    hasStrings(row, ['preference', 'hall', 'start_datetime', 'end_datetime'])
  )
}

function isValidShift(row) {
  return (
    isObject(row) &&
    Number.isInteger(row.id) &&
    hasStrings(row, ['hall', 'start_datetime', 'end_datetime'])
  )
}

function isValidLeave(row) {
  return (
    isObject(row) &&
    Number.isInteger(row.id) &&
    hasStrings(row, ['start_datetime', 'end_datetime'])
  )
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
    Array.isArray(data.shifts) &&
    data.shifts.every(isValidShift) &&
    Array.isArray(data.approved_leave) &&
    data.approved_leave.every(isValidLeave)
  )
}

// Validation of a successful MUTATION response.
//
// A 2xx status means the backend accepted and applied the request - that is
// decided once, from the status code, and is never revisited. What is NOT
// guaranteed is that the response body is well-formed JSON matching what a
// healthy backend would send: a proxy, a future backend change, or a bug
// could still return `200 OK` with an empty, null, or short body. Review
// found that `submit()` handed such a body straight to `describe(payload)`,
// which threw reading a missing field and skipped `setSaving(false)` -
// locking the editor even though the write had genuinely succeeded.
//
// So every success shape is validated the same way the read-only response
// is: before anything reads a field from it. An invalid body is NEVER
// treated as a refusal - the status code already said otherwise - it just
// means the specific "Added the semester 2026-08-24 to 2026-12-11" message
// cannot be built, so a generic, still-truthful one is shown instead and an
// authoritative reload is fetched to show what is actually stored now.

function isValidScheduleWrite(payload) {
  // What create and update both return: the schedule's id and dates.
  return (
    isObject(payload) &&
    Number.isInteger(payload.id) &&
    hasStrings(payload, ['start_date', 'end_date'])
  )
}

function isValidScheduleDeletion(payload) {
  return (
    isObject(payload) &&
    hasStrings(payload, ['start_date', 'end_date']) &&
    Number.isInteger(payload.class_blocks)
  )
}

function isValidBlockWrite(payload) {
  return (
    isObject(payload) &&
    Number.isInteger(payload.id) &&
    Number.isInteger(payload.day_of_week) &&
    payload.day_of_week >= 0 &&
    payload.day_of_week <= 6 &&
    hasStrings(payload, ['start_time', 'end_time'])
  )
}

function isValidBlockDeletion(payload) {
  return (
    isObject(payload) &&
    Number.isInteger(payload.day_of_week) &&
    payload.day_of_week >= 0 &&
    payload.day_of_week <= 6 &&
    hasStrings(payload, ['start_time', 'end_time'])
  )
}

function isValidConfirmation(payload) {
  // `confirmed_at` is a string here, never null: this response only exists
  // because a confirmation was just granted.
  return (
    isObject(payload) &&
    Number.isInteger(payload.id) &&
    hasStrings(payload, ['start_date', 'end_date', 'confirmed_at']) &&
    payload.dates_provisional === false &&
    Number.isInteger(payload.class_count) &&
    payload.class_count >= 0
  )
}

function isValidPreferenceWrite(payload) {
  return (
    isObject(payload) &&
    Number.isInteger(payload.shift_id) &&
    typeof payload.preference === 'string'
  )
}

function isValidLeaveWrite(payload) {
  return (
    isObject(payload) &&
    Number.isInteger(payload.id) &&
    hasStrings(payload, ['start_datetime', 'end_datetime'])
  )
}

function isValidLeaveDeletion(payload) {
  return isObject(payload) && hasStrings(payload, ['start_datetime', 'end_datetime'])
}

async function fetchDetail(employeeCode) {
  const response = await fetch(
    `${EMPLOYEES_URL}/${encodeURIComponent(employeeCode)}`,
  )
  if (response.status === 404) {
    // Distinct from a failure, because retrying will not help.
    const error = new Error('not found')
    error.notFound = true
    throw error
  }
  if (!response.ok) {
    throw new Error(`status ${response.status}`)
  }
  const data = await response.json()
  // The second half is belt and braces: the response must be about the worker
  // that was actually asked for, whatever the timing.
  if (
    !isValidDetailResponse(data) ||
    data.employee.employee_code !== employeeCode
  ) {
    throw new Error('unexpected shape')
  }
  return data
}

function describeSemester(semester) {
  return `${semester.start_date} to ${semester.end_date}`
}

function describeBlock(block) {
  return `${weekdayName(block.day_of_week)} ${block.start_time} to ${block.end_time}`
}

function describeShift(shift) {
  return `${shift.hall} ${shift.start_datetime} to ${shift.end_datetime}`
}

function describeLeave(leave) {
  return `${leave.start_datetime} to ${leave.end_datetime}`
}

// `<input type="datetime-local">` needs 'T' between date and time; the
// backend stores and returns the project's plain-space form (D025). These
// only ever run on values already known to be in one of those two forms.
function toDatetimeLocal(value) {
  return value.replace(' ', 'T')
}

function fromDatetimeLocal(value) {
  return value.replace('T', ' ')
}

function SemesterPanel({
  semester,
  busy,
  onEditDates,
  onDeleteSemester,
  onAddBlock,
  onEditBlock,
  onDeleteBlock,
  onConfirmSemester,
}) {
  const blocks = semester.class_blocks
  const confirmed = semester.confirmed_at !== null

  return (
    <div className="detail-panel">
      <h5>{describeSemester(semester)}</h5>

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
          themselves came across unchanged. Saving the dates below, or
          confirming this semester as shown, both record them as yours and
          remove this notice.
        </p>
      )}

      <div className="list-controls">
        <button
          type="button"
          onClick={() => onEditDates(semester)}
          disabled={busy}
          aria-label={`Edit dates for the semester ${describeSemester(semester)}`}
        >
          Edit dates
        </button>
        <button
          type="button"
          onClick={() => onAddBlock(semester)}
          disabled={busy}
          aria-label={`Add a class to the semester ${describeSemester(semester)}`}
        >
          Add class
        </button>
        <button
          type="button"
          onClick={() => onConfirmSemester(semester)}
          disabled={busy}
          aria-label={`${confirmed ? 'Reconfirm' : 'Confirm'} the timetable for the semester ${describeSemester(semester)}`}
        >
          {confirmed ? 'Reconfirm timetable' : 'Confirm timetable'}
        </button>
        <button
          type="button"
          onClick={() => onDeleteSemester(semester)}
          disabled={busy}
          aria-label={`Delete the semester ${describeSemester(semester)}`}
        >
          Delete semester
        </button>
      </div>

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
              Recurring weekly classes, {describeSemester(semester)}
            </caption>
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col">Starts</th>
                <th scope="col">Ends</th>
                <th scope="col">Hours</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {blocks.map((block) => (
                <tr key={block.id}>
                  <td>{weekdayName(block.day_of_week)}</td>
                  <td>{block.start_time}</td>
                  <td>{block.end_time}</td>
                  <td>{block.hours}</td>
                  <td>
                    {/* Two classes can legitimately look identical, so the
                        accessible name carries the times to tell them apart. */}
                    <button
                      type="button"
                      onClick={() => onEditBlock(semester, block)}
                      disabled={busy}
                      aria-label={`Edit the class ${describeBlock(block)}`}
                    >
                      Edit
                    </button>{' '}
                    <button
                      type="button"
                      onClick={() => onDeleteBlock(semester, block)}
                      disabled={busy}
                      aria-label={`Remove the class ${describeBlock(block)}`}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function EmployeeDetails({ employeeCode, onClose, onChanged }) {
  const [status, setStatus] = useState('loading')
  const [detail, setDetail] = useState(null)
  // Bumped by Try again. Changing it re-runs the effect below, which is the
  // whole retry mechanism - there is no second code path that could get it
  // wrong. The effect deliberately does not reset `status` itself: the
  // parent gives this component a key of the employee code, so opening a
  // different worker mounts a fresh one that starts at 'loading' with no
  // state left over from the last.
  const [attempt, setAttempt] = useState(0)

  // Exactly one editing action is open at a time, described by this object:
  // {kind, semester?, block?}. Holding the records rather than only their ids
  // means every form and confirmation can name what it is about.
  const [action, setAction] = useState(null)
  const [semesterForm, setSemesterForm] = useState(EMPTY_SEMESTER)
  const [blockForm, setBlockForm] = useState(EMPTY_BLOCK)
  const [preferenceForm, setPreferenceForm] = useState(EMPTY_PREFERENCE)
  const [leaveForm, setLeaveForm] = useState(EMPTY_LEAVE)
  // Only meaningful while confirming a semester with zero classes: the
  // deliberate acknowledgement that must be checked before that confirmation
  // can be submitted (a generic form submission must never confirm "no
  // classes" by default).
  const [noClassesChecked, setNoClassesChecked] = useState(false)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState(null)
  const [feedback, setFeedback] = useState(null)
  // A failed reload AFTER a confirmed write is its own outcome. It never
  // means the write failed, and it is reported separately so it cannot be
  // mistaken for one.
  const [refreshError, setRefreshError] = useState(null)

  useEffect(() => {
    // `cancelled` is the guard against a late response. The cleanup runs both
    // when employeeCode changes and when this view closes and unmounts, so a
    // slow reply for the worker you have just navigated away from is thrown
    // away instead of being displayed under the new worker's name - and a
    // reply arriving after Close cannot reopen the view, because there is no
    // longer a component to set state on.
    let cancelled = false

    fetchDetail(employeeCode).then(
      (data) => {
        if (!cancelled) {
          setDetail(data)
          setStatus('success')
        }
      },
      (error) => {
        if (!cancelled) {
          setStatus(error.notFound ? 'notfound' : 'error')
        }
      },
    )

    return () => {
      cancelled = true
    }
  }, [employeeCode, attempt])

  function tryAgain() {
    setStatus('loading')
    setDetail(null)
    setAttempt(attempt + 1)
  }

  // One action owns this view at a time, guarded in the handlers as well as
  // through `disabled`, so a forced click cannot start a second conflicting
  // write or retarget an open confirmation at a different record.
  function busy() {
    return action !== null || saving
  }

  function openAction(next, prefill) {
    if (busy()) {
      return
    }
    setAction(next)
    setFormError(null)
    setFeedback(null)
    if (prefill?.semester !== undefined) {
      setSemesterForm(prefill.semester)
    }
    if (prefill?.block !== undefined) {
      setBlockForm(prefill.block)
    }
    if (prefill?.preference !== undefined) {
      setPreferenceForm(prefill.preference)
    }
    if (prefill?.leave !== undefined) {
      setLeaveForm(prefill.leave)
    }
  }

  function closeAction() {
    setAction(null)
    setFormError(null)
    setSemesterForm(EMPTY_SEMESTER)
    setBlockForm(EMPTY_BLOCK)
    setNoClassesChecked(false)
    setPreferenceForm(EMPTY_PREFERENCE)
    setLeaveForm(EMPTY_LEAVE)
  }

  async function reloadDetail() {
    // Only ever a GET, so it is safe to call again from Retry: it can never
    // repeat a write the server has already applied.
    try {
      setDetail(await fetchDetail(employeeCode))
      setRefreshError(null)
      return true
    } catch {
      setRefreshError(
        'Could not reload this worker’s details. This does not undo anything that was already saved.',
      )
      return false
    }
  }

  /**
   * Send one write, then report its outcome honestly.
   *
   * The write's own response is the only thing that decides whether we claim
   * it succeeded - and that decision is made ONCE, from the HTTP status, and
   * never revisited because of what the body looks like. A 2xx response with
   * a malformed or unreadable body is still a confirmed write: the backend
   * applied it, so this must not resend the request, must not tell the
   * supervisor it failed, and must not invite pressing Save again.
   *
   * `validate` checks the response shape this particular mutation is
   * expected to return; `describe` turns a body that PASSES that check into
   * the specific success message. If validation fails, or if `describe`
   * itself throws for any reason, `fallbackMessage` is shown instead - still
   * true, just not specific - and an authoritative reload fetches what is
   * actually stored. `saving` is cleared in a `finally` so no path, expected
   * or not, can leave the editor locked after a write the server accepted.
   */
  async function submit({ url, method, body, validate, describe, fallbackMessage }) {
    if (saving) {
      return
    }
    setSaving(true)
    setFormError(null)

    let response
    try {
      response = await fetch(url, {
        method,
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
    } catch {
      // No response at all, so whether the server applied it is genuinely
      // unknown. Claiming it was not saved could be wrong.
      setFormError(
        'Could not reach the backend, so it is unclear whether this was saved. ' +
          'Go back and reopen this worker to check before trying again.',
      )
      setSaving(false)
      return
    }

    let payload
    try {
      payload = await response.json()
    } catch {
      // No body, or a body that is not JSON at all. Handled the same as any
      // other unreadable success below - NOT as a parse failure that stops
      // here, because the status code has already been read.
      payload = null
    }

    if (!response.ok) {
      // Genuinely refused - an invalid date, an overlapping semester, a
      // duplicate class. The backend explains which; keep the form open with
      // what was typed so it can be corrected.
      setFormError(payload?.detail ?? `Save failed (status ${response.status}).`)
      setSaving(false)
      return
    }

    // From here on the write is CONFIRMED. Close the form first so a second
    // Save cannot resubmit a request the server has already applied, and
    // mark the list stale, before even looking at what the body contains.
    closeAction()
    onChanged()

    let message
    try {
      message = validate(payload) ? describe(payload) : fallbackMessage
    } catch {
      // describe() threw on a body that passed validation - a bug in this
      // component, not a failed write. The confirmed outcome still stands.
      message = fallbackMessage
    }
    setFeedback({ tone: 'success', message })

    try {
      await reloadDetail()
    } finally {
      setSaving(false)
    }
  }

  const backButton = (
    <div className="list-controls">
      <button type="button" onClick={onClose} disabled={saving}>
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

  const { employee, semesters, shift_preferences, shifts, approved_leave } = detail

  function startAddSemester() {
    openAction({ kind: 'add-semester' }, { semester: EMPTY_SEMESTER })
  }

  function startEditSemester(semester) {
    openAction(
      { kind: 'edit-semester', semester },
      {
        semester: {
          start_date: semester.start_date,
          end_date: semester.end_date,
        },
      },
    )
  }

  function startDeleteSemester(semester) {
    // Opening a confirmation sends no request of any kind.
    openAction({ kind: 'delete-semester', semester })
  }

  function startAddBlock(semester) {
    openAction({ kind: 'add-block', semester }, { block: EMPTY_BLOCK })
  }

  function startEditBlock(semester, block) {
    openAction(
      { kind: 'edit-block', semester, block },
      {
        block: {
          day_of_week: String(block.day_of_week),
          start_time: block.start_time,
          end_time: block.end_time,
        },
      },
    )
  }

  function startDeleteBlock(semester, block) {
    openAction({ kind: 'delete-block', semester, block })
  }

  function startConfirmSemester(semester) {
    // Opening this confirms nothing by itself - it only shows what would be
    // confirmed. Reset so a previous "no classes" checkbox does not carry
    // over onto a different semester.
    setNoClassesChecked(false)
    openAction({ kind: 'confirm-semester', semester })
  }

  function startSetPreference(shiftId, currentPreference) {
    openAction(
      { kind: 'set-preference' },
      {
        preference: {
          shift_id: shiftId === undefined ? '' : String(shiftId),
          preference: currentPreference ?? 'preferred',
        },
      },
    )
  }

  function startAddLeave() {
    openAction({ kind: 'add-leave' }, { leave: EMPTY_LEAVE })
  }

  function startEditLeave(leave) {
    openAction(
      { kind: 'edit-leave', leave },
      {
        leave: {
          start_datetime: leave.start_datetime,
          end_datetime: leave.end_datetime,
        },
      },
    )
  }

  function startDeleteLeave(leave) {
    openAction({ kind: 'delete-leave', leave })
  }

  const semesterBase = `${EMPLOYEES_URL}/${encodeURIComponent(employeeCode)}/semesters`

  function saveSemester(event) {
    event.preventDefault()
    const creating = action.kind === 'add-semester'
    submit({
      url: creating ? semesterBase : `${semesterBase}/${action.semester.id}`,
      method: creating ? 'POST' : 'PUT',
      body: {
        start_date: semesterForm.start_date,
        end_date: semesterForm.end_date,
      },
      validate: isValidScheduleWrite,
      describe: (saved) =>
        creating
          ? `Added the semester ${describeSemester(saved)}. It is not confirmed yet.`
          // Re-saving a semester's own unchanged dates still withdraws its
          // confirmation (D035), so this must stay true whether or not the
          // dates actually changed - it does not claim they are "different".
          : `Saved the semester dates ${describeSemester(saved)}. Saving semester dates withdraws any previous confirmation.`,
      fallbackMessage: creating
        ? 'The semester was saved, but its details could not be read from the response. Reloading to show what is now stored.'
        : 'The semester dates were saved, but the response could not be read. Reloading to show what is now stored. Saving semester dates withdraws any previous confirmation.',
    })
  }

  function saveBlock(event) {
    event.preventDefault()
    const creating = action.kind === 'add-block'
    const blocks = `${semesterBase}/${action.semester.id}/blocks`
    submit({
      url: creating ? blocks : `${blocks}/${action.block.id}`,
      method: creating ? 'POST' : 'PUT',
      body: {
        // The select holds a string; the backend requires a whole number.
        day_of_week: Number(blockForm.day_of_week),
        start_time: blockForm.start_time,
        end_time: blockForm.end_time,
      },
      validate: isValidBlockWrite,
      describe: (saved) =>
        `${creating ? 'Added' : 'Saved'} the class ${describeBlock(saved)}. This semester is no longer confirmed.`,
      fallbackMessage: `The class was saved, but the response could not be read. Reloading to show what is now stored. This semester is no longer confirmed.`,
    })
  }

  function confirmDeleteSemester() {
    submit({
      url: `${semesterBase}/${action.semester.id}`,
      method: 'DELETE',
      validate: isValidScheduleDeletion,
      describe: (removed) =>
        `Deleted the semester ${describeSemester(removed)}` +
        (removed.class_blocks > 0
          ? ` and its ${removed.class_blocks} ${removed.class_blocks === 1 ? 'class' : 'classes'}.`
          : '.'),
      fallbackMessage:
        'The semester was deleted, but the response could not be read. Reloading to show what remains.',
    })
  }

  function confirmSemester() {
    const semester = action.semester
    const classCount = semester.class_blocks.length
    submit({
      url: `${semesterBase}/${semester.id}/confirm`,
      method: 'POST',
      body: {
        start_date: semester.start_date,
        end_date: semester.end_date,
        // The exact classes as currently displayed, not merely how many -
        // a class that moved to a different day while the count stayed the
        // same must still be caught as stale by the backend.
        class_blocks: semester.class_blocks.map((block) => ({
          id: block.id,
          day_of_week: block.day_of_week,
          start_time: block.start_time,
          end_time: block.end_time,
        })),
        acknowledge_no_classes: classCount === 0,
      },
      validate: isValidConfirmation,
      describe: (saved) =>
        classCount === 0
          ? `Confirmed that ${describeSemester(saved)} has no classes.`
          : `Confirmed the timetable for ${describeSemester(saved)} ` +
            `(${classCount} ${classCount === 1 ? 'class' : 'classes'}).`,
      fallbackMessage:
        'The confirmation was saved, but the response could not be read. ' +
        'Reloading to show the current status.',
    })
  }

  function confirmDeleteBlock() {
    submit({
      url: `${semesterBase}/${action.semester.id}/blocks/${action.block.id}`,
      method: 'DELETE',
      validate: isValidBlockDeletion,
      describe: (removed) =>
        `Removed the class ${describeBlock(removed)}. This semester is no longer confirmed.`,
      fallbackMessage:
        'The class was removed, but the response could not be read. Reloading to show what remains. This semester is no longer confirmed.',
    })
  }

  const preferencesBase = `${EMPLOYEES_URL}/${encodeURIComponent(employeeCode)}/preferences`
  const leaveBase = `${EMPLOYEES_URL}/${encodeURIComponent(employeeCode)}/leave`

  function savePreference(event) {
    event.preventDefault()
    const shift = shifts.find((row) => String(row.id) === preferenceForm.shift_id)
    submit({
      url: `${preferencesBase}/${preferenceForm.shift_id}`,
      method: 'PUT',
      body: { preference: preferenceForm.preference },
      validate: isValidPreferenceWrite,
      describe: (saved) =>
        saved.preference === 'neutral'
          ? `Set ${shift ? describeShift(shift) : `shift ${saved.shift_id}`} back to neutral.`
          : `Set ${shift ? describeShift(shift) : `shift ${saved.shift_id}`} to ${PREFERENCE_LABELS[saved.preference] ?? saved.preference}.`,
      fallbackMessage:
        'The preference was saved, but the response could not be read. Reloading to show what is now stored.',
    })
  }

  function saveLeave(event) {
    event.preventDefault()
    const creating = action.kind === 'add-leave'
    submit({
      url: creating ? leaveBase : `${leaveBase}/${action.leave.id}`,
      method: creating ? 'POST' : 'PUT',
      body: {
        start_datetime: leaveForm.start_datetime,
        end_datetime: leaveForm.end_datetime,
      },
      validate: isValidLeaveWrite,
      describe: (saved) =>
        `${creating ? 'Added' : 'Saved'} the approved leave ${describeLeave(saved)}.`,
      fallbackMessage:
        'The leave period was saved, but the response could not be read. Reloading to show what is now stored.',
    })
  }

  function confirmDeleteLeave() {
    submit({
      url: `${leaveBase}/${action.leave.id}`,
      method: 'DELETE',
      validate: isValidLeaveDeletion,
      describe: (removed) => `Removed the approved leave ${describeLeave(removed)}.`,
      fallbackMessage:
        'The leave period was removed, but the response could not be read. Reloading to show what remains.',
    })
  }

  // Each panel is a FUNCTION, not a value, so only the one that is actually
  // open is built. Building them all eagerly meant the delete panels read
  // `action.semester` while some other action was open, and crashed the
  // render on a value that was legitimately absent.
  const renderSemesterForm = () => (
    <form className="employee-form" onSubmit={saveSemester}>
      <h5>
        {action.kind === 'add-semester'
          ? 'Add a semester'
          : `Edit the semester ${describeSemester(action.semester)}`}
      </h5>
      <div className="list-controls">
        <label htmlFor="semester-start">Starts</label>
        <input
          id="semester-start"
          type="date"
          value={semesterForm.start_date}
          onChange={(event) =>
            setSemesterForm({ ...semesterForm, start_date: event.target.value })
          }
        />
        <label htmlFor="semester-end">Ends</label>
        <input
          id="semester-end"
          type="date"
          value={semesterForm.end_date}
          onChange={(event) =>
            setSemesterForm({ ...semesterForm, end_date: event.target.value })
          }
        />
        <button type="submit" disabled={saving}>
          {saving ? 'Saving...' : 'Save'}
        </button>
        <button type="button" onClick={closeAction} disabled={saving}>
          Cancel
        </button>
      </div>
      <p className="table-note">
        Both dates are included in the semester. A worker&rsquo;s semesters
        cannot overlap, so two of them may not share even a single day. Saving
        these dates records them as yours and leaves the timetable
        unconfirmed &mdash; confirming it is a separate step, using Confirm
        timetable on the semester below.
      </p>
      {formError !== null && (
        <p className="backend-status backend-status-error">{formError}</p>
      )}
    </form>
  )

  const renderBlockForm = () => (
    <form className="employee-form" onSubmit={saveBlock}>
      <h5>
        {action.kind === 'add-block'
          ? `Add a class to ${describeSemester(action.semester)}`
          : 'Edit this class'}
      </h5>
      <div className="list-controls">
        <label htmlFor="block-day">Day</label>
        <select
          id="block-day"
          value={blockForm.day_of_week}
          onChange={(event) =>
            setBlockForm({ ...blockForm, day_of_week: event.target.value })
          }
        >
          {WEEKDAY_NAMES.map((name, index) => (
            <option key={name} value={String(index)}>
              {name}
            </option>
          ))}
        </select>
        <label htmlFor="block-start">Starts</label>
        <input
          id="block-start"
          type="time"
          value={blockForm.start_time}
          onChange={(event) =>
            setBlockForm({ ...blockForm, start_time: event.target.value })
          }
        />
        <label htmlFor="block-end">Ends</label>
        <input
          id="block-end"
          type="time"
          value={blockForm.end_time}
          onChange={(event) =>
            setBlockForm({ ...blockForm, end_time: event.target.value })
          }
        />
        <button type="submit" disabled={saving}>
          {saving ? 'Saving...' : 'Save'}
        </button>
        <button type="button" onClick={closeAction} disabled={saving}>
          Cancel
        </button>
      </div>
      <p className="table-note">
        The class repeats every week of this semester. It must end after it
        starts on the same day, and it cannot duplicate or overlap another
        class in this semester &mdash; though one class may start exactly when
        another ends. Saving leaves the timetable unconfirmed.
      </p>
      {formError !== null && (
        <p className="backend-status backend-status-error">{formError}</p>
      )}
    </form>
  )

  const renderDeleteSemester = () => (
    <div className="employee-form" role="alertdialog" aria-label="Confirm semester deletion">
      <h5>Delete the semester {describeSemester(action.semester)}?</h5>
      <p className="table-note">
        This removes the semester and every class in it
        {action.semester.class_blocks.length > 0
          ? ` — ${action.semester.class_blocks.length} ${action.semester.class_blocks.length === 1 ? 'class' : 'classes'}`
          : ''}
        . It cannot be undone. The worker keeps everything else: their other
        semesters, shift preferences, approved leave and shift history are all
        left alone.
      </p>
      <div className="list-controls">
        <button type="button" onClick={confirmDeleteSemester} disabled={saving}>
          {saving ? 'Deleting...' : 'Delete semester'}
        </button>
        <button type="button" onClick={closeAction} disabled={saving}>
          Cancel
        </button>
      </div>
      {formError !== null && (
        <p className="backend-status backend-status-error">{formError}</p>
      )}
    </div>
  )

  const renderConfirmSemester = () => {
    const semester = action.semester
    const classCount = semester.class_blocks.length
    const hasClasses = classCount > 0
    const alreadyConfirmed = semester.confirmed_at !== null

    return (
      <div
        className="employee-form"
        role="alertdialog"
        aria-label="Confirm semester timetable"
      >
        <h5>
          {alreadyConfirmed ? 'Reconfirm' : 'Confirm'} the timetable for{' '}
          {describeSemester(semester)}?
        </h5>
        <p className="table-note">
          Dates: {semester.start_date} to {semester.end_date}.{' '}
          {hasClasses
            ? `${classCount} recurring ${classCount === 1 ? 'class' : 'classes'} currently listed below.`
            : 'No classes are currently listed for this semester.'}
        </p>
        {alreadyConfirmed && (
          <p className="table-note">
            Already confirmed on {semester.confirmed_at}. Reconfirming records
            that a supervisor checked again and the dates and classes above
            still stand.
          </p>
        )}
        {hasClasses ? (
          <p className="table-note">
            Confirming means <strong>this timetable is complete</strong>: the
            dates and classes shown above are correct and nothing is missing.
            It does not mean the worker has no classes &mdash; they have{' '}
            {classCount}.
          </p>
        ) : (
          <>
            <p className="table-note">
              Confirming an empty semester means{' '}
              <strong>this employee genuinely has no classes</strong> during{' '}
              {describeSemester(semester)}, not that nobody has entered them
              yet. This requires a separate, deliberate acknowledgement.
            </p>
            <label className="list-controls">
              <input
                type="checkbox"
                checked={noClassesChecked}
                onChange={(event) => setNoClassesChecked(event.target.checked)}
              />{' '}
              I confirm this worker genuinely has no classes in this semester.
            </label>
          </>
        )}
        <div className="list-controls">
          <button
            type="button"
            onClick={confirmSemester}
            disabled={saving || (!hasClasses && !noClassesChecked)}
          >
            {saving
              ? 'Confirming...'
              : hasClasses
                ? 'Confirm timetable'
                : 'Confirm no classes'}
          </button>
          <button type="button" onClick={closeAction} disabled={saving}>
            Cancel
          </button>
        </div>
        {formError !== null && (
          <p className="backend-status backend-status-error">{formError}</p>
        )}
      </div>
    )
  }

  const renderDeleteBlock = () => (
    <div className="employee-form" role="alertdialog" aria-label="Confirm class removal">
      <h5>Remove the class {describeBlock(action.block)}?</h5>
      <p className="table-note">
        This removes that class from {describeSemester(action.semester)}.
        It cannot be undone, and it leaves the semester unconfirmed &mdash;
        including if it was the last class, because &ldquo;no classes&rdquo; is
        a statement that needs confirming in its own right.
      </p>
      <div className="list-controls">
        <button type="button" onClick={confirmDeleteBlock} disabled={saving}>
          {saving ? 'Removing...' : 'Remove class'}
        </button>
        <button type="button" onClick={closeAction} disabled={saving}>
          Cancel
        </button>
      </div>
      {formError !== null && (
        <p className="backend-status backend-status-error">{formError}</p>
      )}
    </div>
  )

  const renderPreferenceForm = () => (
    <form className="employee-form" onSubmit={savePreference}>
      <h5>Set a shift preference</h5>
      <div className="list-controls">
        <label htmlFor="preference-shift">Shift</label>
        <select
          id="preference-shift"
          value={preferenceForm.shift_id}
          onChange={(event) =>
            setPreferenceForm({ ...preferenceForm, shift_id: event.target.value })
          }
        >
          <option value="" disabled>
            Choose a shift
          </option>
          {shifts.map((shift) => (
            <option key={shift.id} value={String(shift.id)}>
              {describeShift(shift)}
            </option>
          ))}
        </select>
        <label htmlFor="preference-level">Preference</label>
        <select
          id="preference-level"
          value={preferenceForm.preference}
          onChange={(event) =>
            setPreferenceForm({ ...preferenceForm, preference: event.target.value })
          }
        >
          <option value="preferred">Preferred</option>
          <option value="low">Low preference</option>
          <option value="neutral">Neutral (no preference)</option>
        </select>
        <button type="submit" disabled={saving || preferenceForm.shift_id === ''}>
          {saving ? 'Saving...' : 'Save'}
        </button>
        <button type="button" onClick={closeAction} disabled={saving}>
          Cancel
        </button>
      </div>
      <p className="table-note">
        Preferences are soft: a low-preference shift is still one the worker
        can be assigned to, and a preferred one is not a claim on it. Neutral
        removes any stored preference for this shift rather than recording a
        third value. This does not create a shift or a recurring template.
      </p>
      {formError !== null && (
        <p className="backend-status backend-status-error">{formError}</p>
      )}
    </form>
  )

  const renderLeaveForm = () => (
    <form className="employee-form" onSubmit={saveLeave}>
      <h5>
        {action.kind === 'add-leave'
          ? 'Add approved leave'
          : `Edit the approved leave ${describeLeave(action.leave)}`}
      </h5>
      <div className="list-controls">
        <label htmlFor="leave-start">Starts</label>
        <input
          id="leave-start"
          type="datetime-local"
          value={toDatetimeLocal(leaveForm.start_datetime)}
          onChange={(event) =>
            setLeaveForm({
              ...leaveForm,
              start_datetime: fromDatetimeLocal(event.target.value),
            })
          }
        />
        <label htmlFor="leave-end">Ends</label>
        <input
          id="leave-end"
          type="datetime-local"
          value={toDatetimeLocal(leaveForm.end_datetime)}
          onChange={(event) =>
            setLeaveForm({
              ...leaveForm,
              end_datetime: fromDatetimeLocal(event.target.value),
            })
          }
        />
        <button type="submit" disabled={saving}>
          {saving ? 'Saving...' : 'Save'}
        </button>
        <button type="button" onClick={closeAction} disabled={saving}>
          Cancel
        </button>
      </div>
      <p className="table-note">
        This is leave that has already been approved, not a request. It must
        end after it starts. Saving does not change or remove any assignment;
        resolving a conflict between leave and an assignment is not built yet.
      </p>
      {formError !== null && (
        <p className="backend-status backend-status-error">{formError}</p>
      )}
    </form>
  )

  const renderDeleteLeave = () => (
    <div className="employee-form" role="alertdialog" aria-label="Confirm leave removal">
      <h5>Remove the approved leave {describeLeave(action.leave)}?</h5>
      <p className="table-note">
        This removes the leave period. It cannot be undone, and it does not
        change any assignment.
      </p>
      <div className="list-controls">
        <button type="button" onClick={confirmDeleteLeave} disabled={saving}>
          {saving ? 'Removing...' : 'Remove leave'}
        </button>
        <button type="button" onClick={closeAction} disabled={saving}>
          Cancel
        </button>
      </div>
      {formError !== null && (
        <p className="backend-status backend-status-error">{formError}</p>
      )}
    </div>
  )

  const openPanel =
    action === null
      ? null
      : {
          'add-semester': renderSemesterForm,
          'edit-semester': renderSemesterForm,
          'add-block': renderBlockForm,
          'edit-block': renderBlockForm,
          'delete-semester': renderDeleteSemester,
          'delete-block': renderDeleteBlock,
          'confirm-semester': renderConfirmSemester,
          'set-preference': renderPreferenceForm,
          'add-leave': renderLeaveForm,
          'edit-leave': renderLeaveForm,
          'delete-leave': renderDeleteLeave,
        }[action.kind]()

  return (
    <section aria-label={`Details for ${employee.full_name}`}>
      {backButton}

      <h3 className="detail-title">
        {employee.full_name} ({employee.employee_code})
      </h3>
      <p className="table-note">
        Semester dates and class times can be edited here, and each semester
        can be confirmed once its dates and classes are complete. Shift
        preferences and approved leave can also be edited here.
      </p>

      {feedback !== null && (
        <p className={FEEDBACK_CLASSES[feedback.tone]} aria-live="polite">
          {feedback.message}
        </p>
      )}

      {refreshError !== null && (
        <p className="backend-status backend-status-error" aria-live="polite">
          {refreshError}{' '}
          <button type="button" onClick={reloadDetail} disabled={saving}>
            Retry loading
          </button>
        </p>
      )}

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

      <div className="list-controls">
        <button type="button" onClick={startAddSemester} disabled={busy()}>
          Add semester
        </button>
      </div>

      {action !== null && SEMESTER_ACTION_KINDS.includes(action.kind) && openPanel}

      {semesters.length === 0 ? (
        <p className="backend-status backend-status-loading">
          No semester timetable has been entered for this worker. That is
          missing information &mdash; it does not mean they have no classes.
          Add a semester above to enter their class times.
        </p>
      ) : (
        semesters.map((semester) => (
          <SemesterPanel
            key={semester.id}
            semester={semester}
            busy={busy()}
            onEditDates={startEditSemester}
            onDeleteSemester={startDeleteSemester}
            onAddBlock={startAddBlock}
            onEditBlock={startEditBlock}
            onDeleteBlock={startDeleteBlock}
            onConfirmSemester={startConfirmSemester}
          />
        ))
      )}

      <h4>Shift preferences</h4>
      <p className="table-note">
        Only the shifts this worker has a stored preference for are listed
        below. Every other shift is neutral, which is stored as the absence
        of a preference rather than as a third value. Preferences are soft: a
        low-preference shift is still one the worker can be assigned to, and
        a preferred one is not a claim on it.
      </p>

      <div className="list-controls">
        <button type="button" onClick={() => startSetPreference()} disabled={busy()}>
          Set a shift preference
        </button>
      </div>

      {action !== null && action.kind === 'set-preference' && openPanel}

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
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {shift_preferences.map((row) => (
                <tr key={row.shift_id}>
                  <td>{PREFERENCE_LABELS[row.preference] ?? row.preference}</td>
                  <td>{row.hall}</td>
                  {/* Full dates on both ends, so an overnight shift reads as
                      finishing the next morning rather than looking as if it
                      ends before it starts. */}
                  <td>{row.start_datetime}</td>
                  <td>{row.end_datetime}</td>
                  <td>
                    <button
                      type="button"
                      onClick={() => startSetPreference(row.shift_id, row.preference)}
                      disabled={busy()}
                      aria-label={`Change the preference for ${row.hall} ${row.start_datetime} to ${row.end_datetime}`}
                    >
                      Change
                    </button>
                  </td>
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

      <div className="list-controls">
        <button type="button" onClick={startAddLeave} disabled={busy()}>
          Add approved leave
        </button>
      </div>

      {action !== null &&
        ['add-leave', 'edit-leave', 'delete-leave'].includes(action.kind) &&
        openPanel}

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
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {approved_leave.map((row) => (
                <tr key={row.id}>
                  <td>{row.start_datetime}</td>
                  <td>{row.end_datetime}</td>
                  <td>
                    <button
                      type="button"
                      onClick={() => startEditLeave(row)}
                      disabled={busy()}
                      aria-label={`Edit the approved leave ${describeLeave(row)}`}
                    >
                      Edit
                    </button>{' '}
                    <button
                      type="button"
                      onClick={() => startDeleteLeave(row)}
                      disabled={busy()}
                      aria-label={`Remove the approved leave ${describeLeave(row)}`}
                    >
                      Remove
                    </button>
                  </td>
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
