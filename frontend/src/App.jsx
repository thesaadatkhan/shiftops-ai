import { useState } from 'react'
import './App.css'
import AppIcon from './AppIcon.jsx'
import AIAssistant from './AIAssistant.jsx'
import Coverage from './Coverage.jsx'
import Dashboard from './Dashboard.jsx'
import EmployeeList from './EmployeeList.jsx'
import Schedule from './Schedule.jsx'
import WeekSelector from './WeekSelector.jsx'
import WorkforcePlanning from './WorkforcePlanning.jsx'
import { DEFAULT_WEEK_START } from './weeks.js'

const NAV_ITEMS = [
  { id: 'dashboard', label: 'Dashboard', icon: 'dashboard' },
  { id: 'employees', label: 'Employees', icon: 'employees' },
  { id: 'schedule', label: 'Schedule', icon: 'schedule' },
  { id: 'coverage', label: 'Coverage', icon: 'coverage' },
  { id: 'workforce-planning', label: 'Workforce Planning', icon: 'planning' },
  { id: 'ai-assistant', label: 'AI Assistant', icon: 'assistant' },
]

const SECTION_CONTENT = {
  dashboard: {
    title: 'Dashboard',
    description: 'Weekly coverage and staffing at a glance.',
  },
  employees: {
    title: 'Employees',
    description: 'Manage workers, class schedules, capacity, and leave.',
  },
  schedule: {
    title: 'Schedule',
    description: 'Review shifts, coverage gaps, and schedule proposals.',
  },
  coverage: {
    title: 'Coverage',
    description: 'Check who can cover a shift and understand any conflicts.',
  },
  'workforce-planning': {
    title: 'Workforce Planning',
    description: 'Compare scheduled demand with theoretical staffing capacity.',
  },
  'ai-assistant': {
    title: 'AI Assistant',
    description: 'Investigate scheduling questions and prepare changes for your approval.',
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
        <h1 className="app-title">
          <span className="app-mark"><AppIcon name="logo" /></span>
          <span>
            ShiftOps
            <small>Scheduling workspace</small>
          </span>
        </h1>
        <p className="sidebar-label">Workspace</p>
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
                  <AppIcon name={item.icon} />
                  {item.label}
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <div className="sidebar-footer">
          <span><i /> Demo environment</span>
          <small>Synthetic workforce data</small>
        </div>
      </aside>

      <main className={activeSection === 'ai-assistant' ? 'main-content ai-assistant-page' : 'main-content'}>
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
