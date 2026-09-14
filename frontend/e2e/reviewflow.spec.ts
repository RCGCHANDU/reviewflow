import { expect, test, type Page } from '@playwright/test'
import fs from 'node:fs/promises'
import path from 'node:path'

const BASE_URL = 'http://127.0.0.1:8012'
const SCREENSHOTS = path.join('e2e', 'screenshots')
const RESULTS = path.join('e2e', 'results')

let projectId = ''
let sourceId = ''
let successRunId = ''
let degradedRunId = ''

function screenshot(name: string): string {
  return path.join(SCREENSHOTS, `${name}.png`)
}

async function bodyText(page: Page): Promise<string> {
  return page.locator('body').innerText()
}

async function terminalStatus(page: Page, runId: string, expected?: string): Promise<string> {
  let observed = ''
  await expect
    .poll(
      async () => {
        const response = await page.request.get(`${BASE_URL}/api/v1/workflows/${runId}`)
        if (!response.ok()) return `http-${response.status()}`
        const payload = await response.json()
        observed = payload.run.status
        return observed
      },
      { timeout: 35_000, intervals: [100, 250, 500, 1_000] },
    )
    .toMatch(/^(succeeded|degraded|blocked|failed|cancelled)$/)
  if (expected) expect(observed).toBe(expected)
  return observed
}

async function createScenarioRun(
  page: Page,
  scenario: string,
  maxRevisionRounds?: number,
): Promise<{ runId: string; sourceId: string }> {
  if (!projectId) throw new Error('project must be created before scripted runs')

  const sampleResponse = await page.request.post(
    `${BASE_URL}/api/v1/projects/${projectId}/samples?sample_id=scenario:${scenario}`,
  )
  expect(sampleResponse.status()).toBe(201)
  const source = await sampleResponse.json()

  const config: Record<string, unknown> = {
    mode: 'scripted',
    scripted_scenario: scenario,
  }
  if (maxRevisionRounds !== undefined) config.limits = { max_revision_rounds: maxRevisionRounds }

  const runResponse = await page.request.post(`${BASE_URL}/api/v1/workflows`, {
    data: {
      project_id: projectId,
      source_id: source.id,
      objective: `Browser QA ${scenario}`,
      config,
    },
  })
  expect(runResponse.status()).toBe(201)
  const run = await runResponse.json()
  return { runId: run.id, sourceId: source.id }
}

test.describe.configure({ mode: 'serial' })

test('dashboard loads in Chromium without console errors', async ({ page }) => {
  const consoleErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error' || message.type() === 'warning') consoleErrors.push(`${message.type()}: ${message.text()}`)
  })
  page.on('pageerror', (error) => consoleErrors.push(`pageerror: ${error.message}`))

  const response = await page.goto('/', { waitUntil: 'networkidle' })
  expect(response?.status()).toBe(200)
  await expect(page).toHaveTitle('ReviewFlow')
  await expect(page.getByRole('heading', { name: 'Dashboard', exact: true })).toBeVisible()
  await expect(page.getByText('Scripted engine enabled · live runs enabled', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'New review', exact: true }).first()).toBeVisible()
  await page.screenshot({ path: screenshot('01-dashboard') , fullPage: true })
  expect(consoleErrors).toEqual([])
})

test('creates a project through the Projects UI', async ({ page }) => {
  await page.goto('/projects', { waitUntil: 'networkidle' })
  await page.getByLabel('Name').fill('Browser E2E Project')
  await page.getByLabel('Description').fill('Real Chromium acceptance coverage')
  await page.getByLabel('Default objective').fill('Identify unsupported claims and make only defensible edits.')

  await Promise.all([
    page.waitForURL(/\/projects\/prj_[^/]+$/),
    page.getByRole('button', { name: 'Create project', exact: true }).click(),
  ])

  projectId = new URL(page.url()).pathname.split('/').pop() ?? ''
  expect(projectId).toMatch(/^prj_/) 
  await expect(page.getByRole('heading', { name: 'Sources — Browser E2E Project', exact: true })).toBeVisible()
  await expect(page.getByText('Browser E2E Project', { exact: true }).first()).toBeVisible()
  await page.screenshot({ path: screenshot('02-project-created'), fullPage: true })
})

test('loads a deterministic manuscript sample and displays its hash', async ({ page }) => {
  await page.goto(`/projects/${projectId}`, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: 'sample_manuscript', exact: true }).click()

  let detail: { sources: Array<{ id: string; filename: string; sha256: string; size_bytes: number }> } | undefined
  await expect
    .poll(
      async () => {
        const response = await page.request.get(`${BASE_URL}/api/v1/projects/${projectId}`)
        detail = await response.json()
        return detail.sources.length
      },
      { timeout: 10_000 },
    )
    .toBe(1)

  const source = detail?.sources[0]
  expect(source).toBeTruthy()
  expect(source?.filename).toBe('manuscript.md')
  expect(source?.size_bytes).toBeGreaterThan(0)
  expect(source?.sha256).toMatch(/^[a-f0-9]{64}$/)
  await expect(page.getByRole('cell', { name: /manuscript\.md/ })).toBeVisible()
  await expect(page.getByRole('cell', { name: new RegExp(source?.sha256.slice(0, 16) ?? '') })).toBeVisible()

  // The Projects page intentionally shows only the first four samples. Load the full
  // end-to-end scenario through the documented API so its source bytes match the scripted
  // edit contract, then select that real source for the browser workflow.
  const endToEndSample = await page.request.post(
    `${BASE_URL}/api/v1/projects/${projectId}/samples?sample_id=scenario:successful_end_to_end`,
  )
  expect(endToEndSample.status()).toBe(201)
  const scriptedSource = await endToEndSample.json()
  sourceId = scriptedSource.id
  await page.reload({ waitUntil: 'networkidle' })
  await expect(page.getByRole('cell', { name: new RegExp(scriptedSource.sha256.slice(0, 16)) })).toBeVisible()
  expect(scriptedSource.filename).toBe('manuscript.md')
  expect(scriptedSource.size_bytes).toBeGreaterThan(source?.size_bytes ?? 0)
  await page.screenshot({ path: screenshot('03-source-loaded'), fullPage: true })
})

test('runs preflight and starts a scripted successful workflow', async ({ page }) => {
  await page.goto(`/new?project=${projectId}`, { waitUntil: 'networkidle' })
  await page.getByLabel('Source material').selectOption(sourceId)
  await page.getByLabel('Engine').selectOption('scripted')
  await page.getByLabel('Scenario').selectOption('successful_end_to_end')
  await expect(page.getByLabel('What must this review achieve?')).not.toHaveValue('')
  await expect(page.getByRole('button', { name: 'Start review', exact: true })).toBeDisabled()

  await page.getByRole('button', { name: 'Run preflight', exact: true }).click()
  await expect(page.getByText('clear to start', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Start review', exact: true })).toBeEnabled()
  await page.screenshot({ path: screenshot('04-preflight-clear'), fullPage: true })

  await page.getByRole('button', { name: 'Start review', exact: true }).click()
  await page.waitForURL(/\/runs\/run_[^/]+$/)
  successRunId = new URL(page.url()).pathname.split('/').pop() ?? ''
  expect(successRunId).toMatch(/^run_/)

  const liveSnapshots: string[] = []
  const deadline = Date.now() + 10_000
  while (Date.now() < deadline) {
    const text = await bodyText(page)
    if (text.includes('in progress') || text.includes('\nRunning\n')) {
      liveSnapshots.push(text.slice(0, 600))
    }
    if (text.includes('stream closed') && text.includes('Succeeded')) break
    await page.waitForTimeout(50)
  }

  await terminalStatus(page, successRunId, 'succeeded')
  await expect(page.getByText('Succeeded', { exact: true }).first()).toBeVisible()
  const timeline = await page.locator('.timeline').innerText()
  expect(timeline).toContain('Ingest')
  expect(timeline).toContain('Parallel Review')
  expect(timeline).toContain('Final Review')
  expect(timeline).toContain('Completed')
  console.log(`LIVE_TIMELINE_SNAPSHOTS=${liveSnapshots.length}`)
  expect(liveSnapshots.length).toBeGreaterThan(0)
  await page.screenshot({ path: screenshot('05-successful-run'), fullPage: true })
})

test('reviewer findings tab shows the independent reviewer reports', async ({ page }) => {
  await page.goto(`/runs/${successRunId}`, { waitUntil: 'networkidle' })
  await page.getByRole('tab', { name: /^Reviewer findings/ }).click()
  await expect(page.getByRole('heading', { name: 'Evidence Auditor', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Technical Reviewer', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Rhetorical Analyst', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Structure Reviewer', exact: true })).toBeVisible()
  const text = await bodyText(page)
  expect(text).toContain('1 issue')
  expect(text).toContain('0 issues')
  await page.screenshot({ path: screenshot('06-reviewer-findings'), fullPage: true })
})

test('synthesis tab shows accepted issue decisions', async ({ page }) => {
  await page.goto(`/runs/${successRunId}`, { waitUntil: 'networkidle' })
  await page.getByRole('tab', { name: 'Synthesis', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Decisions', exact: true })).toBeVisible()
  await expect(page.getByText('Change required', { exact: true })).toBeVisible()
  await expect(page.getByText('evidence_auditor:ev-1', { exact: true })).toBeVisible()
  await expect(page.getByText('technical_reviewer:tech-1', { exact: true })).toBeVisible()
  await page.screenshot({ path: screenshot('07-synthesis'), fullPage: true })
})

test('edit and diff tab renders the engine unified diff', async ({ page }) => {
  await page.goto(`/runs/${successRunId}`, { waitUntil: 'networkidle' })
  await page.getByRole('tab', { name: 'Edit & diff', exact: true }).click()
  await expect(page.getByText('Diff (computed from bytes on disk)', { exact: true })).toBeVisible()
  const diffText = await page.locator('.diff').innerText()
  expect(diffText).toContain('--- a/manuscript.md')
  expect(diffText).toContain('+++ b/manuscript.md')
  expect(diffText).toContain('@@')
  expect(diffText).toContain('+')
  expect(diffText).toContain('−')
  expect(await page.locator('.diff-line').count()).toBeGreaterThan(0)
  await page.screenshot({ path: screenshot('08-edit-unified-diff'), fullPage: true })
})

test('verification tab shows the terminal check results', async ({ page }) => {
  await page.goto(`/runs/${successRunId}`, { waitUntil: 'networkidle' })
  await page.getByRole('tab', { name: 'Verification', exact: true }).click()
  await expect(page.getByText('all passing', { exact: true })).toBeVisible()
  const verificationText = await bodyText(page)
  expect(verificationText).toContain('structural_preservation')
  expect(verificationText).toContain('no_new_unsupported_claims')
  expect(verificationText).not.toContain('fail\t')
  await page.screenshot({ path: screenshot('09-verification'), fullPage: true })
})

test('result tab shows the terminal report and artifact download works', async ({ page }) => {
  await page.goto(`/runs/${successRunId}`, { waitUntil: 'networkidle' })
  await page.getByRole('tab', { name: 'Result', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Final report', exact: true })).toBeVisible()
  await expect(page.getByText('Artifacts', { exact: true })).toBeVisible()
  const downloadLinks = page.getByRole('link', { name: 'download', exact: true })
  await expect(downloadLinks).toHaveCount(5)
  await page.screenshot({ path: screenshot('10-result-artifacts'), fullPage: true })

  const downloadPromise = page.waitForEvent('download')
  await downloadLinks.first().click()
  const download = await downloadPromise
  expect(await download.failure()).toBeNull()
  expect(download.suggestedFilename()).toBe('manuscript.md')
  const downloadedPath = path.join(RESULTS, `artifact-${successRunId}.md`)
  await download.saveAs(downloadedPath)
  const artifactText = await fs.readFile(downloadedPath, 'utf8')
  expect(artifactText).toContain('preliminary')
  console.log(`ARTIFACT_DOWNLOAD=${downloadedPath}`)
})

test('run history lists the run and reopens the previous run', async ({ page }) => {
  await page.goto('/runs', { waitUntil: 'networkidle' })
  const runLink = page.locator(`a[href="/runs/${successRunId}"]`)
  await expect(runLink).toHaveCount(1)
  await expect(runLink).toBeVisible()
  await page.screenshot({ path: screenshot('11-run-history'), fullPage: true })
  await runLink.click()
  await page.waitForURL(`/runs/${successRunId}`)
  await expect(page.getByText('Succeeded', { exact: true }).first()).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Identify unsupported claims and make only defensible edits.', exact: true })).toBeVisible()
})

test('deep links serve the SPA and unknown API paths return JSON 404', async ({ page }) => {
  const deepLink = await page.goto(`/runs/${successRunId}`, { waitUntil: 'networkidle' })
  expect(deepLink?.status()).toBe(200)
  expect(deepLink?.headers()['content-type']).toMatch(/text\/html/)
  await expect(page.getByText('ReviewFlow', { exact: true }).first()).toBeVisible()
  await expect(page.getByText(successRunId, { exact: true })).toBeVisible()
  await page.screenshot({ path: screenshot('12-deep-link'), fullPage: true })

  const unknownApi = await page.goto('/api/v1/this-path-does-not-exist', { waitUntil: 'domcontentloaded' })
  expect(unknownApi?.status()).toBe(404)
  expect(unknownApi?.headers()['content-type']).toMatch(/application\/json/)
  const apiBody = await bodyText(page)
  expect(() => JSON.parse(apiBody)).not.toThrow()
  expect(apiBody).not.toMatch(/<!doctype html/i)
  console.log(`UNKNOWN_API_404=${JSON.stringify({ status: unknownApi?.status(), contentType: unknownApi?.headers()['content-type'], body: apiBody })}`)
})

test('live parallel reviewer states update and a refresh preserves the run', async ({ page }) => {
  const created = await createScenarioRun(page, 'reviewer_timeout_degrades')
  degradedRunId = created.runId
  await page.goto(`/runs/${degradedRunId}`, { waitUntil: 'domcontentloaded' })

  const parallelStage = page.locator('.timeline li').filter({ hasText: 'Parallel Review' }).first()
  const evidenceRow = parallelStage.locator('.agent-row').filter({ hasText: 'Evidence Auditor' })
  const structureRow = parallelStage.locator('.agent-row').filter({ hasText: 'Structure Reviewer' })
  await expect
    .poll(
      async () => {
        if ((await parallelStage.count()) === 0) return false
        return (await parallelStage.innerText()).includes('in progress')
      },
      { timeout: 8_000, intervals: [50, 100, 200, 400] },
    )
    .toBe(true)

  let reviewerRunningSeen = false
  const reviewerStateDeadline = Date.now() + 5_000
  while (Date.now() < reviewerStateDeadline && !reviewerRunningSeen) {
    if ((await evidenceRow.count()) > 0) reviewerRunningSeen = (await evidenceRow.innerText()).includes('Running')
    if (!reviewerRunningSeen) await page.waitForTimeout(50)
  }
  await expect(structureRow.getByText('Completed', { exact: true })).toBeVisible()
  console.log(`PARALLEL_REVIEW_REVIEWER_RUNNING_OBSERVED=${reviewerRunningSeen}`)
  console.log(`PARALLEL_REVIEW_STAGE_TEXT=${JSON.stringify(await parallelStage.innerText())}`)
  await page.screenshot({ path: screenshot('13-live-parallel-review'), fullPage: true })

  await page.reload({ waitUntil: 'domcontentloaded' })
  await terminalStatus(page, degradedRunId, 'degraded')
  await expect(page.getByText('Degraded', { exact: true }).first()).toBeVisible()
  const refreshedText = await bodyText(page)
  expect(refreshedText).toContain('reported as degraded rather than successful')
  expect(refreshedText).not.toContain('Succeeded')
  await page.screenshot({ path: screenshot('14-after-live-refresh'), fullPage: true })
})

test('blocked and degraded outcomes never use success wording', async ({ page }) => {
  const cases = [
    { scenario: 'conflicting_reviewers', rounds: 0, expected: 'Blocked', notice: 'verification still failing', image: '15-blocked-conflict' },
    { scenario: 'failed_verification_blocks_success', rounds: 1, expected: 'Blocked', notice: 'verification still failing', image: '16-blocked-verification' },
  ]

  for (const item of cases) {
    const created = await createScenarioRun(page, item.scenario, item.rounds)
    await terminalStatus(page, created.runId, item.expected.toLowerCase())
    await page.goto(`/runs/${created.runId}`, { waitUntil: 'networkidle' })
    await expect(page.getByText(item.expected, { exact: true }).first()).toBeVisible()
    const header = await page.locator('.page-head').innerText()
    const notice = await page.locator('.notice').first().innerText()
    expect(header).not.toContain('Succeeded')
    expect(notice.toLowerCase()).toContain(item.notice)
    expect(notice).not.toContain('Every mandatory verification check passed.')
    console.log(`STATUS_TRUTH=${JSON.stringify({ scenario: item.scenario, header, notice })}`)
    await page.screenshot({ path: screenshot(item.image), fullPage: true })
  }

  await page.goto(`/runs/${degradedRunId}`, { waitUntil: 'networkidle' })
  await expect(page.getByText('Degraded', { exact: true }).first()).toBeVisible()
  const degradedHeader = await page.locator('.page-head').innerText()
  const degradedNotice = await page.locator('.notice').first().innerText()
  expect(degradedHeader).not.toContain('Succeeded')
  expect(degradedNotice).toContain('Gates passed, but reviewer coverage was incomplete.')
  expect(degradedNotice).toContain('rather than successful')
  expect(degradedNotice).not.toContain('Every mandatory verification check passed.')
  console.log(`STATUS_TRUTH=${JSON.stringify({ scenario: 'reviewer_timeout_degrades', header: degradedHeader, notice: degradedNotice })}`)
  await page.screenshot({ path: screenshot('17-degraded-truthful-wording'), fullPage: true })
})

