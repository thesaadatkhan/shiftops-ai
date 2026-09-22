import { useState } from 'react'
import './App.css'
import AIAssistant from './AIAssistant.jsx'
import Coverage from './Coverage.jsx'
import Dashboard from './Dashboard.jsx'
import EmployeeList from './EmployeeList.jsx'
import Schedule from './Schedule.jsx'
import WeekSelector from './WeekSelector.jsx'
import WorkforcePlanning from './WorkforcePlanning.jsx'
import { DEFAULT_WEEK_START } from './weeks.js'

const NAV_ITEMS = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'employees', label: 'Employees' },
  { id: 'schedule', label: 'Schedule' },
  { id: 'coverage', label: 'Coverage' },
  { id: 'workforce-planning', label: 'Workforce Planning' },
  { id: 'ai-assistant', label: 'AI Assistant' },
]

const SECTION_CONTENT = {
  dashboard: {
    title: 'Dashboard',
    description:
      'Coverage and workforce metrics for the selected reporting week, computed from stored shifts, assignments and current workforce records.',
  },
  employees: {
    title: 'Employees',
    description: 'Synthetic student workers, their course loads, and approved leave.',
  },
  schedule: {
    title: 'Schedule',
    description:
      'The stored schedule for the selected week: every prepared shift, who is assigned, and what remains uncovered. Generate, review, and approve a proposal from here too.',
  },
  coverage: {
    title: 'Coverage',
    description:
      'Select a stored shift to see which workers are deterministically eligible to cover it, and why others are not.',
  },
  'workforce-planning': {
    title: 'Workforce Planning',
    description:
      'Current operational feasibility for the selected week, plus an aggregate, lower-bound workforce-size scenario calculator - not a feasibility guarantee.',
  },
  'ai-assistant': {
    title: 'AI Assistant',
    description:
      'Describe a scheduling problem in plain language and the agent will investigate using the same deterministic backend data as the rest of this app. It can propose a specific replacement or fill, but nothing changes until you explicitly approve it.',
  },
}

function App() {
  const [activeSection, setActiveSection] = useState('dashboard')
  // One reporting week, shared across every section that reads it (Phase 7
  // increment 4), so switching between Employees and Schedule never resets
  // which week is on screen. Changing it here never calls the backend on
  // its own - it only changes what the two screens ask for next.
  const [weekStart, setWeekStart] = useState(DEFAULT_WEEK_START)
  const content = SECTION_CONTENT[activeSection]
  const showWeekSelector =
    activeSection === 'employees' ||
    activeSection === 'schedule' ||
    activeSection === 'dashboard' ||
    activeSection === 'workforce-planning'

  return (
    <>
      <aside className="sidebar">
        <h1 className="app-title">ShiftOps AI</h1>
        <nav>
          <ul>
            {NAV_ITEMS.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  className={
                    item.id === activeSection ? 'nav-item active' : 'nav-item'
                  }
                  aria-current={item.id === activeSection ? 'page' : undefined}
                  onClick={() => setActiveSection(item.id)}
                >
                  {item.label}
                </button>
              </li>
            ))}
          </ul>
        </nav>
      </aside>

      <main className="main-content">
        <div className="page-header">
          <h2>{content.title}</h2>
          <p>{content.description}</p>
        </div>
        {showWeekSelector && <WeekSelector weekStart={weekStart} onChange={setWeekStart} />}
        {/* Dashboard and Workforce Planning stay mounted across a week
            change and re-fetch via their own weekStart-keyed effect (each
            guards a slow response for a since-abandoned week with its own
            request-token ref) - unlike Schedule below, neither holds
            per-week workflow state that a week change should reset, and
            Workforce Planning's scenario inputs are deliberately kept
            across a week change (a supervisor's "what if N workers"
            question is not specific to one week). */}
        {activeSection === 'dashboard' && <Dashboard weekStart={weekStart} />}
        {activeSection === 'employees' && <EmployeeList weekStart={weekStart} />}
        {/* Schedule.jsx also contains the Generate Schedule action, proposal
            review, approval and replacement flows - there is no separate
            "Generate Schedule" screen, just this one, remounted only when
            the shared week changes so there is exactly one copy of its
            proposal/replace state. */}
        {activeSection === 'schedule' && <Schedule key={weekStart} weekStart={weekStart} />}
        {activeSection === 'coverage' && <Coverage />}
        {activeSection === 'workforce-planning' && <WorkforcePlanning weekStart={weekStart} />}
        {activeSection === 'ai-assistant' && <AIAssistant />}
      </main>
    </>
  )
}

export default App
