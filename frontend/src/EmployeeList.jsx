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

// Why a worker the user just acted on is no longer in the table, and what to
// do about it. Saying "hidden" without saying which control hid them leaves
// them looking for a row that is filtered out, not missing.
//
// The advice is derived from the worker's own status, so it names the filter
// that actually contains them: Active after a reactivation, Inactive after a
// deactivation. Reset is deliberately never suggested here - it selects
// Active, which would still hide a worker who has just been deactivated.
function hiddenReason(employee, statusFilter, query) {
  const byStatus = !matchesStatus(employee, statusFilter)
  const bySearch = !matchesSearch(employee, query)
  const filter = employee.is_active ? 'Active' : 'Inactive'

  if (byStatus && bySearch) {
    return (
      'They are hidden by the current search and the Status filter - clear the ' +
      `search box and set Status to ${filter} or All to find them.`
    )
  }
  if (byStatus) {
    return `They are hidden by the Status filter - choose ${filter} or All to find them.`
  }
  if (bySearch) {
    return 'They are hidden by the current search - clear the search box to find them.'
  }
  return null
}

const FEEDBACK_CLASSES = {
  success: 'backend-status backend-status-success',
  error: 'backend-status backend-status-error',
  notice: 'backend-status backend-status-loading',
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
  // The employee_code of the row whose status change or deletion is in
  // flight, or null.
  const [pendingCode, setPendingCode] = useState(null)
  // The employee awaiting delete confirmation, or null. Holding the employee
  // rather than a boolean means the confirmation can name exactly who it is
  // about, and opening it performs no request of any kind.
  const [confirmingDelete, setConfirmingDelete] = useState(null)
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

  // One action owns the interface at a time. An open form, a write already
  // in flight, and an open delete confirmation each block the others.
  //
  // Checked here as well as through the `disabled` attribute on the buttons,
  // so a forced click, a very fast double click, or anything else that
  // bypasses the markup still cannot start a second, conflicting action.
  // Search, sorting and the status filter are deliberately not blocked -
  // they only change what is displayed and mutate nothing.
  function anotherActionIsOpen() {
    return (
      formMode !== null ||
      saving ||
      pendingCode !== null ||
      confirmingDelete !== null
    )
  }

  function openCreateForm() {
    if (anotherActionIsOpen()) {
      return
    }
    setFormMode('create')
    setEditingCode(null)
    setFormValues(EMPTY_FORM)
    setFormError(null)
    setFeedback(null)
  }

  function startDelete(employee) {
    if (anotherActionIsOpen()) {
      return
    }
    // Opening the confirmation sends no request of any kind.
    setConfirmingDelete(employee)
  }

  function openEditForm(employee) {
    if (anotherActionIsOpen()) {
      return
    }
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

  async function refreshList(affected = null, lead = '') {
    // Only ever reloads. Safe to call again from the Retry control, because
    // it never re-sends a write.
    //
    // Search, sort and status-filter live in their own state and are not
    // touched here, so a refresh leaves the user's selections exactly as they
    // were - which is also why the row they acted on may drop out of view.
    try {
      const data = await fetchEmployees()
      setEmployees(data.employees)
      setWeek({ start: data.week_start, end: data.week_end })
      setListError(null)

      if (affected !== null) {
        const current = data.employees.find(
          (employee) => employee.employee_code === affected.employee_code,
        )
        const reason =
          current === undefined
            ? null
            : hiddenReason(current, statusFilter, searchText.trim().toLowerCase())

        if (reason !== null) {
          setFeedback({ tone: 'notice', message: `${lead} ${reason}` })
        }
      }
      return true
    } catch {
      setListError(
        // Deliberately not "still saved": this same banner follows a delete,
        // where nothing was saved. Whatever the action was, failing to reload
        // the table does not undo it.
        'Could not reload the employee list. This does not undo anything that was already applied.',
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

    // Step 2: the write is confirmed. Close the form first so a second Save
    // cannot resubmit it, and report success before attempting the reload -
    // a failed reload does not undo the save.
    closeForm()
    const saved = `Saved ${body.full_name} (${body.employee_code}).`
    setFeedback({ tone: 'success', message: saved })
    await refreshList(body, saved)
    setSaving(false)
  }

  function describeRemoved(removed) {
    // Only the record types they actually had, so the message does not list
    // "0 courses" at somebody who never had any.
    const parts = [
      [removed.courses, 'course', 'courses'],
      [removed.class_meetings, 'class meeting', 'class meetings'],
      [removed.shift_preferences, 'shift preference', 'shift preferences'],
      [removed.approved_leave, 'approved leave period', 'approved leave periods'],
    ]
      .filter(([count]) => count > 0)
      .map(([count, one, many]) => `${count} ${count === 1 ? one : many}`)

    return parts.length === 0
      ? 'They had no classes, preferences or leave on record.'
      : `Also removed: ${parts.join(', ')}.`
  }

  async function handleDelete(employee) {
    // Deliberately not `anotherActionIsOpen()`: this runs from inside the
    // confirmation, so `confirmingDelete` is set by definition. What must
    // not happen is a second send while the first is in flight.
    if (pendingCode !== null || saving) {
      return
    }

    setPendingCode(employee.employee_code)
    setFeedback(null)

    let response

    // Step 1: the write. Its outcome alone decides what we may claim.
    try {
      response = await fetch(
        `${EMPLOYEES_URL}/${encodeURIComponent(employee.employee_code)}`,
        { method: 'DELETE' },
      )
    } catch {
      // No response at all, so whether the row is gone is genuinely unknown.
      // Deletion cannot be undone, so guessing here would be the worst kind
      // of wrong - leave the confirmation open and say to check.
      setFeedback({
        tone: 'error',
        message:
          `Could not reach the backend, so it is unclear whether ${employee.full_name} ` +
          `(${employee.employee_code}) was deleted. Reload the list to check before trying again.`,
      })
      setPendingCode(null)
      return
    }

    const body = await response.json().catch(() => null)

    if (!response.ok) {
      // 404 means somebody else already removed them; 409 means they have
      // shift history. Either way nothing changed here. Close the
      // confirmation, because retrying it would ask the same refused
      // question, and reload so the table matches the server.
      setConfirmingDelete(null)
      setFeedback({
        tone: 'error',
        message:
          body?.detail ??
          `Could not delete ${employee.full_name} (status ${response.status}).`,
      })
      setPendingCode(null)
      await refreshList()
      return
    }

    // Step 2: confirmed. Report it before reloading - a failed reload does
    // not bring the worker back.
    setConfirmingDelete(null)
    setFeedback({
      tone: 'success',
      message:
        `Permanently deleted ${body.full_name} (${body.employee_code}). ` +
        `${describeRemoved(body.removed)} This cannot be undone.`,
    })
    // No `affected` argument: the worker no longer exists, so there is no row
    // to explain the absence of.
    await refreshList()
    setPendingCode(null)
  }

  async function handleStatusChange(employee, nextActive) {
    if (anotherActionIsOpen()) {
      return
    }

    const action = nextActive ? 'reactivate' : 'deactivate'
    setPendingCode(employee.employee_code)
    setFeedback(null)

    let response

    // Step 1: the write. Its outcome alone decides what we may claim.
    try {
      response = await fetch(
        `${EMPLOYEES_URL}/${encodeURIComponent(employee.employee_code)}/${action}`,
        { method: 'POST' },
      )
    } catch {
      // No response came back, so whether the server applied the change is
      // genuinely unknown - do not claim either way.
      setFeedback({
        tone: 'error',
        message:
          `Could not reach the backend, so it is unclear whether ${employee.full_name} ` +
          `(${employee.employee_code}) was ${action}d. Reload the list to check before trying again.`,
      })
      setPendingCode(null)
      return
    }

    const body = await response.json().catch(() => null)

    if (!response.ok) {
      // Rejected outright - for a blocked deactivation the backend explains
      // which shifts are unresolved. Nothing was changed, and no shift was
      // deleted, cancelled or reassigned.
      setFeedback({
        tone: 'error',
        message: body?.detail ?? `Could not ${action} this employee (status ${response.status}).`,
      })
      setPendingCode(null)
      return
    }

    // Step 2: the change is confirmed. Report it before reloading, because a
    // failed reload does not undo it.
    const lead = nextActive
      ? `Reactivated ${body.full_name} (${body.employee_code}).`
      : `Deactivated ${body.full_name} (${body.employee_code}). Their classes, preferences, leave and assignment history are unchanged.`
    setFeedback({ tone: 'success', message: lead })
    await refreshList(body, lead)
    setPendingCode(null)
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

  // Rendering this panel changes nothing on the server; only Delete
  // permanently does, and Cancel simply closes it.
  const deleteConfirmation = confirmingDelete !== null && (
    <div className="employee-form" role="alertdialog" aria-label="Confirm deletion">
      <h3>
        Permanently delete {confirmingDelete.full_name} (
        {confirmingDelete.employee_code})?
      </h3>
      <p className="table-note">
        This removes their profile, courses, class meetings, shift preferences
        and approved leave from the database for good. It cannot be undone.
      </p>
      <p className="table-note">
        Their employee ID is recorded as retired. Automatic IDs, once they
        arrive, will never reuse it &mdash; but IDs are still typed in by hand
        today and nothing yet stops that, so do not enter{' '}
        {confirmingDelete.employee_code} for anyone else.
      </p>
      <p className="table-note">
        To keep their records instead, cancel and use Deactivate &mdash; that
        hides them from the active workforce and can be reversed.
      </p>
      <div className="list-controls">
        <button
          type="button"
          onClick={() => handleDelete(confirmingDelete)}
          disabled={pendingCode !== null}
        >
          {pendingCode === confirmingDelete.employee_code
            ? 'Deleting...'
            : 'Delete permanently'}
        </button>
        <button
          type="button"
          onClick={() => setConfirmingDelete(null)}
          disabled={pendingCode !== null}
        >
          Cancel
        </button>
      </div>
    </div>
  )

  const employeeForm = (
    <form className="employee-form" onSubmit={handleSave}>
      <h3>{formMode === 'create' ? 'Add employee' : `Edit ${editingCode}`}</h3>
      <div className="list-controls">
        <label htmlFor="form-employee-code">Employee ID</label>
        {/* Typed only when adding. An existing code is a stable identifier
            that already appears elsewhere, so editing shows it read-only
            rather than as a field that looks changeable (D042). readOnly
            rather than disabled, so it can still be selected and copied. */}
        <input
          id="form-employee-code"
          value={formValues.employee_code}
          readOnly={formMode === 'edit'}
          aria-readonly={formMode === 'edit'}
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

      <p className="table-note">
        {formMode === 'edit'
          ? 'Employee IDs cannot be changed. This one appears in schedules and reports, so renaming it would make one worker look like two. Edit the name or student type instead, or delete the worker if they were created by mistake.'
          : 'Employee IDs are typed in by hand for now and cannot be changed afterwards, so check it before saving. Letters, digits, hyphens and underscores only.'}
      </p>

      {formError !== null && (
        <p className="backend-status backend-status-error">{formError}</p>
      )}
    </form>
  )

  if (employees.length === 0) {
    return (
      <>
        <div className="list-controls">
          <button type="button" onClick={openCreateForm} disabled={anotherActionIsOpen()}>
            Add employee
          </button>
        </div>
        {feedback !== null && (
          <p className={FEEDBACK_CLASSES[feedback.tone]} aria-live="polite">
            {feedback.message}
          </p>
        )}
        {listError !== null && (
          <p className="backend-status backend-status-error" aria-live="polite">
            {listError}{' '}
            <button
              type="button"
              onClick={() => refreshList()}
              disabled={saving || pendingCode !== null}
            >
              Retry loading
            </button>
          </p>
        )}

        {formMode !== null && employeeForm}
      {deleteConfirmation}
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
        <button type="button" onClick={openCreateForm} disabled={anotherActionIsOpen()}>
          Add employee
        </button>
      </div>

      {feedback !== null && (
        <p className={FEEDBACK_CLASSES[feedback.tone]} aria-live="polite">
          {feedback.message}
        </p>
      )}

      {listError !== null && (
        <p className="backend-status backend-status-error" aria-live="polite">
          {listError}{' '}
          <button
            type="button"
            onClick={() => refreshList()}
            disabled={saving || pendingCode !== null}
          >
            Retry loading
          </button>
        </p>
      )}

      {formMode !== null && employeeForm}
      {deleteConfirmation}

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
          {/* Reset is deliberately not offered as the way to find a missing
              worker: it selects Active, so it cannot reveal an inactive one.
              All is the only status that never hides anybody. */}
          {query === ''
            ? 'No employees match the current status filter. Choose All to show every worker, active and inactive.'
            : `No employees match "${searchText.trim()}" with the current status filter. Try a different name or employee ID, and choose the All status to include inactive workers.`}
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
                      disabled={anotherActionIsOpen()}
                    >
                      Edit
                    </button>{' '}
                    {/* Every row's button reads the same, so the accessible
                        name carries which worker it acts on. */}
                    <button
                      type="button"
                      onClick={() => handleStatusChange(employee, !employee.is_active)}
                      disabled={anotherActionIsOpen()}
                      aria-label={`${employee.is_active ? 'Deactivate' : 'Reactivate'} ${employee.full_name}`}
                    >
                      {pendingCode === employee.employee_code
                        ? 'Working...'
                        : employee.is_active
                          ? 'Deactivate'
                          : 'Reactivate'}
                    </button>{' '}
                    <button
                      type="button"
                      onClick={() => startDelete(employee)}
                      disabled={anotherActionIsOpen()}
                      aria-label={`Delete ${employee.full_name}`}
                    >
                      Delete
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
