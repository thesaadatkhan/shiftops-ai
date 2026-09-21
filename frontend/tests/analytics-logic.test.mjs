// Focused regression checks for frontend/src/analytics.js (Phase 8
// acceptance corrections): the week-binding validator and the
// whole-worker-count rule for workforceScenario(). Same standalone,
// no-framework style as tests/schedule-logic.test.mjs - mocks
// `global.fetch`, asserts with a small inline `check()` helper, no server,
// no browser, no project database.
//
// Run with:  node tests/analytics-logic.test.mjs   (from the frontend/ directory)
// Exits non-zero if any check fails.

import { fetchWeekAnalytics, workforceScenario } from '../src/analytics.js'

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

function validAnalyticsBody(weekStart) {
  return {
    coverage: {
      week_start: weekStart,
      week_end: '2026-10-11',
      shift_count: 0,
      required_positions: 0,
      filled_positions: 0,
      uncovered_positions: 0,
      unfilled_shift_count: 0,
      excess_assignments: 0,
      required_coverage_hours: 0,
      scheduled_coverage_hours: 0,
      recorded_assignment_hours: 0,
      coverage_percentage: 100.0,
    },
    workforce: {
      total_workers: 0,
      active_workers: 0,
      timetable_ready_workers: 0,
      active_and_timetable_ready_workers: 0,
      active_theoretical_capacity_hours: 0,
      active_assigned_hours: 0,
      theoretical_remaining_active_capacity_hours: 0,
      recorded_assigned_hours_all_workers: 0,
      workers: [],
    },
  }
}

// ------------------------------------------------- week-binding validator

mockFetchOnce(200, validAnalyticsBody('2026-10-05'))
const matching = await fetchWeekAnalytics('2026-10-05')
check(matching.coverage.week_start === '2026-10-05', 'a response whose week_start matches the request is accepted')

mockFetchOnce(200, validAnalyticsBody('2026-10-12'))
try {
  await fetchWeekAnalytics('2026-10-05')
  check(false, 'a response for a DIFFERENT week than requested is rejected, not silently accepted')
} catch (error) {
  check(
    error.message.includes('expected shape'),
    `a week-mismatched response is rejected as malformed ("${error.message}")`,
  )
}

// --------------------------------------------- whole-worker-count rule

const wholeCount = workforceScenario(100, 5, 20)
check(wholeCount.hypotheticalWorkerCount === 5, 'a valid whole hypothetical worker count is accepted')
check(wholeCount.theoreticalMinimumWorkers === 5, 'and produces a scenario result (ceil(100/20) = 5)')

try {
  workforceScenario(100, 2.5, 20)
  check(false, 'a fractional hypothetical worker count is rejected, not silently truncated or accepted')
} catch (error) {
  check(
    error.message.toLowerCase().includes('whole number'),
    `a fractional worker count throws a clear whole-number message ("${error.message}")`,
  )
}

// Weekly hours per worker is deliberately NOT restricted to a whole number.
const fractionalHours = workforceScenario(100, 5, 7.5)
check(
  fractionalHours.hypotheticalCapacityHours === 37.5,
  'fractional weekly hours per worker is accepted and used directly (5 x 7.5 = 37.5)',
)

console.log(`\n${passed} passed, ${failed} failed.`)
process.exit(failed === 0 ? 0 : 1)
