// Repository-tracked logic checks for frontend/src/schedule.js (Codex
// whole-project review finding 7: journey checks must be reproducible in
// the repository, not left as one-off scratch evidence).
//
// This project has no test runner installed (no vitest/jest/@playwright/test
// in package.json) - adding one was judged out of scope for a Monday-deadline
// correctness pass. So this file is a standalone Node script: it imports
// schedule.js directly, mocks `global.fetch` per case, and asserts with a
// small inline `check()` helper. No server, no browser, no project database.
//
// Run with:  node tests/schedule-logic.test.mjs   (from the frontend/ directory)
// Exits non-zero if any check fails.
//
// What this does NOT cover: Schedule.jsx's own interactive behavior (the
// approval-confirmation panel, double-submit locking, the replacement
// candidate race guard, rendering). Those are verified by code review plus
// this logic layer, and - where a live browser journey exists - by
// tests/e2e-journey.mjs alongside this file.

import {
  ApiError,
  ResponseValidationError,
  NetworkOutcomeUnknownError,
  isOutcomeUncertain,
  approveProposal,
  createAssignment,
  createProposal,
  fetchProposalsForWeek,
  fetchWeekSchedule,
  groupProposalReviewShifts,
  proposalReviewFilter,
  rejectProposal,
  replaceAssignment,
  describeError,
} from '../src/schedule.js'

let passed = 0
let failed = 0
function check(condition, description) {
  if (condition) {
    passed += 1
    console.log(`PASS  ${description}`)
  } else {
    failed += 1
    console.log(`FAIL  ${description}`)
  }
}

function mockFetchOnce(status, body) {
  global.fetch = async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  })
}

function mockFetchThrows(message) {
  global.fetch = async () => {
    throw new TypeError(message)
  }
}

function mockFetchMalformedBody(status) {
  global.fetch = async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      throw new SyntaxError('Unexpected end of JSON input')
    },
  })
}

const validShift = {
  id: 1,
  hall: 'Andromeda',
  start_datetime: '2026-10-05 08:00',
  end_datetime: '2026-10-05 13:00',
  duration_hours: 5,
  required_staff: 1,
  assigned_employees: [{ employee_id: 1, employee_code: 'SW-001', full_name: 'A', conflicts: null }],
  assigned_count: 1,
  covered: true,
}

const validReview = {
  shifts: [
    {
      id: 1,
      hall: 'Andromeda',
      start_datetime: '2026-10-05 08:00',
      end_datetime: '2026-10-05 13:00',
      required_staff: 1,
      existing_assignments: [],
      proposed_assignments: [{ employee_id: 1, employee_code: 'SW-001', full_name: 'A' }],
      filled_count: 1,
      excess_assignments: 0,
      covered: true,
      uncovered_positions: 0,
    },
  ],
  summary: {
    required_positions: 1,
    existing_filled_positions: 0,
    proposed_filled_positions: 1,
    total_filled_positions: 1,
    uncovered_positions: 0,
    excess_assignments: 0,
  },
}

const validProposal = {
  id: 7,
  week_start: '2026-10-05',
  status: 'pending',
  review: validReview,
  assignments: [
    {
      shift_id: 1,
      employee_id: 1,
      employee_code: 'SW-001',
      full_name: 'A',
      hall: 'Andromeda',
      start_datetime: '2026-10-05 08:00',
      end_datetime: '2026-10-05 13:00',
    },
  ],
}

const unchangedCoveredReviewShift = {
  ...validReview.shifts[0],
  id: 2,
  hall: 'Capella',
  start_datetime: '2026-10-06 08:00',
  end_datetime: '2026-10-06 13:00',
  existing_assignments: [{ employee_id: 2, employee_code: 'SW-002', full_name: 'B' }],
  proposed_assignments: [],
}
const uncoveredReviewShift = {
  ...validReview.shifts[0],
  id: 3,
  hall: 'Capella',
  start_datetime: '2026-10-06 14:00',
  end_datetime: '2026-10-06 19:00',
  proposed_assignments: [],
  filled_count: 0,
  covered: false,
  uncovered_positions: 1,
}

check(proposalReviewFilter(validReview.shifts[0], 'changes'), 'Changes includes shifts with proposed workers')
check(!proposalReviewFilter(unchangedCoveredReviewShift, 'changes'), 'Changes omits unchanged covered shifts')
check(proposalReviewFilter(uncoveredReviewShift, 'uncovered'), 'Uncovered includes only shifts still needing positions')
const reviewGroups = groupProposalReviewShifts([validReview.shifts[0], unchangedCoveredReviewShift, uncoveredReviewShift], 'all')
check(reviewGroups.length === 2 && reviewGroups[1].halls[0].hall === 'Capella' && reviewGroups[1].halls[0].shifts.length === 2, 'review shifts group by date and hall without changing the stored snapshot')

// ---- three-way error typing (Codex review finding 5) --------------------

// 1. A confirmed refusal (non-2xx): ApiError, never claims uncertainty.
mockFetchOnce(409, { detail: { message: 'stale', conflicts: [] } })
try {
  await approveProposal(7, [])
  check(false, 'a 409 refusal throws')
} catch (error) {
  check(error instanceof ApiError, 'a 409 refusal throws ApiError specifically')
  check(!isOutcomeUncertain(error), 'a confirmed refusal (ApiError) is never treated as an uncertain outcome')
}

// 2. A committed write with an unparseable body: ResponseValidationError,
// and isOutcomeUncertain() must recognize it so the UI can reconcile.
mockFetchMalformedBody(200)
try {
  await approveProposal(7, [])
  check(false, 'a 2xx with unparseable JSON throws')
} catch (error) {
  check(
    error instanceof ResponseValidationError,
    'a 2xx response with unparseable JSON throws ResponseValidationError (the write still committed)',
  )
  check(isOutcomeUncertain(error), 'ResponseValidationError is recognized as an uncertain outcome to reconcile')
}

// 3. A 2xx with a well-formed but shape-invalid body is the SAME case -
// committed, but this response cannot be trusted.
mockFetchOnce(200, { not: 'a proposal' })
try {
  await approveProposal(7, [])
  check(false, 'a 2xx with the wrong shape throws')
} catch (error) {
  check(
    error instanceof ResponseValidationError,
    'a 2xx with a malformed shape also throws ResponseValidationError, not a generic Error',
  )
}

// 4. The request never reaching the backend at all: NetworkOutcomeUnknownError.
mockFetchThrows('network error')
try {
  await approveProposal(7, [])
  check(false, 'a network-level failure throws')
} catch (error) {
  check(
    error instanceof NetworkOutcomeUnknownError,
    'fetch() rejecting (no response at all) throws NetworkOutcomeUnknownError',
  )
  check(isOutcomeUncertain(error), 'NetworkOutcomeUnknownError is also recognized as an uncertain outcome')
  check(
    !(error instanceof ApiError),
    'a network failure is never mistaken for a confirmed ApiError refusal',
  )
}

// ---- identity/shape validation (unchanged behavior, re-asserted) --------

mockFetchOnce(200, { week_start: '2026-10-12', week_end: '2026-10-18', shifts: [] })
try {
  await fetchWeekSchedule('2026-10-05')
  check(false, 'a weekly schedule response for a different week than requested is rejected')
} catch (error) {
  check(error instanceof ResponseValidationError, 'the week-mismatch rejection is a ResponseValidationError')
}

mockFetchOnce(200, { week_start: '2026-10-05', week_end: '2026-10-11', shifts: [validShift] })
const week = await fetchWeekSchedule('2026-10-05')
check(week.shifts.length === 1, 'a well-formed, matching-week schedule response is accepted')
check(week.shifts[0].assigned_employees[0].conflicts === null, 'a conflict-free assignment carries conflicts: null')

mockFetchOnce(200, {
  ...week,
  shifts: [
    {
      ...validShift,
      assigned_employees: [{ ...validShift.assigned_employees[0], conflicts: { reason_codes: ['x'], reasons: ['y'] } }],
    },
  ],
})
const withConflict = await fetchWeekSchedule('2026-10-05')
check(
  withConflict.shifts[0].assigned_employees[0].conflicts.reasons[0] === 'y',
  'a structured conflict on an assigned worker is accepted and passed through',
)

mockFetchOnce(201, validProposal)
const created = await createProposal('2026-10-05')
check(created.id === 7, 'a well-formed, matching-week proposal is accepted')

mockFetchOnce(200, [{ ...validProposal, review: null }])
const list = await fetchProposalsForWeek('2026-10-05')
check(list.length === 1 && list[0].review === null, 'a legacy proposal with review: null still passes list validation')

mockFetchOnce(200, validProposal)
const rejected = await rejectProposal(7)
check(rejected.status === 'pending', 'rejectProposal returns the validated body unchanged')

mockFetchOnce(200, { shift_id: 1, outgoing_employee_code: 'SW-001', incoming_employee_code: 'SW-002', occurred_at: 'x' })
const replaced = await replaceAssignment(1, 'SW-001', 'SW-002')
check(replaced.incoming_employee_code === 'SW-002', 'replaceAssignment returns the backend body (no shape contract of its own)')

mockFetchOnce(201, { shift_id: 1, incoming_employee_code: 'SW-002', occurred_at: '2026-09-20 12:00' })
const assigned = await createAssignment(1, 'SW-002')
check(assigned.incoming_employee_code === 'SW-002', 'createAssignment accepts an exact matching mutation response')

mockFetchOnce(201, { shift_id: 999, incoming_employee_code: 'SW-002', occurred_at: '2026-09-20 12:00' })
try {
  await createAssignment(1, 'SW-002')
  check(false, 'createAssignment should reject a response for another shift')
} catch (error) {
  check(error instanceof ResponseValidationError, 'createAssignment rejects a mismatched success response as outcome-uncertain')
}

check(describeError(null) === null, 'describeError(null) is null - no error to describe')
mockFetchThrows('offline')
try {
  await approveProposal(7, [])
} catch (error) {
  check(
    describeError(error).includes('reconcile') || describeError(error).toLowerCase().includes('unknown'),
    `describeError() explains an uncertain-outcome error accurately rather than a generic message ("${describeError(error)}")`,
  )
}

console.log(`\n${passed} passed, ${failed} failed.`)
process.exit(failed === 0 ? 0 : 1)
