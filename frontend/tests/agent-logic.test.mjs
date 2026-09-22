// Repository-tracked logic checks for frontend/src/agent.js (Phase 9
// increment 3), following the exact pattern tests/schedule-logic.test.mjs
// already established: a standalone Node script, `global.fetch` mocked per
// case, a small inline `check()` helper. No server, no browser, no project
// database.
//
// Run with:  node tests/agent-logic.test.mjs   (from the frontend/ directory)
// Exits non-zero if any check fails.

import {
  ApiError,
  ResponseValidationError,
  NetworkOutcomeUnknownError,
  isOutcomeUncertain,
  isValidProposal,
  isValidReadback,
  createTask,
  sendMessage,
  fetchTask,
  approveProposal,
  rejectProposal,
  fetchProposalDecision,
  describeError,
  loadStoredTaskId,
  storeActiveTaskId,
  clearStoredTaskId,
  ACTIVE_TASK_STORAGE_KEY,
  transcriptText,
  visibleTranscriptMessages,
} from '../src/agent.js'

let passed = 0
let failed = 0
function check(condition, description) {
  if (condition) {
    passed += 1
    console.log(`PASS  ${description}`)
  } else {
    failed += 1
    console.log(`FAIL  ${description}`)
  }
}

function mockFetchOnce(status, body) {
  global.fetch = async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  })
}

function mockFetchThrows(message) {
  global.fetch = async () => {
    throw new TypeError(message)
  }
}

function mockFetchMalformedBody(status) {
  global.fetch = async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      throw new SyntaxError('Unexpected end of JSON input')
    },
  })
}

// A minimal in-memory localStorage stand-in - this file runs under plain
// Node, which has no `window`/`localStorage` of its own.
globalThis.window = {
  localStorage: (() => {
    let store = {}
    return {
      getItem: (key) => (key in store ? store[key] : null),
      setItem: (key, value) => {
        store[key] = String(value)
      },
      removeItem: (key) => {
        delete store[key]
      },
    }
  })(),
}

const validProposal = {
  id: 7,
  task_id: 3,
  created_at: '2027-01-11 22:05',
  status: 'pending',
  decided_at: null,
  action_type: 'replace_assignment',
  shift_id: 42,
  hall: 'Capella',
  start_datetime: '2027-01-11 22:00',
  end_datetime: '2027-01-12 03:00',
  outgoing_employee_code: 'SW-704',
  outgoing_full_name: 'Jordan Rivera',
  incoming_employee_code: 'SW-705',
  incoming_full_name: 'Sam Osei',
  rationale: 'SW-705 is the top-ranked eligible candidate.',
  executed_at: null,
  execution_outcome: null,
  verified_at: null,
  verification_outcome: null,
}

const validFillProposal = { ...validProposal, outgoing_employee_code: null, outgoing_full_name: null }

const validTask = {
  id: 3,
  created_at: '2027-01-11 22:00',
  updated_at: '2027-01-11 22:05',
  status: 'awaiting_approval',
  request_text: 'Jordan Rivera called out.',
  step_count: 3,
  messages: [
    { id: 1, created_at: '2027-01-11 22:00', sequence: 1, role: 'supervisor', tool_name: null, content: 'Jordan Rivera called out.' },
    { id: 2, created_at: '2027-01-11 22:01', sequence: 2, role: 'tool_call', tool_name: 'find_shift', content: '{}' },
    { id: 3, created_at: '2027-01-11 22:02', sequence: 3, role: 'assistant', tool_name: null, content: 'Proposed a replacement.' },
  ],
  proposals: [validProposal],
}

const validReadback = {
  shift: { shift_id: 42, hall: 'Capella', start_datetime: '2027-01-11 22:00', end_datetime: '2027-01-12 03:00', required_staff: 1 },
  assigned_employees: [{ employee_code: 'SW-705', full_name: 'Sam Osei', is_active: true }],
  assigned_count: 1,
  covered: true,
}

// -------------------------------------------------------- transcript display

const visibleMessages = visibleTranscriptMessages(validTask.messages)
check(
  visibleMessages.length === 2 && visibleMessages.every((message) => ['supervisor', 'assistant'].includes(message.role)),
  'the display transcript keeps only supervisor and assistant messages',
)
check(
  validTask.messages.some((message) => message.role === 'tool_call'),
  'filtering the display does not remove tool messages from the validated task data',
)
check(
  transcriptText({ role: 'assistant', content: '**Ready**\n- Candidate: `SW-705`' }) === 'Ready\n• Candidate: SW-705',
  'assistant Markdown markers are normalized into readable plain text',
)
check(
  transcriptText({ role: 'assistant', content: '{"message":"The shift is covered."}' }) === 'The shift is covered.',
  'an ordinary assistant response wrapped in JSON is shown without raw JSON syntax',
)
check(
  !transcriptText({ role: 'assistant', content: '{not valid JSON}' }).includes('{'),
  'malformed structured assistant output is replaced by a readable message instead of leaking JSON markers',
)
check(
  transcriptText({ role: 'supervisor', content: '**keep my exact input**' }) === '**keep my exact input**',
  'supervisor text is preserved exactly rather than rewritten',
)

// ------------------------------------------------------------ isValidProposal

check(isValidProposal(validProposal), 'a well-formed replacement proposal passes validation')
check(isValidProposal(validFillProposal), 'a well-formed fill proposal (outgoing null) passes validation')
check(!isValidProposal({ ...validProposal, outgoing_employee_code: null }), 'outgoing_employee_code null with a non-null full_name is rejected (mismatched pair)')
check(!isValidProposal({ ...validProposal, status: 'unknown' }), 'an unrecognized proposal status is rejected')
check(!isValidProposal(validProposal, { expectedTaskId: 999 }), 'a proposal naming the wrong task_id is rejected')
check(!isValidProposal({ ...validProposal, id: 'seven' }), 'a non-numeric proposal id is rejected')
check(!isValidProposal({ ...validProposal, action_type: 'delete_employee' }), 'an unsupported proposal action type is rejected')
check(
  !isValidProposal({ ...validProposal, execution_outcome: 'applied' }),
  'a pending proposal carrying execution state is rejected',
)

// ------------------------------------------------------------- isValidReadback

check(isValidReadback(validReadback), 'a well-formed readback passes validation')
check(!isValidReadback({ ...validReadback, assigned_count: 2 }), 'a readback whose assigned_count does not match its list is rejected')
check(!isValidReadback({ ...validReadback, assigned_employees: [{ employee_code: 'SW-705' }] }), 'a readback with an incomplete worker entry is rejected')

// ------------------------------------------------------------------ createTask

mockFetchOnce(201, { task: validTask, result: { kind: 'proposal', message: 'Proposed a replacement.', proposal: validProposal, steps_used: 3, blocked_reason: null } })
const created = await createTask('Jordan Rivera called out.')
check(created.task.id === 3 && created.result.kind === 'proposal', 'a well-formed new-task response is accepted')

mockFetchOnce(201, { task: { ...validTask, messages: 'not an array' }, result: null })
try {
  await createTask('x')
  check(false, 'a task envelope with a malformed messages field is rejected')
} catch (error) {
  check(error instanceof ResponseValidationError, 'a task envelope with a malformed messages field is rejected')
}

mockFetchOnce(400, { detail: "'shift_id' and 'incoming_employee_code' are required." })
try {
  await createTask('')
  check(false, 'a 400 from the backend surfaces as ApiError')
} catch (error) {
  check(error instanceof ApiError && error.status === 400, 'a 400 from the backend surfaces as ApiError')
}

mockFetchOnce(503, { detail: 'AI is not configured: the OPENAI_API_KEY environment variable is not set.' })
try {
  await createTask('anything')
  check(false, 'a 503 configuration failure surfaces as ApiError with the backend detail intact')
} catch (error) {
  check(
    error instanceof ApiError && error.status === 503 && describeError(error).includes('OPENAI_API_KEY'),
    'a 503 configuration failure surfaces as ApiError with the backend detail intact, never a secret value',
  )
}

// ---------------------------------------------------------------- sendMessage

mockFetchOnce(200, { task: validTask, result: { kind: 'answer', message: 'ok', proposal: null, steps_used: 1, blocked_reason: null } })
const sent = await sendMessage(3, 'follow-up')
check(sent.task.id === 3, 'sendMessage accepts a well-formed response')

mockFetchOnce(200, { task: { ...validTask, id: 999 }, result: { kind: 'answer', message: 'ok', proposal: null, steps_used: 1, blocked_reason: null } })
try {
  await sendMessage(3, 'follow-up')
  check(false, 'sendMessage rejects a response naming a different task id than the one requested')
} catch (error) {
  check(error instanceof ResponseValidationError, 'sendMessage rejects a response naming a different task id than the one requested')
}

// ----------------------------------------------------------------- fetchTask

mockFetchOnce(200, validTask)
const fetched = await fetchTask(3)
check(fetched.id === 3, 'fetchTask accepts a well-formed task')

mockFetchOnce(404, { detail: 'No agent task 999.' })
try {
  await fetchTask(999)
  check(false, 'fetchTask surfaces a 404 as ApiError')
} catch (error) {
  check(error instanceof ApiError && error.status === 404, 'fetchTask surfaces a 404 as ApiError')
}

// ------------------------------------------------------------- approve/reject

const action = { shift_id: 42, outgoing_employee_code: 'SW-704', incoming_employee_code: 'SW-705' }
const approvedProposal = { ...validProposal, status: 'approved', execution_outcome: 'applied', verification_outcome: 'verified', decided_at: '2027-01-11 22:10', executed_at: '2027-01-11 22:10', verified_at: '2027-01-11 22:10' }
const rejectedProposal = { ...validProposal, status: 'rejected', decided_at: '2027-01-12 08:10' }
const appliedUnverifiedProposal = { ...validProposal, status: 'approved', execution_outcome: 'applied', decided_at: '2027-01-11 22:10', executed_at: '2027-01-11 22:10' }
const verificationFailedProposal = { ...appliedUnverifiedProposal, verification_outcome: 'verification_failed: readback unavailable', verified_at: '2027-01-11 22:11' }

mockFetchOnce(200, { task_id: 3, task_status: 'closed', proposal: approvedProposal, readback: validReadback })
const approved = await approveProposal(7, 3, action)
check(approved.proposal.status === 'approved' && approved.readback.covered === true, 'a well-formed approval response is accepted, including its readback')

mockFetchOnce(200, { task_id: 3, task_status: 'closed', proposal: { ...approvedProposal, id: 999 }, readback: validReadback })
try {
  await approveProposal(7, 3, action)
  check(false, 'approveProposal rejects a response naming a different proposal id than the one requested')
} catch (error) {
  check(error instanceof ResponseValidationError, 'approveProposal rejects a response naming a different proposal id than the one requested')
}

mockFetchOnce(409, {
  detail: { message: '1 conflict(s) found during revalidation.', conflicts: [{ reason_codes: ['worker_inactive'], reasons: ['This worker is not active.'] }] },
})
try {
  await approveProposal(7, 3, action)
  check(false, 'a stale-state 409 surfaces its structured conflicts, not a single generic string')
} catch (error) {
  check(
    error instanceof ApiError && error.status === 409 && Array.isArray(error.detail.conflicts) && error.detail.conflicts[0].reason_codes[0] === 'worker_inactive',
    'a stale-state 409 surfaces its structured conflicts, not a single generic string',
  )
}

mockFetchOnce(200, { task_id: 3, task_status: 'closed', proposal: rejectedProposal, readback: null })
const rejected = await rejectProposal(7, 3, action)
check(rejected.proposal.status === 'rejected' && rejected.readback === null, 'a well-formed rejection response is accepted, with no readback')

// ---------------------------------------------------------- network/malformed

mockFetchThrows('offline')
try {
  await approveProposal(7, 3, action)
  check(false, 'a network failure during approval is NetworkOutcomeUnknownError, never a confirmed refusal or success')
} catch (error) {
  check(
    error instanceof NetworkOutcomeUnknownError && isOutcomeUncertain(error),
    'a network failure during approval is NetworkOutcomeUnknownError, never a confirmed refusal or success',
  )
}

mockFetchMalformedBody(200)
try {
  await approveProposal(7, 3, action)
  check(false, 'a committed-but-unreadable approval response is ResponseValidationError, distinct from a network failure')
} catch (error) {
  check(
    error instanceof ResponseValidationError && isOutcomeUncertain(error) && !(error instanceof ApiError),
    'a committed-but-unreadable approval response is ResponseValidationError, distinct from a network failure',
  )
}

// ------------------------------------------- decision response fully bound to request

async function expectDecisionRejected(mockBody, description) {
  mockFetchOnce(200, mockBody)
  try {
    await approveProposal(7, 3, action)
    check(false, description)
  } catch (error) {
    check(error instanceof ResponseValidationError, description)
  }
}

await expectDecisionRejected(
  { task_id: 999, task_status: 'closed', proposal: approvedProposal, readback: validReadback },
  'a response naming a different task_id than requested is rejected',
)

await expectDecisionRejected(
  { task_id: 3, task_status: 'closed', proposal: { ...approvedProposal, shift_id: 999 }, readback: validReadback },
  'a response whose proposal names a different shift than the exact submitted action is rejected',
)

await expectDecisionRejected(
  { task_id: 3, task_status: 'closed', proposal: { ...approvedProposal, outgoing_employee_code: 'SW-999' }, readback: validReadback },
  'a response whose proposal names a different outgoing worker than the exact submitted action is rejected',
)

await expectDecisionRejected(
  { task_id: 3, task_status: 'closed', proposal: { ...approvedProposal, incoming_employee_code: 'SW-999' }, readback: validReadback },
  'a response whose proposal names a different incoming worker than the exact submitted action is rejected',
)

await expectDecisionRejected(
  {
    task_id: 3,
    task_status: 'closed',
    proposal: approvedProposal,
    readback: { ...validReadback, shift: { ...validReadback.shift, shift_id: 999 } },
  },
  'a readback naming a different shift than the proposal itself is rejected',
)

await expectDecisionRejected(
  { task_id: 3, task_status: 'closed', proposal: approvedProposal, readback: null },
  'a verified approval with no readback at all is rejected',
)

await expectDecisionRejected(
  {
    task_id: 3,
    task_status: 'closed',
    proposal: approvedProposal,
    readback: { ...validReadback, assigned_employees: [{ employee_code: 'SW-999', full_name: 'Someone Else', is_active: true }] },
  },
  "a verified readback that does not contain the incoming worker is rejected",
)

await expectDecisionRejected(
  {
    task_id: 3,
    task_status: 'closed',
    proposal: approvedProposal,
    readback: {
      ...validReadback,
      assigned_employees: [
        { employee_code: 'SW-705', full_name: 'Sam Osei', is_active: true },
        { employee_code: 'SW-704', full_name: 'Jordan Rivera', is_active: true },
      ],
      assigned_count: 2,
    },
  },
  'a verified replacement readback that still contains the outgoing worker is rejected',
)

await expectDecisionRejected(
  { task_id: 3, task_status: 'closed', proposal: { ...rejectedProposal, execution_outcome: 'applied' }, readback: null },
  'a rejected proposal carrying a non-null execution outcome is rejected',
)

await expectDecisionRejected(
  { task_id: 3, task_status: 'closed', proposal: rejectedProposal, readback: validReadback },
  'a rejected proposal carrying a non-null readback is rejected',
)

await expectDecisionRejected(
  { task_id: 3, task_status: 'open', proposal: { ...approvedProposal, verification_outcome: 'nonsense', verified_at: null }, readback: null },
  'an approved proposal carrying an unknown verification outcome is rejected',
)

await expectDecisionRejected(
  { task_id: 3, task_status: 'blocked', proposal: approvedProposal, readback: validReadback },
  'a verified proposal whose task is not closed is rejected',
)

mockFetchOnce(200, { task_id: 3, task_status: 'awaiting_approval', proposal: appliedUnverifiedProposal, readback: null })
const appliedUnverified = await fetchProposalDecision(7, 3, action)
check(
  appliedUnverified.proposal.verification_outcome === null,
  'an applied-but-unverified proposal is accepted only with its awaiting-approval task state',
)

mockFetchOnce(200, { task_id: 3, task_status: 'blocked', proposal: verificationFailedProposal, readback: null })
const verificationFailed = await fetchProposalDecision(7, 3, action)
check(
  verificationFailed.proposal.verification_outcome.startsWith('verification_failed: '),
  'a recorded verification failure is accepted with its blocked task state and null readback',
)

mockFetchOnce(200, { task_id: 3, task_status: 'closed', proposal: approvedProposal, readback: validReadback })
const decision = await fetchProposalDecision(7, 3, action)
check(decision.readback.covered === true, 'fetchProposalDecision recovers a verified proposal\'s readback via the read-only GET route')

// --------------------------------------------------------------- localStorage

check(loadStoredTaskId() === null, 'no active task is remembered initially')
storeActiveTaskId(3)
check(loadStoredTaskId() === 3, 'a stored task id round-trips through localStorage')
clearStoredTaskId()
check(loadStoredTaskId() === null, '"Start new task" clears the remembered task id')
window.localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, 'not-a-number')
check(loadStoredTaskId() === null, 'a corrupted stored value is treated as "no active task", not a crash')

console.log(`\n${passed} passed, ${failed} failed.`)
process.exit(failed === 0 ? 0 : 1)
