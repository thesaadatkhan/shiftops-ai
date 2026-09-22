// The weekly Schedule view (Phase 7 increment 4, corrected): reads the
// stored schedule for the shared reporting week, offers an explicit
// Prepare-week action, an explicit Generate Schedule action that creates a
// stored proposal (never real assignments on its own), recovers and reviews
// any proposal already stored for this week, requires an explicit
// confirmation step before approval, and offers an explicit
// worker-replacement flow for an existing assignment. This is what the
// "Schedule" sidebar item renders (see App.jsx) - one component and one
// copy of this workflow's state for both viewing the schedule and
// generating a proposal for it, not two separately maintained screens.

import { useEffect, useRef, useState } from 'react'

import { coverageUrl } from './coverage.js'
import {
  ApiError,
  approveProposal,
  createAssignment,
  createProposal,
  describeError,
  fetchProposalsForWeek,
  fetchWeekSchedule,
  groupProposalReviewShifts,
  isOutcomeUncertain,
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

function weekdayLabel(date) {
  return new Date(`${date}T12:00:00`).toLocaleDateString('en-US', { weekday: 'short' })
}

function ScheduleShiftGrid({ shifts, onAssign, onReplace, actionDisabled }) {
  const byDay = groupByDayThenHall(shifts)
  const dates = [...byDay.keys()].sort()
  const halls = orderedHalls(new Map(shifts.map((shift) => [shift.hall, true])))
  const [menuShift, setMenuShift] = useState(null)

  function toggleMenu(shift) {
    setMenuShift((current) => current?.id === shift.id ? null : shift)
  }

  return (
    <div className="schedule-grid-wrapper">
      <div className="weekly-schedule-grid schedule-shift-grid" role="table" aria-label="Weekly hall shift grid">
        <div className="weekly-schedule-header" role="row">
          <div role="columnheader">Hall</div>
          {dates.map((date) => <div key={date} role="columnheader">{weekdayLabel(date)}</div>)}
        </div>
        {halls.map((hall) => (
          <div key={hall} className="weekly-schedule-row" role="row">
            <div className="weekly-schedule-worker" role="rowheader"><strong>{hall}</strong></div>
            {dates.map((date) => {
              const dayShifts = byDay.get(date)?.get(hall) || []
              return (
                <div key={date} className="weekly-schedule-day schedule-grid-day" role="cell">
                  {dayShifts.map((shift) => {
                    const isOpen = menuShift?.id === shift.id
                    const assignedNames = shift.assigned_employees.map((worker) => worker.full_name).join(', ')
                    return (
                      <div key={shift.id} className="schedule-grid-block">
                        <button
                          type="button"
                          className={`schedule-grid-shift ${shift.covered ? 'schedule-shift-covered' : 'schedule-shift-uncovered'}`}
                          data-shift-id={shift.id}
                          aria-expanded={isOpen}
                          onClick={() => toggleMenu(shift)}
                        >
                          <span>{shiftTimeLabel(shift)}</span>
                          <small>{shift.covered ? `Covered: ${assignedNames}` : `${shift.required_staff - shift.assigned_count} open`}</small>
                        </button>
                        {isOpen && (
                          <div className="schedule-grid-menu" role="menu" aria-label={`${hall} shift actions`}>
                            {!shift.covered && <button type="button" role="menuitem" disabled={actionDisabled} onClick={() => { setMenuShift(null); onAssign(shift) }}>Assign</button>}
                            {shift.assigned_employees.map((worker) => (
                              <button key={worker.employee_id} type="button" role="menuitem" disabled={actionDisabled} onClick={() => { setMenuShift(null); onReplace(shift, worker) }}>
                                Replace {worker.full_name}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}

function ProposalReview({ review, filter, onFilterChange }) {
  const groups = groupProposalReviewShifts(review.shifts, filter)
  return (
    <>
      <div className="proposal-summary" aria-label="Expected coverage summary">
        <div><span>Required</span><strong>{review.summary.required_positions}</strong></div>
        <div><span>Existing</span><strong>{review.summary.existing_filled_positions}</strong></div>
        <div><span>Proposed</span><strong>{review.summary.proposed_filled_positions}</strong></div>
        <div><span>Uncovered</span><strong>{review.summary.uncovered_positions}</strong></div>
        <div className="proposal-coverage"><span>Expected coverage</span><strong>{review.summary.total_filled_positions} / {review.summary.required_positions}</strong></div>
      </div>
      {review.summary.excess_assignments > 0 && <p className="table-note">{review.summary.excess_assignments} existing overstaffed position{review.summary.excess_assignments === 1 ? '' : 's'} remain visible in the full review.</p>}
      <div className="proposal-filter" role="group" aria-label="Proposal shift filter">
        {[['changes', 'Changes'], ['uncovered', 'Uncovered'], ['all', 'All shifts']].map(([value, label]) => (
          <button key={value} type="button" className={filter === value ? 'is-selected' : ''} aria-pressed={filter === value} onClick={() => onFilterChange(value)}>{label}</button>
        ))}
      </div>
      <div className="proposal-review-list" aria-live="polite">
        {groups.length === 0 ? <p className="table-note">No shifts match this filter.</p> : groups.map(({ date, halls }) => (
          <section key={date} className="proposal-review-date">
            <h4>{friendlyDate(date)}</h4>
            {halls.map(({ hall, shifts }) => (
              <div key={hall} className="proposal-review-hall">
                <h5>{hall}</h5>
                {shifts.map((shift) => {
                  const unchangedCovered = shift.proposed_assignments.length === 0 && shift.uncovered_positions === 0
                  const content = <>
                    <p className="proposal-shift-title">{shiftTimeLabel(shift)} <span>{shift.filled_count} of {shift.required_staff} filled{shift.uncovered_positions > 0 ? ` · ${shift.uncovered_positions} uncovered` : ''}</span></p>
                    <div className="proposal-workers">
                      <p><strong>Current</strong> {shift.existing_assignments.length ? shift.existing_assignments.map((worker) => worker.full_name).join(', ') : 'Nobody assigned'}</p>
                      {shift.proposed_assignments.length > 0 && <p><strong>Proposed</strong> {shift.proposed_assignments.map((worker) => worker.full_name).join(', ')}</p>}
                    </div>
                    {shift.uncovered_positions > 0 && (
                      <details className="proposal-uncovered">
                        <summary>Why {shift.uncovered_positions} position{shift.uncovered_positions === 1 ? '' : 's'} remain uncovered</summary>
                        <p>{(shift.uncovered_reasons || []).map((reason) => reason.detail).join('; ') || 'No explanation was recorded.'}</p>
                      </details>
                    )}
                    <details className="proposal-technical-details"><summary>Technical details</summary><p>Shift #{shift.id}. Current: {shift.existing_assignments.map((worker) => `${worker.employee_code} (#${worker.employee_id})`).join(', ') || 'none'}. Proposed: {shift.proposed_assignments.map((worker) => `${worker.employee_code} (#${worker.employee_id})`).join(', ') || 'none'}.</p></details>
                  </>
                  return unchangedCovered ? <details key={shift.id} className="proposal-shift proposal-shift-collapsed"><summary>{shiftTimeLabel(shift)} — unchanged and covered</summary>{content}</details> : <article key={shift.id} className="proposal-shift">{content}</article>
                })}
              </div>
            ))}
          </section>
        ))}
      </div>
    </>
  )
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
  const [reviewFilter, setReviewFilter] = useState('changes')
  // These tabs only decide which already-loaded part of this one workflow is
  // visible. They deliberately do not own a second schedule/proposal state.
  const [activeTab, setActiveTab] = useState('shifts')
  const [shiftView, setShiftView] = useState('grid')

  const [generateNotice, setGenerateNotice] = useState(null)
  // True only while a Generate request's outcome is genuinely unresolved -
  // an uncertain response AND the reconciling reload also failed. A second
  // Generate must never be sendable in that state (it could create a
  // duplicate proposal for a request that already committed); it is cleared
  // once a reload proves either "no proposal was created" or recovers the
  // one that was.
  const [generateBlocked, setGenerateBlocked] = useState(false)
  // The proposal ids known before the request that left generateBlocked
  // true, so a later Reload can still tell a genuinely NEW proposal apart
  // from one that already existed - a ref because it does not itself drive
  // rendering.
  const pendingGenerateKnownIds = useRef(new Set())

  const [replaceTarget, setReplaceTarget] = useState(null) // {shift, outgoing} | null
  const [replaceCandidates, setReplaceCandidates] = useState({ status: 'idle', options: [] })
  const [replaceChoice, setReplaceChoice] = useState('')
  const [replaceStatus, setReplaceStatus] = useState('idle') // idle | loading
  const [replaceError, setReplaceError] = useState(null)
  // True only while a replacement's outcome is genuinely unresolved - an
  // uncertain response AND the reconciling reload also failed. Confirm
  // replacement must never be clickable in that state (a second submission
  // could retry a replacement that already committed, targeting an outgoing
  // assignment that no longer exists).
  const [replaceBlocked, setReplaceBlocked] = useState(false)
  // Bumped on every new candidate fetch (and on closing the panel), so a
  // slow response from an earlier target - or one still in flight when the
  // panel was cancelled - can never populate state for a different, later
  // target or after the panel has already closed.
  const replaceRequestToken = useRef(0)

  const [fillTarget, setFillTarget] = useState(null)
  const [fillCandidates, setFillCandidates] = useState({ status: 'idle', options: [] })
  const [fillChoice, setFillChoice] = useState('')
  const [fillStatus, setFillStatus] = useState('idle')
  const [fillError, setFillError] = useState(null)
  const [fillBlocked, setFillBlocked] = useState(false)
  const fillRequestToken = useRef(0)

  const proposal = proposalsState.list.find((item) => item.id === selectedProposalId) || null

  // Deliberately does not flip back to a "loading" status on a refetch (the
  // same choice EmployeeList.jsx's own refreshList makes): the screen keeps
  // showing the data it already has until the new fetch resolves, rather
  // than blanking out between a Prepare/Generate/Approve action and its
  // authoritative reload.
  // Both return the fetched value (or null on failure) as well as updating
  // state, so a caller reconciling an uncertain mutation outcome can inspect
  // the authoritative result directly - React state updates are not
  // readable synchronously right after the `set...` call that schedules
  // them, so returning the value is the only way a caller in the same tick
  // can act on what was actually reloaded.
  async function loadWeek() {
    try {
      const data = await fetchWeekSchedule(weekStart)
      setWeekState({ status: 'success', data, error: null })
      return data
    } catch (error) {
      // Codex review: this used to always clear `data` to null on failure,
      // which - for a background reconciliation reload triggered from
      // inside an open Approve/Replace panel - blew away the ALREADY
      // LOADED schedule and fell through to the full-page "Could not load"
      // screen below, taking the open panel (and its own error/Reload UI)
      // down with it. Keeping whatever data is already on screen is what
      // the comment above this function already promised ("keeps showing
      // the data it already has"); only a genuine INITIAL load failure (no
      // data yet at all) should show the full-page error.
      setWeekState((state) => ({ status: 'error', data: state.data, error }))
      return null
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
      return list
    } catch (error) {
      setProposalsState({ status: 'error', list: [], error })
      return null
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
    loadWeek()
    loadProposals()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // A replacement panel counts as "in flight" for as long as it is OPEN, not
  // only while its candidate fetch is loading (Codex review: the panel used
  // to stop blocking Approve/Generate/Reject the moment candidates finished
  // loading, even though the panel itself - and the decision it represents
  // - was still open and unresolved). `approvalOrGenerationBusy` is exposed
  // separately so `confirmReplace` can also check it directly, rather than
  // only relying on the replace panel never having opened while one of
  // those was already busy.
  const approvalOrGenerationBusy = preparing || generating || confirmingApproval || decisionStatus !== 'idle'
  const replacePanelOpen = replaceTarget !== null
  const fillPanelOpen = fillTarget !== null
  const anyActionInFlight =
    approvalOrGenerationBusy ||
    replacePanelOpen ||
    fillPanelOpen ||
    replaceStatus === 'loading' ||
    fillStatus === 'loading'

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
      // A committed-but-unreadable response, or a request whose fate is
      // genuinely unknown (Codex review finding 5), is reconciled with an
      // authoritative reload rather than left showing stale pre-prepare
      // state next to an error that might not even mean it failed.
      // prepareWeek is idempotent either way, so this never risks a
      // duplicate write - but reloading first is still the honest answer
      // before inviting a retry.
      if (isOutcomeUncertain(error)) {
        await loadWeek()
      }
    } finally {
      setPreparing(false)
    }
  }

  // Shared by handleGenerate's own catch block and the read-only Reload
  // button: reloads the authoritative proposal list and decides whether the
  // uncertain request is now resolved. Returns nothing - it only updates
  // state (generateBlocked/generateNotice/generateError/selectedProposalId).
  async function reconcileGenerate(knownIds) {
    const list = await loadProposals()
    if (list === null) {
      // The reconciling read itself failed - genuinely still unknown.
      // Generate stays blocked; the Reload button remains the only option.
      setGenerateBlocked(true)
      return
    }
    setGenerateBlocked(false)
    const recovered = list.find((item) => !knownIds.has(item.id))
    if (recovered) {
      // Identifiable: a proposal that was not there before this request
      // now is. SQLite's transactional guarantees mean this reload is
      // authoritative, not a guess - the uncertainty is actually resolved.
      setSelectedProposalId(recovered.id)
      setGenerateError(null)
      setGenerateNotice(
        `The previous request had already created proposal #${recovered.id}; it is shown below. ` +
          'No new proposal is needed.',
      )
    } else {
      setGenerateNotice(
        'Reloaded the stored proposals for this week - no new proposal was created by that request. ' +
          'It is safe to try Generate Schedule again.',
      )
    }
  }

  async function handleGenerate() {
    if (anyActionInFlight || generateBlocked) {
      return
    }
    setGenerating(true)
    setGenerateError(null)
    setGenerateNotice(null)
    setDecisionError(null)
    setDecisionConflicts(null)
    // Captured before the request, so reconciliation below can tell a
    // genuinely NEW proposal apart from one that already existed.
    const knownIds = new Set(proposalsState.list.map((item) => item.id))
    try {
      const created = await createProposal(weekStart)
      setProposalsState((state) => ({ ...state, list: upsertProposal(state.list, created) }))
      setSelectedProposalId(created.id)
    } catch (error) {
      setGenerateError(error)
      if (isOutcomeUncertain(error)) {
        // The generation request may have already created a stored
        // proposal even though this response could not confirm it -
        // reload the week's persisted proposals BEFORE allowing another
        // Generate click, so a second attempt can never create a
        // duplicate proposal for a request that already succeeded.
        pendingGenerateKnownIds.current = knownIds
        await reconcileGenerate(knownIds)
      }
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
      // The confirmation panel closes in every branch below because a real
      // approval request was just sent - a plain ApiError refusal means
      // nothing was written, so there is nothing left to confirm; an
      // uncertain outcome means a second click here would be a genuine
      // retry of something that may already be applied, which must never
      // happen invisibly.
      setConfirmingApproval(false)
      if (error instanceof ApiError && error.status === 409 && error.detail && typeof error.detail === 'object' && Array.isArray(error.detail.conflicts)) {
        // A structured revalidation conflict (D047): the backend refused
        // the WHOLE approval and wrote nothing - a confirmed refusal, not
        // an uncertain outcome.
        setDecisionConflicts(error.detail.conflicts)
      } else if (error instanceof ApiError) {
        setDecisionError(error)
      } else {
        // ResponseValidationError or NetworkOutcomeUnknownError (Codex
        // review finding 5): the approval may have already committed even
        // though this response could not confirm it. Never claim success
        // and never silently invite a retry - reconcile through the same
        // authoritative reads a successful approval would have triggered,
        // and let the reloaded proposal/schedule state speak for itself.
        setDecisionError(error)
        await loadWeek()
        await loadProposals()
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
      if (isOutcomeUncertain(error)) {
        // The rejection may have already committed - reconcile via the
        // same authoritative read a successful rejection would have used.
        await loadProposals()
      }
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
    setReplaceBlocked(false)
    // Codex review finding 3: this used to leave `replaceCandidates.status`
    // at 'loading' when cancelling while a candidate fetch was still in
    // flight. That response is correctly ignored (the bumped token above
    // makes sure of it), but nothing ever reset the status itself back to
    // idle - so `anyActionInFlight` (which checks
    // `replaceCandidates.status === 'loading'`) stayed true forever,
    // locking every other action on the whole screen even after the panel
    // was gone. Resetting it here is what actually releases that lock.
    setReplaceCandidates({ status: 'idle', options: [] })
  }

  // Shared by confirmReplace's own catch block and the read-only Reload
  // button: reloads the authoritative schedule and checks the EXACT
  // intended replacement (this outgoing worker, this incoming worker) -
  // not merely "did the shift change" - then decides whether Confirm stays
  // blocked, is safe to retry, or the form should close because it already
  // happened. SQLite's transactional guarantees make this reload
  // conclusive, not a guess.
  async function reconcileReplace(target, incomingCode, error) {
    const week = await loadWeek()
    if (week === null) {
      // The reconciling reload itself failed - genuinely still unknown.
      // Confirm replacement stays blocked; Reload remains the only option.
      setReplaceError(error)
      setReplaceBlocked(true)
      return
    }
    const shift = week.shifts.find((item) => item.id === target.shift.id)
    const alreadyReplaced =
      shift &&
      incomingCode &&
      shift.assigned_employees.some((worker) => worker.employee_code === incomingCode) &&
      !shift.assigned_employees.some((worker) => worker.employee_code === target.outgoing.employee_code)
    if (alreadyReplaced) {
      // Resolved: it already happened. Close the stale form rather than
      // leaving a "Confirm replacement" button that would resubmit a
      // replacement targeting an outgoing assignment that no longer exists.
      closeReplace()
      return
    }
    // Resolved the other way: the outgoing worker still holds the
    // assignment and the incoming one does not, so the replacement did NOT
    // commit. Safe to leave the form open for a deliberate retry.
    setReplaceError(error)
    setReplaceBlocked(false)
  }

  async function confirmReplace() {
    // Also re-checks `approvalOrGenerationBusy` directly (Codex review): the
    // replacement panel being open already blocks those actions from
    // STARTING via `anyActionInFlight`, but confirming the replacement
    // itself must not proceed on the assumption that guard was never
    // bypassed - defense in depth, not reliance on a single check.
    if (!replaceTarget || !replaceChoice || replaceStatus === 'loading' || approvalOrGenerationBusy || replaceBlocked) {
      return
    }
    setReplaceStatus('loading')
    setReplaceError(null)
    const target = replaceTarget // captured before any await - closeReplace() clears the state value
    const incomingCode = replaceChoice // captured for the same reason
    try {
      await replaceAssignment(target.shift.id, target.outgoing.employee_code, incomingCode)
      closeReplace()
      await loadWeek()
    } catch (error) {
      if (isOutcomeUncertain(error)) {
        // The replacement may have already committed even though this
        // response could not confirm it - never send a second "Confirm
        // replacement" before this is reconciled.
        await reconcileReplace(target, incomingCode, error)
      } else {
        setReplaceError(error)
      }
    } finally {
      setReplaceStatus('idle')
    }
  }

  async function openFill(shift) {
    if (anyActionInFlight || shift.covered) return
    const token = ++fillRequestToken.current
    setFillTarget(shift)
    setFillChoice('')
    setFillError(null)
    setFillBlocked(false)
    setFillCandidates({ status: 'loading', options: [] })
    try {
      const response = await fetch(coverageUrl(shift.id))
      if (!response.ok) throw new Error(`Backend responded with status ${response.status}`)
      const data = await response.json()
      if (token !== fillRequestToken.current) return
      const assignedCodes = new Set(shift.assigned_employees.map((worker) => worker.employee_code))
      const options = (Array.isArray(data.eligible_candidates) ? data.eligible_candidates : []).filter(
        (candidate) => !assignedCodes.has(candidate.employee_code),
      )
      setFillCandidates({ status: 'success', options })
    } catch (error) {
      if (token === fillRequestToken.current) {
        setFillCandidates({ status: 'error', options: [] })
        setFillError(error)
      }
    }
  }

  function closeFill() {
    fillRequestToken.current += 1
    setFillTarget(null)
    setFillChoice('')
    setFillCandidates({ status: 'idle', options: [] })
    setFillError(null)
    setFillBlocked(false)
  }

  async function reconcileFill(target, incomingCode, error) {
    const week = await loadWeek()
    if (week === null) {
      setFillError(error)
      setFillBlocked(true)
      return
    }
    const shift = week.shifts.find((item) => item.id === target.id)
    const alreadyAssigned =
      shift && shift.assigned_employees.some((worker) => worker.employee_code === incomingCode)
    if (alreadyAssigned) {
      closeFill()
      return
    }
    setFillError(error)
    setFillBlocked(false)
  }

  async function confirmFill() {
    if (
      !fillTarget || !fillChoice || fillStatus === 'loading' ||
      approvalOrGenerationBusy || replacePanelOpen || fillBlocked
    ) return
    setFillStatus('loading')
    setFillError(null)
    const target = fillTarget
    const incomingCode = fillChoice
    try {
      await createAssignment(target.id, incomingCode)
      closeFill()
      await loadWeek()
    } catch (error) {
      if (isOutcomeUncertain(error)) await reconcileFill(target, incomingCode, error)
      else setFillError(error)
    } finally {
      setFillStatus('idle')
    }
  }

  if (weekState.status === 'loading') {
    return <p>Loading the schedule for this week…</p>
  }

  // Only a genuine INITIAL failure (no schedule loaded yet at all) takes
  // over the whole screen - a background reconciliation reload that fails
  // while data is already on screen is surfaced by whichever action
  // triggered it (generateError/replaceError/decisionError), not here.
  if (weekState.status === 'error' && !weekState.data) {
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
      <div className="schedule-view-controls">
        <div className="schedule-tabs" role="tablist" aria-label="Schedule views">
          <button type="button" role="tab" aria-selected={activeTab === 'shifts'} className={activeTab === 'shifts' ? 'is-selected' : ''} onClick={() => setActiveTab('shifts')}>Shifts</button>
          <button type="button" role="tab" aria-selected={activeTab === 'proposals'} className={activeTab === 'proposals' ? 'is-selected' : ''} onClick={() => setActiveTab('proposals')}>Proposals</button>
        </div>
        {activeTab === 'shifts' && !isUnprepared && <button type="button" className="schedule-view-toggle" onClick={() => setShiftView((view) => view === 'grid' ? 'list' : 'grid')}>Switch to {shiftView === 'grid' ? 'list' : 'grid'} view</button>}
      </div>

      {isUnprepared && activeTab === 'shifts' ? (
        <div className="schedule-unprepared">
          <p>No shifts are prepared for the week of {friendlyDate(data.week_start)}.</p>
          <button type="button" className="btn-primary" disabled={preparing} onClick={handlePrepare}>
            {preparing ? 'Preparing…' : 'Prepare week'}
          </button>
          {prepareError && <p role="alert">{describeError(prepareError)}</p>}
        </div>
      ) : isUnprepared ? (
        <p className="table-note">Prepare the week from the Shifts tab before generating or reviewing schedule proposals.</p>
      ) : (
        <>
          {activeTab === 'proposals' && <>
          <div className="schedule-actions">
            <button
              type="button"
              className="btn-primary"
              disabled={anyActionInFlight || generateBlocked}
              onClick={handleGenerate}
            >
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
          {generateError && (
            <p role="alert">
              {describeError(generateError)}
              {generateBlocked && ' Generate Schedule is disabled until this is reconciled.'}{' '}
              <button
                type="button"
                onClick={() => reconcileGenerate(pendingGenerateKnownIds.current)}
                disabled={anyActionInFlight}
              >
                Reload
              </button>
            </p>
          )}
          {generateNotice && <p className="table-note">{generateNotice}</p>}
          {proposalsState.status === 'error' && (
            <p role="alert">
              Could not load stored proposals for this week: {describeError(proposalsState.error)}{' '}
              <button type="button" onClick={loadProposals} disabled={anyActionInFlight}>
                Reload
              </button>
            </p>
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

              {review && <ProposalReview review={review} filter={reviewFilter} onFilterChange={setReviewFilter} />}

              {review ? (
                <details className="proposal-technical-details">
                  <summary>Detailed coverage diagnostics</summary>
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
                </details>
              ) : (
                <p className="table-note">
                  Detailed coverage review is not available for this proposal.
                </p>
              )}

              {!review && proposal.assignments.length > 0 && (
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
                <div className="list-controls proposal-actions">
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
                    <button
                      type="button"
                      className="btn-primary"
                      disabled={decisionStatus !== 'idle'}
                      onClick={confirmApprove}
                    >
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
          </>}

          {activeTab === 'shifts' && shiftView === 'grid' && <ScheduleShiftGrid shifts={data.shifts} onAssign={openFill} onReplace={openReplace} actionDisabled={anyActionInFlight} />}

          {activeTab === 'shifts' && shiftView === 'list' && Array.from(groupByDayThenHall(data.shifts).entries()).map(([date, byHall]) => (
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
                                    {worker.conflicts && (
                                      // Codex review finding 2: a class/leave
                                      // edit or a status change since approval
                                      // can invalidate an assignment that was
                                      // valid when it was approved. Purely
                                      // informational - the assignment above
                                      // is untouched and still counts as
                                      // staffing; this only surfaces the
                                      // conflict for the supervisor to resolve
                                      // explicitly, through Replace.
                                      <details className="assignment-conflict">
                                        <summary>Assignment conflict — see details</summary>
                                        <p role="alert" className="table-note">
                                          {worker.conflicts.reasons.join('; ')}
                                        </p>
                                      </details>
                                    )}
                                  </li>
                                ))}
                              </ul>
                            )}
                          </td>
                          <td>
                            {shift.covered
                              ? 'Covered'
                              : `Uncovered (${shift.required_staff - shift.assigned_count} open)`}
                            {!shift.covered && (
                              <div>
                                <button
                                  type="button"
                                  disabled={anyActionInFlight}
                                  onClick={() => openFill(shift)}
                                >
                                  Assign worker
                                </button>
                              </div>
                            )}
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
        <div className="schedule-modal-backdrop">
          <div className="replace-panel schedule-modal" role="alertdialog" aria-modal="true" aria-label="Replace assignment">
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
          {replaceError && (
            <p role="alert">
              {describeError(replaceError)}
              {replaceBlocked && ' Confirm replacement is disabled until this is reconciled.'}
            </p>
          )}
          <div className="list-controls">
            <button
              type="button"
              className="btn-primary"
              disabled={
                !replaceChoice ||
                replaceStatus === 'loading' ||
                replaceCandidates.status === 'loading' ||
                replaceBlocked
              }
              onClick={confirmReplace}
            >
              {replaceStatus === 'loading' ? 'Replacing…' : 'Confirm replacement'}
            </button>
            {replaceError && (
              // A read-only way to re-check the exact intended replacement
              // after an uncertain outcome, instead of only a mutation
              // button to retry blindly.
              <button
                type="button"
                onClick={() => reconcileReplace(replaceTarget, replaceChoice, replaceError)}
                disabled={replaceStatus === 'loading'}
              >
                Reload
              </button>
            )}
            <button type="button" onClick={closeReplace} disabled={replaceStatus === 'loading'}>
              Cancel
            </button>
          </div>
          </div>
        </div>
      )}

      {fillTarget && (
        <div className="schedule-modal-backdrop">
          <div className="replace-panel schedule-modal" role="alertdialog" aria-modal="true" aria-label="Assign worker">
          <h3>Assign a worker to {fillTarget.hall}, {shiftTimeLabel(fillTarget)}?</h3>
          <p className="table-note">
            This fills one currently uncovered position. Eligibility is rechecked before saving.
          </p>
          {fillCandidates.status === 'loading' && <p>Loading eligible workers...</p>}
          {fillCandidates.status === 'error' && <p role="alert">Could not load eligible workers.</p>}
          {fillCandidates.status === 'success' && (
            <label>
              Worker
              <select value={fillChoice} onChange={(event) => setFillChoice(event.target.value)}>
                <option value="">Choose a worker...</option>
                {fillCandidates.options.map((candidate) => (
                  <option key={candidate.employee_code} value={candidate.employee_code}>
                    {candidate.full_name} ({candidate.employee_code})
                  </option>
                ))}
              </select>
              {fillCandidates.options.length === 0 && (
                <p className="table-note">No eligible worker is currently available for this shift.</p>
              )}
            </label>
          )}
          {fillError && (
            <p role="alert">
              {describeError(fillError)}
              {fillBlocked && ' Confirm assignment is disabled until this is reconciled.'}
            </p>
          )}
          <div className="list-controls">
            <button
              type="button"
              className="btn-primary"
              disabled={!fillChoice || fillStatus === 'loading' || fillCandidates.status === 'loading' || fillBlocked}
              onClick={confirmFill}
            >
              {fillStatus === 'loading' ? 'Assigning...' : 'Confirm assignment'}
            </button>
            {fillError && (
              <button
                type="button"
                onClick={() => reconcileFill(fillTarget, fillChoice, fillError)}
                disabled={fillStatus === 'loading'}
              >
                Reload
              </button>
            )}
            <button type="button" onClick={closeFill} disabled={fillStatus === 'loading'}>Cancel</button>
          </div>
          </div>
        </div>
      )}
    </div>
  )
}
