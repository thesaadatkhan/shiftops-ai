// Shared by Dashboard and WorkforcePlanning: the one Phase 8 analytics API
// call and its response-shape validation - the same discipline schedule.js
// and employees.js already established, so neither screen can render a
// malformed or unexpectedly-shaped backend response as if it were real
// data.

const ANALYTICS_WEEKS_URL = 'http://127.0.0.1:8000/api/analytics/weeks'

function analyticsUrl(weekStart) {
  return `${ANALYTICS_WEEKS_URL}/${encodeURIComponent(weekStart)}`
}

// A non-2xx response from this read-only endpoint always means nothing was
// changed (there is nothing here to change) - carries the real HTTP status
// and the backend's own string `detail` (D033) so 400 (invalid week) reads
// distinctly from a 500 (corrupt stored shift data).
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

const COVERAGE_NUMBER_FIELDS = [
  'shift_count',
  'required_positions',
  'filled_positions',
  'uncovered_positions',
  'unfilled_shift_count',
  'excess_assignments',
  'required_coverage_hours',
  'scheduled_coverage_hours',
  'recorded_assignment_hours',
  'coverage_percentage',
]

function isValidCoverage(coverage) {
  return (
    typeof coverage === 'object' &&
    coverage !== null &&
    typeof coverage.week_start === 'string' &&
    typeof coverage.week_end === 'string' &&
    COVERAGE_NUMBER_FIELDS.every((field) => typeof coverage[field] === 'number')
  )
}

const WORKFORCE_NUMBER_FIELDS = [
  'total_workers',
  'active_workers',
  'timetable_ready_workers',
  'active_and_timetable_ready_workers',
  'active_theoretical_capacity_hours',
  'active_assigned_hours',
  'theoretical_remaining_active_capacity_hours',
  'recorded_assigned_hours_all_workers',
]

function isValidWorkerRow(row) {
  return (
    typeof row === 'object' &&
    row !== null &&
    typeof row.employee_code === 'string' &&
    typeof row.full_name === 'string' &&
    typeof row.is_active === 'boolean' &&
    typeof row.scheduling_ready === 'boolean' &&
    typeof row.weekly_hour_limit === 'number' &&
    typeof row.assigned_hours === 'number' &&
    typeof row.remaining_capacity_hours === 'number' &&
    typeof row.utilization_percentage === 'number'
  )
}

function isValidWorkforce(workforce) {
  return (
    typeof workforce === 'object' &&
    workforce !== null &&
    WORKFORCE_NUMBER_FIELDS.every((field) => typeof workforce[field] === 'number') &&
    Array.isArray(workforce.workers) &&
    workforce.workers.every(isValidWorkerRow)
  )
}

// `expectedWeekStart` binds a response to the request that asked for it -
// not just "is this shaped like an analytics payload", but "is this
// actually the week we requested". Without this, a late response for an
// abandoned week (already guarded against at the React layer by a
// requestToken ref in Dashboard.jsx/WorkforcePlanning.jsx) or a
// misconfigured/rewritten backend response could pass shape validation
// while silently describing the wrong week - the same class of bug D033's
// week-mismatch check in schedule.js's `isValidWeekSchedule` already guards
// against for the schedule endpoint.
function isValidAnalytics(data, expectedWeekStart) {
  return (
    typeof data === 'object' &&
    data !== null &&
    isValidCoverage(data.coverage) &&
    data.coverage.week_start === expectedWeekStart &&
    isValidWorkforce(data.workforce)
  )
}

export async function fetchWeekAnalytics(weekStart) {
  let response
  try {
    response = await fetch(analyticsUrl(weekStart))
  } catch (error) {
    throw new Error(`Could not reach the backend (${error.message}).`, { cause: error })
  }
  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response))
  }
  let data
  try {
    data = await response.json()
  } catch (error) {
    throw new Error(
      `The backend accepted this request (status ${response.status}), but its response body could not ` +
        `be read (${error.message}).`,
      { cause: error },
    )
  }
  if (!isValidAnalytics(data, weekStart)) {
    throw new Error('The analytics response did not have the expected shape.')
  }
  return data
}

export function describeError(error) {
  if (!error) {
    return null
  }
  if (error instanceof ApiError) {
    return typeof error.detail === 'string'
      ? `${error.status}: ${error.detail}`
      : `Request failed with status ${error.status}.`
  }
  return error.message || 'Something went wrong.'
}

// Workforce Planning's scenario calculator - pure client-side arithmetic,
// deliberately NOT a second backend round trip: it only needs the
// `required_coverage_hours` figure already fetched by `fetchWeekAnalytics`
// plus two explicit supervisor inputs, and recomputing it live as those
// inputs change would otherwise mean a network request per keystroke.
// Mirrors `backend/analytics.py`'s `workforce_scenario` formula exactly -
// see that function's docstring and `backend/verify_analytics.py` for the
// tested formula this must stay identical to.
//
// This is an AGGREGATE LOWER BOUND ONLY. It never proves a feasible
// schedule exists - specific workers' classes, approved leave, overlapping
// shifts and per-shift availability are not modeled at all. Callers must
// present `theoreticalMinimumWorkers`/`capacitySufficient` as exactly that:
// a lower bound, never a feasible workforce size, and never a hiring or
// firing recommendation.
export function workforceScenario(requiredCoverageHours, hypotheticalWorkerCount, weeklyHoursPerWorker) {
  // A worker count is a count - 2.5 hypothetical workers is not a
  // meaningful input, unlike weekly hours per worker, which may legitimately
  // be fractional (a supervisor testing a part-time assumption, say).
  if (!Number.isInteger(hypotheticalWorkerCount)) {
    throw new Error('Hypothetical worker count must be a whole number.')
  }
  if (hypotheticalWorkerCount < 0 || weeklyHoursPerWorker < 0) {
    throw new Error('Scenario inputs must not be negative.')
  }

  const hypotheticalCapacityHours = hypotheticalWorkerCount * weeklyHoursPerWorker
  const capacityGapHours = hypotheticalCapacityHours - requiredCoverageHours

  let theoreticalMinimumWorkers
  if (requiredCoverageHours <= 0) {
    // Nothing is required - zero hypothetical workers already suffice.
    theoreticalMinimumWorkers = 0
  } else if (weeklyHoursPerWorker <= 0) {
    // A zero-hour worker can never accumulate any positive required hours,
    // no matter how many of them exist - mathematically undefined, not 0.
    theoreticalMinimumWorkers = null
  } else {
    theoreticalMinimumWorkers = Math.ceil(requiredCoverageHours / weeklyHoursPerWorker)
  }

  return {
    requiredCoverageHours,
    hypotheticalWorkerCount,
    weeklyHoursPerWorker,
    hypotheticalCapacityHours,
    capacityGapHours,
    theoreticalMinimumWorkers,
    capacitySufficient: hypotheticalCapacityHours >= requiredCoverageHours,
  }
}
