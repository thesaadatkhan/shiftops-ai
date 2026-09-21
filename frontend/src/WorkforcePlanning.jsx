// The Workforce Planning screen (Phase 8): a read-only aggregate scenario
// calculator plus the current week's actual operational feasibility, kept
// visibly separate (see docs/PROJECT_SPEC.md's Workforce Planning section
// and D051) - the theoretical minimum from the scenario calculator is never
// labeled as a feasible workforce size, and nothing here generates workers,
// runs a hiring/firing recommendation, or modifies any stored record.
//
// Uses the same App-owned reporting week Dashboard/Employees/Schedule share
// (see App.jsx), and the same `GET /api/analytics/weeks/{week_start}` call
// Dashboard.jsx uses - one shared analytics contract, not a second one.

import { useEffect, useRef, useState } from 'react'

import { describeError, fetchWeekAnalytics, workforceScenario } from './analytics.js'
import MetricCard from './MetricCard.jsx'
import { friendlyDate } from './shiftPicker.js'

const DEFAULT_HOURS_PER_WORKER = 20

export default function WorkforcePlanning({ weekStart }) {
  const [state, setState] = useState({ status: 'loading', data: null, error: null })
  const requestToken = useRef(0)

  // Explicit supervisor inputs only - never inferred or defaulted from the
  // CURRENT workforce beyond a reasonable starting point to type over.
  const [workerCountText, setWorkerCountText] = useState('')
  const [hoursPerWorkerText, setHoursPerWorkerText] = useState(String(DEFAULT_HOURS_PER_WORKER))

  async function load() {
    const token = ++requestToken.current
    setState((current) => ({ status: 'loading', data: current.data, error: null }))
    try {
      const data = await fetchWeekAnalytics(weekStart)
      if (token === requestToken.current) {
        setState({ status: 'success', data, error: null })
        // Seed the worker-count input with the current active headcount the
        // first time real data arrives for this week, purely as a
        // convenient starting point to type over - never re-applied on a
        // later reload, so it does not fight with whatever the supervisor
        // has already typed.
        setWorkerCountText((current) => (current === '' ? String(data.workforce.active_workers) : current))
      }
    } catch (error) {
      if (token === requestToken.current) {
        setState({ status: 'error', data: null, error })
      }
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [weekStart])

  if (state.status === 'loading' && !state.data) {
    return <p>Loading workforce planning data…</p>
  }

  if (state.status === 'error') {
    return (
      <div>
        <p role="alert">Could not load workforce planning data: {describeError(state.error)}</p>
        <button type="button" onClick={load}>
          Retry
        </button>
      </div>
    )
  }

  const { coverage } = state.data
  const workerCount = Number(workerCountText)
  const hoursPerWorker = Number(hoursPerWorkerText)
  // A worker count is a count - 2.5 hypothetical workers is not a
  // meaningful input, unlike weekly hours per worker, which may
  // legitimately be fractional (a part-time assumption, say).
  const validInputs =
    workerCountText !== '' &&
    hoursPerWorkerText !== '' &&
    Number.isInteger(workerCount) &&
    Number.isFinite(hoursPerWorker) &&
    workerCount >= 0 &&
    hoursPerWorker >= 0

  const scenario = validInputs
    ? workforceScenario(coverage.required_coverage_hours, workerCount, hoursPerWorker)
    : null

  return (
    <div className="workforce-planning-view">
      <p className="reporting-period">
        Reporting week: {friendlyDate(coverage.week_start)} – {friendlyDate(coverage.week_end)}
      </p>

      <section className="dashboard-section">
        <h3>Current operational feasibility</h3>
        {coverage.shift_count === 0 ? (
          <p className="table-note">
            No shifts are prepared for this week yet, so there is no actual coverage to report.
          </p>
        ) : (
          <p className="table-note">
            The stored schedule for this week is currently at {coverage.coverage_percentage}% coverage,
            with {coverage.uncovered_positions} uncovered position
            {coverage.uncovered_positions === 1 ? '' : 's'} across {coverage.unfilled_shift_count} shift
            {coverage.unfilled_shift_count === 1 ? '' : 's'}. This is the actual, current schedule - not a
            projection.
          </p>
        )}
        <p className="table-note">
          Required coverage hours for this week: <strong>{coverage.required_coverage_hours}</strong>.
        </p>
      </section>

      <section className="dashboard-section">
        <h3>Workforce-size scenario</h3>
        <p role="alert" className="scenario-disclaimer">
          This is only an aggregate lower bound on hours. It does not prove a feasible schedule
          exists - specific workers&apos; classes, approved leave, overlapping shifts, and per-shift
          availability all still matter and are not modeled here. It never generates workers, runs
          a hiring/firing recommendation, or changes any stored record.
        </p>

        <div className="list-controls">
          <label>
            Hypothetical worker count
            <input
              type="number"
              min="0"
              step="1"
              value={workerCountText}
              onChange={(event) => setWorkerCountText(event.target.value)}
            />
          </label>
          <label>
            Weekly hours per hypothetical worker
            <input
              type="number"
              min="0"
              step="0.5"
              value={hoursPerWorkerText}
              onChange={(event) => setHoursPerWorkerText(event.target.value)}
            />
          </label>
        </div>

        {!validInputs && (
          <p role="alert">
            Enter a whole worker count (0, 1, 2, …) and nonnegative weekly hours to see a scenario
            result.
          </p>
        )}

        {scenario && (
          <div className="metric-cards">
            <MetricCard
              label="Hypothetical theoretical capacity"
              value={`${scenario.hypotheticalCapacityHours} hrs`}
            />
            <MetricCard
              label={scenario.capacityGapHours >= 0 ? 'Capacity surplus' : 'Capacity shortfall'}
              value={`${Math.abs(scenario.capacityGapHours)} hrs`}
            />
            <MetricCard
              label="Theoretical minimum workers"
              value={
                scenario.theoreticalMinimumWorkers === null
                  ? 'Undefined (0 weekly hours)'
                  : scenario.theoreticalMinimumWorkers
              }
              note="ceil(required coverage hours / weekly hours per worker) - a lower bound, not a feasible size."
            />
            <MetricCard
              label="Aggregate capacity sufficient?"
              value={scenario.capacitySufficient ? 'Yes' : 'No'}
            />
          </div>
        )}
      </section>
    </div>
  )
}

