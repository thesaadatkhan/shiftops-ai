import { useEffect, useState } from 'react'

const EMPLOYEES_URL = 'http://127.0.0.1:8000/api/employees'

const STUDENT_TYPE_LABELS = {
  undergraduate: 'Undergraduate',
  masters: "Master's",
}

function EmployeeList() {
  const [status, setStatus] = useState('loading')
  const [employees, setEmployees] = useState([])

  useEffect(() => {
    let cancelled = false

    async function loadEmployees() {
      try {
        const response = await fetch(EMPLOYEES_URL)

        if (!response.ok) {
          throw new Error(`Backend responded with status ${response.status}`)
        }

        const data = await response.json()

        if (!cancelled) {
          setEmployees(data)
          setStatus('success')
        }
      } catch {
        if (!cancelled) {
          setStatus('error')
        }
      }
    }

    loadEmployees()

    return () => {
      cancelled = true
    }
  }, [])

  if (status === 'loading') {
    return <p className="backend-status backend-status-loading">Loading employees...</p>
  }

  if (status === 'error') {
    return (
      <p className="backend-status backend-status-error">
        Could not load employees. The backend may be offline or returned an
        unexpected response.
      </p>
    )
  }

  if (employees.length === 0) {
    return (
      <p className="backend-status backend-status-loading">
        No employees found. Seed the database with: python seed.py
      </p>
    )
  }

  return (
    <div className="table-wrapper">
      <table className="data-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>Name</th>
            <th>Student type</th>
            <th>Courses</th>
            <th>Class meetings</th>
            <th>Class hours/week</th>
            <th>Weekly limit</th>
            <th>Approved leave</th>
          </tr>
        </thead>
        <tbody>
          {employees.map((employee) => (
            <tr key={employee.employee_code}>
              <td>{employee.employee_code}</td>
              <td>{employee.full_name}</td>
              <td>
                {STUDENT_TYPE_LABELS[employee.student_type] ??
                  employee.student_type}
              </td>
              <td>{employee.course_count}</td>
              <td>{employee.class_meeting_count}</td>
              <td>{employee.weekly_class_hours}</td>
              <td>{employee.weekly_hour_limit} h</td>
              <td>{employee.approved_leave_count}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default EmployeeList
