// The weekly Schedule view (Phase 7 increment 4, corrected): reads the
// stored schedule for the shared reporting week, offers an explicit
// Prepare-week action, an explicit Generate Schedule action that creates a
// stored proposal (never real assignments on its own), recovers and reviews
// any proposal already stored for this week, requires an explicit
// confirmation step before approval, and offers an explicit
// worker-replacement flow for an existing assignment. This same component is
// what both the "Schedule" and "Generate Schedule" sidebar items render
// (see App.jsx), so there is exactly one copy of this workflow's state and
// logic, not two independently maintained ones.

import { useEffect, useRef, useState } from 'react'

import { coverageUrl } from './coverage.js'
import {
  approveProposal,
  createProposal,
  describeError,
  fetchProposalsForWeek,
  fetchWeekSchedule,
  prepareWeek,
  rejectProposal,
  replaceAssignment,
} from './schedule.js'
import { friendlyDate, shiftTimeLabel } from './shiftPicker.js'

const HALL_ORDER = ['Andromeda', 'Capella', 'Vega', 'Helix', 'Sirius']

function splitDateTime(datetime) {
  const [date, time] = datetime.split(' ')
  return { date, time }
}

function groupByDayThenHall(shifts) {
  const byDay = new Map()
  for (const shift of shifts) {
    const date = splitDateTime(shift.start_datetime).date
    if (!byDay.has(date)) {
      byDay.set(date, new Map())
    }
    const byHall = byDay.get(date)
    if (!byHall.has(shift.hall)) {
      byHall.set(shift.hall, [])
    }
    byHall.get(shift.hall).push(shift)
  }
  return byDay
}

/** Every hall present, in the preferred order first, then any other stored
 * hall this project does not otherwise name, alphabetically - so a hall
 * added to the data later is still rendered, deterministically, rather than
 * silently dropped for not appearing in HALL_ORDER. */
function orderedHalls(byHall) {
  const known = HALL_ORDER.filter((hall) => byHall.has(hall))
  const others = [...byHall.keys()].filter((hall) => !HALL_ORDER.includes(hall)).sort()
  return [...known, ...others]
}

function upsertProposal(list, updated) {
  const index = list.findIndex((item) => item.id === updated.id)
  if (index === -1) {
    return [updated, ...list]
  }
  const copy = list.slice()
  copy[index] = updated
  return copy
}

export default function Schedule({ weekStart }) {
  const [weekState, setWeekState] = useState({ status: 'loading', data: null, error: null })
  const [preparing, setPreparing] = useState(false)
  const [prepareError, setPrepareError] = useState(null)

  // Every proposal stored for this week (Phase 7 increment 4 recovery), not
  // just the one this browser session happened to generate - a refresh or a
  // switch between "Schedule" and "Generate Schedule" must not lose track of
  // a pending or recently decided proposal.
  const [proposalsState, setProposalsState] = useState({ status: 'loading', list: [], error: null })
  const [selectedProposalId, setSelectedProposalId] = useState(null)
  const [generating, setGenerating] = useState(false)
  const [generateError, setGenerateError] = useState(null)
  const [confirmingApproval, setConfirmingApproval] = useState(false)
  const [decisionStatus, setDecisionStatus] = useState('idle') // idle | approving | rejecting
  const [decisionError, setDecisionError] = useState(null)
  const [decisionConflicts, setDecisionConflicts] = useState(null)

  const [replaceTarget, setReplaceTarget] = useState(null) // {shift, outgoing} | null
  const [replaceCandidates, setReplaceCandidates] = useState({ status: 'idle', options: [] })
  const [replaceChoice, setReplaceChoice] = useState('')
  const [replaceStatus, setReplaceStatus] = useState('idle') // idle | loading
  const [replaceError, setReplaceError] = useState(null)
  // Bumped on every new candidate fetch (and on closing the panel), so a
  // slow response from an earlier target - or one still in flight when the
  // panel was cancelled - can never populate state for a different, later
  // target or after the panel has already closed.
  const replaceRequestToken = useRef(0)

  const proposal = proposalsState.list.find((item) => item.id === selectedProposalId) || null

  // Deliberately does not flip back to a "loading" status on a refetch (the
  // same choice EmployeeList.jsx's own refreshList makes): the screen keeps
  // showing the data it already has until the new fetch resolves, rather
  // than blanking out between a Prepare/Generate/Approve action and its
  // authoritative reload.
  async function loadWeek() {
    try {
      const data = await fetchWeekSchedule(weekStart)
      setWeekState({ status: 'success', data, error: null })
    } catch (error) {
      setWeekState({ status: 'error', data: null, error })
    }
  }

  async function loadProposals() {
    try {
      const list = await fetchProposalsForWeek(weekStart)
      setProposalsState({ status: 'success', list, error: null })
      setSelectedProposalId((current) =>
        current !== null && list.some((item) => item.id === current)
          ? current
          : list.length > 0
            ? list[0].id
            : null,
      )
    } catch (error) {
      setProposalsState({ status: 'error', list: [], error })
    }
  }

  // The parent mounts this component with `key={weekStart}` (see App.jsx),
  // so switching the shared week remounts it fresh rather than reusing
  // state across weeks - all of the state above naturally resets, with no
  // manual clearing needed.
  useEffect(() => {
    // Both helpers' own setState calls happen after their internal `await`,
    // so this is not a synchronous setState-in-effect in practice - the
    // rule cannot see past a named helper to confirm that, unlike an inline
    // async arrow it can trace directly.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadWeek()
    loadProposals()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const anyActionInFlight =
    preparing ||
    generating ||
    confirmingApproval ||
    decisionStatus !== 'idle' ||
    replaceStatus === 'loading' ||
    replaceCandidates.status === 'loading'

  async function handlePrepare() {
    if (anyActionInFlight) {
      return
    }
    setPreparing(true)
    setPrepareError(null)
    try {
      await prepareWeek(weekStart)
      await loadWeek()
    } catch (error) {
      setPrepareError(error)
    } finally {
      setPreparing(false)
    }
  }

  async function handleGenerate() {
    if (anyActionInFlight) {
      return
    }
    setGenerating(true)
    setGenerateError(null)
    setDecisionError(null)
    setDecisionConflicts(null)
    try {
      const created = await createProposal(weekStart)
      setProposalsState((state) => ({ ...state, list: upsertProposal(state.list, created) }))
      setSelectedProposalId(created.id)
    } catch (error) {
      setGenerateError(error)
    } finally {
      setGenerating(false)
    }
  }

  function openApprovalConfirm() {
    if (!proposal || proposal.status !== 'pending' || anyActionInFlight) {
      return
    }
    setDecisionError(null)
    setDecisionConflicts(null)
    setConfirmingApproval(true)
  }

  function cancelApprovalConfirm() {
    if (decisionStatus !== 'idle') {
      return
    }
    setConfirmingApproval(false)
  }

  async function confirmApprove() {
    if (!proposal || decisionStatus !== 'idle') {
      return
    }
    setDecisionStatus('approving')
    setDecisionError(null)
    setDecisionConflicts(null)
    try {
      // The exact stored snapshot, submitted back unchanged - approval
      // refuses anything that does not match what was actually generated.
      const payload = proposal.assignments.map((row) => ({
        shift_id: row.shift_id,
        employee_code: row.employee_code,
      }))
      const approved = await approveProposal(proposal.id, payload)
      setProposalsState((state) => ({ ...state, list: upsertProposal(state.list, approved) }))
      setConfirmingApproval(false)
      await loadWeek()
    } catch (error) {
      setConfirmingApproval(false)
      if (error?.status === 409 && error.detail && typeof error.detail === 'object' && Array.isArray(error.detail.conflicts)) {
        setDecisionConflicts(error.detail.conflicts)
      } else {
        setDecisionError(error)
      }
    } finally {
      setDecisionStatus('idle')
    }
  }

  async function handleReject() {
    if (!proposal || proposal.status !== 'pending' || anyActionInFlight) {
      return
    }
    setDecisionStatus('rejecting')
    setDecisionError(null)
    try {
      const rejected = await rejectProposal(proposal.id)
      setProposalsState((state) => ({ ...state, list: upsertProposal(state.list, rejected) }))
    } catch (error) {
      setDecisionError(error)
    } finally {
      setDecisionStatus('idle')
    }
  }

  async function openReplace(shift, outgoing) {
    if (anyActionInFlight) {
      return
    }
    const token = ++replaceRequestToken.current
    setReplaceTarget({ shift, outgoing })
    setReplaceChoice('')
    setReplaceError(null)
    setReplaceCandidates({ status: 'loading', options: [] })
    try {
      // Current eligibility/coverage information, reusing the same Phase 6
      // Coverage query this project already relies on elsewhere - never a
      // second, independently reimplemented eligibility check.
      const response = await fetch(coverageUrl(shift.id, outgoing.employee_code))
      if (!response.ok) {
        throw new Error(`Backend responded with status ${response.status}`)
      }
      const data = await response.json()
      if (token !== replaceRequestToken.current) {
        return // superseded by a newer target, or the panel was closed
      }
      const alreadyAssignedCodes = new Set(shift.assigned_employees.map((worker) => worker.employee_code))
      const options = (Array.isArray(data.eligible_candidates) ? data.eligible_candidates : []).filter(
        // Every worker already on this shift is excluded from replacement
        // choices, not only the one being replaced - a co-worker on the
        // same shift is technically "eligible" by the hard-rule check
        // (it does not conflict with a shift it already holds), but
        // offering them here would only lead to a 409 at confirm time.
        (candidate) => !alreadyAssignedCodes.has(candidate.employee_code),
      )
      setReplaceCandidates({ status: 'success', options })
    } catch {
      if (token !== replaceRequestToken.current) {
        return
      }
      setReplaceCandidates({ status: 'error', options: [] })
    }
  }

  function closeReplace() {
    replaceRequestToken.current += 1 // invalidate any still-in-flight fetch
    setReplaceTarget(null)
    setReplaceChoice('')
    setReplaceError(null)
  }

  async function confirmReplace() {
    if (!replaceTarget || !replaceChoice || replaceStatus === 'loading') {
      return
    }
    setReplaceStatus('loading')
    setReplaceError(null)
    try {
      await replaceAssignment(replaceTarget.shift.id, replaceTarget.outgoing.employee_code, replaceChoice)
      closeReplace()
      await loadWeek()
    } catch (error) {
      setReplaceError(error)
    } finally {
      setReplaceStatus('idle')
    }
  }

  if (weekState.status === 'loading') {
    return <p>Loading the schedule for this week…</p>
  }

  if (weekState.status === 'error') {
    return (
      <div>
        <p role="alert">Could not load the schedule for this week: {describeError(weekState.error)}</p>
        <button type="button" onClick={loadWeek}>
          Retry
        </button>
      </div>
    )
  }

  const { data } = weekState
  const isUnprepared = data.shifts.length === 0
  const review = proposal?.review || null

  return (
    <div className="schedule-view">
      {isUnprepared ? (
        <div className="schedule-unprepared">
          <p>No shifts are prepared for the week of {friendlyDate(data.week_start)}.</p>
          <button type="button" disabled={preparing} onClick={handlePrepare}>
            {preparing ? 'Preparing…' : 'Prepare week'}
          </button>
          {prepareError && <p role="alert">{describeError(prepareError)}</p>}
        </div>
      ) : (
        <>
          <div className="schedule-actions">
            <button type="button" disabled={anyActionInFlight} onClick={handleGenerate}>
              {generating ? 'Generating…' : 'Generate Schedule'}
            </button>
            {proposalsState.list.length > 1 && (
              <label className="week-selector-jump">
                Proposal
                <select
                  value={selectedProposalId ?? ''}
                  disabled={anyActionInFlight}
                  onChange={(event) => setSelectedProposalId(Number(event.target.value))}
                >
                  {proposalsState.list.map((item) => (
                    <option key={item.id} value={item.id}>
                      #{item.id} — {item.status} ({item.assignments.length} assignment
                      {item.assignments.length === 1 ? '' : 's'})
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
          {generateError && <p role="alert">{describeError(generateError)}</p>}
          {proposalsState.status === 'error' && (
            <p role="alert">Could not load stored proposals for this week: {describeError(proposalsState.error)}</p>
          )}

          {proposal && (
            <div className="proposal-panel" role="region" aria-label="Schedule proposal">
              <h3>
                Proposal #{proposal.id} — {proposal.status}
              </h3>
              <p className="table-note">
                This is a PROPOSAL, not a real schedule change, until it is approved. It
                proposes {proposal.assignments.length} new assignment{proposal.assignments.length === 1 ? '' : 's'}.
              </p>

              {review ? (
                <div className="table-note">
                  <p>
                    Coverage if approved: {review.summary.total_filled_positions} of{' '}
                    {review.summary.required_positions} required position(s) filled (
                    {review.summary.existing_filled_positions} already existing,{' '}
                    {review.summary.proposed_filled_positions} newly proposed),{' '}
                    {review.summary.uncovered_positions} still uncovered
                    {review.summary.excess_assignments > 0 &&
                      `, ${review.summary.excess_assignments} shift(s) overstaffed`}
                    .
                  </p>
                  {review.shifts.filter((shift) => shift.uncovered_positions > 0).length > 0 && (
                    <>
                      <p>Positions that would remain uncovered:</p>
                      <ul>
                        {review.shifts
                          .filter((shift) => shift.uncovered_positions > 0)
                          .map((shift) => (
                            <li key={shift.id}>
                              Shift {shift.id}: {shift.uncovered_positions} open —{' '}
                              {(shift.uncovered_reasons || []).map((reason) => reason.detail).join('; ') ||
                                'no reason recorded'}
                            </li>
                          ))}
                      </ul>
                    </>
                  )}
                </div>
              ) : (
                <p className="table-note">
                  Detailed coverage review is not available for this proposal.
                </p>
              )}

              {proposal.assignments.length > 0 && (
                <ul>
                  {proposal.assignments.map((row) => (
                    <li key={`${row.shift_id}-${row.employee_id}`}>
                      {row.hall}, {friendlyDate(splitDateTime(row.start_datetime).date)},{' '}
                      {shiftTimeLabel(row)} — {row.full_name} ({row.employee_code})
                    </li>
                  ))}
                </ul>
              )}

              {proposal.status === 'pending' && !confirmingApproval && (
                <div className="list-controls">
                  <button type="button" disabled={anyActionInFlight} onClick={openApprovalConfirm}>
                    Approve
                  </button>
                  <button type="button" disabled={anyActionInFlight} onClick={handleReject}>
                    {decisionStatus === 'rejecting' ? 'Rejecting…' : 'Reject'}
                  </button>
                </div>
              )}

              {confirmingApproval && (
                <div className="table-note" role="alertdialog" aria-label="Confirm approval">
                  <p>
                    Approve proposal #{proposal.id}? This will create {proposal.assignments.length} real
                    assignment{proposal.assignments.length === 1 ? '' : 's'}. Approval revalidates every proposed
                    assignment against current data first, then creates the assignments atomically - if anything
                    has changed, nothing is created and you will see exactly what changed below.
                  </p>
                  <div className="list-controls">
                    <button type="button" disabled={decisionStatus !== 'idle'} onClick={confirmApprove}>
                      {decisionStatus === 'approving' ? 'Approving…' : 'Confirm approval'}
                    </button>
                    <button type="button" disabled={decisionStatus !== 'idle'} onClick={cancelApprovalConfirm}>
                      Cancel
                    </button>
                  </div>
                </div>
              )}

              {proposal.status === 'approved' && (
                <p>Approved. The schedule below now reflects these assignments.</p>
              )}
              {proposal.status === 'rejected' && <p>Rejected. No assignments were created.</p>}
              {decisionError && <p role="alert">{describeError(decisionError)}</p>}
              {decisionConflicts && (
                <div role="alert" className="table-note">
                  <p>
                    This proposal is stale and could not be approved. Something changed since
                    it was generated:
                  </p>
                  <ul>
                    {decisionConflicts.map((conflict, index) => (
                      <li key={index}>
                        {conflict.shift_id !== undefined ? `Shift ${conflict.shift_id}` : 'This proposal'}
                        {conflict.employee_code ? ` — ${conflict.employee_code}` : ''}:{' '}
                        {(conflict.reasons && conflict.reasons.length
                          ? conflict.reasons
                          : conflict.reason_codes || ['no longer valid']
                        ).join('; ')}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}

          {Array.from(groupByDayThenHall(data.shifts).entries()).map(([date, byHall]) => (
            <section key={date} className="schedule-day">
              <h3>{friendlyDate(date)}</h3>
              {orderedHalls(byHall).map((hall) => (
                <div key={hall} className="schedule-hall">
                  <h4>{hall}</h4>
                  <table>
                    <thead>
                      <tr>
                        <th>Time</th>
                        <th>Required</th>
                        <th>Assigned</th>
                        <th>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {byHall.get(hall).map((shift) => (
                        <tr key={shift.id}>
                          <td>{shiftTimeLabel(shift)}</td>
                          <td>{shift.required_staff}</td>
                          <td>
                            {shift.assigned_employees.length === 0 ? (
                              <em>Nobody assigned</em>
                            ) : (
                              <ul>
                                {shift.assigned_employees.map((worker) => (
                                  <li key={worker.employee_id}>
                                    {worker.full_name} ({worker.employee_code}){' '}
                                    <button
                                      type="button"
                                      disabled={anyActionInFlight}
                                      onClick={() => openReplace(shift, worker)}
                                    >
                                      Replace
                                    </button>
                                  </li>
                                ))}
                              </ul>
                            )}
                          </td>
                          <td>
                            {shift.covered
                              ? 'Covered'
                              : `Uncovered (${shift.required_staff - shift.assigned_count} open)`}
                            {shift.assigned_count > shift.required_staff &&
                              ` · ${shift.assigned_count - shift.required_staff} extra`}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
            </section>
          ))}
        </>
      )}

      {replaceTarget && (
        <div className="replace-panel" role="alertdialog" aria-label="Replace assignment">
          <h3>
            Replace {replaceTarget.outgoing.full_name} ({replaceTarget.outgoing.employee_code}) on{' '}
            {replaceTarget.shift.hall}, {shiftTimeLabel(replaceTarget.shift)}?
          </h3>
          {replaceCandidates.status === 'loading' && <p>Loading eligible workers…</p>}
          {replaceCandidates.status === 'error' && (
            <p role="alert">Could not load eligible workers for this shift.</p>
          )}
          {replaceCandidates.status === 'success' && (
            <label>
              Incoming worker
              <select value={replaceChoice} onChange={(event) => setReplaceChoice(event.target.value)}>
                <option value="">Choose a worker…</option>
                {replaceCandidates.options.map((candidate) => (
                  <option key={candidate.employee_code} value={candidate.employee_code}>
                    {candidate.full_name} ({candidate.employee_code})
                  </option>
                ))}
              </select>
              {replaceCandidates.options.length === 0 && (
                <p className="table-note">No other eligible worker is currently available for this shift.</p>
              )}
            </label>
          )}
          {replaceError && <p role="alert">{describeError(replaceError)}</p>}
          <div className="list-controls">
            <button
              type="button"
              disabled={!replaceChoice || replaceStatus === 'loading' || replaceCandidates.status === 'loading'}
              onClick={confirmReplace}
            >
              {replaceStatus === 'loading' ? 'Replacing…' : 'Confirm replacement'}
            </button>
            <button type="button" onClick={closeReplace} disabled={replaceStatus === 'loading'}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
