// The AI Assistant screen's backend layer (Phase 9 increment 3): where the
// agent task/message/proposal-decision endpoints live, and defensive
// response-shape validation before anything reaches state - the same
// discipline schedule.js already established for the Schedule screen. A
// syntactically valid 2xx body is never trusted by construction; every
// field this screen actually reads is checked before it is stored.

const AGENT_TASKS_URL = 'http://127.0.0.1:8000/api/agent/tasks'
const AGENT_PROPOSALS_URL = 'http://127.0.0.1:8000/api/agent/proposals'

function taskUrl(taskId) {
  return `${AGENT_TASKS_URL}/${encodeURIComponent(taskId)}`
}

function taskMessagesUrl(taskId) {
  return `${taskUrl(taskId)}/messages`
}

function proposalUrl(id) {
  return `${AGENT_PROPOSALS_URL}/${encodeURIComponent(id)}`
}

// Reused verbatim from schedule.js's own three-way error split (D033/D047):
// a confirmed HTTP refusal (nothing written) versus a 2xx whose body could
// not be trusted (a mutation may have committed) versus a request whose
// fate never reached the backend at all (fetch itself failed). Re-declared
// here rather than imported so this module has no dependency on the
// Schedule screen ever changing its own error types for schedule-specific
// reasons; the shapes and the reasoning are identical on purpose.
export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === 'string' ? detail : `Request failed with status ${status}`)
    this.status = status
    this.detail = detail
  }
}

export class ResponseValidationError extends Error {}

export class NetworkOutcomeUnknownError extends Error {}

async function readDetail(response) {
  try {
    const body = await response.json()
    return body && typeof body === 'object' && 'detail' in body ? body.detail : null
  } catch {
    return null
  }
}

async function requestJson(url, options) {
  let response
  try {
    response = await fetch(url, options)
  } catch (error) {
    throw new NetworkOutcomeUnknownError(
      `Could not reach the backend (${error.message}). Whether this request was applied is unknown - ` +
        'reload before retrying, rather than sending it again blindly.',
    )
  }
  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response))
  }
  try {
    return await response.json()
  } catch (error) {
    throw new ResponseValidationError(
      `The backend accepted this request (status ${response.status}), but its response body could not ` +
        `be read (${error.message}). Reload to see the current, authoritative state.`,
    )
  }
}

const MESSAGE_ROLES = ['supervisor', 'assistant', 'tool_call', 'tool_result']
const TASK_STATUSES = ['open', 'awaiting_approval', 'blocked', 'closed']
const PROPOSAL_STATUSES = ['pending', 'approved', 'rejected']
const RESULT_KINDS = ['answer', 'clarification_required', 'blocked', 'proposal']

function isValidMessage(row) {
  return (
    typeof row === 'object' &&
    row !== null &&
    typeof row.id === 'number' &&
    typeof row.created_at === 'string' &&
    typeof row.sequence === 'number' &&
    MESSAGE_ROLES.includes(row.role) &&
    (row.tool_name === null || typeof row.tool_name === 'string') &&
    typeof row.content === 'string'
  )
}

function isOrderedBySequence(messages) {
  for (let i = 1; i < messages.length; i += 1) {
    if (messages[i].sequence <= messages[i - 1].sequence) {
      return false
    }
  }
  return true
}

/** A stored agent proposal exactly as `agent_tools.proposal_payload` shapes
 * it. Every field the proposal card and the approval confirmation panel
 * actually render is checked here, including the null-vs-string distinction
 * that tells a replacement apart from an uncovered-position fill. */
export function isValidProposal(data, { expectedTaskId } = {}) {
  const commonFieldsAreValid =
    typeof data === 'object' &&
    data !== null &&
    typeof data.id === 'number' &&
    typeof data.task_id === 'number' &&
    (expectedTaskId === undefined || data.task_id === expectedTaskId) &&
    typeof data.created_at === 'string' &&
    PROPOSAL_STATUSES.includes(data.status) &&
    (data.decided_at === null || typeof data.decided_at === 'string') &&
    data.action_type === 'replace_assignment' &&
    typeof data.shift_id === 'number' &&
    typeof data.hall === 'string' &&
    typeof data.start_datetime === 'string' &&
    typeof data.end_datetime === 'string' &&
    // The outgoing worker is null for an uncovered-position fill, and only
    // ever a matched code+name pair otherwise - never one without the other.
    ((data.outgoing_employee_code === null && data.outgoing_full_name === null) ||
      (typeof data.outgoing_employee_code === 'string' && typeof data.outgoing_full_name === 'string')) &&
    typeof data.incoming_employee_code === 'string' &&
    typeof data.incoming_full_name === 'string' &&
    typeof data.rationale === 'string' &&
    (data.executed_at === null || typeof data.executed_at === 'string') &&
    (data.execution_outcome === null || typeof data.execution_outcome === 'string') &&
    (data.verified_at === null || typeof data.verified_at === 'string') &&
    (data.verification_outcome === null || typeof data.verification_outcome === 'string')

  if (!commonFieldsAreValid) {
    return false
  }

  if (data.status === 'pending') {
    return (
      data.decided_at === null &&
      data.executed_at === null &&
      data.execution_outcome === null &&
      data.verified_at === null &&
      data.verification_outcome === null
    )
  }

  if (data.status === 'rejected') {
    return (
      data.decided_at !== null &&
      data.executed_at === null &&
      data.execution_outcome === null &&
      data.verified_at === null &&
      data.verification_outcome === null
    )
  }

  if (data.decided_at === null || data.executed_at === null || data.execution_outcome !== 'applied') {
    return false
  }
  if (data.verification_outcome === null) {
    return data.verified_at === null
  }
  if (
    data.verification_outcome === 'verified' ||
    data.verification_outcome.startsWith('verification_failed: ')
  ) {
    return data.verified_at !== null
  }
  return false
}

function isProposalConsistentWithTaskStatus(proposal, taskStatus) {
  if (proposal.status === 'pending') {
    return taskStatus === 'awaiting_approval'
  }
  if (proposal.status === 'rejected') {
    return taskStatus === 'closed'
  }
  if (proposal.verification_outcome === null) {
    return taskStatus === 'awaiting_approval'
  }
  return proposal.verification_outcome === 'verified' ? taskStatus === 'closed' : taskStatus === 'blocked'
}

function isValidTask(data, { expectedId } = {}) {
  const fieldsAreValid =
    typeof data === 'object' &&
    data !== null &&
    typeof data.id === 'number' &&
    (expectedId === undefined || data.id === expectedId) &&
    typeof data.created_at === 'string' &&
    typeof data.updated_at === 'string' &&
    TASK_STATUSES.includes(data.status) &&
    typeof data.request_text === 'string' &&
    typeof data.step_count === 'number' &&
    Array.isArray(data.messages) &&
    data.messages.every(isValidMessage) &&
    isOrderedBySequence(data.messages) &&
    Array.isArray(data.proposals) &&
    data.proposals.length <= 1 &&
    data.proposals.every((proposal) => isValidProposal(proposal, { expectedTaskId: data.id }))

  if (!fieldsAreValid) {
    return false
  }
  return data.proposals.length === 0 || isProposalConsistentWithTaskStatus(data.proposals[0], data.status)
}

function isValidRunResult(data, { expectedTaskId } = {}) {
  return (
    typeof data === 'object' &&
    data !== null &&
    RESULT_KINDS.includes(data.kind) &&
    (data.message === null || typeof data.message === 'string') &&
    (data.proposal === null || isValidProposal(data.proposal, { expectedTaskId })) &&
    typeof data.steps_used === 'number' &&
    (data.blocked_reason === null || typeof data.blocked_reason === 'string')
  )
}

function isValidTaskEnvelope(data, { expectedId } = {}) {
  return (
    typeof data === 'object' &&
    data !== null &&
    isValidTask(data.task, { expectedId }) &&
    isValidRunResult(data.result, { expectedTaskId: data.task.id })
  )
}

function isValidShiftSummary(shift) {
  return (
    typeof shift === 'object' &&
    shift !== null &&
    typeof shift.shift_id === 'number' &&
    typeof shift.hall === 'string' &&
    typeof shift.start_datetime === 'string' &&
    typeof shift.end_datetime === 'string' &&
    typeof shift.required_staff === 'number'
  )
}

/** The post-approval readback (`agent_tools.get_shift_details`'s own
 * shape): the shift's current occupants and whether it is covered - the
 * "current shift/assignment readback" the API contract requires on a
 * verified success. */
export function isValidReadback(data) {
  return (
    typeof data === 'object' &&
    data !== null &&
    isValidShiftSummary(data.shift) &&
    Array.isArray(data.assigned_employees) &&
    data.assigned_employees.every(
      (worker) =>
        typeof worker.employee_code === 'string' &&
        typeof worker.full_name === 'string' &&
        typeof worker.is_active === 'boolean',
    ) &&
    typeof data.assigned_count === 'number' &&
    data.assigned_count === data.assigned_employees.length &&
    typeof data.covered === 'boolean'
  )
}

function readbackHasWorker(readback, employeeCode) {
  return readback.assigned_employees.some((worker) => worker.employee_code === employeeCode)
}

/** The approve/reject/decision-state response contract, fully bound to the
 * request that produced it - not merely "the proposal id matches." Rejects
 * a response whose task ownership, exact submitted/stored action, or
 * internal outcome/readback consistency does not match what was actually
 * requested, regardless of HTTP status:
 *
 * - `task_id` (both the envelope's and the proposal's own) must be
 *   `expectedTaskId`, and the proposal id must be `expectedProposalId`;
 * - the proposal's `shift_id`/`outgoing_employee_code`/`incoming_employee_code`
 *   must exactly equal `expectedAction`'s - a response describing a
 *   DIFFERENT action than the one submitted (or, for a decision-state read,
 *   than the one already on file) is never trusted;
 * - `action_type` must be `'replace_assignment'`, the only value this
 *   schema currently allows;
 * - a `readback`, when present, must name the same shift as the proposal,
 *   and is required (non-null, containing the incoming worker and, for a
 *   replacement, NOT containing the outgoing worker) exactly when
 *   `verification_outcome === 'verified'` - and required to be `null` for
 *   every other status/outcome (pending, rejected, applied-but-unverified,
 *   or a recorded `verification_failed`), since none of those have a
 *   confirmed current state to show;
 * - a rejected proposal must show a closed task and null execution/
 *   verification outcomes;
 * - an approved proposal must have both a decision and an execution
 *   timestamp, and `execution_outcome === 'applied'`.
 */
function isValidDecisionResponse(data, { expectedProposalId, expectedTaskId, expectedAction }) {
  if (
    typeof data !== 'object' ||
    data === null ||
    data.task_id !== expectedTaskId ||
    !TASK_STATUSES.includes(data.task_status) ||
    !isValidProposal(data.proposal, { expectedTaskId }) ||
    (data.readback !== null && !isValidReadback(data.readback))
  ) {
    return false
  }

  const proposal = data.proposal
  if (
    proposal.id !== expectedProposalId ||
    proposal.action_type !== 'replace_assignment' ||
    proposal.shift_id !== expectedAction.shift_id ||
    proposal.outgoing_employee_code !== expectedAction.outgoing_employee_code ||
    proposal.incoming_employee_code !== expectedAction.incoming_employee_code
  ) {
    return false
  }
  if (data.readback !== null && data.readback.shift.shift_id !== proposal.shift_id) {
    return false
  }

  if (proposal.status === 'rejected') {
    return (
      isProposalConsistentWithTaskStatus(proposal, data.task_status) &&
      data.readback === null
    )
  }

  if (proposal.status === 'approved') {
    if (proposal.decided_at === null || proposal.executed_at === null || proposal.execution_outcome !== 'applied') {
      return false
    }
    if (proposal.verification_outcome === 'verified') {
      if (!isProposalConsistentWithTaskStatus(proposal, data.task_status) || data.readback === null) {
        return false
      }
      if (!readbackHasWorker(data.readback, expectedAction.incoming_employee_code)) {
        return false
      }
      if (
        expectedAction.outgoing_employee_code !== null &&
        readbackHasWorker(data.readback, expectedAction.outgoing_employee_code)
      ) {
        return false
      }
      return true
    }
    // Applied-but-unverified (`verification_outcome === null`, the
    // increment-2 recovery-gap state) or a recorded `verification_failed`:
    // neither has a confirmed current state to show, so both require a
    // null readback and the task status that belongs to that exact outcome.
    return isProposalConsistentWithTaskStatus(proposal, data.task_status) && data.readback === null
  }

  // A still-pending proposal has nothing decided yet, and therefore no
  // readback either - this validator should not normally see one, but a
  // pending proposal with a null readback is at least internally consistent.
  return isProposalConsistentWithTaskStatus(proposal, data.task_status) && data.readback === null
}

export async function createTask(message) {
  const data = await requestJson(AGENT_TASKS_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  })
  if (!isValidTaskEnvelope(data)) {
    throw new ResponseValidationError('The new-task response did not have the expected shape.')
  }
  return data
}

export async function sendMessage(taskId, message) {
  const data = await requestJson(taskMessagesUrl(taskId), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  })
  if (!isValidTaskEnvelope(data, { expectedId: taskId })) {
    throw new ResponseValidationError('The task-message response did not have the expected shape.')
  }
  return data
}

export async function fetchTask(taskId) {
  const data = await requestJson(taskUrl(taskId))
  if (!isValidTask(data, { expectedId: taskId })) {
    throw new ResponseValidationError('The task response did not have the expected shape.')
  }
  return data
}

/** Submits the proposal's exact stored action - never anything derived from
 * chat text or model output - to the one endpoint that can apply it.
 * `expectedTaskId` and `action` (the exact same object submitted) are both
 * enforced against the response by `isValidDecisionResponse` - a response
 * naming a different task or a different action than what was actually
 * sent is never trusted merely because the proposal id matches. */
export async function approveProposal(proposalId, expectedTaskId, action) {
  const data = await requestJson(`${proposalUrl(proposalId)}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(action),
  })
  if (!isValidDecisionResponse(data, { expectedProposalId: proposalId, expectedTaskId, expectedAction: action })) {
    throw new ResponseValidationError('The approval response did not have the expected shape.')
  }
  return data
}

/** `expectedAction` is the proposal's own stored action (nothing is
 * submitted in the request body for a rejection) - the response is still
 * checked against it, since a rejection response naming a different action
 * than what was actually on file is exactly as untrustworthy as one naming
 * the wrong proposal. */
export async function rejectProposal(proposalId, expectedTaskId, expectedAction) {
  const data = await requestJson(`${proposalUrl(proposalId)}/reject`, { method: 'POST' })
  if (!isValidDecisionResponse(data, { expectedProposalId: proposalId, expectedTaskId, expectedAction })) {
    throw new ResponseValidationError('The rejection response did not have the expected shape.')
  }
  return data
}

/** Read-only decision-state recovery (`GET /api/agent/proposals/{id}`) -
 * the same response shape and validation as approve/reject, but makes no
 * mutation and is never used merely to trigger verification (see
 * `reconcileMissingVerification` in AIAssistant.jsx for that). Used to
 * recover an already-verified proposal's current readback after a
 * refresh/navigation, checked against the proposal's own stored action as
 * already known from the loaded task. */
export async function fetchProposalDecision(proposalId, expectedTaskId, expectedAction) {
  const data = await requestJson(proposalUrl(proposalId))
  if (!isValidDecisionResponse(data, { expectedProposalId: proposalId, expectedTaskId, expectedAction })) {
    throw new ResponseValidationError('The proposal decision-state response did not have the expected shape.')
  }
  return data
}

/** Whether a write's outcome is uncertain and must be reconciled through an
 * authoritative GET rather than assumed to have failed - a committed write
 * with an unreadable response, or a request whose fate is genuinely
 * unknown. An `ApiError` is a confirmed refusal (nothing written); there is
 * nothing to reconcile for one. */
export function isOutcomeUncertain(error) {
  return error instanceof ResponseValidationError || error instanceof NetworkOutcomeUnknownError
}

/** A short, accurate description of any error this module's functions
 * throw - real HTTP status plus the backend's own detail, or a plain
 * message. Never collapses a 400/404/409/503 into one generic string. */
export function describeError(error) {
  if (!error) {
    return null
  }
  if (error instanceof ApiError) {
    if (typeof error.detail === 'string') {
      return `${error.status}: ${error.detail}`
    }
    if (error.detail && typeof error.detail === 'object') {
      const reasons =
        error.detail.reasons ||
        error.detail.reason_codes ||
        (error.detail.message ? [error.detail.message] : [])
      return `${error.status}: ${reasons.length ? reasons.join('; ') : 'Refused - see the conflict details below.'}`
    }
    return `Request failed with status ${error.status}.`
  }
  return error.message || 'Something went wrong.'
}

export const ACTIVE_TASK_STORAGE_KEY = 'shiftops.agent.activeTaskId'

export function loadStoredTaskId() {
  try {
    const raw = window.localStorage.getItem(ACTIVE_TASK_STORAGE_KEY)
    if (!raw) {
      return null
    }
    const id = Number(raw)
    return Number.isInteger(id) && id > 0 ? id : null
  } catch {
    // Private browsing / blocked storage - behave exactly as "no active task
    // remembered" rather than crash the screen.
    return null
  }
}

export function storeActiveTaskId(taskId) {
  try {
    window.localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, String(taskId))
  } catch {
    // Best-effort only: losing the remembered task id just means a refresh
    // will start a new one instead of recovering this one.
  }
}

export function clearStoredTaskId() {
  try {
    window.localStorage.removeItem(ACTIVE_TASK_STORAGE_KEY)
  } catch {
    // Nothing to clean up if storage was never reachable in the first place.
  }
}
