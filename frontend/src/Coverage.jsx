import { useEffect, useState } from 'react'

import { SHIFTS_URL, coverageUrl, preferenceLabel } from './coverage.js'
import {
  datesForHall,
  friendlyDate,
  hallsFromShifts,
  shiftPickerLabel,
  shiftSummaryLabel,
  shiftsForHallAndDate,
} from './shiftPicker.js'

// Response validation, the same defensive shape EmployeeDetails.jsx uses:
// every field this component actually reads is checked before anything
// reaches state, so a malformed or unexpected response shows the error state
// instead of crashing the render partway down the page.

function isObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function hasStrings(value, keys) {
  return keys.every((key) => typeof value[key] === 'string')
}

function isValidShift(row) {
  return (
    isObject(row) &&
    Number.isInteger(row.id) &&
    hasStrings(row, ['hall', 'start_datetime', 'end_datetime'])
  )
}

function isValidShiftListResponse(data) {
  return isObject(data) && Array.isArray(data.shifts) && data.shifts.every(isValidShift)
}

const VALID_PREFERENCES = new Set(['preferred', 'low', 'neutral'])

function isValidEligibilityResult(row) {
  return (
    isObject(row) &&
    Number.isInteger(row.employee_id) &&
    Number.isInteger(row.shift_id) &&
    hasStrings(row, ['employee_code', 'full_name']) &&
    typeof row.eligible === 'boolean' &&
    Array.isArray(row.reason_codes) &&
    row.reason_codes.every((code) => typeof code === 'string') &&
    Array.isArray(row.reasons) &&
    row.reasons.every((reason) => typeof reason === 'string') &&
    Number.isFinite(row.shift_duration_hours) &&
    Number.isFinite(row.assigned_hours_this_week) &&
    Number.isFinite(row.projected_weekly_hours) &&
    Number.isFinite(row.weekly_hour_limit) &&
    VALID_PREFERENCES.has(row.preference)
  )
}

function isValidCoverageShift(shift) {
  return (
    isObject(shift) &&
    Number.isInteger(shift.id) &&
    hasStrings(shift, ['hall', 'start_datetime', 'end_datetime']) &&
    Number.isFinite(shift.duration_hours)
  )
}

// No two rows may claim the same worker: a malformed duplicate (two rows
// for the same employee_id, or the same employee_code under two different
// ids) could otherwise let a later check "find" a match that is really a
// different, contradictory row.
function hasNoDuplicateWorkers(rows) {
  const seenIds = new Set()
  const seenCodes = new Set()
  for (const row of rows) {
    if (seenIds.has(row.employee_id) || seenCodes.has(row.employee_code)) {
      return false
    }
    seenIds.add(row.employee_id)
    seenCodes.add(row.employee_code)
  }
  return true
}

// Every field the Coverage UI actually reads or relies on from a single
// eligibility row. `conflicts` is deliberately excluded: the UI never reads
// it, so it is not "used or relied upon" and checking it would validate data
// this screen does not consume.
const CANDIDATE_SCALAR_FIELDS = [
  'employee_id',
  'employee_code',
  'full_name',
  'shift_id',
  'eligible',
  'preference',
  'shift_duration_hours',
  'assigned_hours_this_week',
  'projected_weekly_hours',
  'weekly_hour_limit',
]

function scalarFieldsMatch(candidate, result) {
  return CANDIDATE_SCALAR_FIELDS.every((field) => candidate[field] === result[field])
}

// Content comparison, not reference or JSON-string comparison - two arrays
// with the same items in the same order match regardless of how each was
// constructed, and this never depends on object-property order the way a
// naive JSON.stringify(a) === JSON.stringify(b) check would.
function arraysMatch(a, b) {
  return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((value, index) => value === b[index])
}

function candidateMatchesResult(candidate, result) {
  return (
    scalarFieldsMatch(candidate, result) &&
    arraysMatch(candidate.reason_codes, result.reason_codes) &&
    arraysMatch(candidate.reasons, result.reasons)
  )
}

// eligible_candidates must be exactly the truthful subset the API contract
// promises: every eligible worker in `results`, minus the excluded one if
// any - no fewer, no more, and every field on each candidate must actually
// match its own results row rather than merely sharing an id and an
// `eligible: true` flag. Review found the first version proved only that
// much, which let the UI render a candidate object carrying an altered
// preference or projected-hours value, or let a genuinely eligible worker be
// silently dropped from the list, without either being caught.
function isExactEligibleSubset(candidates, results, excludeCode) {
  const resultByEmployeeId = new Map(results.map((row) => [row.employee_id, row]))
  const expectedIds = new Set(
    results
      .filter((row) => row.eligible === true && row.employee_code !== excludeCode)
      .map((row) => row.employee_id),
  )

  if (candidates.length !== expectedIds.size) {
    return false
  }

  const seenIds = new Set()
  for (const candidate of candidates) {
    if (!expectedIds.has(candidate.employee_id) || seenIds.has(candidate.employee_id)) {
      return false
    }
    seenIds.add(candidate.employee_id)

    const matching = resultByEmployeeId.get(candidate.employee_id)
    if (matching === undefined || !candidateMatchesResult(candidate, matching)) {
      return false
    }
  }

  return true
}

// Request-aware: a syntactically well-shaped 2xx body is not enough on its
// own. Review found this component's first version checked field types only,
// so a response for the WRONG shift, a stale exclusion echo, or an
// ineligible/excluded worker smuggled into eligible_candidates would all
// have rendered as if they were correct. Every relationship this screen
// actually relies on is checked before the response reaches state:
//   - the shift and every result actually describe the REQUESTED shift;
//   - excluded_employee_code echoes what was actually requested (null when
//     none was);
//   - no duplicate worker identity can make a later check "find" a row that
//     is really a different, contradictory one;
//   - eligible_candidates is the EXACT expected subset of results - every
//     eligible, non-excluded worker and no one else - with every field this
//     screen reads matching that worker's own results row.
function isValidCoverageResponse(data, requestedShiftId, requestedExcludeCode) {
  const shiftId = Number(requestedShiftId)
  const excludeCode = requestedExcludeCode === '' ? null : requestedExcludeCode

  if (!isObject(data) || !isValidCoverageShift(data.shift)) {
    return false
  }
  if (data.shift.id !== shiftId) {
    return false
  }
  if (
    !(data.excluded_employee_code === null || typeof data.excluded_employee_code === 'string') ||
    data.excluded_employee_code !== excludeCode
  ) {
    return false
  }
  if (
    !Array.isArray(data.results) ||
    !data.results.every(isValidEligibilityResult) ||
    !data.results.every((row) => row.shift_id === shiftId) ||
    !hasNoDuplicateWorkers(data.results)
  ) {
    return false
  }
  if (
    !Array.isArray(data.eligible_candidates) ||
    !data.eligible_candidates.every(isValidEligibilityResult) ||
    !hasNoDuplicateWorkers(data.eligible_candidates)
  ) {
    return false
  }

  return isExactEligibleSubset(data.eligible_candidates, data.results, excludeCode)
}

async function fetchShiftList() {
  const response = await fetch(SHIFTS_URL)
  if (!response.ok) {
    throw new Error(`status ${response.status}`)
  }
  const data = await response.json()
  if (!isValidShiftListResponse(data)) {
    throw new Error('unexpected shape')
  }
  return data.shifts
}

async function fetchCoverage(shiftId, excludeEmployeeCode) {
  const response = await fetch(coverageUrl(shiftId, excludeEmployeeCode))
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    // A genuine refusal (unknown shift, invalid excluded worker - typically
    // a worker who existed when the picker was populated but has since been
    // removed). The backend's own detail is the authority on why; no
    // eligibility rule is reimplemented here to guess at it.
    const error = new Error(body?.detail ?? `status ${response.status}`)
    error.detail = body?.detail ?? null
    throw error
  }
  if (!isValidCoverageResponse(body, shiftId, excludeEmployeeCode)) {
    throw new Error('unexpected shape')
  }
  return body
}

function ResultRow({ result }) {
  return (
    <tr>
      <td>{result.employee_code}</td>
      <td>{result.full_name}</td>
      <td>{preferenceLabel(result.preference)}</td>
      <td>{result.assigned_hours_this_week} h</td>
      <td>{result.projected_weekly_hours} h</td>
      <td>{result.weekly_hour_limit} h</td>
    </tr>
  )
}

function Coverage() {
  const [shiftStatus, setShiftStatus] = useState('loading')
  const [shifts, setShifts] = useState([])
  const [shiftAttempt, setShiftAttempt] = useState(0)

  // Progressive selection: a hall must be chosen before a date is offered,
  // and a date before a shift is offered. Each is derived client-side from
  // the already-loaded `shifts` list - see shiftPicker.js.
  const [selectedHall, setSelectedHall] = useState('')
  const [selectedDate, setSelectedDate] = useState('')
  const [selectedShiftId, setSelectedShiftId] = useState('')
  const [excludeCode, setExcludeCode] = useState('')

  // null until a shift is selected, so "nothing chosen yet" is a distinct
  // state from "loading" or "failed to load".
  const [coverageStatus, setCoverageStatus] = useState(null)
  const [coverage, setCoverage] = useState(null)
  const [coverageErrorMessage, setCoverageErrorMessage] = useState(null)
  const [coverageAttempt, setCoverageAttempt] = useState(0)

  // The "loading" transition is set by whatever TRIGGERS a fetch (the retry
  // button, selecting a shift, changing the exclusion) rather than inside the
  // effect body itself: an effect should only report the async result it is
  // synchronizing with, not also perform the synchronous state change that
  // starts it - the initial value above already covers first mount.
  useEffect(() => {
    let cancelled = false

    fetchShiftList().then(
      (list) => {
        if (!cancelled) {
          setShifts(list)
          setShiftStatus('success')
        }
      },
      () => {
        if (!cancelled) {
          setShiftStatus('error')
        }
      },
    )

    return () => {
      cancelled = true
    }
  }, [shiftAttempt])

  useEffect(() => {
    if (selectedShiftId === '') {
      return
    }
    let cancelled = false

    fetchCoverage(selectedShiftId, excludeCode).then(
      (data) => {
        if (!cancelled) {
          setCoverage(data)
          setCoverageStatus('success')
        }
      },
      (error) => {
        if (!cancelled) {
          setCoverageErrorMessage(error.detail ?? null)
          setCoverageStatus('error')
        }
      },
    )

    return () => {
      cancelled = true
    }
  }, [selectedShiftId, excludeCode, coverageAttempt])

  function retryShiftList() {
    setShiftStatus('loading')
    setShiftAttempt(shiftAttempt + 1)
  }

  // Clears everything downstream of a filter that just changed, including
  // any displayed coverage - a stale result for a shift no longer selected
  // must never linger on screen looking current.
  function resetShiftSelection() {
    setSelectedShiftId('')
    setExcludeCode('')
    setCoverageStatus(null)
    setCoverage(null)
    setCoverageErrorMessage(null)
  }

  function selectHall(hall) {
    if (hall === selectedHall) {
      return
    }
    setSelectedHall(hall)
    setSelectedDate('')
    resetShiftSelection()
  }

  function selectDate(date) {
    if (date === selectedDate) {
      return
    }
    setSelectedDate(date)
    resetShiftSelection()
  }

  function selectShift(shiftId) {
    // Reselecting the shift already shown is a no-op: nothing to reload, and
    // unconditionally resetting to 'loading' here would freeze on it forever,
    // since the effect below only re-runs when one of its dependencies
    // actually changes.
    if (shiftId === selectedShiftId) {
      return
    }
    // A different shift starts over: the exclusion picker is worker-specific
    // to the shift's own results, and holding a code that happens to also
    // exist among the new shift's workers would silently carry an exclusion
    // the supervisor never chose for this shift.
    setSelectedShiftId(shiftId)
    setExcludeCode('')
    setCoverageStatus('loading')
    setCoverageErrorMessage(null)
  }

  function changeExclude(code) {
    // Same guard as selectShift: choosing the exclusion already in effect
    // changes nothing, and must not reset to 'loading' with no dependency
    // change left to end it.
    if (code === excludeCode) {
      return
    }
    setExcludeCode(code)
    setCoverageStatus('loading')
    setCoverageErrorMessage(null)
  }

  function retryCoverage() {
    setCoverageStatus('loading')
    setCoverageErrorMessage(null)
    setCoverageAttempt(coverageAttempt + 1)
  }

  if (shiftStatus === 'loading') {
    return (
      <p className="backend-status backend-status-loading">Loading shifts...</p>
    )
  }

  if (shiftStatus === 'error') {
    return (
      <p className="backend-status backend-status-error" aria-live="polite">
        Could not load shifts. The backend may be offline or returned an
        unexpected response.{' '}
        <button type="button" onClick={retryShiftList}>
          Retry loading
        </button>
      </p>
    )
  }

  if (shifts.length === 0) {
    return (
      <p className="backend-status backend-status-loading">
        No shifts are stored yet. Generate the demo dataset or add shifts
        before looking up coverage.
      </p>
    )
  }

  const excludedButEligible =
    coverage !== null &&
    coverage.excluded_employee_code !== null &&
    coverage.results.find(
      (result) => result.employee_code === coverage.excluded_employee_code,
    )
  const excludedIsEligible = excludedButEligible && excludedButEligible.eligible

  const ineligible =
    coverage !== null ? coverage.results.filter((result) => !result.eligible) : []

  const halls = hallsFromShifts(shifts)
  const dates = selectedHall === '' ? [] : datesForHall(shifts, selectedHall)
  const matchingShifts =
    selectedHall === '' || selectedDate === ''
      ? []
      : shiftsForHallAndDate(shifts, selectedHall, selectedDate)

  return (
    <>
      <div className="list-controls">
        <label htmlFor="coverage-hall">Hall</label>
        <select
          id="coverage-hall"
          value={selectedHall}
          onChange={(event) => selectHall(event.target.value)}
        >
          <option value="" disabled>
            Choose a hall
          </option>
          {halls.map((hall) => (
            <option key={hall} value={hall}>
              {hall}
            </option>
          ))}
        </select>

        <label htmlFor="coverage-date">Date</label>
        <select
          id="coverage-date"
          value={selectedDate}
          onChange={(event) => selectDate(event.target.value)}
          disabled={selectedHall === ''}
        >
          <option value="" disabled>
            {selectedHall === '' ? 'Choose a hall first' : 'Choose a date'}
          </option>
          {dates.map((date) => (
            <option key={date} value={date}>
              {friendlyDate(date)}
            </option>
          ))}
        </select>

        <label htmlFor="coverage-shift">Shift</label>
        <select
          id="coverage-shift"
          value={selectedShiftId}
          onChange={(event) => selectShift(event.target.value)}
          disabled={selectedDate === ''}
        >
          <option value="" disabled>
            {selectedDate === '' ? 'Choose a date first' : 'Choose a shift'}
          </option>
          {matchingShifts.map((shift) => (
            <option key={shift.id} value={String(shift.id)}>
              {shiftPickerLabel(shift)}
            </option>
          ))}
        </select>
      </div>

      {selectedHall !== '' && selectedDate !== '' && (
        <p className="table-note">
          {matchingShifts.length} {matchingShifts.length === 1 ? 'shift' : 'shifts'}{' '}
          at {selectedHall} on {friendlyDate(selectedDate)}.
        </p>
      )}

      {selectedHall === '' && (
        <p className="backend-status backend-status-loading">
          Select a hall, then a date, to choose a shift.
        </p>
      )}

      {selectedHall !== '' && selectedDate === '' && (
        <p className="backend-status backend-status-loading">
          Select a date to choose a shift at {selectedHall}.
        </p>
      )}

      {selectedDate !== '' && selectedShiftId === '' && (
        <p className="backend-status backend-status-loading">
          Select a shift above to see who is eligible to cover it.
        </p>
      )}

      {coverageStatus === 'loading' && (
        <p className="backend-status backend-status-loading">
          Loading coverage for shift #{selectedShiftId}...
        </p>
      )}

      {coverageStatus === 'error' && (
        <p className="backend-status backend-status-error" aria-live="polite">
          {coverageErrorMessage ?? 'Could not load coverage for this shift.'}{' '}
          <button type="button" onClick={retryCoverage}>
            Retry
          </button>
          {/* An exclusion can fail on its own - the excluded worker existed
              when the picker was populated but has since been removed, for
              example - and plain Retry would just resend the same refused
              request forever. This clears only the exclusion, keeping the
              selected shift, and immediately requests coverage without it. */}
          {excludeCode !== '' && (
            <>
              {' '}
              <button type="button" onClick={() => changeExclude('')}>
                Clear exclusion and retry
              </button>
            </>
          )}
        </p>
      )}

      {coverageStatus === 'success' && coverage !== null && (
        <>
          <h4 className="detail-title">{shiftSummaryLabel(coverage.shift)}</h4>
          <p className="table-note">
            Duration: {coverage.shift.duration_hours} hours. Eligibility
            below uses only the backend&rsquo;s deterministic rules &mdash;
            active status, confirmed and accepted (non-provisional) timetable
            coverage, class conflicts, approved leave, existing assignments
            and the weekly hour limit. Preference (preferred, low or neutral)
            is shown as a fact only: a low-preference worker who passes every
            rule still counts as eligible.
          </p>

          <div className="list-controls">
            <label htmlFor="coverage-exclude">Exclude worker</label>
            <select
              id="coverage-exclude"
              value={excludeCode}
              onChange={(event) => changeExclude(event.target.value)}
            >
              <option value="">None</option>
              {coverage.results.map((result) => (
                <option key={result.employee_code} value={result.employee_code}>
                  {result.employee_code} — {result.full_name}
                </option>
              ))}
            </select>
          </div>
          <p className="table-note">
            For a replacement or call-out: excluding a worker removes them
            from the eligible list below so they are not proposed to replace
            themselves. It changes nothing about their stored records or
            their own eligibility facts, and makes no assignment.
          </p>

          <h4>
            Eligible workers ({coverage.eligible_candidates.length})
          </h4>
          {excludedIsEligible && (
            <p className="table-note">
              {excludedButEligible.employee_code} ({excludedButEligible.full_name})
              is eligible but excluded from this list above, so they cannot be
              proposed as their own replacement.
            </p>
          )}
          {coverage.eligible_candidates.length === 0 ? (
            <p className="backend-status backend-status-loading">
              No eligible workers for this shift right now.
            </p>
          ) : (
            <div className="table-wrapper">
              <table className="data-table">
                <caption className="table-caption">
                  Eligible for {shiftSummaryLabel(coverage.shift)}
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Employee ID</th>
                    <th scope="col">Name</th>
                    <th scope="col">Preference</th>
                    <th scope="col">Assigned this week</th>
                    <th scope="col">Projected if assigned</th>
                    <th scope="col">Weekly limit</th>
                  </tr>
                </thead>
                <tbody>
                  {coverage.eligible_candidates.map((result) => (
                    <ResultRow key={result.employee_id} result={result} />
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h4>Not eligible ({ineligible.length})</h4>
          {ineligible.length === 0 ? (
            <p className="backend-status backend-status-loading">
              Every current worker is eligible for this shift.
            </p>
          ) : (
            <div className="table-wrapper">
              <table className="data-table">
                <caption className="table-caption">
                  Not eligible for {shiftSummaryLabel(coverage.shift)}
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Employee ID</th>
                    <th scope="col">Name</th>
                    <th scope="col">Preference</th>
                    <th scope="col">Reasons</th>
                  </tr>
                </thead>
                <tbody>
                  {ineligible.map((result) => (
                    <tr key={result.employee_id}>
                      <td>{result.employee_code}</td>
                      <td>{result.full_name}</td>
                      <td>{preferenceLabel(result.preference)}</td>
                      <td>
                        <ul>
                          {result.reasons.map((reason, index) => (
                            <li key={index}>{reason}</li>
                          ))}
                        </ul>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </>
  )
}

export default Coverage
