// The Dashboard screen (Phase 8): a supervisor's high-level read of one
// reporting week, built entirely from `GET /api/analytics/weeks/{week_start}`
// - the same shared App-owned reporting week Employees and Schedule already
// use (see App.jsx), so switching weeks anywhere keeps every screen in
// sync. This never prepares a week's shifts; an unprepared week is shown
// honestly as zero stored shifts, not an error and not a silent side effect
// of viewing the dashboard.

import { useEffect, useRef, useState } from 'react'

import { describeError, fetchWeekAnalytics } from './analytics.js'
import MetricCard from './MetricCard.jsx'
import { friendlyDate } from './shiftPicker.js'

function ProgressBar({ percentage }) {
  const clamped = Math.max(0, Math.min(100, percentage))
  return (
    <div className="progress-bar" role="progressbar" aria-valuenow={percentage} aria-valuemin={0} aria-valuemax={100}>
      <div className="progress-bar-fill" style={{ width: `${clamped}%` }} />
    </div>
  )
}

export default function Dashboard({ weekStart }) {
  const [state, setState] = useState({ status: 'loading', data: null, error: null })
  // Guards against a slow response for a PREVIOUS week landing after the
  // supervisor has already moved to a different one - the same pattern
  // EmployeeDetails.jsx's requestToken and Schedule.jsx's
  // replaceRequestToken already use for exactly this race.
  const requestToken = useRef(0)

  async function load() {
    const token = ++requestToken.current
    setState((current) => ({ status: 'loading', data: current.data, error: null }))
    try {
      const data = await fetchWeekAnalytics(weekStart)
      if (token === requestToken.current) {
        setState({ status: 'success', data, error: null })
      }
    } catch (error) {
      if (token === requestToken.current) {
        setState({ status: 'error', data: null, error })
      }
    }
  }

  useEffect(() => {
    // load()'s own setState calls happen after its internal `await`, so
    // this is not a synchronous setState-in-effect in practice.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [weekStart])

  if (state.status === 'loading' && !state.data) {
    return <p>Loading dashboard metrics…</p>
  }

  if (state.status === 'error') {
    return (
      <div>
        <p role="alert">Could not load dashboard metrics: {describeError(state.error)}</p>
        <button type="button" onClick={load}>
          Retry
        </button>
      </div>
    )
  }

  const { coverage, workforce } = state.data

  return (
    <div className="dashboard-view">
      <p className="reporting-period">
        Reporting week: {friendlyDate(coverage.week_start)} – {friendlyDate(coverage.week_end)}
      </p>

      {coverage.shift_count === 0 ? (
        <p className="table-note">
          No shifts are prepared for this week yet. Prepare the week from the Schedule tab to see
          real coverage metrics here - this dashboard never prepares a week on its own.
        </p>
      ) : (
        <section className="dashboard-section">
          <h3>Coverage</h3>
          <ProgressBar percentage={coverage.coverage_percentage} />
          <p className="table-note">
            {coverage.coverage_percentage}% of required coverage hours are scheduled (
            {coverage.scheduled_coverage_hours} of {coverage.required_coverage_hours} hours).
          </p>
          <div className="metric-cards">
            <MetricCard label="Stored shifts" value={coverage.shift_count} />
            <MetricCard label="Required positions" value={coverage.required_positions} />
            <MetricCard label="Filled positions" value={coverage.filled_positions} />
            <MetricCard label="Uncovered positions" value={coverage.uncovered_positions} />
            <MetricCard label="Unfilled shifts" value={coverage.unfilled_shift_count} />
            <MetricCard
              label="Excess assignments"
              value={coverage.excess_assignments}
              note={coverage.excess_assignments > 0 ? 'More workers assigned than a shift requires.' : null}
            />
          </div>
        </section>
      )}

      <section className="dashboard-section">
        <h3>Workforce</h3>
        <div className="metric-cards">
          <MetricCard label="Total workers" value={workforce.total_workers} />
          <MetricCard label="Active workers" value={workforce.active_workers} />
          <MetricCard label="Timetable-ready workers" value={workforce.timetable_ready_workers} />
          <MetricCard
            label="Active & timetable-ready"
            value={workforce.active_and_timetable_ready_workers}
          />
          <MetricCard
            label="Active theoretical capacity"
            value={`${workforce.active_theoretical_capacity_hours} hrs`}
            note="Sum of active workers' weekly hour limits - not eligibility."
          />
          <MetricCard label="Active assigned hours" value={`${workforce.active_assigned_hours} hrs`} />
          <MetricCard
            label="Theoretical remaining capacity"
            value={`${workforce.theoretical_remaining_active_capacity_hours} hrs`}
          />
          <MetricCard
            label="Recorded hours, all workers"
            value={`${workforce.recorded_assigned_hours_all_workers} hrs`}
            note="Includes workers who are now inactive."
          />
        </div>

        <h4>Worker utilization</h4>
        <div className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th>Worker</th>
                <th>Status</th>
                <th>Timetable ready</th>
                <th>Weekly limit</th>
                <th>Assigned hours</th>
                <th>Remaining capacity</th>
                <th>Utilization</th>
              </tr>
            </thead>
            <tbody>
              {workforce.workers.map((worker) => (
                <tr key={worker.employee_code}>
                  <td>
                    {worker.full_name} ({worker.employee_code})
                  </td>
                  <td>{worker.is_active ? 'Active' : 'Inactive'}</td>
                  <td>{worker.scheduling_ready ? 'Yes' : 'No'}</td>
                  <td>{worker.weekly_hour_limit}</td>
                  <td>{worker.assigned_hours}</td>
                  <td>{worker.remaining_capacity_hours}</td>
                  <td>{worker.utilization_percentage}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}

