// Shared by the Schedule screen: where the Phase 7 increment 1-3 APIs live,
// response-shape validation before anything reaches state (the same
// discipline EmployeeDetails.jsx and Coverage.jsx already established - a
// syntactically valid 2xx body is not trusted by construction), and a small
// typed error so callers can tell a 400/404/409/503 apart instead of
// collapsing every failure into "backend offline".

const SCHEDULE_WEEKS_URL = 'http://127.0.0.1:8000/api/schedule/weeks'
const SCHEDULE_PROPOSALS_URL = 'http://127.0.0.1:8000/api/schedule/proposals'
export const SCHEDULE_REPLACE_URL = 'http://127.0.0.1:8000/api/schedule/assignments/replace'

function weekScheduleUrl(weekStart) {
  return `${SCHEDULE_WEEKS_URL}/${encodeURIComponent(weekStart)}`
}

function prepareWeekUrl(weekStart) {
  return `${weekScheduleUrl(weekStart)}/prepare`
}

function createProposalUrl(weekStart) {
  return `${weekScheduleUrl(weekStart)}/proposals`
}

function listProposalsUrl(weekStart) {
  return `${weekScheduleUrl(weekStart)}/proposals`
}

function proposalUrl(id) {
  return `${SCHEDULE_PROPOSALS_URL}/${encodeURIComponent(id)}`
}

// Carries the real HTTP status and the backend's own `detail` (a string for
// most errors, or a structured object for a revalidation/replacement
// conflict - see D047) so a caller can render 400/404/409/503 distinctly
// instead of folding every failure into one generic message.
export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === 'string' ? detail : `Request failed with status ${status}`)
    this.status = status
    this.detail = detail
  }
}

async function readDetail(response) {
  try {
    const body = await response.json()
    return body && typeof body === 'object' && 'detail' in body ? body.detail : null
  } catch {
    return null
  }
}

async function requestJson(url, options) {
  const response = await fetch(url, options)
  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response))
  }
  return response.json()
}

function hasUniqueKeys(items, keyOf) {
  const seen = new Set()
  for (const item of items) {
    const key = keyOf(item)
    if (seen.has(key)) {
      return false
    }
    seen.add(key)
  }
  return true
}

function isValidShift(shift) {
  return (
    typeof shift === 'object' &&
    shift !== null &&
    typeof shift.id === 'number' &&
    typeof shift.hall === 'string' &&
    typeof shift.start_datetime === 'string' &&
    typeof shift.end_datetime === 'string' &&
    typeof shift.duration_hours === 'number' &&
    typeof shift.required_staff === 'number' &&
    Array.isArray(shift.assigned_employees) &&
    shift.assigned_employees.every(
      (worker) =>
        typeof worker.employee_id === 'number' &&
        typeof worker.employee_code === 'string' &&
        typeof worker.full_name === 'string',
    ) &&
    // No two rows may name the same worker twice on one shift.
    hasUniqueKeys(shift.assigned_employees, (worker) => worker.employee_id) &&
    typeof shift.assigned_count === 'number' &&
    // The count field must actually match the list it claims to count.
    shift.assigned_count === shift.assigned_employees.length &&
    typeof shift.covered === 'boolean' &&
    // Coverage must agree with staffing: covered iff enough workers are on it.
    shift.covered === shift.assigned_count >= shift.required_staff
  )
}

function isValidWeekSchedule(data, expectedWeekStart) {
  return (
    typeof data === 'object' &&
    data !== null &&
    typeof data.week_start === 'string' &&
    (expectedWeekStart === undefined || data.week_start === expectedWeekStart) &&
    typeof data.week_end === 'string' &&
    Array.isArray(data.shifts) &&
    data.shifts.every(isValidShift) &&
    hasUniqueKeys(data.shifts, (shift) => shift.id)
  )
}

function isValidReviewSummary(summary) {
  if (typeof summary !== 'object' || summary === null) {
    return false
  }
  const fields = [
    'required_positions',
    'existing_filled_positions',
    'proposed_filled_positions',
    'total_filled_positions',
    'uncovered_positions',
    'excess_assignments',
  ]
  if (!fields.every((field) => typeof summary[field] === 'number')) {
    return false
  }
  return (
    summary.total_filled_positions === summary.existing_filled_positions + summary.proposed_filled_positions &&
    summary.uncovered_positions === Math.max(0, summary.required_positions - summary.total_filled_positions)
  )
}

function isValidReview(review) {
  // No stored snapshot (a legacy proposal predating this column) is a valid
  // answer, not a malformed one - it just means no detailed review to show.
  if (review === null) {
    return true
  }
  return (
    typeof review === 'object' &&
    Array.isArray(review.shifts) &&
    isValidReviewSummary(review.summary) &&
    review.shifts.every(
      (shift) =>
        typeof shift.id === 'number' &&
        typeof shift.required_staff === 'number' &&
        Array.isArray(shift.existing_assignments) &&
        Array.isArray(shift.proposed_assignments) &&
        typeof shift.filled_count === 'number' &&
        typeof shift.excess_assignments === 'number' &&
        typeof shift.covered === 'boolean' &&
        typeof shift.uncovered_positions === 'number',
    )
  )
}

function reviewProposedPairs(review) {
  const pairs = new Set()
  for (const shift of review.shifts) {
    for (const worker of shift.proposed_assignments) {
      pairs.add(`${shift.id}:${worker.employee_id}`)
    }
  }
  return pairs
}

function isValidProposal(data, { expectedWeekStart, expectedId } = {}) {
  if (
    typeof data !== 'object' ||
    data === null ||
    typeof data.id !== 'number' ||
    (expectedId !== undefined && data.id !== expectedId) ||
    typeof data.week_start !== 'string' ||
    (expectedWeekStart !== undefined && data.week_start !== expectedWeekStart) ||
    !['pending', 'approved', 'rejected'].includes(data.status) ||
    !Array.isArray(data.assignments) ||
    !data.assignments.every(
      (row) =>
        typeof row.shift_id === 'number' &&
        typeof row.employee_id === 'number' &&
        typeof row.employee_code === 'string' &&
        typeof row.full_name === 'string' &&
        typeof row.hall === 'string' &&
        typeof row.start_datetime === 'string' &&
        typeof row.end_datetime === 'string',
    ) ||
    // The same worker cannot be named twice for the same shift in one proposal.
    !hasUniqueKeys(data.assignments, (row) => `${row.shift_id}:${row.employee_id}`) ||
    !('review' in data) ||
    !isValidReview(data.review)
  ) {
    return false
  }
  if (data.review !== null) {
    const reviewPairs = reviewProposedPairs(data.review)
    const assignmentPairs = new Set(data.assignments.map((row) => `${row.shift_id}:${row.employee_id}`))
    if (reviewPairs.size !== assignmentPairs.size) {
      return false
    }
    for (const pair of assignmentPairs) {
      if (!reviewPairs.has(pair)) {
        return false
      }
    }
  }
  return true
}

export async function fetchWeekSchedule(weekStart) {
  const data = await requestJson(weekScheduleUrl(weekStart))
  if (!isValidWeekSchedule(data, weekStart)) {
    throw new Error('The weekly schedule response did not have the expected shape.')
  }
  return data
}

export async function prepareWeek(weekStart) {
  return requestJson(prepareWeekUrl(weekStart), { method: 'POST' })
}

export async function createProposal(weekStart) {
  const data = await requestJson(createProposalUrl(weekStart), { method: 'POST' })
  if (!isValidProposal(data, { expectedWeekStart: weekStart })) {
    throw new Error('The proposal response did not have the expected shape.')
  }
  return data
}

export async function fetchProposalsForWeek(weekStart) {
  const data = await requestJson(listProposalsUrl(weekStart))
  if (
    !Array.isArray(data) ||
    !data.every((proposal) => isValidProposal(proposal, { expectedWeekStart: weekStart }))
  ) {
    throw new Error('The proposal list response did not have the expected shape.')
  }
  return data
}

export async function approveProposal(id, assignments) {
  const data = await requestJson(`${proposalUrl(id)}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assignments }),
  })
  if (!isValidProposal(data, { expectedId: id })) {
    throw new Error('The approval response did not have the expected shape.')
  }
  return data
}

export async function rejectProposal(id) {
  const data = await requestJson(`${proposalUrl(id)}/reject`, { method: 'POST' })
  if (!isValidProposal(data, { expectedId: id })) {
    throw new Error('The rejection response did not have the expected shape.')
  }
  return data
}

export async function replaceAssignment(shiftId, outgoingEmployeeCode, incomingEmployeeCode) {
  return requestJson(SCHEDULE_REPLACE_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      shift_id: shiftId,
      outgoing_employee_code: outgoingEmployeeCode,
      incoming_employee_code: incomingEmployeeCode,
    }),
  })
}

/** A short, accurate description of any error this module's functions throw
 * - an ApiError's real status plus its string or structured `detail`, or a
 * plain Error's message. Never collapses a 400/404/409/503 into one generic
 * "backend offline" line. */
export function describeError(error) {
  if (!error) {
    return null
  }
  if (error instanceof ApiError) {
    if (typeof error.detail === 'string') {
      return `${error.status}: ${error.detail}`
    }
    if (error.detail && typeof error.detail === 'object') {
      const reasons =
        error.detail.reasons ||
        error.detail.reason_codes ||
        (error.detail.message ? [error.detail.message] : [])
      return `${error.status}: ${reasons.length ? reasons.join('; ') : 'Refused - see the conflict details below.'}`
    }
    return `Request failed with status ${error.status}.`
  }
  return error.message || 'Something went wrong.'
}
