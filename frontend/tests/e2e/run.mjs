// Repository-tracked browser end-to-end journeys (Phase 7 closeout,
// whole-project review finding 5). Exercises the ACTUAL React components in
// a real Chromium browser via Playwright - not just schedule.js's pure
// helpers (see ../schedule-logic.test.mjs for those).
//
// Isolation, strictly: a fresh isolated backend (backend/e2e_server.py,
// which redirects database.DATABASE_PATH before ever importing `main` -
// never opens backend/shiftops.db) on a free, non-8000 port, and an
// isolated `vite preview` static server on a free, non-5173 port. The
// frontend's API modules hardcode http://127.0.0.1:8000; every request to
// that origin is intercepted here and rewritten to the isolated backend's
// port, so the developer's real backend is never contacted and never
// touched. The developer's own dev servers (backend 8000, frontend 5173)
// are never started, stopped, or interfered with - this harness never binds
// either port.
//
// Two kinds of coverage, kept distinct (per instruction):
//   - REAL-BACKEND journeys: every request reaches the real isolated
//     backend and returns its real response. Timing-only interception
//     (adding a delay before letting the real request through) is used for
//     race/cancellation journeys, but never changes content.
//   - MOCKED/intercepted journeys: one specific response is deliberately
//     corrupted (after the real backend has already committed the write)
//     to exercise the response-validation/reconciliation path, which a real
//     backend cannot be made to produce on its own without modifying it.
//
// Run with:  node tests/e2e/run.mjs   (from the frontend/ directory)
// Requires: `npm run build` has produced frontend/dist (run automatically
// below), and the `playwright` devDependency's Chromium browser installed
// (already present in this environment; see package.json).
// Exits non-zero if any journey fails.

import { spawn } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import net from 'node:net'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = path.resolve(__dirname, '..', '..')
const BACKEND_ROOT = path.resolve(FRONTEND_ROOT, '..', 'backend')

// A real OS temp directory, never inside the repo - the isolated database
// this harness creates must never be mistaken for, or accidentally land
// next to, the project's own backend/shiftops.db.
let SCRATCH_DIR

// Dynamically selected free ports (Codex review: fixed ports 8321/8322 made
// the harness fail to start whenever a PRIOR run's process had leaked and
// was still holding one of them - the exact failure mode that made this
// harness unreliable to rerun). Still refuses the developer's own ports
// outright, and refuses to pick the same port twice.
function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.unref()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
  })
}

function isPortFree(port) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ port, host: '127.0.0.1' })
    socket.once('connect', () => {
      socket.destroy()
      resolve(false)
    })
    socket.once('error', () => resolve(true))
    socket.setTimeout(1000, () => {
      socket.destroy()
      resolve(true)
    })
  })
}

let BACKEND_PORT
let FRONTEND_PORT
let BACKEND_ORIGIN
let FRONTEND_ORIGIN
const REAL_BACKEND_ORIGIN = 'http://127.0.0.1:8000'

async function chooseIsolatedPorts() {
  BACKEND_PORT = await getFreePort()
  FRONTEND_PORT = await getFreePort()
  while (FRONTEND_PORT === BACKEND_PORT) {
    FRONTEND_PORT = await getFreePort()
  }
  if ([8000, 5173].includes(BACKEND_PORT) || [8000, 5173].includes(FRONTEND_PORT)) {
    throw new Error('refusing to use the developer server ports for the isolated harness')
  }
  BACKEND_ORIGIN = `http://127.0.0.1:${BACKEND_PORT}`
  FRONTEND_ORIGIN = `http://127.0.0.1:${FRONTEND_PORT}`
}

// Force-kills the whole process tree rooted at `proc` and waits for the
// actual server process to exit before resolving (Codex review: a venv
// `python.exe` launcher on Windows re-execs into a separate child
// interpreter process that actually holds the listening socket - killing
// only the parent PID left that child running and the port still bound,
// which is exactly how prior runs of this harness leaked long-lived
// listeners). `taskkill /T` kills the whole tree in one call; plain
// `proc.kill()` from Node on Windows only ever targets the single PID.
function waitForExit(proc, timeoutMs) {
  if (!proc || proc.exitCode !== null || proc.signalCode !== null) {
    return Promise.resolve(true)
  }
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), timeoutMs)
    proc.once('exit', () => {
      clearTimeout(timer)
      resolve(true)
    })
  })
}

function runTaskkill(pid) {
  return new Promise((resolve) => {
    const killer = spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' })
    killer.once('error', () => resolve(false))
    killer.once('exit', (code) => resolve(code === 0))
  })
}

async function killProcessTree(proc) {
  if (!proc || proc.exitCode !== null || proc.signalCode !== null) {
    return true
  }

  if (process.platform === 'win32') {
    // Await taskkill itself as well as the tracked process. A failed or
    // blocked taskkill must never be mistaken for completed cleanup.
    await runTaskkill(proc.pid)
  } else {
    proc.kill('SIGTERM')
  }

  if (await waitForExit(proc, 5000)) {
    return true
  }

  // The Vite server is launched directly, so this is a useful fallback on
  // Windows if taskkill is unavailable. The later port check still catches
  // an untracked descendant that somehow survives.
  proc.kill('SIGTERM')
  return waitForExit(proc, 3000)
}

let passed = 0
let failed = 0
const failures = []

function check(condition, description) {
  if (condition) {
    passed += 1
    console.log(`PASS  ${description}`)
  } else {
    failed += 1
    failures.push(description)
    console.log(`FAIL  ${description}`)
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function waitForHealth(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url)
      if (response.ok) {
        return true
      }
    } catch {
      // not up yet
    }
    await sleep(200)
  }
  return false
}

/** The sidebar nav is a <nav><ul>...</ul></nav>, so its buttons have role
 * "list" as an ancestor - scoping to it is what disambiguates the "Schedule"
 * nav item from Schedule.jsx's identically-labeled in-page action buttons
 * ("Schedule"/"Generate Schedule" both also appear as in-page text). */
function navButton(page, label) {
  return page.getByRole('list').getByRole('button', { name: label, exact: true })
}

/** Every in-page action button, scoped to <main> to avoid the sidebar's
 * identically-labeled "Schedule" nav button. */
function mainButton(page, label) {
  return page.getByRole('main').getByRole('button', { name: label, exact: true })
}

async function showScheduleTab(page, label) {
  const tab = page.getByRole('tab', { name: label, exact: true })
  await tab.click()
  await tab.getAttribute('aria-selected')
}

/** The AI Assistant chat textarea, identified by its placeholder text. */
function chatTextarea(page) {
  return page.getByRole('textbox', { name: 'Message the AI Assistant' })
}

async function sendChat(page, text) {
  await chatTextarea(page).fill(text)
  await page.getByRole('button', { name: 'Send', exact: true }).click()
}

/** Always safe to click - the AI Assistant screen renders this button
 * unconditionally - so every agent journey starts from a known, blank
 * state rather than recovering whatever task an earlier journey left
 * active in localStorage. */
async function startNewAgentTask(page) {
  await mainButton(page, 'Start new task').click()
}

function toIsolatedUrl(originalUrl) {
  const url = new URL(originalUrl)
  url.protocol = 'http:'
  url.hostname = '127.0.0.1'
  url.port = String(BACKEND_PORT)
  return url.toString()
}

// --------------------------------------------------------------- fixtures

/** Ground truth read directly from the isolated backend (Node-side, no
 * CORS concerns, not a UI interaction) - used only to drive the browser
 * test precisely (exact shift ids/times), never as the thing under test. */
async function backendJson(pathAndQuery) {
  const response = await fetch(`${BACKEND_ORIGIN}${pathAndQuery}`)
  if (!response.ok) {
    throw new Error(`isolated backend ${pathAndQuery} returned ${response.status}`)
  }
  return response.json()
}

// ------------------------------------------------------------- processes

function spawnPython(args) {
  const venvPython = path.join(BACKEND_ROOT, '.venv', 'Scripts', 'python.exe')
  const pythonExe = process.env.SHIFTOPS_PYTHON || (existsSync(venvPython) ? venvPython : 'python')
  return spawn(pythonExe, args, {
    cwd: BACKEND_ROOT,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONPATH: process.env.SHIFTOPS_PYTHONPATH || process.env.PYTHONPATH },
  })
}

async function main() {
  let backend
  let frontend
  let browser
  let runError
  let backendOutput = ''
  const viteBin = path.join(FRONTEND_ROOT, 'node_modules', 'vite', 'bin', 'vite.js')

  try {
    await chooseIsolatedPorts()
    SCRATCH_DIR = mkdtempSync(path.join(tmpdir(), 'shiftops-e2e-'))

    console.log(`Starting isolated backend on ${BACKEND_ORIGIN} ...`)
    backend = spawnPython([
      'e2e_server.py',
      '--port',
      String(BACKEND_PORT),
      '--db',
      path.join(SCRATCH_DIR, 'isolated.sqlite'),
      '--origin',
      FRONTEND_ORIGIN,
    ])
    backend.stdout.on('data', (chunk) => {
      backendOutput += chunk.toString()
    })
    backend.stderr.on('data', (chunk) => {
      backendOutput += chunk.toString()
    })

    console.log('Building the frontend once (vite build) ...')
    await new Promise((resolve, reject) => {
      const build = spawn(process.execPath, [viteBin, 'build'], {
        cwd: FRONTEND_ROOT,
        stdio: 'inherit',
      })
      build.once('error', reject)
      build.once('exit', (code) =>
        code === 0 ? resolve() : reject(new Error(`vite build exited ${code}`)),
      )
    })

    console.log(`Starting isolated frontend preview on ${FRONTEND_ORIGIN} ...`)
    // Vite's own JS entry, launched directly with this same Node executable -
    // never `npx`/`shell: true`, so the tracked process owns the listener.
    frontend = spawn(
      process.execPath,
      [viteBin, 'preview', '--port', String(FRONTEND_PORT), '--strictPort', '--host', '127.0.0.1'],
      { cwd: FRONTEND_ROOT, stdio: 'ignore' },
    )

    const backendUp = await waitForHealth(`${BACKEND_ORIGIN}/api/health`, 20000)
    if (!backendUp) {
      console.error('Isolated backend output so far:\n' + backendOutput)
    }
    check(backendUp, 'the isolated backend became healthy')
    const frontendUp = await waitForHealth(FRONTEND_ORIGIN, 20000)
    check(frontendUp, 'the isolated frontend preview server became reachable')
    if (!backendUp || !frontendUp) {
      throw new Error('one or more isolated test servers failed to start')
    }

    browser = await chromium.launch()
    const context = await browser.newContext()
    const page = await context.newPage()

    const consoleErrors = []
    page.on('pageerror', (error) => consoleErrors.push(String(error)))
    if (process.env.E2E_DEBUG) {
      page.on('console', (msg) => console.log('[console]', msg.text()))
      page.on('requestfailed', (req) => console.log('[requestfailed]', req.url(), req.failure()?.errorText))
    }

    // The one generic redirect every request needs, since the frontend's
    // API modules hardcode the real backend's port. Registered first so
    // more specific per-journey routes (added later, per Playwright's
    // most-recently-added-first matching) can `fallback()` into it after
    // adding a delay, or bypass it entirely when they fulfill their own
    // response.
    await page.route(`${REAL_BACKEND_ORIGIN}/**`, (route) => {
      route.continue({ url: toIsolatedUrl(route.request().url()) })
    })

    await page.goto(FRONTEND_ORIGIN)
    // Phase 8 acceptance correction: "Schedule" and "Generate Schedule" used
    // to be two separate sidebar entries into the identical Schedule.jsx
    // workflow - now there is exactly one "Schedule" nav item, and
    // generating a proposal is reachable via the in-page button inside it.
    check(
      (await navButton(page, 'Schedule').count()) === 1,
      'there is exactly one "Schedule" navigation item, not a duplicate "Generate Schedule" entry',
    )
    check(
      (await page.getByRole('list').getByRole('button', { name: 'Generate Schedule', exact: true }).count()) === 0,
      'the sidebar no longer has a separate "Generate Schedule" navigation item',
    )
    await navButton(page, 'Schedule').click()
    await showScheduleTab(page, 'Proposals')
    await mainButton(page, 'Generate Schedule').waitFor({ timeout: 10000 })
    check(
      await mainButton(page, 'Generate Schedule').isVisible(),
      'the Generate Schedule action remains accessible inside the single Schedule screen',
    )
    if (process.env.E2E_DEBUG) {
      await sleep(1000)
      console.log('[debug] body text:', (await page.locator('body').innerText()).slice(0, 800))
    }

    await journeyManualUncoveredFill(page)
    await journeyGenerateReviewApprove(page)
    await journeyConflictAndReplace(page)
    await journeyUncertainGenerateBlocksRetry(page)
    await journeyUncertainReplaceBlocksRetry(page)
    await journeyCancelDelayedReplacement(page)
    await journeyMutualExclusion(page)
    await journeyCorruptedApprovalResponse(page)
    await journeyWeekChangeDuringDelayedRefresh(page)
    await journeyDashboardAndWorkforcePlanning(page)
    await journeyEmployeesResponsiveLayout(page)

    // ----------------------------------------------- Phase 9 increment 3: AI Assistant
    await journeyAgentInfoAndApprovedTrap(page)
    await journeyAgentAmbiguousClarification(page)
    await journeyAgentNoCandidates(page)
    await journeyAgentCalloutApproveVerify(page)
    await journeyAgentRecoverVerifiedReadback(page)
    await journeyAgentReject(page)
    await journeyAgentStaleConflict(page)
    await journeyAgentDuplicateClicks(page)
    await journeyAgentNetworkUnknownReconcile(page)
    await journeyAgentVerificationFailedDisplay(page)
    await journeyAgentAppliedUnverifiedReconcile(page)
    await journeyAgentMissingConfiguration(page)

    check(consoleErrors.length === 0, `no uncaught page errors occurred (${consoleErrors.join('; ')})`)
  } catch (error) {
    runError = error
    check(false, `the browser journey completed without an infrastructure error (${error.message})`)
  } finally {
    if (browser) {
      await browser.close()
    }
    // Both awaited in parallel, and each wait is for the actual server
    // process to exit (not merely for the kill signal to be sent) - see
    // killProcessTree.
    const [frontendStopped, backendStopped] = await Promise.all([
      killProcessTree(frontend),
      killProcessTree(backend),
    ])
    check(frontendStopped, 'the isolated frontend process exited during cleanup')
    check(backendStopped, 'the isolated backend process exited during cleanup')
    if (SCRATCH_DIR) {
      rmSync(SCRATCH_DIR, { recursive: true, force: true })
    }
  }

  // Proves this run leaves no listener behind on either port it created -
  // not just that the tracked child processes exited, but that the actual
  // sockets are gone.
  if (BACKEND_PORT) {
    const backendPortFree = await isPortFree(BACKEND_PORT)
    check(backendPortFree, `the isolated backend port ${BACKEND_PORT} is free after cleanup`)
  }
  if (FRONTEND_PORT) {
    const frontendPortFree = await isPortFree(FRONTEND_PORT)
    check(frontendPortFree, `the isolated frontend port ${FRONTEND_PORT} is free after cleanup`)
  }

  console.log(`\n${passed} passed, ${failed} failed.`)
  if (failed > 0) {
    console.log('Failures:')
    for (const description of failures) {
      console.log(`  - ${description}`)
    }
  }
  if (runError) {
    console.error(runError)
  }
  process.exit(failed === 0 ? 0 : 1)
}

// ------------------------------------------------------------- journeys

/** uncovered shift -> choose an eligible worker -> cancel -> confirm */
async function journeyManualUncoveredFill(page) {
  await showScheduleTab(page, 'Shifts')
  const before = await backendJson('/api/schedule/weeks/2026-09-21')
  const target = before.shifts.find((shift) => !shift.covered)
  check(Boolean(target), 'fixture sanity: the selected week has an uncovered shift for manual fill')
  if (!target) return
  const coverage = await backendJson(`/api/shifts/${target.id}/coverage`)
  const incoming = coverage.eligible_candidates[0]
  check(Boolean(incoming), 'fixture sanity: the uncovered shift has an eligible manual-fill candidate')
  if (!incoming) return

  await page.getByRole('button', { name: 'Assign worker', exact: true }).first().click()
  const dialog = page.getByRole('alertdialog', { name: 'Assign worker' })
  await dialog.waitFor()
  await dialog.locator('select').selectOption(incoming.employee_code)
  const stillBefore = await backendJson('/api/schedule/weeks/2026-09-21')
  const stillTarget = stillBefore.shifts.find((shift) => shift.id === target.id)
  check(stillTarget.assigned_count === 0, 'choosing a worker does not assign them before confirmation')
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  await dialog.waitFor({ state: 'hidden' })

  await page.getByRole('button', { name: 'Assign worker', exact: true }).first().click()
  await dialog.waitFor()
  await dialog.locator('select').selectOption(incoming.employee_code)
  await dialog.getByRole('button', { name: 'Confirm assignment', exact: true }).click()
  await dialog.waitFor({ state: 'hidden', timeout: 10000 })
  const after = await backendJson('/api/schedule/weeks/2026-09-21')
  const filled = after.shifts.find((shift) => shift.id === target.id)
  check(
    filled.assigned_employees.some((worker) => worker.employee_code === incoming.employee_code),
    'explicit confirmation persists the selected manual assignment',
  )
}

/** generate -> review -> cancel approval -> approve -> reload/recover */
async function journeyGenerateReviewApprove(page) {
  await showScheduleTab(page, 'Proposals')
  await mainButton(page, 'Generate Schedule').click()
  await page.getByText(/^Proposal #\d+ — pending$/).waitFor({ timeout: 15000 })
  check(true, 'Generate Schedule produced a pending proposal')

  const proposalPanel = page.getByRole('region', { name: 'Schedule proposal' })
  await proposalPanel.getByLabel('Expected coverage summary').waitFor()
  check(
    await proposalPanel.getByText('Required', { exact: true }).isVisible() &&
      await proposalPanel.getByText('Expected coverage', { exact: true }).isVisible(),
    'the proposal review opens with a concise required/existing/proposed/uncovered coverage summary',
  )
  const filters = proposalPanel.getByRole('group', { name: 'Proposal shift filter' })
  check(await filters.getByRole('button', { name: 'Changes', exact: true }).getAttribute('aria-pressed') === 'true', 'proposal review defaults to the Changes filter')
  await filters.getByRole('button', { name: 'Uncovered', exact: true }).click()
  const uncoveredReason = proposalPanel.getByText(/Why .*remain uncovered/, { exact: true }).first()
  await uncoveredReason.waitFor()
  check(await uncoveredReason.isVisible(), 'the Uncovered filter keeps the at-a-glance reason label visible while details stay collapsed')
  await filters.getByRole('button', { name: 'All shifts', exact: true }).click()
  await proposalPanel.locator('.proposal-shift-collapsed').first().waitFor()
  check(true, 'the All shifts review keeps unchanged covered shifts collapsed')
  await filters.getByRole('button', { name: 'Changes', exact: true }).click()
  await proposalPanel.locator('.proposal-technical-details').first().locator('summary').click()
  check(await proposalPanel.getByText(/Shift #\d+\. Current:/).first().isVisible(), 'worker names are primary and technical IDs are available as secondary details')

  await page.getByRole('button', { name: 'Approve', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Confirm approval' }).waitFor()
  check(true, 'clicking Approve opens the confirmation panel, not an immediate write')

  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Confirm approval' }).waitFor({ state: 'hidden' })
  const stillPending = await page.getByText(/^Proposal #\d+ — pending$/).count()
  check(stillPending === 1, 'cancelling the confirmation leaves the proposal pending, nothing created')

  await page.getByRole('button', { name: 'Approve', exact: true }).click()
  await page.getByRole('button', { name: 'Confirm approval', exact: true }).click()
  await page.getByText(/^Proposal #\d+ — approved$/).waitFor({ timeout: 15000 })
  check(true, 'confirming approval creates the assignments and the proposal reports approved')

  await page.reload()
  await navButton(page, 'Schedule').click()
  await showScheduleTab(page, 'Proposals')
  await page.getByText(/^Proposal #\d+ — approved$/).waitFor({ timeout: 15000 })
  check(true, 'reloading the page recovers the approved proposal from the backend, not from lost client state')
}

/** later class/leave edit -> visible assignment conflict -> replacement -> verified resolution */
async function journeyConflictAndReplace(page) {
  // Deliberately on WEEK_B (2026-09-28), not the week Journey A generated
  // and approved a full schedule on: maximizing coverage there naturally
  // pushes every seeded worker toward their weekly hour cap, leaving no
  // genuine replacement candidate anywhere. WEEK_B carries exactly one
  // hand-seeded assignment (SW-201, see e2e_fixtures.py) with every other
  // worker completely free - a real candidate is guaranteed by construction.
  const weekB = await backendJson('/api/schedule/weeks/2026-09-28')
  const assignedShift = weekB.shifts.find((shift) => shift.assigned_employees.length > 0)
  check(Boolean(assignedShift), 'fixture sanity: WEEK_B has the one hand-seeded assignment')
  if (!assignedShift) return
  const outgoing = assignedShift.assigned_employees[0]
  const coverage = await backendJson(`/api/shifts/${assignedShift.id}/coverage?exclude_employee_code=${outgoing.employee_code}`)
  const expectedCandidateCode = coverage.eligible_candidates[0]?.employee_code
  check(Boolean(expectedCandidateCode), 'fixture sanity: another worker is eligible to replace the seeded assignment')
  if (!expectedCandidateCode) return

  await navButton(page, 'Schedule').click()
  await mainButton(page, 'Next week').click()
  await page.getByText('September 28, 2026').first().waitFor({ timeout: 5000 })
  await showScheduleTab(page, 'Proposals')

  await navButton(page, 'Employees').click()
  await page.getByRole('button', { name: `View details for ${outgoing.full_name}` }).click()
  await page.getByRole('button', { name: 'Add approved leave', exact: true }).click()

  const startLocal = assignedShift.start_datetime.replace(' ', 'T')
  const endLocal = assignedShift.end_datetime.replace(' ', 'T')
  await page.locator('#leave-start').fill(startLocal)
  await page.locator('#leave-end').fill(endLocal)
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await page.getByText(/Added the approved leave|Saved the approved leave/i).first().waitFor({ timeout: 10000 })

  await navButton(page, 'Schedule').click()
  const conflictText = page.getByText('Assignment conflict — see details', { exact: true }).first()
  await conflictText.waitFor({ timeout: 10000 })
  check(await conflictText.isVisible(), 'the leave edit surfaces a visible conflict label on the affected assignment')

  const conflictRow = page.locator('.schedule-hall li', { hasText: outgoing.employee_code }).filter({ hasText: 'Assignment conflict' })
  await conflictRow.getByRole('button', { name: 'Replace', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Replace assignment' }).waitFor()
  await page.locator('select').last().selectOption(expectedCandidateCode)
  await page.getByRole('button', { name: 'Confirm replacement', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Replace assignment' }).waitFor({ state: 'hidden', timeout: 10000 })

  const afterReplace = await backendJson('/api/schedule/weeks/2026-09-28')
  const afterShift = afterReplace.shifts.find((shift) => shift.id === assignedShift.id)
  const stillHasOutgoing = afterShift.assigned_employees.some((worker) => worker.employee_code === outgoing.employee_code)
  check(!stillHasOutgoing, 'the replaced worker no longer holds the shift after confirmation')
  check(
    afterShift.assigned_employees.every((worker) => worker.conflicts === null),
    'the resolved shift shows no remaining conflicts after replacement',
  )

  // Back to WEEK_A for the journeys that follow.
  await mainButton(page, 'Previous week').click()
  await page.getByText('September 21, 2026').first().waitFor({ timeout: 5000 })
}

/** uncertain Generate response whose reconciling reload ALSO fails -> Generate
 * stays disabled (never a blind second mutation) -> Reload resolves it and
 * re-enables Generate. Runs on WEEK_B, which has no proposal yet. */
async function journeyUncertainGenerateBlocksRetry(page) {
  await navButton(page, 'Schedule').click()
  await mainButton(page, 'Next week').click()
  await page.getByText('September 28, 2026').first().waitFor({ timeout: 5000 })
  await showScheduleTab(page, 'Proposals')

  let postCount = 0
  await page.route('**/api/schedule/weeks/*/proposals', async (route) => {
    if (route.request().method() === 'POST') {
      postCount += 1
      // The real backend already committed this write - only the
      // DELIVERED body is corrupted, and the reconciling reload below is
      // ALSO made to fail, so the outcome stays genuinely unresolved.
      const response = await route.fetch({ url: toIsolatedUrl(route.request().url()) })
      await route.fulfill({ status: response.status(), body: 'not valid json{{{' })
      return
    }
    await route.abort('failed')
  })

  await mainButton(page, 'Generate Schedule').click()
  await page.getByText(/Generate Schedule is disabled until this is reconciled/).waitFor({ timeout: 10000 })
  check(true, 'an uncertain Generate response whose reconciling reload also fails disables Generate Schedule')
  check(
    await mainButton(page, 'Generate Schedule').isDisabled(),
    'Generate Schedule stays disabled while the outcome is unresolved',
  )

  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.route(`${REAL_BACKEND_ORIGIN}/**`, (route) => {
    route.continue({ url: toIsolatedUrl(route.request().url()) })
  })

  await page
    .locator('p', { hasText: 'Generate Schedule is disabled' })
    .getByRole('button', { name: 'Reload', exact: true })
    .click()
  await page.getByText(/no new proposal is needed|safe to try Generate Schedule again/i).waitFor({ timeout: 10000 })
  check(
    await mainButton(page, 'Generate Schedule').isEnabled(),
    'Generate Schedule re-enables once reconciliation succeeds',
  )
  check(postCount === 1, 'exactly one Generate request was sent despite the uncertain outcome')

  await mainButton(page, 'Previous week').click()
  await page.getByText('September 21, 2026').first().waitFor({ timeout: 5000 })
}

/** uncertain replacement response whose reconciling reload ALSO fails -> Confirm
 * replacement stays disabled (never a blind second mutation) -> Reload
 * detects the replacement actually committed and closes the stale form. */
async function journeyUncertainReplaceBlocksRetry(page) {
  const week = await backendJson('/api/schedule/weeks/2026-09-28')
  const target = week.shifts.find((shift) => shift.assigned_employees.length > 0)
  check(Boolean(target), 'fixture sanity: WEEK_B still has an assignment for the uncertain-replace journey')
  if (!target) return
  const worker = target.assigned_employees[0]
  const coverage = await backendJson(`/api/shifts/${target.id}/coverage?exclude_employee_code=${worker.employee_code}`)
  const incomingCode = coverage.eligible_candidates[0]?.employee_code
  check(Boolean(incomingCode), 'fixture sanity: another worker is eligible for the uncertain-replace journey')
  if (!incomingCode) return

  await navButton(page, 'Schedule').click()
  await mainButton(page, 'Next week').click()
  await page.getByText('September 28, 2026').first().waitFor({ timeout: 5000 })

  const row = page.locator('.schedule-hall li', { hasText: worker.employee_code }).first()
  await row.getByRole('button', { name: 'Replace', exact: true }).click()
  const replacementDialog = page.getByRole('alertdialog', { name: 'Replace assignment' })
  await replacementDialog.waitFor()
  const [replacementBox, replacementViewport] = await Promise.all([
    replacementDialog.boundingBox(),
    page.evaluate(() => ({ width: window.innerWidth, height: window.innerHeight })),
  ])
  check(
    replacementBox !== null &&
      Math.abs(replacementBox.x + replacementBox.width / 2 - replacementViewport.width / 2) < 2 &&
      Math.abs(replacementBox.y + replacementBox.height / 2 - replacementViewport.height / 2) < 2 &&
      await page.locator('.schedule-modal-backdrop').isVisible(),
    'replacement opens as a centered modal with a viewport backdrop rather than an inline panel',
  )
  await page.locator('select').last().selectOption(incomingCode)

  let replaceRequests = 0
  await page.route('**/api/schedule/assignments/replace', async (route) => {
    replaceRequests += 1
    const response = await route.fetch({ url: toIsolatedUrl(route.request().url()) })
    await route.fulfill({ status: response.status(), body: 'not valid json{{{' })
  })
  await page.route('**/api/schedule/weeks/*', async (route) => {
    if (route.request().method() === 'GET') {
      await route.abort('failed')
      return
    }
    await route.fallback()
  })

  await page.getByRole('button', { name: 'Confirm replacement', exact: true }).click()
  await page.getByText(/Confirm replacement is disabled until this is reconciled/).waitFor({ timeout: 10000 })
  check(true, 'an uncertain replacement whose reconciling reload also fails disables Confirm replacement')
  check(
    await page.getByRole('button', { name: 'Confirm replacement', exact: true }).isDisabled(),
    'Confirm replacement stays disabled while the outcome is unresolved',
  )

  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.route(`${REAL_BACKEND_ORIGIN}/**`, (route) => {
    route.continue({ url: toIsolatedUrl(route.request().url()) })
  })

  await page
    .getByRole('alertdialog', { name: 'Replace assignment' })
    .getByRole('button', { name: 'Reload', exact: true })
    .click()
  await page.getByRole('alertdialog', { name: 'Replace assignment' }).waitFor({ state: 'hidden', timeout: 10000 })
  check(true, 'the reconciling Reload closes the stale form once it detects the replacement already committed')
  check(replaceRequests === 1, 'exactly one replace request was sent despite the uncertain outcome')

  const afterReplace = await backendJson('/api/schedule/weeks/2026-09-28')
  const afterShift = afterReplace.shifts.find((shift) => shift.id === target.id)
  check(
    afterShift.assigned_employees.some((w) => w.employee_code === incomingCode) &&
      !afterShift.assigned_employees.some((w) => w.employee_code === worker.employee_code),
    'the reconciled replacement actually took effect on the backend',
  )

  await mainButton(page, 'Previous week').click()
  await page.getByText('September 21, 2026').first().waitFor({ timeout: 5000 })
}

/** cancel delayed replacement loading -> late response -> another action succeeds */
async function journeyCancelDelayedReplacement(page) {
  const week = await backendJson('/api/schedule/weeks/2026-09-21')
  const target = week.shifts.find((shift) => shift.assigned_employees.length > 0)
  check(Boolean(target), 'fixture sanity: a replaceable assignment exists for the cancellation journey')
  if (!target) return
  const worker = target.assigned_employees[0]

  let delayed = false
  await page.route('**/api/shifts/*/coverage*', async (route) => {
    if (!delayed) {
      delayed = true
      await sleep(2000)
    }
    await route.fallback()
  })

  const row = page.locator('.schedule-hall li', { hasText: worker.employee_code }).first()
  await row.getByRole('button', { name: 'Replace', exact: true }).click()
  await page.getByText('Loading eligible workers…').waitFor()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Replace assignment' }).waitFor({ state: 'hidden' })
  check(true, 'cancelling while candidates are still loading closes the panel immediately')

  // Let the delayed response actually land in the background before moving on.
  await sleep(2500)

  await showScheduleTab(page, 'Proposals')
  const generateButton = mainButton(page, 'Generate Schedule')
  check(await generateButton.isEnabled(), 'another action is usable after the late candidate response arrives - no permanent lock')
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.route(`${REAL_BACKEND_ORIGIN}/**`, (route) => {
    route.continue({ url: toIsolatedUrl(route.request().url()) })
  })
}

/** approval/replacement mutual exclusion */
async function journeyMutualExclusion(page) {
  const week = await backendJson('/api/schedule/weeks/2026-09-21')
  const target = week.shifts.find((shift) => shift.assigned_employees.length > 0)
  check(Boolean(target), 'fixture sanity: a replaceable assignment exists for the mutual-exclusion journey')
  if (!target) return
  const worker = target.assigned_employees[0]

  const proposal = await page.evaluate(async ({ backendOrigin, weekStart }) => {
    const response = await fetch(`${backendOrigin}/api/schedule/weeks/${weekStart}/proposals`, { method: 'POST' })
    return response.status
  }, { backendOrigin: BACKEND_ORIGIN, weekStart: '2026-09-21' })
  check(proposal === 201 || proposal === 503 || proposal === 409, `a proposal exists or was just created for the mutual-exclusion journey (${proposal})`)
  await page.reload()
  await navButton(page, 'Schedule').click()

  const row = page.locator('.schedule-hall li', { hasText: worker.employee_code }).first()
  await row.getByRole('button', { name: 'Replace', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Replace assignment' }).waitFor()

  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Replace assignment' }).waitFor({ state: 'hidden' })
  await showScheduleTab(page, 'Proposals')
  const generateButton = mainButton(page, 'Generate Schedule')
  check(await generateButton.isEnabled(), 'Generate Schedule is enabled again once the replacement panel closes')
}

/** committed mutation with unreadable/lost response -> reconciliation without blind replay */
async function journeyCorruptedApprovalResponse(page) {
  const created = await page.evaluate(async ({ backendOrigin, weekStart }) => {
    const response = await fetch(`${backendOrigin}/api/schedule/weeks/${weekStart}/proposals`, { method: 'POST' })
    if (!response.ok) return null
    return response.json()
  }, { backendOrigin: BACKEND_ORIGIN, weekStart: '2026-09-21' })
  check(Boolean(created), 'fixture sanity: a fresh proposal was created for the corrupted-response journey')
  if (!created) return

  await page.reload()
  await navButton(page, 'Schedule').click()
  await showScheduleTab(page, 'Proposals')
  const select = page.locator('select').first()
  if (await select.count()) {
    await select.selectOption(String(created.id))
  }

  let approveRequests = 0
  await page.route('**/api/schedule/proposals/*/approve', async (route) => {
    approveRequests += 1
    const response = await route.fetch({ url: toIsolatedUrl(route.request().url()) })
    // The real backend already committed this write. Only the DELIVERED
    // body is corrupted here - the commit itself is real, not simulated.
    await route.fulfill({ status: response.status(), body: 'not valid json{{{' })
  })

  await page.getByRole('button', { name: 'Approve', exact: true }).click()
  await page.getByRole('button', { name: 'Confirm approval', exact: true }).click()
  await page.getByText(/could not be (read|parsed)|response body/i).first().waitFor({ timeout: 10000 })
  check(true, 'a corrupted-but-committed response is reported honestly, not as a silent failure')

  await page.getByText(/^Proposal #\d+ — approved$/).waitFor({ timeout: 10000 })
  check(true, 'the screen reconciles via authoritative reload and shows the real approved state')

  await sleep(500)
  check(approveRequests === 1, 'no automatic blind retry of the approval request occurred')

  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.route(`${REAL_BACKEND_ORIGIN}/**`, (route) => {
    route.continue({ url: toIsolatedUrl(route.request().url()) })
  })
}

/** change reporting week during a delayed post-edit refresh */
async function journeyWeekChangeDuringDelayedRefresh(page) {
  await navButton(page, 'Employees').click()
  await page.getByRole('button', { name: 'View details for Journey Charlie' }).click()
  await page.getByRole('button', { name: 'Add approved leave', exact: true }).click()
  await page.locator('#leave-start').fill('2026-09-22T09:00')
  await page.locator('#leave-end').fill('2026-09-22T10:00')

  let delayedOnce = false
  await page.route('**/api/employees/SW-203*', async (route) => {
    if (route.request().method() === 'GET' && !delayedOnce) {
      delayedOnce = true
      await sleep(2500)
    }
    await route.fallback()
  })

  await page.getByRole('button', { name: 'Save', exact: true }).click()
  // The success notification for the committed write must still appear,
  // even though the reload it triggers is about to be superseded below.
  await page.getByText(/Added the approved leave|Saved the approved leave/i).first().waitFor({ timeout: 10000 })
  check(true, 'the edit-committed notification appears even though its reload is still in flight')

  // Change the shared week WHILE the delayed post-edit reload for week A is
  // still in flight - this is the exact race the request-identity token
  // guards against.
  await page.getByRole('button', { name: 'Next week', exact: true }).click()
  await page.getByText('2026-09-28').first().waitFor({ timeout: 5000 })

  await sleep(3000) // let the delayed week-A response actually land

  const heading = await page.getByText(/^Timetable, /).first().textContent()
  check(
    heading.includes('2026-09-28') || heading.includes('2026-10-04'),
    `the details view shows week B's dates, not overwritten by the late week-A response (saw: "${heading}")`,
  )

  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.route(`${REAL_BACKEND_ORIGIN}/**`, (route) => {
    route.continue({ url: toIsolatedUrl(route.request().url()) })
  })
}

/** Phase 8: switching weeks visibly changes dashboard metrics; Workforce
 * Planning's scenario calculator reacts to explicit supervisor inputs and
 * shows the required aggregate-lower-bound feasibility warning; browsing to
 * a never-prepared week never prepares it. Entering this journey, the
 * shared reporting week is WEEK_B (2026-09-28), left behind by the previous
 * journey. */
async function journeyDashboardAndWorkforcePlanning(page) {
  function metricValue(label) {
    return page.locator('.metric-card', { hasText: label }).locator('.metric-value')
  }

  // Dashboard does not remount on a week change (see App.jsx) - it re-fetches
  // in place, so the OLD card is already attached and visible the instant
  // after clicking a week-navigation button. Waiting only for the locator to
  // exist would read the stale pre-fetch value; this polls until the text
  // actually changes (or times out), the same race `waitFor` alone cannot
  // catch.
  async function waitForValueChange(locator, previousText, timeoutMs) {
    const deadline = Date.now() + timeoutMs
    let current = previousText
    while (Date.now() < deadline) {
      current = await locator.innerText()
      if (current !== previousText) {
        return current
      }
      await sleep(100)
    }
    return current
  }

  await navButton(page, 'Dashboard').click()
  await metricValue('Filled positions').waitFor({ timeout: 10000 })
  const weekBFilled = await metricValue('Filled positions').innerText()

  await mainButton(page, 'Previous week').click()
  await page.getByText('September 21, 2026').first().waitFor({ timeout: 5000 })
  const weekAFilled = await waitForValueChange(metricValue('Filled positions'), weekBFilled, 10000)

  check(
    weekAFilled !== weekBFilled,
    `dashboard metrics visibly change between reporting weeks (week A filled=${weekAFilled}, week B filled=${weekBFilled})`,
  )

  // --------------------------------------------------- Workforce Planning
  await navButton(page, 'Workforce Planning').click()
  await page.getByText(/Required coverage hours for this week:\s*\d+/).waitFor({ timeout: 10000 })
  check(true, 'Workforce Planning shows the current week\'s required coverage hours')

  const workerCountInput = page.locator('label', { hasText: 'Hypothetical worker count' }).locator('input')
  const hoursInput = page.locator('label', { hasText: 'Weekly hours per hypothetical worker' }).locator('input')

  await workerCountInput.fill('1')
  await hoursInput.fill('5')
  await page.getByText('Capacity shortfall').waitFor({ timeout: 10000 })
  check(
    (await metricValue('Aggregate capacity sufficient?').innerText()) === 'No',
    'a deliberately insufficient hypothetical workforce reports capacity as NOT sufficient',
  )
  check(
    await page.getByText(/aggregate lower bound/i).isVisible(),
    'the required feasibility warning (aggregate lower bound, not a proof) is visible',
  )

  await workerCountInput.fill('80')
  await hoursInput.fill('20')
  await page.getByText('Capacity surplus').waitFor({ timeout: 10000 })
  check(
    (await metricValue('Aggregate capacity sufficient?').innerText()) === 'Yes',
    'a large hypothetical workforce reports capacity as sufficient, with a surplus rather than a shortfall',
  )

  // ----------------------------------------- browsing never prepares a week
  await navButton(page, 'Dashboard').click()
  await mainButton(page, 'Next week').click() // back to WEEK_B
  await mainButton(page, 'Next week').click() // WEEK_C, 2026-10-05 - never prepared by any fixture or journey
  await page.getByText('October 5, 2026').first().waitFor({ timeout: 5000 })
  await page.getByText(/No shifts are prepared for this week yet/).waitFor({ timeout: 10000 })
  check(true, 'an unprepared week is shown honestly on the dashboard - zero stored shifts, not an error')

  const weekC = await backendJson('/api/schedule/weeks/2026-10-05')
  check(weekC.shifts.length === 0, 'browsing the dashboard for a never-prepared week never prepares it')

  // Back to WEEK_A, in case anything runs after this journey.
  await mainButton(page, 'Previous week').click()
  await mainButton(page, 'Previous week').click()
  await page.getByText('September 21, 2026').first().waitFor({ timeout: 5000 })
}

/** Run 2: Employees keeps its actions in view, gives the list its own
 * vertical scroller and switches to cards at the narrower laptop width. */
async function journeyEmployeesResponsiveLayout(page) {
  await navButton(page, 'Employees').click()
  await page.locator('.employee-table-wrapper').waitFor({ timeout: 10000 })

  for (const width of [1366, 1024]) {
    await page.setViewportSize({ width, height: 768 })
    const layout = await page.evaluate(() => {
      const root = document.documentElement
      const wrapper = document.querySelector('.employee-table-wrapper')
      const action = document.querySelector('.employee-row-actions button')
      const header = document.querySelector('.employee-table th')
      const actionRect = action.getBoundingClientRect()
      return {
        noPageOverflow: root.scrollWidth <= root.clientWidth,
        ownVerticalScroll: wrapper.scrollHeight > wrapper.clientHeight,
        actionInViewport: actionRect.left >= 0 && actionRect.right <= root.clientWidth,
        headerPosition: getComputedStyle(header).position,
        rowDisplay: getComputedStyle(document.querySelector('.employee-table tbody tr')).display,
      }
    })
    check(layout.noPageOverflow, `Employees has no page-level horizontal scrolling at ${width}x768`)
    check(layout.ownVerticalScroll, `Employees uses its own vertical list scroller at ${width}x768`)
    check(layout.actionInViewport, `employee actions are visible at ${width}x768`)
    if (width === 1366) {
      check(layout.headerPosition === 'sticky', 'the compact Employees table header is sticky at 1366x768')
    } else {
      check(layout.rowDisplay === 'grid', 'Employees uses the responsive card layout at 1024x768')
    }
  }

  const employees = await backendJson('/api/employees?week_start=2026-09-21')
  const target = employees.employees[0]
  await page.getByLabel('Search').fill(target.employee_code)
  check((await page.locator('.employee-table tbody tr').count()) === 1, 'employee search still filters the responsive list')
  await page.getByLabel('Search').fill('')
  await page.getByLabel('Status').selectOption('all')
  await page.getByLabel('Sort by').selectOption('full_name')
  await page.getByLabel('Sort direction').selectOption('desc')
  check((await page.locator('.employee-table tbody tr').count()) > 1, 'employee status and sort controls still operate after the redesign')

  await page.setViewportSize({ width: 1280, height: 720 })
}

// --------------------------------------------------------- AI Assistant (Phase 9)
//
// Every scenario's shift is dated in 2027 and only its own dedicated
// worker(s) have a schedule covering that date at all (see
// backend/e2e_agent_fixtures.py) - each journey's eligible-candidate set is
// exactly and only what that scenario constructs, deterministically. The
// fake model (also in e2e_agent_fixtures.py) is a rule-based router over
// the supervisor's own chat text, never a real network call and never
// `OPENAI_API_KEY`.

/** ordinary informational question, and: model text saying "approved"
 * cannot execute anything - the assistant's own final reply uses that word
 * as plain prose, with no proposal ever created and no request ever
 * reaching an approval endpoint. */
async function journeyAgentInfoAndApprovedTrap(page) {
  await navButton(page, 'AI Assistant').click()
  await startNewAgentTask(page)

  check(
    await page.getByRole('button', { name: 'Find a call-out replacement' }).isVisible(),
    'the empty AI Assistant offers starting prompts',
  )

  let approveRequests = 0
  const listener = (request) => {
    if (request.url().includes('/approve')) approveRequests += 1
  }
  page.on('request', listener)

  await sendChat(page, 'How many hours has Taylor Brooks worked this week, and is everything approved?')
  await page.getByText(/approved as originally scheduled/i).waitFor({ timeout: 15000 })
  check(true, 'an ordinary informational question gets a plain-text answer')
  const transcript = page.locator('.agent-transcript')
  const latestMessage = transcript.locator('.agent-message').last()
  await latestMessage.waitFor()
  const transcriptMetrics = await transcript.evaluate((element) => ({
    remainingBelow: element.scrollHeight - element.scrollTop - element.clientHeight,
  }))
  check(transcriptMetrics.remainingBelow < 2, 'a newly received assistant message auto-scrolls into view when the transcript is at its latest position')
  const [latestBox, composerBox] = await Promise.all([latestMessage.boundingBox(), page.locator('.agent-composer').boundingBox()])
  check(
    latestBox !== null && composerBox !== null && latestBox.y + latestBox.height <= composerBox.y,
    'the composer is laid out after the transcript and never covers the newest message',
  )
  check(
    (await page.locator('.agent-proposal-card').count()) === 0,
    'no proposal card appears for a plain informational answer',
  )

  await sleep(300)
  page.off('request', listener)
  check(approveRequests === 0, 'the model\'s own text saying "approved" never triggers a real approval request')
}

/** ambiguity followed by supervisor clarification */
async function journeyAgentAmbiguousClarification(page) {
  await navButton(page, 'AI Assistant').click()
  await startNewAgentTask(page)

  await sendChat(page, 'Can you check on Blaine for me?')
  await page.getByText(/which one did you mean/i).waitFor({ timeout: 15000 })
  check(true, 'an ambiguous name produces a clarification request, not a guess')

  await sendChat(page, 'I meant Blaine Sato')
  await page.getByText(/Found Blaine Sato/i).waitFor({ timeout: 15000 })
  check(true, "the supervisor's clarification reply resolves the ambiguity")
}

/** no eligible candidates */
async function journeyAgentNoCandidates(page) {
  await navButton(page, 'AI Assistant').click()
  await startNewAgentTask(page)

  await sendChat(page, 'Morgan Reyes called out for the Helix shift - can anyone cover it?')
  await page.getByText(/Nobody is currently eligible/i).waitFor({ timeout: 15000 })
  check(true, 'a call-out with zero eligible candidates is reported as blocked, not a fabricated candidate')
  check(
    (await page.locator('.agent-proposal-card').count()) === 0,
    'no proposal card appears when there are no eligible candidates',
  )
}

/** call-out investigation -> exactly one pending replacement proposal ->
 * refresh/navigation recovery -> explicit approval confirmation -> applied
 * and verified. */
async function journeyAgentCalloutApproveVerify(page) {
  await navButton(page, 'AI Assistant').click()
  await startNewAgentTask(page)

  await sendChat(page, "Jordan Rivera called out for tonight's Capella shift. Find a replacement.")
  await page.locator('.agent-proposal-card').waitFor({ timeout: 15000 })
  check(true, 'the call-out investigation produces exactly one pending replacement proposal')
  check((await page.locator('.agent-proposal-card').count()) === 1, 'exactly one proposal card is shown')
  check(await page.getByText('Sam Osei').first().isVisible(), 'the proposal card names the proposed incoming worker')
  check(
    await page
      .getByText('No assignment changes are made until this proposal is explicitly approved.')
      .isVisible(),
    'the card states plainly that nothing has changed yet',
  )
  check(
    (await page.locator('.agent-message-tool-call, .agent-message-tool-result').count()) === 0,
    'tool calls and results are hidden from the normal AI transcript',
  )
  const taskIdWithTools = await page.evaluate(() => Number(window.localStorage.getItem('shiftops.agent.activeTaskId')))
  const persistedTask = await backendJson(`/api/agent/tasks/${taskIdWithTools}`)
  check(
    persistedTask.messages.some((message) => message.role === 'tool_call') &&
      persistedTask.messages.some((message) => message.role === 'tool_result'),
    'the hidden tool calls and results remain persisted in the backend task',
  )

  await page.reload()
  await navButton(page, 'AI Assistant').click()
  await page.locator('.agent-proposal-card').waitFor({ timeout: 15000 })
  check(true, 'refreshing the page recovers the active task and its pending proposal from the backend')

  await page.getByRole('button', { name: 'Approve', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Confirm approval' }).waitFor()
  check(true, 'clicking Approve opens an explicit confirmation panel, not an immediate write')
  await page.getByRole('button', { name: 'Confirm approval', exact: true }).click()
  await page.getByText('Approved, applied, and verified.').waitFor({ timeout: 15000 })
  check(true, 'confirming approval applies the assignment and reports it verified')
  await page.getByText('Sam Osei (SW-705)').first().waitFor({ timeout: 5000 })
  check(true, 'the verified-success readback lists the current occupant')
}

/** Recover a previously approved and verified task after a SECOND
 * refresh/navigation (distinct from journeyAgentCalloutApproveVerify's own
 * pre-approval recovery check above) and confirm its current worker
 * readback is shown again - via the new read-only
 * GET /api/agent/proposals/{id} route, never by re-invoking approval. */
async function journeyAgentRecoverVerifiedReadback(page) {
  await page.reload()
  await navButton(page, 'AI Assistant').click()
  await page.getByText('Approved, applied, and verified.').waitFor({ timeout: 15000 })
  check(true, 'refreshing after approval recovers the already-decided, verified proposal')
  await page.getByText('Sam Osei (SW-705)').first().waitFor({ timeout: 5000 })
  check(true, "the verified proposal's current worker readback is shown again after refresh, via the read-only GET route")
}

/** rejecting a proposal changes no assignment */
async function journeyAgentReject(page) {
  await navButton(page, 'AI Assistant').click()
  await startNewAgentTask(page)

  await sendChat(page, 'Devon Cole called out for the Vega shift. Find a replacement.')
  await page.locator('.agent-proposal-card').waitFor({ timeout: 15000 })

  await page.getByRole('button', { name: 'Reject', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Confirm rejection' }).waitFor()
  await page.getByRole('button', { name: 'Confirm rejection', exact: true }).click()
  await page.getByText('Rejected. No assignment was changed. This task is closed.').waitFor({ timeout: 15000 })
  check(true, 'confirming rejection changes no assignment and closes the task')

  const taskId = await page.evaluate(() => Number(window.localStorage.getItem('shiftops.agent.activeTaskId')))
  const task = await backendJson(`/api/agent/tasks/${taskId}`)
  check(
    task.status === 'closed' && task.proposals[0].status === 'rejected',
    'the backend itself confirms the rejection - the closed status is not merely a client-side label',
  )
}

/** stale approval displays structured conflicts - a real change to live
 * state (deactivating the proposed incoming worker) between proposal
 * creation and the approval confirmation click, not a mocked response. */
async function journeyAgentStaleConflict(page) {
  await navButton(page, 'AI Assistant').click()
  await startNewAgentTask(page)

  await sendChat(page, 'Riley Chen called out for the Sirius shift. Find a replacement.')
  await page.locator('.agent-proposal-card').waitFor({ timeout: 15000 })

  await page.evaluate(async (backendOrigin) => {
    await fetch(`${backendOrigin}/api/employees/SW-710/deactivate`, { method: 'POST' })
  }, BACKEND_ORIGIN)

  await page.getByRole('button', { name: 'Approve', exact: true }).click()
  await page.getByRole('button', { name: 'Confirm approval', exact: true }).click()
  await page.getByText(/stale and could not be approved/i).waitFor({ timeout: 15000 })
  check(true, 'a stale approval displays the structured revalidation conflicts')
  check(
    await page.getByText(/not active/i).isVisible(),
    'the real, specific stale-conflict reason is shown, not a single generic string',
  )
  check(
    await page.locator('.agent-proposal-card').isVisible(),
    'the stored proposal remains visible after a stale-approval refusal',
  )
}

/** duplicate clicks cannot send duplicate approval requests */
async function journeyAgentDuplicateClicks(page) {
  await navButton(page, 'AI Assistant').click()
  await startNewAgentTask(page)

  await sendChat(page, 'Jamie Fox called out for the Andromeda shift. Find a replacement.')
  await page.locator('.agent-proposal-card').waitFor({ timeout: 15000 })

  let approveRequests = 0
  await page.route('**/api/agent/proposals/*/approve', async (route) => {
    approveRequests += 1
    await route.continue({ url: toIsolatedUrl(route.request().url()) })
  })

  await page.getByRole('button', { name: 'Approve', exact: true }).click()
  const confirmButton = page.getByRole('button', { name: 'Confirm approval', exact: true })
  await confirmButton.waitFor()
  await confirmButton.click()
  // A second click while the button is already disabled (decisionStatus !==
  // 'idle') - Playwright's actionability check means this either never
  // dispatches at all, or the disabled DOM element itself swallows it; both
  // are the desired outcome and neither should throw the whole journey.
  await confirmButton.click({ timeout: 1000 }).catch(() => {})

  await page.getByText('Approved, applied, and verified.').waitFor({ timeout: 15000 })
  await sleep(300)
  check(
    approveRequests === 1,
    `duplicate clicks on Confirm approval never send more than one approval request (sent ${approveRequests})`,
  )

  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.route(`${REAL_BACKEND_ORIGIN}/**`, (route) => {
    route.continue({ url: toIsolatedUrl(route.request().url()) })
  })
}

/** network-unknown approval outcome reconciles through GET without a blind
 * retry - the real backend already committed the write; only the
 * DELIVERED response body is corrupted here (the same technique
 * journeyCorruptedApprovalResponse already uses for Phase 7 approval). */
async function journeyAgentNetworkUnknownReconcile(page) {
  await navButton(page, 'AI Assistant').click()
  await startNewAgentTask(page)

  await sendChat(page, "Quinn Adams called out for tonight's Capella shift. Find a replacement.")
  await page.locator('.agent-proposal-card').waitFor({ timeout: 15000 })

  let approveRequests = 0
  await page.route('**/api/agent/proposals/*/approve', async (route) => {
    approveRequests += 1
    const response = await route.fetch({ url: toIsolatedUrl(route.request().url()) })
    await route.fulfill({ status: response.status(), body: 'not valid json{{{' })
  })

  await page.getByRole('button', { name: 'Approve', exact: true }).click()
  await page.getByRole('button', { name: 'Confirm approval', exact: true }).click()
  await page.getByText(/could not be (read|parsed)|response body/i).first().waitFor({ timeout: 10000 })
  check(true, 'an unreadable-but-committed approval response is reported honestly, not as a silent failure')

  await page.getByRole('button', { name: 'Reload / Reconcile', exact: true }).click()
  await page.getByText('Approved, applied, and verified.').waitFor({ timeout: 15000 })
  check(true, 'Reload/Reconcile recovers the real, already-applied and verified state via GET, without a blind retry')

  await sleep(300)
  check(approveRequests === 1, 'exactly one approval request was sent despite the uncertain outcome')

  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.route(`${REAL_BACKEND_ORIGIN}/**`, (route) => {
    route.continue({ url: toIsolatedUrl(route.request().url()) })
  })
}

/** assignment applied but verification failed is displayed accurately. The
 * real backend commits and genuinely verifies this write - a real backend
 * cannot be made to fail its own verification on demand without breaking
 * it, so only the DELIVERED response body is altered here, exactly the
 * same "commit is real, only the delivered body is corrupted" technique
 * the other mocked journeys in this file already use. */
async function journeyAgentVerificationFailedDisplay(page) {
  await navButton(page, 'AI Assistant').click()
  await startNewAgentTask(page)

  await sendChat(page, 'Skyler Moss called out for the Vega shift. Find a replacement.')
  await page.locator('.agent-proposal-card').waitFor({ timeout: 15000 })

  await page.route('**/api/agent/proposals/*/approve', async (route) => {
    const response = await route.fetch({ url: toIsolatedUrl(route.request().url()) })
    const body = await response.json()
    body.proposal.verification_outcome = 'verification_failed: simulated for e2e'
    body.task_status = 'blocked'
    // A recorded verification_failed outcome has no confirmed current state
    // to show - agent.js's decision-response validator now enforces a null
    // readback for exactly this outcome, so the mocked body must be
    // internally consistent with that rule too, not just with the outcome
    // string alone.
    body.readback = null
    await route.fulfill({
      status: response.status(),
      body: JSON.stringify(body),
      headers: { 'content-type': 'application/json' },
    })
  })

  await page.getByRole('button', { name: 'Approve', exact: true }).click()
  await page.getByRole('button', { name: 'Confirm approval', exact: true }).click()
  await page.getByText(/could not be verified afterward/i).waitFor({ timeout: 15000 })
  check(true, 'an applied-but-verification-failed outcome is displayed accurately, distinct from a failed write')
  check(
    (await page.getByText('Approved, applied, and verified.').count()) === 0,
    'a verification failure is never shown as a verified success',
  )

  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.route(`${REAL_BACKEND_ORIGIN}/**`, (route) => {
    route.continue({ url: toIsolatedUrl(route.request().url()) })
  })
}

/** Present an applied-but-unverified proposal - the real backend commits
 * AND genuinely verifies on this real first approval; only that FIRST
 * response's delivered body is altered (never the second) to display the
 * recovery-gap state a real backend cannot be made to produce on its own
 * without deliberately corrupting a second database write mid-flight.
 * Clicking Reconcile then issues a genuine SECOND approval request, which
 * the real backend answers through its own idempotent branch (D053's
 * addendum: the resubmitted action still exactly matches what is really
 * stored, and the real, already-'verified' outcome is simply read back -
 * never rerun, never repeating the assignment or writing a duplicate audit
 * entry, exactly as `verify_agent_decision.py` already proves at the
 * backend level). */
async function journeyAgentAppliedUnverifiedReconcile(page) {
  await navButton(page, 'AI Assistant').click()
  await startNewAgentTask(page)

  await sendChat(page, 'Reagan Wells called out for the Helix shift. Find a replacement.')
  await page.locator('.agent-proposal-card').waitFor({ timeout: 15000 })

  let approveRequests = 0
  await page.route('**/api/agent/proposals/*/approve', async (route) => {
    approveRequests += 1
    const response = await route.fetch({ url: toIsolatedUrl(route.request().url()) })
    if (approveRequests === 1) {
      const body = await response.json()
      body.proposal.verification_outcome = null
      body.proposal.verified_at = null
      body.task_status = 'awaiting_approval'
      // Applied-but-unverified has no confirmed current state either -
      // must stay internally consistent with agent.js's own validation rule.
      body.readback = null
      await route.fulfill({
        status: response.status(),
        body: JSON.stringify(body),
        headers: { 'content-type': 'application/json' },
      })
      return
    }
    // Reconcile's own request: passed through completely unmodified.
    await route.fulfill({
      status: response.status(),
      body: await response.text(),
      headers: { 'content-type': 'application/json' },
    })
  })

  await page.getByRole('button', { name: 'Approve', exact: true }).click()
  await page.getByRole('button', { name: 'Confirm approval', exact: true }).click()
  await page.getByText(/verification never completed/i).waitFor({ timeout: 15000 })
  check(true, 'an applied-but-unverified proposal is displayed distinctly, offering Reconcile rather than a blind retry')

  await page.getByRole('button', { name: 'Reconcile', exact: true }).click()
  await page.getByText('Approved, applied, and verified.').waitFor({ timeout: 15000 })
  check(true, 'clicking Reconcile completes verification and shows the real, already-applied assignment as verified')

  await sleep(300)
  check(
    approveRequests === 2,
    `Reconcile sends exactly one further approval request (2 total: the original approval plus the reconcile; sent ${approveRequests})`,
  )

  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.route(`${REAL_BACKEND_ORIGIN}/**`, (route) => {
    route.continue({ url: toIsolatedUrl(route.request().url()) })
  })
}

/** missing backend AI configuration is explained without exposing secrets.
 * Mocked at the route level (the isolated backend genuinely has no
 * OPENAI_API_KEY either, but asserting the FRONTEND's handling of the
 * real 503 shape does not require actually removing server configuration
 * mid-run) - the response body is the real backend's own exact message
 * text (see backend/ai_config.py), not an invented one. */
async function journeyAgentMissingConfiguration(page) {
  await navButton(page, 'AI Assistant').click()
  await startNewAgentTask(page)

  await page.route('**/api/agent/tasks', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.fallback()
      return
    }
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({
        detail:
          'AI is not configured: the OPENAI_API_KEY environment variable is not set. Set it (for ' +
          'example in a local, gitignored .env file) before sending a message to the scheduling agent.',
      }),
    })
  })

  await sendChat(page, 'Anything at all - this call will be refused by configuration.')
  await page.getByText(/OPENAI_API_KEY environment variable is not set/).waitFor({ timeout: 10000 })
  check(true, 'a missing backend AI configuration is explained clearly, naming the environment variable to set')
  check(
    !(await page.locator('body').innerText()).includes('sk-'),
    'no API key value is ever displayed, only the fact that one is missing',
  )

  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.route(`${REAL_BACKEND_ORIGIN}/**`, (route) => {
    route.continue({ url: toIsolatedUrl(route.request().url()) })
  })
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
