import { expect, test, type Page } from '@playwright/test'

const BASE_URL = 'http://127.0.0.1:8012'

async function createApiRun(page: Page, scenario: string, name: string): Promise<string> {
  const projectResponse = await page.request.post(`${BASE_URL}/api/v1/projects`, {
    data: { name, description: 'Known defect browser probe', default_objective: 'Browser defect probe.' },
  })
  expect(projectResponse.status()).toBe(201)
  const project = await projectResponse.json()
  const sourceResponse = await page.request.post(
    `${BASE_URL}/api/v1/projects/${project.id}/samples?sample_id=scenario:${scenario}`,
  )
  expect(sourceResponse.status()).toBe(201)
  const source = await sourceResponse.json()
  const runResponse = await page.request.post(`${BASE_URL}/api/v1/workflows`, {
    data: {
      project_id: project.id,
      source_id: source.id,
      objective: `Browser defect probe ${scenario}`,
      config: { mode: 'scripted', scripted_scenario: scenario },
    },
  })
  expect(runResponse.status()).toBe(201)
  const run = await runResponse.json()
  return run.id
}

async function waitForStatus(page: Page, runId: string, expected: string): Promise<void> {
  await expect
    .poll(
      async () => {
        const response = await page.request.get(`${BASE_URL}/api/v1/workflows/${runId}`)
        return (await response.json()).run.status
      },
      { timeout: 35_000, intervals: [100, 250, 500, 1_000] },
    )
    .toBe(expected)
}

test('KNOWN PRODUCT DEFECT: parallel reviewer rows never show Running', async ({ page }) => {
  const runId = await createApiRun(page, 'reviewer_timeout_degrades', 'Browser QA defect reviewer state')
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.goto(`/runs/${runId}`, { waitUntil: 'domcontentloaded' })

  const parallelStage = page.locator('.timeline li').filter({ hasText: 'Parallel Review' }).first()
  const evidenceRow = parallelStage.locator('.agent-row').filter({ hasText: 'Evidence Auditor' })
  let runningSeen = false
  const observations: string[] = []
  const deadline = Date.now() + 3_500
  while (Date.now() < deadline) {
    const stageText = (await parallelStage.count()) > 0 ? await parallelStage.innerText() : ''
    if (stageText) observations.push(stageText)
    if ((await evidenceRow.count()) > 0 && (await evidenceRow.innerText()).includes('Running')) {
      runningSeen = true
      break
    }
    await page.waitForTimeout(50)
  }
  const observedText = await page.locator('body').innerText()
  console.log(`PARALLEL_REVIEW_RUNNING_OBSERVED=${runningSeen}`)
  console.log(`PARALLEL_REVIEW_LAST_STAGE=${JSON.stringify(observations.at(-1) ?? '')}`)
  console.log(`PARALLEL_REVIEW_DOM_TEXT=${JSON.stringify(observedText.slice(0, 1800))}`)
  console.log(`PARALLEL_REVIEW_PAGE_ERRORS=${JSON.stringify(pageErrors)}`)
  await page.screenshot({ path: 'e2e/screenshots/19-reviewer-running-defect.png', fullPage: true })

  // This is expected to fail against the current build: agent.started events use
  // payload.role_key while the UI derives running state from payload.agent_role.
  expect(runningSeen).toBe(true)
})

test('adversarial tab renders the verdict without crashing the React tree', async ({ page }) => {
  // Was a defect-pinning test (QA-009): the engine sends structured findings
  // ({description, location, evidence, related_issue_id}) and the UI typed them as strings,
  // so React threw "objects are not valid as a React child" (#31) and the whole tree
  // unmounted. The types and the render were fixed; this now asserts the correct behaviour.
  const runId = await createApiRun(page, 'successful_end_to_end', 'Browser QA adversarial tab')
  await waitForStatus(page, runId, 'succeeded')
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.goto(`/runs/${runId}`, { waitUntil: 'networkidle' })
  await page.getByRole('tab', { name: 'Adversarial', exact: true }).click()
  await page.waitForTimeout(250)
  const observedText = await page.locator('body').innerText()
  console.log(`ADVERSARIAL_OBSERVED_DOM_TEXT=${JSON.stringify(observedText)}`)
  console.log(`ADVERSARIAL_PAGE_ERRORS=${JSON.stringify(pageErrors)}`)
  await page.screenshot({ path: 'e2e/screenshots/18-adversarial-verdict.png', fullPage: true })

  expect(pageErrors).toEqual([])
  await expect(page.getByRole('heading', { name: 'Adversarial review', exact: true })).toBeVisible()
  // The verdict body must actually be rendered, not merely present in the payload.
  await expect(page.getByText(/revision survived|did not survive/)).toBeVisible()
})
