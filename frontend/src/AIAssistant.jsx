// The AI Assistant screen (Phase 9 increment 3): a chat-style supervisor
// interface over the existing agent backend (increments 1-2). This
// component owns no scheduling logic of its own - every fact it shows
// (transcript, proposal content, execution/verification outcome) comes
// straight from a validated backend response (see agent.js). Model text is
// always rendered as plain text/data, never as HTML, and the ONLY thing
// that can ever apply or refuse a proposed assignment is the explicit
// Approve/Reject confirmation flow below, which submits the proposal's
// exact stored action to the exact two decision endpoints - never anything
// derived from chat input or model output.

import { useEffect, useRef, useState } from 'react'

import {
  ApiError,
  approveProposal,
  clearStoredTaskId,
  createTask,
  describeError,
  fetchProposalDecision,
  fetchTask,
  isOutcomeUncertain,
  loadStoredTaskId,
  rejectProposal,
  sendMessage,
  storeActiveTaskId,
  transcriptText,
  visibleTranscriptMessages,
} from './agent.js'
import { friendlyDate, shiftTimeLabel } from './shiftPicker.js'

function roleLabel(role) {
  return role === 'supervisor' ? 'You' : 'ShiftOps AI'
}

function roleClass(role) {
  return `agent-message agent-message-${role.replace('_', '-')}`
}

function TranscriptMessage({ message }) {
  return (
    <div className={roleClass(message.role)}>
      <div className="agent-message-meta">
        <span className="agent-message-role">{roleLabel(message.role)}</span>
      </div>
      {/* Still plain text and escaped by React. Formatting markers are
          normalized for readability; model output is never rendered as HTML. */}
      <div className="agent-message-content">{transcriptText(message)}</div>
    </div>
  )
}

const GUIDE_PROMPTS = [
  "Someone called out for tonight's shift. Find a replacement.",
  'Who is eligible for an uncovered shift this week?',
  'How many hours has Taylor Brooks worked this week?',
  'Can you check whether Morgan Reyes is scheduled this week?',
  'Find a replacement for Jordan Rivera on tonight\'s Capella shift.',
]

function AssistantGuide({ onChoosePrompt, compact = false }) {
  return (
    <div className={compact ? 'agent-guide agent-guide-compact' : 'agent-guide'}>
      {!compact && <h3 id="agent-start-title">What can I help with?</h3>}
      <p>
        This assistant handles one shift or worker at a time. It always proposes a change for you to approve — it never
        assigns or edits anything on its own. For week-wide or hall-wide views, use Coverage or Schedule.
      </p>
      <div className="agent-starters">
        {GUIDE_PROMPTS.map((prompt) => (
          <button key={prompt} type="button" onClick={() => onChoosePrompt(prompt)}>
            {prompt}
          </button>
        ))}
      </div>
    </div>
  )
}

function taskStatusLabel(status) {
  switch (status) {
    case 'open':
      return 'Open'
    case 'awaiting_approval':
      return 'Awaiting approval'
    case 'blocked':
      return 'Blocked'
    case 'closed':
      return 'Closed'
    default:
      return status
  }
}

function outgoingLabel(proposal) {
  return proposal.outgoing_employee_code === null
    ? 'Uncovered position'
    : `${proposal.outgoing_full_name} (${proposal.outgoing_employee_code})`
}

function incomingLabel(proposal) {
  return `${proposal.incoming_full_name} (${proposal.incoming_employee_code})`
}

function proposalAction(proposal) {
  // The exact stored action, resubmitted unchanged - the only thing the
  // approval endpoint ever accepts. Never built from chat text.
  return {
    shift_id: proposal.shift_id,
    outgoing_employee_code: proposal.outgoing_employee_code,
    incoming_employee_code: proposal.incoming_employee_code,
  }
}

function approvalQuestion(proposal, shiftDate, timeLabel) {
  if (proposal.outgoing_employee_code === null) {
    return (
      <p>
        Assign <strong>{incomingLabel(proposal)}</strong> to the uncovered <strong>{proposal.hall}</strong> shift on{' '}
        <strong>{shiftDate}</strong>, <strong>{timeLabel}</strong>?
      </p>
    )
  }
  return (
    <p>
      Replace <strong>{outgoingLabel(proposal)}</strong> with <strong>{incomingLabel(proposal)}</strong> on the{' '}
      <strong>{proposal.hall}</strong> shift on <strong>{shiftDate}</strong>, <strong>{timeLabel}</strong>?
    </p>
  )
}

function ProposalCard({
  proposal,
  readback,
  confirming,
  decisionStatus,
  decisionError,
  decisionConflicts,
  decisionBlocked,
  anyBusy,
  onOpenApprove,
  onOpenReject,
  onCancelConfirm,
  onConfirmApprove,
  onConfirmReject,
  onReloadTask,
  onReconcileMissingVerification,
  reconciling,
}) {
  const shiftDate = friendlyDate(proposal.start_datetime.split(' ')[0])
  const timeLabel = shiftTimeLabel(proposal)
  const verificationFailed =
    typeof proposal.verification_outcome === 'string' &&
    proposal.verification_outcome.startsWith('verification_failed')
  const appliedUnverified = proposal.execution_outcome === 'applied' && proposal.verification_outcome === null

  return (
    <div className="agent-proposal-card">
      <h4>Proposed assignment change</h4>
      <dl className="detail-list">
        <dt>Hall</dt>
        <dd>{proposal.hall}</dd>
        <dt>Date</dt>
        <dd>{shiftDate}</dd>
        <dt>Time</dt>
        <dd>{timeLabel}</dd>
        <dt>Outgoing</dt>
        <dd>{outgoingLabel(proposal)}</dd>
        <dt>Proposed incoming</dt>
        <dd>{incomingLabel(proposal)}</dd>
        <dt>Rationale</dt>
        <dd>{proposal.rationale}</dd>
        <dt>Status</dt>
        <dd>
          <span className={`status-badge status-badge-${proposal.status}`}>{proposal.status}</span>
        </dd>
      </dl>
      <p className="table-note">No assignment changes are made until this proposal is explicitly approved.</p>

      {proposal.status === 'pending' && !confirming && (
        <div className="schedule-actions">
          <button type="button" disabled={anyBusy} onClick={onOpenApprove}>
            Approve
          </button>
          <button type="button" className="btn-danger" disabled={anyBusy} onClick={onOpenReject}>
            Reject
          </button>
        </div>
      )}

      {confirming === 'approve' && (
        <div className="table-note agent-confirm-panel" role="alertdialog" aria-label="Confirm approval">
          <h5>Confirm this assignment change</h5>
          {approvalQuestion(proposal, shiftDate, timeLabel)}
          <div className="schedule-actions">
            <button type="button" className="btn-primary" disabled={decisionStatus !== 'idle'} onClick={onConfirmApprove}>
              {decisionStatus === 'approving' ? 'Approving…' : 'Confirm approval'}
            </button>
            <button type="button" disabled={decisionStatus !== 'idle'} onClick={onCancelConfirm}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {confirming === 'reject' && (
        <div className="table-note agent-confirm-panel" role="alertdialog" aria-label="Confirm rejection">
          <h5>Confirm rejection</h5>
          <p>
            Reject this proposal? <strong>{incomingLabel(proposal)}</strong> will not be assigned, and this task
            will close with no assignment change.
          </p>
          <div className="schedule-actions">
            <button type="button" className="btn-danger" disabled={decisionStatus !== 'idle'} onClick={onConfirmReject}>
              {decisionStatus === 'rejecting' ? 'Rejecting…' : 'Confirm rejection'}
            </button>
            <button type="button" disabled={decisionStatus !== 'idle'} onClick={onCancelConfirm}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {proposal.status === 'approved' && proposal.verification_outcome === 'verified' && (
        <div className="agent-outcome agent-outcome-success">
          <p>Approved, applied, and verified.</p>
          {readback && (
            <ul>
              {readback.assigned_employees.map((worker) => (
                <li key={worker.employee_code}>
                  {worker.full_name} ({worker.employee_code})
                </li>
              ))}
              {readback.assigned_employees.length === 0 && <li>No one currently assigned.</li>}
            </ul>
          )}
        </div>
      )}

      {proposal.status === 'approved' && verificationFailed && (
        <div className="agent-outcome agent-outcome-warning">
          <p>
            The assignment write succeeded, but it could not be verified afterward. This is a recorded, completed
            outcome - Reload to see the current state; do not approve again.
          </p>
          <button type="button" disabled={reconciling} onClick={onReloadTask}>
            {reconciling ? 'Reloading…' : 'Reload'}
          </button>
        </div>
      )}

      {proposal.status === 'approved' && appliedUnverified && (
        <div className="agent-outcome agent-outcome-warning">
          <p>
            This assignment was applied, but verification never completed (a prior recovery gap). Reconcile to run
            verification now - the assignment itself will not be repeated or modified.
          </p>
          <button type="button" disabled={anyBusy} onClick={onReconcileMissingVerification}>
            {decisionStatus === 'approving' ? 'Reconciling…' : 'Reconcile'}
          </button>
        </div>
      )}

      {proposal.status === 'rejected' && (
        <div className="agent-outcome">
          <p>Rejected. No assignment was changed. This task is closed.</p>
        </div>
      )}

      {decisionConflicts && (
        <div className="agent-outcome agent-outcome-warning">
          <p>This proposal is stale and could not be approved. Something changed since it was created:</p>
          <ul>
            {decisionConflicts.map((conflict, index) => (
              <li key={index}>
                {conflict.employee_code ? `${conflict.employee_code}: ` : ''}
                {(conflict.reasons && conflict.reasons.length ? conflict.reasons : conflict.reason_codes || ['no longer valid']).join('; ')}
              </li>
            ))}
          </ul>
        </div>
      )}

      {decisionError && !decisionConflicts && (
        <div className="agent-outcome agent-outcome-warning">
          <p>{describeError(decisionError)}</p>
          {decisionBlocked && (
            <button type="button" disabled={reconciling} onClick={onReloadTask}>
              {reconciling ? 'Reloading…' : 'Reload / Reconcile'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

export default function AIAssistant() {
  const [taskId, setTaskId] = useState(() => loadStoredTaskId())
  const [task, setTask] = useState(null)
  // loading | idle | ready - computed from the same initial read so the
  // very first render already knows whether a recovery fetch is pending,
  // rather than starting 'loading' and synchronously flipping to 'idle'
  // inside an effect for the common case of no remembered task at all.
  const [phase, setPhase] = useState(() => (loadStoredTaskId() === null ? 'idle' : 'loading'))
  const [loadError, setLoadError] = useState(null)

  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState(null)
  const [sendBlocked, setSendBlocked] = useState(false)
  const [lastResult, setLastResult] = useState(null)

  const [confirming, setConfirming] = useState(null) // null | 'approve' | 'reject'
  const [decisionStatus, setDecisionStatus] = useState('idle') // idle | approving | rejecting
  const [decisionError, setDecisionError] = useState(null)
  const [decisionConflicts, setDecisionConflicts] = useState(null)
  const [decisionBlocked, setDecisionBlocked] = useState(false)
  const [readbackByProposal, setReadbackByProposal] = useState({})
  const [reconciling, setReconciling] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)
  const transcriptRef = useRef(null)
  // A new message should be visible immediately when the supervisor is
  // following the conversation. Once they deliberately scroll up, preserve
  // that reading position instead of pulling them away from history.
  const transcriptNearBottom = useRef(true)

  useEffect(() => {
    if (taskId === null) {
      return undefined
    }
    // A request-local flag, not a ref that outlives this effect run:
    // React StrictMode's development-only mount/cleanup/remount cycle runs
    // this effect's cleanup once before the "real" mount - a ref set to
    // false in that first cleanup would stay false forever with nothing to
    // ever set it back to true, silently discarding every future recovery
    // fetch's result. Scoping cancellation to each individual effect
    // invocation avoids that: this run's own flag is only ever flipped by
    // this run's own cleanup.
    let cancelled = false
    fetchTask(taskId)
      .then(async (data) => {
        if (cancelled) return
        setTask(data)
        setPhase('ready')
        await loadVerifiedReadback(data)
      })
      .catch((error) => {
        if (cancelled) return
        if (error instanceof ApiError && error.status === 404) {
          clearStoredTaskId()
          setTaskId(null)
          setTask(null)
          setPhase('idle')
          setLoadError('Your previous task could not be found any more. Start a new one below.')
        } else {
          setLoadError(describeError(error))
          setPhase('idle')
        }
      })
    return () => {
      cancelled = true
    }
    // Recovery fetch runs once for whichever task id localStorage had at
    // mount; later taskId changes (a new task created, Start new task) are
    // handled by their own state updates, not by refiring this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const anyBusy = sending || decisionStatus !== 'idle' || reconciling
  // `agent_proposals.UNIQUE(task_id)` (D052) means a task has at most one
  // proposal, ever - pending or already decided. One shared reference, not
  // a separate "pending" list and "decided" list, is what lets a single
  // ProposalCard correctly show busy/error state for whichever single
  // proposal actually exists, including while reconciling a decided one.
  const proposal = task && task.proposals.length > 0 ? task.proposals[0] : null
  const pendingProposal = proposal && proposal.status === 'pending' ? proposal : null
  const visibleMessages = task ? visibleTranscriptMessages(task.messages) : []

  useEffect(() => {
    const transcript = transcriptRef.current
    if (transcript && transcriptNearBottom.current) {
      transcript.scrollTop = transcript.scrollHeight
    }
  }, [visibleMessages.length])

  function trackTranscriptScroll(event) {
    const { scrollHeight, scrollTop, clientHeight } = event.currentTarget
    transcriptNearBottom.current = scrollHeight - scrollTop - clientHeight < 48
  }

  /** Fetches a decided, verified proposal's current shift readback through
   * the read-only decision-state endpoint - never the approval endpoint,
   * which would be misusing a mutation-authorizing route merely to read
   * something that already happened. A pending proposal, or one with no
   * verified outcome yet, has nothing to fetch. Best-effort: a failure here
   * only means the verified-success card renders without its worker list
   * until the next successful reload. */
  async function loadVerifiedReadback(taskData) {
    const decided = taskData.proposals[0]
    if (!decided || decided.status === 'pending' || decided.verification_outcome !== 'verified') {
      return
    }
    try {
      const decision = await fetchProposalDecision(decided.id, taskData.id, proposalAction(decided))
      if (decision.readback) {
        setReadbackByProposal((current) => ({ ...current, [decided.id]: decision.readback }))
      }
    } catch {
      // Best-effort only - see docstring above.
    }
  }

  /** Ordinary authoritative reconciliation: a read-only GET, for an
   * uncertain send/decision outcome or to refresh a recorded, already-
   * complete `verification_failed` state. Never itself runs verification -
   * see `reconcileMissingVerification` for the one case that must. */
  async function reloadTask() {
    if (taskId === null) {
      return
    }
    setReconciling(true)
    try {
      const data = await fetchTask(taskId)
      setTask(data)
      setSendBlocked(false)
      setDecisionBlocked(false)
      setDecisionConflicts(null)
      setDecisionError(null)
      setSendError(null)
      await loadVerifiedReadback(data)
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        clearStoredTaskId()
        setTaskId(null)
        setTask(null)
        setPhase('idle')
        setLoadError('This task no longer exists. Start a new one below.')
      }
      // Otherwise the reconciling read itself failed - stay blocked; the
      // Reload/Reconcile button remains the only way forward, never an
      // automatic retry of the original mutation.
    } finally {
      setReconciling(false)
    }
  }

  /** The ONE case that must invoke the approval endpoint again rather than
   * a plain GET: a proposal durably applied (`execution_outcome ===
   * 'applied'`) whose verification was never recorded (`verification_outcome
   * === null`) - a prior process/write failure between the two, per the
   * increment-2 recovery correction. Resubmits the proposal's own exact
   * stored action; the backend treats this exact state as verification-only
   * recovery and never repeats or modifies the assignment. Kept entirely
   * separate from `reloadTask`: an uncertain outcome or an already-recorded
   * `verification_failed` must never reach this function. */
  async function reconcileMissingVerification(target) {
    if (!task || !target || decisionStatus !== 'idle') {
      return
    }
    setDecisionStatus('approving')
    setDecisionError(null)
    try {
      const response = await approveProposal(target.id, task.id, proposalAction(target))
      applyDecisionResponse(response)
    } catch (error) {
      handleDecisionError(error)
    } finally {
      setDecisionStatus('idle')
    }
  }

  function startNewTask() {
    clearStoredTaskId()
    setTaskId(null)
    setTask(null)
    setPhase('idle')
    setLoadError(null)
    setInput('')
    setSendError(null)
    setSendBlocked(false)
    setLastResult(null)
    setConfirming(null)
    setDecisionError(null)
    setDecisionConflicts(null)
    setDecisionBlocked(false)
    setHelpOpen(false)
  }

  async function handleSend(event) {
    event.preventDefault()
    const text = input.trim()
    if (!text || anyBusy || sendBlocked) {
      return
    }
    setSending(true)
    setSendError(null)
    setLastResult(null)
    try {
      const response = taskId === null ? await createTask(text) : await sendMessage(taskId, text)
      setTask(response.task)
      setLastResult(response.result)
      setInput('')
      setPhase('ready')
      if (taskId === null) {
        setTaskId(response.task.id)
        storeActiveTaskId(response.task.id)
      }
    } catch (error) {
      setSendError(error)
      if (taskId !== null && isOutcomeUncertain(error)) {
        // A message may have already been recorded, or a whole model run
        // may have already happened, even though this response could not
        // confirm it - never resend automatically. Reload/Reconcile via
        // GET is the only way forward until the user explicitly tries again.
        setSendBlocked(true)
      }
      // With no task yet, there is no id to reconcile against - the error
      // is shown as-is and the supervisor can deliberately try sending
      // again, which is a new explicit action, not an automatic resend.
    } finally {
      setSending(false)
    }
  }

  function openApprove() {
    if (!pendingProposal || anyBusy) return
    setDecisionError(null)
    setDecisionConflicts(null)
    setDecisionBlocked(false)
    setConfirming('approve')
  }

  function openReject() {
    if (!pendingProposal || anyBusy) return
    setDecisionError(null)
    setDecisionConflicts(null)
    setDecisionBlocked(false)
    setConfirming('reject')
  }

  function cancelConfirm() {
    if (decisionStatus !== 'idle') return
    setConfirming(null)
  }

  async function confirmApprove() {
    if (!pendingProposal || !task || decisionStatus !== 'idle') return
    setDecisionStatus('approving')
    setDecisionError(null)
    setDecisionConflicts(null)
    try {
      const response = await approveProposal(pendingProposal.id, task.id, proposalAction(pendingProposal))
      applyDecisionResponse(response)
      setConfirming(null)
    } catch (error) {
      setConfirming(null)
      handleDecisionError(error)
    } finally {
      setDecisionStatus('idle')
    }
  }

  async function confirmReject() {
    if (!pendingProposal || !task || decisionStatus !== 'idle') return
    setDecisionStatus('rejecting')
    setDecisionError(null)
    try {
      const response = await rejectProposal(pendingProposal.id, task.id, proposalAction(pendingProposal))
      applyDecisionResponse(response)
      setConfirming(null)
    } catch (error) {
      setConfirming(null)
      handleDecisionError(error)
    } finally {
      setDecisionStatus('idle')
    }
  }

  function applyDecisionResponse(response) {
    setTask((current) => {
      if (!current) return current
      const proposals = current.proposals.map((proposal) =>
        proposal.id === response.proposal.id ? response.proposal : proposal,
      )
      return { ...current, status: response.task_status, proposals }
    })
    if (response.readback) {
      setReadbackByProposal((current) => ({ ...current, [response.proposal.id]: response.readback }))
    }
  }

  function handleDecisionError(error) {
    if (error instanceof ApiError && error.status === 409 && error.detail && typeof error.detail === 'object' && Array.isArray(error.detail.conflicts)) {
      // A structured stale-state revalidation conflict: a confirmed refusal,
      // nothing was written, and the stored (still-pending) proposal stays
      // visible exactly as it was.
      setDecisionConflicts(error.detail.conflicts)
      return
    }
    if (error instanceof ApiError) {
      setDecisionError(error)
      return
    }
    // Uncertain outcome (malformed response body or a network failure) - the
    // decision may already have been applied. Never claim success and never
    // silently retry; require an explicit Reload/Reconcile first.
    setDecisionError(error)
    setDecisionBlocked(true)
  }

  const busyMessage = decisionStatus !== 'idle' || sending

  function chooseGuidePrompt(prompt) {
    setInput(prompt)
    setHelpOpen(false)
  }

  return (
    <div className="agent-assistant">
      <div className="agent-toolbar">
        <button type="button" onClick={startNewTask} disabled={anyBusy}>
          Start new task
        </button>
        {task && (
          <span className={`status-badge status-badge-${task.status}`} aria-label={`Task status: ${taskStatusLabel(task.status)}`}>
            Task: {taskStatusLabel(task.status)}
          </span>
        )}
        {task && (
          <button type="button" className="agent-help-button" aria-label="Show assistant help" onClick={() => setHelpOpen(true)}>
            ?
          </button>
        )}
      </div>

      {!task && phase !== 'loading' && (
        <section className="agent-start" aria-labelledby="agent-start-title">
          <AssistantGuide onChoosePrompt={chooseGuidePrompt} />
        </section>
      )}

      {task && (
        <div className="agent-transcript" ref={transcriptRef} onScroll={trackTranscriptScroll} aria-live="polite">
          <div className="agent-transcript-content">
            {visibleMessages.map((message) => (
              <TranscriptMessage key={message.id} message={message} />
            ))}
            {visibleMessages.length === 0 && <p className="table-note">No conversation messages yet.</p>}

            {lastResult && lastResult.kind === 'clarification_required' && (
              <p className="backend-status backend-status-loading">The assistant needs clarification before continuing - see its message above.</p>
            )}
            {lastResult && lastResult.kind === 'blocked' && lastResult.blocked_reason && (
              <p className="backend-status backend-status-error">Blocked: {lastResult.blocked_reason.replace(/_/g, ' ')}.</p>
            )}

            {proposal && (
              <ProposalCard
                proposal={proposal}
                readback={readbackByProposal[proposal.id]}
                confirming={confirming}
                decisionStatus={decisionStatus}
                decisionError={decisionError}
                decisionConflicts={decisionConflicts}
                decisionBlocked={decisionBlocked}
                anyBusy={anyBusy}
                reconciling={reconciling}
                onOpenApprove={openApprove}
                onOpenReject={openReject}
                onCancelConfirm={cancelConfirm}
                onConfirmApprove={confirmApprove}
                onConfirmReject={confirmReject}
                onReloadTask={reloadTask}
                onReconcileMissingVerification={() => reconcileMissingVerification(proposal)}
              />
            )}

            {busyMessage && (
              <p className="backend-status backend-status-loading" role="status">
                {decisionStatus === 'idle' ? 'The assistant is working on your request…' : 'Updating the proposal…'}
              </p>
            )}
          </div>
        </div>
      )}

      {!task && loadError && <p className="backend-status backend-status-error">{loadError}</p>}
      {!task && phase === 'loading' && <p className="backend-status backend-status-loading">Loading your active task…</p>}

      <form className="agent-composer" onSubmit={handleSend}>
        <textarea
          aria-label="Message the AI Assistant"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder="e.g. Jordan called out for tonight's Capella shift. Find a replacement."
          rows={3}
          disabled={anyBusy || sendBlocked}
        />
        <div className="schedule-actions">
          <button type="submit" disabled={anyBusy || sendBlocked || !input.trim()}>
            {busyMessage ? 'Working…' : 'Send'}
          </button>
        </div>
      </form>

      {sendError && (
        <div className="agent-outcome agent-outcome-warning">
          <p>{describeError(sendError)}</p>
          {sendBlocked && (
            <button type="button" disabled={reconciling} onClick={reloadTask}>
              {reconciling ? 'Reloading…' : 'Reload / Reconcile'}
            </button>
          )}
        </div>
      )}

      {helpOpen && (
        <div className="schedule-modal-backdrop">
          <section className="agent-help-panel schedule-modal" role="dialog" aria-modal="true" aria-label="Assistant help">
            <div className="agent-help-header">
              <h3>What you can ask</h3>
              <button type="button" aria-label="Close assistant help" onClick={() => setHelpOpen(false)}>Close</button>
            </div>
            <AssistantGuide compact onChoosePrompt={chooseGuidePrompt} />
          </section>
        </div>
      )}
    </div>
  )
}
