import { useState } from 'react'
import './App.css'
import BackendStatus from './BackendStatus.jsx'
import EmployeeList from './EmployeeList.jsx'

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
    description: 'Generated and existing assignments will appear here.',
  },
  coverage: {
    title: 'Coverage',
    description: 'Shift eligibility lookups will appear here.',
  },
  'generate-schedule': {
    title: 'Generate Schedule',
    description: 'Schedule generation controls will appear here.',
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
  const content = SECTION_CONTENT[activeSection]

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
        {activeSection === 'dashboard' && <BackendStatus />}
        {activeSection === 'employees' && <EmployeeList />}
      </main>
    </>
  )
}

export default App
