// The Dashboard screen (Phase 8): a supervisor's high-level read of one
// reporting week, built entirely from `GET /api/analytics/weeks/{week_start}`
// - the same shared App-owned reporting week Employees and Schedule already
// use (see App.jsx), so switching weeks anywhere keeps every screen in
// sync. This never prepares a week's shifts; an unprepared week is shown
// honestly as zero stored shifts, not an error and not a silent side effect
// of viewing the dashboard.

import { useEffect, useRef, useState } from 'react'

import { describeError, fetchWeekAnalytics } from './analytics.js'
import { EMPLOYEES_URL } from './employees.js'
import MetricCard from './MetricCard.jsx'
import { fetchWeekSchedule } from './schedule.js'
import { friendlyDate } from './shiftPicker.js'

const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function shiftBlockClass(hall) {
  return `schedule-grid-shift hall-${hall.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`
}

async function fetchWeekEmployees(weekStart) {
  const response = await fetch(`${EMPLOYEES_URL}?week_start=${encodeURIComponent(weekStart)}`)
  if (!response.ok) throw new Error(`Backend responded with status ${response.status}`)
  const data = await response.json()
  if (!data || !Array.isArray(data.employees)) {
    throw new Error('Employee response did not have the expected shape.')
  }
  return data.employees
}

function WeeklyScheduleGrid({ employees, shifts, weekStart }) {
  const shiftsByWorkerAndDay = new Map()
  for (const shift of shifts) {
    const date = shift.start_datetime.split(' ')[0]
    for (const worker of shift.assigned_employees) {
      const key = `${worker.employee_code}:${date}`
      shiftsByWorkerAndDay.set(key, [...(shiftsByWorkerAndDay.get(key) || []), shift])
    }
  }
  const days = Array.from({ length: 7 }, (_, index) => {
    const date = new Date(`${weekStart}T12:00:00`)
    date.setDate(date.getDate() + index)
    return date.toISOString().slice(0, 10)
  })

  return (
    <section className="dashboard-section weekly-schedule-section" aria-labelledby="weekly-schedule-heading">
      <h3 id="weekly-schedule-heading">Weekly assignment grid</h3>
      <p className="table-note">Read-only view of stored assignments. A leave marker means approved leave is on record; this response does not provide leave dates.</p>
      <div className="weekly-schedule-grid" role="table" aria-label="Weekly employee assignment grid">
        <div className="weekly-schedule-header" role="row">
          <div role="columnheader">Worker</div>
          {days.map((date, index) => <div key={date} role="columnheader">{WEEKDAY_LABELS[index]}</div>)}
        </div>
        {employees.map((employee) => (
          <div key={employee.employee_code} className="weekly-schedule-row" role="row">
            <div className="weekly-schedule-worker" role="rowheader">
              <strong>{employee.full_name}</strong>
              <span>{employee.employee_code}</span>
              {employee.approved_leave_count > 0 && <span className="weekly-status-indicator" title="Approved leave on record" aria-label="Approved leave on record">Leave</span>}
            </div>
            {days.map((date) => (
              <div key={date} className="weekly-schedule-day" role="cell">
                {(shiftsByWorkerAndDay.get(`${employee.employee_code}:${date}`) || []).map((shift) => (
                  <div key={shift.id} className={shiftBlockClass(shift.hall)} title={`${shift.hall}, ${shift.start_datetime.slice(11)}–${shift.end_datetime.slice(11)}`}>
                    <span>{shift.hall}</span><small>{shift.start_datetime.slice(11, 16)}</small>
                  </div>
                ))}
              </div>
            ))}
          </div>
        ))}
      </div>
    </section>
  )
}

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
  const [activeView, setActiveView] = useState('grid')
  // Guards against a slow response for a PREVIOUS week landing after the
  // supervisor has already moved to a different one - the same pattern
  // EmployeeDetails.jsx's requestToken and Schedule.jsx's
  // replaceRequestToken already use for exactly this race.
  const requestToken = useRef(0)

  async function load() {
    const token = ++requestToken.current
    setState((current) => ({ status: 'loading', data: current.data, error: null }))
    try {
      const [data, schedule, employees] = await Promise.all([
        fetchWeekAnalytics(weekStart),
        fetchWeekSchedule(weekStart),
        fetchWeekEmployees(weekStart),
      ])
      if (token === requestToken.current) {
        setState({ status: 'success', data: { ...data, schedule, employees }, error: null })
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

  const { coverage, workforce, schedule, employees } = state.data

  return (
    <div className="dashboard-view">
      <p className="reporting-period">
        Reporting week: {friendlyDate(coverage.week_start)} – {friendlyDate(coverage.week_end)}
      </p>

      <div className="dashboard-summary-grid">
        {coverage.shift_count === 0 ? (
          <section className="dashboard-section"><h3>Coverage</h3><p className="table-note">
            No shifts are prepared for this week yet. Prepare the week from the Schedule tab to see
            real coverage metrics here - this dashboard never prepares a week on its own.
          </p></section>
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

        </section>
      </div>

      <div className="schedule-tabs dashboard-tabs" role="tablist" aria-label="Dashboard detail view">
        <button type="button" role="tab" aria-selected={activeView === 'grid'} className={activeView === 'grid' ? 'is-selected' : ''} onClick={() => setActiveView('grid')}>Weekly grid</button>
        <button type="button" role="tab" aria-selected={activeView === 'utilization'} className={activeView === 'utilization' ? 'is-selected' : ''} onClick={() => setActiveView('utilization')}>Worker utilization</button>
      </div>
      {activeView === 'grid' ? <WeeklyScheduleGrid employees={employees} shifts={schedule.shifts} weekStart={weekStart} /> : (
      <section className="dashboard-section" role="tabpanel" aria-label="Worker utilization">
        <h3>Worker utilization</h3>
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
                  <td><div className="utilization-cell"><ProgressBar percentage={worker.utilization_percentage} /><span>{worker.utilization_percentage}%</span></div></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>)}
    </div>
  )
}
