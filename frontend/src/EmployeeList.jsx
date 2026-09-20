import { useEffect, useState } from 'react'

const EMPLOYEES_URL = 'http://127.0.0.1:8000/api/employees'

const STUDENT_TYPE_LABELS = {
  undergraduate: 'Undergraduate',
  masters: "Master's",
}

const SORT_FIELDS = [
  { value: 'employee_code', label: 'Employee ID' },
  { value: 'full_name', label: 'Name' },
  { value: 'student_type', label: 'Student type' },
  { value: 'course_count', label: 'Courses' },
  { value: 'weekly_class_hours', label: 'Class hours/week' },
  { value: 'remaining_capacity_hours', label: 'Remaining capacity' },
]

// Sorted with subtraction rather than localeCompare, so 10 sorts after 9
// instead of before it.
const NUMERIC_SORT_FIELDS = new Set([
  'course_count',
  'weekly_class_hours',
  'remaining_capacity_hours',
])

const STATUS_FILTERS = [
  { value: 'active', label: 'Active' },
  { value: 'inactive', label: 'Inactive' },
  { value: 'all', label: 'All' },
]

// Matches the order the backend already returns, so the default view is
// exactly the table as it looked before these controls existed.
const DEFAULT_SORT_FIELD = 'employee_code'
const DEFAULT_SORT_DIRECTION = 'asc'
const DEFAULT_STATUS_FILTER = 'active'

function studentTypeLabel(employee) {
  return STUDENT_TYPE_LABELS[employee.student_type] ?? employee.student_type
}

function sortValue(employee, field) {
  // Sort student type by the label shown in the table, so the ordering the
  // user sees matches the text they are reading.
  return field === 'student_type' ? studentTypeLabel(employee) : employee[field]
}

function compareEmployees(first, second, field) {
  if (NUMERIC_SORT_FIELDS.has(field)) {
    return first[field] - second[field]
  }
  return sortValue(first, field).localeCompare(sortValue(second, field))
}

function matchesSearch(employee, query) {
  if (query === '') {
    return true
  }
  return (
    employee.full_name.toLowerCase().includes(query) ||
    employee.employee_code.toLowerCase().includes(query)
  )
}

function isValidEmployeesResponse(data) {
  // Checked before anything reaches state, so an older or otherwise
  // unexpected response (for example the bare array this endpoint used to
  // return) shows the error state instead of crashing the render.
  return (
    typeof data === 'object' &&
    data !== null &&
    !Array.isArray(data) &&
    typeof data.week_start === 'string' &&
    typeof data.week_end === 'string' &&
    Array.isArray(data.employees)
  )
}

const EMPTY_FORM = { employee_code: '', full_name: '', student_type: 'undergraduate' }

async function fetchEmployees() {
  const response = await fetch(EMPLOYEES_URL)
  if (!response.ok) {
    throw new Error(`Backend responded with status ${response.status}`)
  }
  const data = await response.json()
  if (!isValidEmployeesResponse(data)) {
    throw new Error('Employee response did not have the expected shape')
  }
  return data
}

function matchesStatus(employee, statusFilter) {
  if (statusFilter === 'all') {
    return true
  }
  return statusFilter === 'active' ? employee.is_active : !employee.is_active
}

function EmployeeList() {
  const [status, setStatus] = useState('loading')
  const [employees, setEmployees] = useState([])
  const [week, setWeek] = useState({ start: '', end: '' })
  const [searchText, setSearchText] = useState('')
  const [sortField, setSortField] = useState(DEFAULT_SORT_FIELD)
  const [sortDirection, setSortDirection] = useState(DEFAULT_SORT_DIRECTION)
  const [statusFilter, setStatusFilter] = useState(DEFAULT_STATUS_FILTER)
  const [formMode, setFormMode] = useState(null)
  const [formValues, setFormValues] = useState(EMPTY_FORM)
  const [editingCode, setEditingCode] = useState(null)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState(null)
  const [feedback, setFeedback] = useState(null)
  const [listError, setListError] = useState(null)

  useEffect(() => {
    let cancelled = false

    async function loadEmployees() {
      try {
        const data = await fetchEmployees()

        if (!cancelled) {
          setEmployees(data.employees)
          setWeek({ start: data.week_start, end: data.week_end })
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

  function openCreateForm() {
    setFormMode('create')
    setEditingCode(null)
    setFormValues(EMPTY_FORM)
    setFormError(null)
    setFeedback(null)
  }

  function openEditForm(employee) {
    setFormMode('edit')
    setEditingCode(employee.employee_code)
    setFormValues({
      employee_code: employee.employee_code,
      full_name: employee.full_name,
      student_type: employee.student_type,
    })
    setFormError(null)
    setFeedback(null)
  }

  function closeForm() {
    // Cancel discards the entered values; a failed save keeps them instead.
    setFormMode(null)
    setEditingCode(null)
    setFormValues(EMPTY_FORM)
    setFormError(null)
  }

  async function refreshList(savedEmployee = null) {
    // Only ever reloads. Safe to call again from the Retry control, because
    // it never re-sends a write.
    try {
      const data = await fetchEmployees()
      setEmployees(data.employees)
      setWeek({ start: data.week_start, end: data.week_end })
      setListError(null)

      if (savedEmployee !== null) {
        const saved = data.employees.find(
          (employee) => employee.employee_code === savedEmployee.employee_code,
        )
        const query = searchText.trim().toLowerCase()
        const hidden =
          saved !== undefined &&
          !(matchesStatus(saved, statusFilter) && matchesSearch(saved, query))

        if (hidden) {
          setFeedback({
            tone: 'notice',
            message: `Saved ${savedEmployee.full_name} (${savedEmployee.employee_code}), but the current search or status filter hides them. Reset the controls to see them.`,
          })
        }
      }
      return true
    } catch {
      setListError(
        'Could not reload the employee list. Anything already saved is still saved.',
      )
      return false
    }
  }

  async function handleSave(event) {
    event.preventDefault()
    if (saving) {
      return
    }

    setSaving(true)
    setFormError(null)

    const creating = formMode === 'create'
    let response

    // Step 1: the write. Its outcome decides what we are allowed to claim.
    try {
      response = await fetch(
        creating
          ? EMPLOYEES_URL
          : `${EMPLOYEES_URL}/${encodeURIComponent(editingCode)}`,
        {
          method: creating ? 'POST' : 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(formValues),
        },
      )
    } catch {
      // The request never produced a response, so whether the server applied
      // it is genuinely unknown. Claiming it was not saved could be wrong.
      setFormError(
        'Could not reach the backend, so it is unclear whether this was saved. ' +
          'Reload the list to check before trying again.',
      )
      setSaving(false)
      return
    }

    const body = await response.json().catch(() => null)

    if (!response.ok) {
      // Genuinely rejected: keep the form open with what was typed.
      setFormError(body?.detail ?? `Save failed (status ${response.status}).`)
      setSaving(false)
      return
    }

    // Step 2: the write is confirmed. Close the form first so it can never
    // still target the old employee code after a rename, and report success
    // before attempting the reload - a failed reload does not undo the save.
    closeForm()
    setFeedback({
      tone: 'success',
      message: `Saved ${body.full_name} (${body.employee_code}).`,
    })
    await refreshList(body)
    setSaving(false)
  }

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

  const query = searchText.trim().toLowerCase()

  // filter() returns a new array, so the sort below never reorders the
  // `employees` state itself - sorting in place would corrupt the fetched data.
  const visibleEmployees = employees
    .filter(
      (employee) =>
        matchesStatus(employee, statusFilter) && matchesSearch(employee, query),
    )
    .sort((first, second) => {
      const comparison = compareEmployees(first, second, sortField)
      if (comparison !== 0) {
        return sortDirection === 'asc' ? comparison : -comparison
      }
      // Employee code breaks ties in the same direction every time, so equal
      // values never shuffle when the sort direction flips.
      return first.employee_code.localeCompare(second.employee_code)
    })

  const isDefaultView =
    searchText === '' &&
    sortField === DEFAULT_SORT_FIELD &&
    sortDirection === DEFAULT_SORT_DIRECTION &&
    statusFilter === DEFAULT_STATUS_FILTER

  function resetControls() {
    setSearchText('')
    setSortField(DEFAULT_SORT_FIELD)
    setSortDirection(DEFAULT_SORT_DIRECTION)
    setStatusFilter(DEFAULT_STATUS_FILTER)
  }

  const employeeForm = (
    <form className="employee-form" onSubmit={handleSave}>
      <h3>{formMode === 'create' ? 'Add employee' : `Edit ${editingCode}`}</h3>
      <div className="list-controls">
        <label htmlFor="form-employee-code">Employee ID</label>
        <input
          id="form-employee-code"
          value={formValues.employee_code}
          onChange={(event) =>
            setFormValues({ ...formValues, employee_code: event.target.value })
          }
        />

        <label htmlFor="form-full-name">Name</label>
        <input
          id="form-full-name"
          value={formValues.full_name}
          onChange={(event) =>
            setFormValues({ ...formValues, full_name: event.target.value })
          }
        />

        <label htmlFor="form-student-type">Student type</label>
        <select
          id="form-student-type"
          value={formValues.student_type}
          onChange={(event) =>
            setFormValues({ ...formValues, student_type: event.target.value })
          }
        >
          <option value="undergraduate">Undergraduate</option>
          <option value="masters">Master&rsquo;s</option>
        </select>

        <button type="submit" disabled={saving}>
          {saving ? 'Saving...' : 'Save'}
        </button>
        <button type="button" onClick={closeForm} disabled={saving}>
          Cancel
        </button>
      </div>

      {formError !== null && (
        <p className="backend-status backend-status-error">{formError}</p>
      )}
    </form>
  )

  if (employees.length === 0) {
    return (
      <>
        <div className="list-controls">
          <button type="button" onClick={openCreateForm} disabled={formMode !== null}>
            Add employee
          </button>
        </div>
        {feedback !== null && (
          <p className="backend-status backend-status-success" aria-live="polite">
            {feedback.message}
          </p>
        )}
        {listError !== null && (
          <p className="backend-status backend-status-error" aria-live="polite">
            {listError}{' '}
            <button type="button" onClick={() => refreshList()} disabled={saving}>
                Retry loading
            </button>
          </p>
        )}

        {formMode !== null && employeeForm}
        <p className="backend-status backend-status-loading">
          No employees yet. Add one with the button above, or load the demo
          workforce with: python seed.py
        </p>
      </>
    )
  }

  return (
    <>
      <div className="list-controls">
        <button type="button" onClick={openCreateForm} disabled={formMode !== null}>
          Add employee
        </button>
      </div>

      {feedback !== null && (
        <p
          className={
            feedback.tone === 'success'
              ? 'backend-status backend-status-success'
              : 'backend-status backend-status-loading'
          }
          aria-live="polite"
        >
          {feedback.message}
        </p>
      )}

      {listError !== null && (
        <p className="backend-status backend-status-error" aria-live="polite">
          {listError}{' '}
          <button type="button" onClick={() => refreshList()} disabled={saving}>
            Retry loading
          </button>
        </p>
      )}

      {formMode !== null && employeeForm}

      <div className="list-controls">
        <label htmlFor="employee-search">Search</label>
        <input
          id="employee-search"
          type="search"
          value={searchText}
          onChange={(event) => setSearchText(event.target.value)}
          placeholder="Name or employee ID"
        />

        <label htmlFor="employee-status">Status</label>
        <select
          id="employee-status"
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value)}
        >
          {STATUS_FILTERS.map((filter) => (
            <option key={filter.value} value={filter.value}>
              {filter.label}
            </option>
          ))}
        </select>

        <label htmlFor="employee-sort-field">Sort by</label>
        <select
          id="employee-sort-field"
          value={sortField}
          onChange={(event) => setSortField(event.target.value)}
        >
          {SORT_FIELDS.map((field) => (
            <option key={field.value} value={field.value}>
              {field.label}
            </option>
          ))}
        </select>

        <select
          id="employee-sort-direction"
          aria-label="Sort direction"
          value={sortDirection}
          onChange={(event) => setSortDirection(event.target.value)}
        >
          <option value="asc">Ascending</option>
          <option value="desc">Descending</option>
        </select>

        <button type="button" onClick={resetControls} disabled={isDefaultView}>
          Reset
        </button>
      </div>

      <p className="result-count" aria-live="polite">
        Showing {visibleEmployees.length} of {employees.length} employees
      </p>

      <p className="table-note">
        Remaining capacity is the unused part of each worker&rsquo;s 20-hour
        weekly limit for {week.start} to {week.end}, after the shifts they are
        already assigned. It is theoretical spare capacity only &mdash; it does
        not mean a worker is eligible or available for a given shift, and class
        hours and approved leave are not subtracted from it.
      </p>

      {visibleEmployees.length === 0 ? (
        <p className="backend-status backend-status-loading">
          {query === ''
            ? 'No employees match the current status filter. Try a different status, or reset the controls.'
            : `No employees match "${searchText.trim()}" with the current status filter. Try a different name or employee ID, or reset the controls.`}
        </p>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Name</th>
                <th>Status</th>
                <th>Student type</th>
                <th>Courses</th>
                <th>Class meetings</th>
                <th>Class hours/week</th>
                <th>Weekly limit</th>
                <th>Assigned</th>
                <th>Remaining capacity</th>
                <th>Approved leave</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {visibleEmployees.map((employee) => (
                <tr key={employee.employee_code}>
                  <td>{employee.employee_code}</td>
                  <td>{employee.full_name}</td>
                  <td>{employee.is_active ? 'Active' : 'Inactive'}</td>
                  <td>{studentTypeLabel(employee)}</td>
                  <td>{employee.course_count}</td>
                  <td>{employee.class_meeting_count}</td>
                  <td>{employee.weekly_class_hours}</td>
                  <td>{employee.weekly_hour_limit} h</td>
                  <td>{employee.assigned_hours} h</td>
                  <td>{employee.remaining_capacity_hours} h</td>
                  <td>{employee.approved_leave_count}</td>
                  <td>
                    <button
                      type="button"
                      onClick={() => openEditForm(employee)}
                      disabled={formMode !== null}
                    >
                      Edit
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

export default EmployeeList
