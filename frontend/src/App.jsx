import { useState } from 'react'
import './App.css'
import BackendStatus from './BackendStatus.jsx'
import Coverage from './Coverage.jsx'
import EmployeeList from './EmployeeList.jsx'
import Schedule from './Schedule.jsx'
import WeekSelector from './WeekSelector.jsx'
import { DEFAULT_WEEK_START } from './weeks.js'

const NAV_ITEMS = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'employees', label: 'Employees' },
  { id: 'schedule', label: 'Schedule' },
  { id: 'coverage', label: 'Coverage' },
  { id: 'generate-schedule', label: 'Generate Schedule' },
  { id: 'workforce-planning', label: 'Workforce Planning' },
  { id: 'ai-assistant', label: 'AI Assistant' },
]

const SECTION_CONTENT = {
  dashboard: {
    title: 'Dashboard',
    description: 'Operational overview will appear here.',
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
  'generate-schedule': {
    title: 'Generate Schedule',
    description:
      'Generate a coverage-maximizing proposal for the selected week, review exactly what it would change, and approve or reject it. This is the same Schedule view as the Schedule tab.',
  },
  'workforce-planning': {
    title: 'Workforce Planning',
    description: 'Capacity and staffing scenarios will appear here.',
  },
  'ai-assistant': {
    title: 'AI Assistant',
    description: 'Natural-language scheduling questions will appear here.',
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
  const showsSchedule = activeSection === 'schedule' || activeSection === 'generate-schedule'
  const showWeekSelector = activeSection === 'employees' || showsSchedule

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
        <h2>{content.title}</h2>
        <p>{content.description}</p>
        {showWeekSelector && <WeekSelector weekStart={weekStart} onChange={setWeekStart} />}
        {activeSection === 'dashboard' && <BackendStatus />}
        {activeSection === 'employees' && <EmployeeList weekStart={weekStart} />}
        {/* "Schedule" and "Generate Schedule" are two sidebar entries into
            the same workflow (see Schedule.jsx) - one mounted instance kept
            alive across both, remounted only when the shared week changes,
            so there is exactly one copy of its proposal/replace state. */}
        {showsSchedule && <Schedule key={weekStart} weekStart={weekStart} />}
        {activeSection === 'coverage' && <Coverage />}
      </main>
    </>
  )
}

export default App
