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
  const pythonExe = existsSync(venvPython) ? venvPython : 'python'
  return spawn(pythonExe, args, { cwd: BACKEND_ROOT, stdio: ['ignore', 'pipe', 'pipe'] })
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
    await mainButton(page, 'Generate Schedule').waitFor({ timeout: 10000 })
    check(
      await mainButton(page, 'Generate Schedule').isVisible(),
      'the Generate Schedule action remains accessible inside the single Schedule screen',
    )
    if (process.env.E2E_DEBUG) {
      await sleep(1000)
      console.log('[debug] body text:', (await page.locator('body').innerText()).slice(0, 800))
    }

    await journeyGenerateReviewApprove(page)
    await journeyConflictAndReplace(page)
    await journeyUncertainGenerateBlocksRetry(page)
    await journeyUncertainReplaceBlocksRetry(page)
    await journeyCancelDelayedReplacement(page)
    await journeyMutualExclusion(page)
    await journeyCorruptedApprovalResponse(page)
    await journeyWeekChangeDuringDelayedRefresh(page)
    await journeyDashboardAndWorkforcePlanning(page)

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

/** generate -> review -> cancel approval -> approve -> reload/recover */
async function journeyGenerateReviewApprove(page) {
  await mainButton(page, 'Generate Schedule').click()
  await page.getByText(/^Proposal #\d+ — pending$/).waitFor({ timeout: 15000 })
  check(true, 'Generate Schedule produced a pending proposal')

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
  await page.getByText(/^Proposal #\d+ — approved$/).waitFor({ timeout: 15000 })
  check(true, 'reloading the page recovers the approved proposal from the backend, not from lost client state')
}

/** later class/leave edit -> visible assignment conflict -> replacement -> verified resolution */
async function journeyConflictAndReplace(page) {
  // Deliberately on WEEK_B (2026-10-12), not the week Journey A generated
  // and approved a full schedule on: maximizing coverage there naturally
  // pushes every seeded worker toward their weekly hour cap, leaving no
  // genuine replacement candidate anywhere. WEEK_B carries exactly one
  // hand-seeded assignment (SW-201, see e2e_fixtures.py) with every other
  // worker completely free - a real candidate is guaranteed by construction.
  const weekB = await backendJson('/api/schedule/weeks/2026-10-12')
  const assignedShift = weekB.shifts.find((shift) => shift.assigned_employees.length > 0)
  check(Boolean(assignedShift), 'fixture sanity: WEEK_B has the one hand-seeded assignment')
  if (!assignedShift) return
  const outgoing = assignedShift.assigned_employees[0]
  const coverage = await backendJson(`/api/shifts/${assignedShift.id}/coverage?exclude_employee_code=${outgoing.employee_code}`)
  const expectedCandidateCode = coverage.eligible_candidates[0]?.employee_code
  check(Boolean(expectedCandidateCode), 'fixture sanity: another worker is eligible to replace the seeded assignment')
  if (!expectedCandidateCode) return

  await navButton(page, 'Schedule').click()
  await mainButton(page, 'Next week →').click()
  await page.getByText('October 12, 2026').first().waitFor({ timeout: 5000 })

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
  const conflictText = page.getByText(/^Conflict: /).first()
  await conflictText.waitFor({ timeout: 10000 })
  check(await conflictText.isVisible(), 'the leave edit surfaces a visible conflict on the affected assignment')

  const conflictRow = page.locator('.schedule-hall li', { hasText: outgoing.employee_code }).filter({ hasText: 'Conflict:' })
  await conflictRow.getByRole('button', { name: 'Replace', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Replace assignment' }).waitFor()
  await page.locator('select').last().selectOption(expectedCandidateCode)
  await page.getByRole('button', { name: 'Confirm replacement', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Replace assignment' }).waitFor({ state: 'hidden', timeout: 10000 })

  const afterReplace = await backendJson('/api/schedule/weeks/2026-10-12')
  const afterShift = afterReplace.shifts.find((shift) => shift.id === assignedShift.id)
  const stillHasOutgoing = afterShift.assigned_employees.some((worker) => worker.employee_code === outgoing.employee_code)
  check(!stillHasOutgoing, 'the replaced worker no longer holds the shift after confirmation')
  check(
    afterShift.assigned_employees.every((worker) => worker.conflicts === null),
    'the resolved shift shows no remaining conflicts after replacement',
  )

  // Back to WEEK_A for the journeys that follow.
  await mainButton(page, '← Previous week').click()
  await page.getByText('October 5, 2026').first().waitFor({ timeout: 5000 })
}

/** uncertain Generate response whose reconciling reload ALSO fails -> Generate
 * stays disabled (never a blind second mutation) -> Reload resolves it and
 * re-enables Generate. Runs on WEEK_B, which has no proposal yet. */
async function journeyUncertainGenerateBlocksRetry(page) {
  await navButton(page, 'Schedule').click()
  await mainButton(page, 'Next week →').click()
  await page.getByText('October 12, 2026').first().waitFor({ timeout: 5000 })

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

  await mainButton(page, '← Previous week').click()
  await page.getByText('October 5, 2026').first().waitFor({ timeout: 5000 })
}

/** uncertain replacement response whose reconciling reload ALSO fails -> Confirm
 * replacement stays disabled (never a blind second mutation) -> Reload
 * detects the replacement actually committed and closes the stale form. */
async function journeyUncertainReplaceBlocksRetry(page) {
  const week = await backendJson('/api/schedule/weeks/2026-10-12')
  const target = week.shifts.find((shift) => shift.assigned_employees.length > 0)
  check(Boolean(target), 'fixture sanity: WEEK_B still has an assignment for the uncertain-replace journey')
  if (!target) return
  const worker = target.assigned_employees[0]
  const coverage = await backendJson(`/api/shifts/${target.id}/coverage?exclude_employee_code=${worker.employee_code}`)
  const incomingCode = coverage.eligible_candidates[0]?.employee_code
  check(Boolean(incomingCode), 'fixture sanity: another worker is eligible for the uncertain-replace journey')
  if (!incomingCode) return

  await navButton(page, 'Schedule').click()
  await mainButton(page, 'Next week →').click()
  await page.getByText('October 12, 2026').first().waitFor({ timeout: 5000 })

  const row = page.locator('.schedule-hall li', { hasText: worker.employee_code }).first()
  await row.getByRole('button', { name: 'Replace', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Replace assignment' }).waitFor()
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

  const afterReplace = await backendJson('/api/schedule/weeks/2026-10-12')
  const afterShift = afterReplace.shifts.find((shift) => shift.id === target.id)
  check(
    afterShift.assigned_employees.some((w) => w.employee_code === incomingCode) &&
      !afterShift.assigned_employees.some((w) => w.employee_code === worker.employee_code),
    'the reconciled replacement actually took effect on the backend',
  )

  await mainButton(page, '← Previous week').click()
  await page.getByText('October 5, 2026').first().waitFor({ timeout: 5000 })
}

/** cancel delayed replacement loading -> late response -> another action succeeds */
async function journeyCancelDelayedReplacement(page) {
  const week = await backendJson('/api/schedule/weeks/2026-10-05')
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

  const generateButton = mainButton(page, 'Generate Schedule')
  check(await generateButton.isEnabled(), 'another action is usable after the late candidate response arrives - no permanent lock')
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.route(`${REAL_BACKEND_ORIGIN}/**`, (route) => {
    route.continue({ url: toIsolatedUrl(route.request().url()) })
  })
}

/** approval/replacement mutual exclusion */
async function journeyMutualExclusion(page) {
  const week = await backendJson('/api/schedule/weeks/2026-10-05')
  const target = week.shifts.find((shift) => shift.assigned_employees.length > 0)
  check(Boolean(target), 'fixture sanity: a replaceable assignment exists for the mutual-exclusion journey')
  if (!target) return
  const worker = target.assigned_employees[0]

  const proposal = await page.evaluate(async ({ backendOrigin, weekStart }) => {
    const response = await fetch(`${backendOrigin}/api/schedule/weeks/${weekStart}/proposals`, { method: 'POST' })
    return response.status
  }, { backendOrigin: BACKEND_ORIGIN, weekStart: '2026-10-05' })
  check(proposal === 201 || proposal === 503 || proposal === 409, `a proposal exists or was just created for the mutual-exclusion journey (${proposal})`)
  await page.reload()
  await navButton(page, 'Schedule').click()

  const row = page.locator('.schedule-hall li', { hasText: worker.employee_code }).first()
  await row.getByRole('button', { name: 'Replace', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Replace assignment' }).waitFor()

  const generateButton = mainButton(page, 'Generate Schedule')
  check(await generateButton.isDisabled(), 'Generate Schedule is disabled while the replacement panel is open')
  const approveButtons = page.getByRole('button', { name: 'Approve', exact: true })
  if (await approveButtons.count()) {
    check(await approveButtons.first().isDisabled(), 'Approve is disabled while the replacement panel is open')
  }

  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await page.getByRole('alertdialog', { name: 'Replace assignment' }).waitFor({ state: 'hidden' })
  check(await generateButton.isEnabled(), 'Generate Schedule is enabled again once the replacement panel closes')
}

/** committed mutation with unreadable/lost response -> reconciliation without blind replay */
async function journeyCorruptedApprovalResponse(page) {
  const created = await page.evaluate(async ({ backendOrigin, weekStart }) => {
    const response = await fetch(`${backendOrigin}/api/schedule/weeks/${weekStart}/proposals`, { method: 'POST' })
    if (!response.ok) return null
    return response.json()
  }, { backendOrigin: BACKEND_ORIGIN, weekStart: '2026-10-05' })
  check(Boolean(created), 'fixture sanity: a fresh proposal was created for the corrupted-response journey')
  if (!created) return

  await page.reload()
  await navButton(page, 'Schedule').click()
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
  await page.locator('#leave-start').fill('2026-10-06T09:00')
  await page.locator('#leave-end').fill('2026-10-06T10:00')

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
  await page.getByRole('button', { name: 'Next week →', exact: true }).click()
  await page.getByText('2026-10-12').first().waitFor({ timeout: 5000 })

  await sleep(3000) // let the delayed week-A response actually land

  const heading = await page.getByText(/^Timetable, /).first().textContent()
  check(
    heading.includes('2026-10-12') || heading.includes('2026-10-18'),
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
 * shared reporting week is WEEK_B (2026-10-12), left behind by the previous
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

  await mainButton(page, '← Previous week').click()
  await page.getByText('October 5, 2026').first().waitFor({ timeout: 5000 })
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
  await mainButton(page, 'Next week →').click() // back to WEEK_B
  await mainButton(page, 'Next week →').click() // WEEK_C, 2026-10-19 - never prepared by any fixture or journey
  await page.getByText('October 19, 2026').first().waitFor({ timeout: 5000 })
  await page.getByText(/No shifts are prepared for this week yet/).waitFor({ timeout: 10000 })
  check(true, 'an unprepared week is shown honestly on the dashboard - zero stored shifts, not an error')

  const weekC = await backendJson('/api/schedule/weeks/2026-10-19')
  check(weekC.shifts.length === 0, 'browsing the dashboard for a never-prepared week never prepares it')

  // Back to WEEK_A, in case anything runs after this journey.
  await mainButton(page, '← Previous week').click()
  await mainButton(page, '← Previous week').click()
  await page.getByText('October 5, 2026').first().waitFor({ timeout: 5000 })
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
