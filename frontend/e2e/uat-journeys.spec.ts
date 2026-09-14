/**
 * UAT Layer 1 - the journey portfolio.
 *
 * Runs the ten user journeys against the frozen build and records ONLY what a user can see on
 * screen: the terminal status label, the notice, the verification card, the coverage line, the
 * Result tab, and the interaction cost of reaching a defensible decision.
 */
import { test } from '@playwright/test'
import type { Page } from '@playwright/test'
import fs from 'node:fs/promises'
import path from 'node:path'

const BASE = 'http://127.0.0.1:8012'
const OUT = path.resolve('..', 'artifacts', 'uat', 'journeys.json')

type Journey = {
  id: string
  name: string
  user_goal: string
  scenario?: string
  long_artifact?: boolean
  expect_status?: string
  expect_note: string
}

const JOURNEYS: Journey[] = [
  { id: 'J1', name: 'Clean artifact', scenario: 'clean_input_no_edit', user_goal: 'A sound document must not attract invented problems', expect_status: 'succeeded', expect_note: 'no unnecessary edits' },
  { id: 'J2', name: 'Unsupported claim', scenario: 'unsupported_claim_detected_and_fixed', user_goal: 'Understand the defect and accept a defensible correction', expect_status: 'succeeded', expect_note: 'defect explained, correction defensible' },
  { id: 'J3', name: 'Conflicting reviewers', scenario: 'conflicting_reviewers', user_goal: 'Understand conflict, synthesis and residual uncertainty', expect_status: 'blocked', expect_note: 'conflict visible, uncertainty preserved' },
  { id: 'J4', name: 'Bad edit', scenario: 'editor_fabricates_adversarial_catches', user_goal: 'Unsafe revision prevented and the reason understood', expect_status: 'succeeded', expect_note: 'adversarial layer catches it, user told why' },
  { id: 'J5', name: 'Missing evidence', scenario: 'reviewer_hallucination_rejected', user_goal: 'System communicates uncertainty instead of fabricating support', expect_status: 'succeeded', expect_note: 'uncertainty stated, no invented citations' },
  { id: 'J6', name: 'Degraded run', scenario: 'reviewer_timeout_degrades', user_goal: 'Understand that reviewer coverage was reduced', expect_status: 'degraded', expect_note: 'reduced coverage obvious to a skimming user' },
  { id: 'J7', name: 'Blocked run', scenario: 'failed_verification_blocks_success', user_goal: 'Do not treat the artifact as ready', expect_status: 'blocked', expect_note: 'user cannot mistake this for ready' },
  { id: 'J8', name: 'Provider failure', user_goal: 'Recover from an unavailable model/provider understandably', expect_note: 'error is actionable, completed work preserved' },
  { id: 'J9', name: 'Long technical artifact', scenario: 'clean_input_no_edit', long_artifact: true, user_goal: 'Navigate a long document without drowning in it', expect_status: 'succeeded', expect_note: 'navigable, critical items not buried' },
  { id: 'J10', name: 'First-time user', user_goal: 'Start a review with no documentation and no training', expect_note: 'startable without internal knowledge' },
]

async function makeRun(page: Page, journey: Journey) {
  const project = await (
    await page.request.post(`${BASE}/api/v1/projects`, {
      data: { name: `UAT ${journey.id} ${journey.name}`, description: `Journey ${journey.id}`, default_objective: journey.user_goal },
    })
  ).json()

  let source: { id: string }
  if (journey.long_artifact) {
    // A genuinely long technical artifact: the shipped sample plus 12 appended sections.
    const base = (await (await page.request.get(`${BASE}/api/v1/samples`)).json()) as unknown
    const text = LONG_ARTIFACT
    const res = await page.request.post(`${BASE}/api/v1/projects/${project.id}/sources`, {
      multipart: {
        file: { name: 'manuscript.md', mimeType: 'text/markdown', buffer: Buffer.from(text, 'utf8') },
      },
    })
    if (!res.ok()) throw new Error(`long upload failed ${res.status()} ${await res.text()}`)
    source = (await res.json()) as { id: string }
    void base
  } else {
    source = await (
      await page.request.post(`${BASE}/api/v1/projects/${project.id}/samples?sample_id=scenario:${journey.scenario}`)
    ).json()
  }

  const run = await (
    await page.request.post(`${BASE}/api/v1/workflows`, {
      data: {
        project_id: project.id,
        source_id: source.id,
        objective: journey.user_goal,
        config: journey.long_artifact ? { mode: 'scripted', scripted_scenario: 'clean_input_no_edit' } : { mode: 'scripted', scripted_scenario: journey.scenario },
      },
    })
  ).json()
  return run.id as string
}

async function settle(page: Page, runId: string) {
  for (let i = 0; i < 90; i++) {
    const payload = await (await page.request.get(`${BASE}/api/v1/workflows/${runId}`)).json()
    if (!['running', 'queued', 'pending'].includes(payload.run.status)) return payload
    await page.waitForTimeout(400)
  }
  return await (await page.request.get(`${BASE}/api/v1/workflows/${runId}`)).json()
}

const LONG_ARTIFACT = `# A Long Technical Manuscript for Navigation Testing

## Abstract
This study evaluates a multi-stage review workflow. ${'The evidence base is described in detail. '.repeat(30)}

## 1. Introduction
We claim our method improves review outcomes by 40% across all benchmarks. ${'Prior work is discussed at length. '.repeat(80)}

${Array.from({ length: 12 }, (_, i) => `## Section ${i + 2}. Detailed Technical Material\n${('This section contains dense methodological detail, including equations, ablations, and protocol descriptions. ').repeat(40)}\n\nWe assert that claim ${i + 2} holds without qualification. ${'Supporting discussion follows. '.repeat(30)}\n`).join('\n')}

## Conclusion
The results strongly support all claims. ${'Final remarks. '.repeat(20)}
`

test('UAT journeys J1-J10 against the frozen build', async ({ page }) => {
  const results: Record<string, unknown>[] = []

  for (const journey of JOURNEYS) {
    if (journey.id === 'J8' || journey.id === 'J10') continue // handled below, they are not happy-path runs
    const started = Date.now()
    const runId = await makeRun(page, journey)
    const payload = await settle(page, runId)
    const wall = Date.now() - started

    // Interaction cost from arriving at the run detail to having seen the decision surface.
    let clicks = 0
    await page.goto(`/runs/${runId}`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(250)
    const headerText = (await page.locator('body').innerText()).slice(0, 1400)
    const tabs: Record<string, string> = {}
    for (const name of ['Result', 'Verification', 'Reviewer findings', 'Synthesis', 'Edit & diff', 'Adversarial']) {
      const tab = page.getByRole('tab', { name: new RegExp(name) }).first()
      if ((await tab.count()) === 0) continue
      clicks++
      await tab.click()
      await page.waitForTimeout(200)
      tabs[name] = (await page.locator('body').innerText()).slice(0, 4000)
    }
    const interacted = await page.evaluate(() => document.querySelectorAll('[role=tab]').length)

    results.push({
      journey: journey.id,
      name: journey.name,
      user_goal: journey.user_goal,
      expect_note: journey.expect_note,
      run_id: runId,
      api_status: payload.run.status,
      engine_status: payload.run.engine_status ?? null,
      usage: payload.run.usage ?? null,
      failure_reason: payload.run.failure_reason ?? null,
      result: payload.run.result ?? null,
      header_text: headerText,
      tabs,
      tab_clicks_to_result: clicks,
      tab_count: interacted,
      wall_ms_to_terminal: wall,
      expected_status: journey.expect_status ?? null,
    })
  }

  // J8 - the user picks a model the provider cannot serve.
  const j8project = await (
    await page.request.post(`${BASE}/api/v1/projects`, { data: { name: 'UAT J8 Provider failure', default_objective: 'Recover from a bad model choice' } })
  ).json()
  const j8source = await (
    await page.request.post(`${BASE}/api/v1/projects/${j8project.id}/samples?sample_id=scenario:clean_input_no_edit`)
  ).json()
  const preflight = await page.request.post(`${BASE}/api/v1/workflows/preflight`, {
    data: { project_id: j8project.id, source_id: j8source.id, objective: 'Recover from a bad model choice', config: { mode: 'live', provider: 'commandcode', role_models: { evidence_auditor: 'commandcode:no-such-model-xyz' } } },
  })
  const badModel = await page.request.post(`${BASE}/api/v1/workflows`, {
    data: { project_id: j8project.id, source_id: j8source.id, objective: 'Recover from a bad model choice', config: { mode: 'live', provider: 'commandcode', role_models: { evidence_auditor: 'commandcode:no-such-model-xyz' } } },
  })
  const badProvider = await page.request.post(`${BASE}/api/v1/workflows`, {
    data: { project_id: j8project.id, source_id: j8source.id, objective: 'Recover from a bad model choice', config: { mode: 'live', provider: 'does-not-exist' } },
  })
  let j8Run: unknown = null
  if (badModel.status() === 201) {
    const id = (await badModel.json()).id as string
    const payload = await settle(page, id)
    await page.goto(`/runs/${id}`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(300)
    j8Run = {
      run_id: id,
      api_status: payload.run.status,
      failure_reason: payload.run.failure_reason ?? null,
      visible: (await page.locator('body').innerText()).slice(0, 2500),
    }
  }
  results.push({
    journey: 'J8',
    name: 'Provider failure',
    user_goal: 'Recover from an unavailable model/provider understandably',
    expect_note: 'error is actionable, completed work preserved',
    preflight_status: preflight.status(),
    preflight_body: (await preflight.text()).slice(0, 900),
    bad_model_status: badModel.status(),
    bad_model_body: (await badModel.text()).slice(0, 900),
    bad_provider_status: badProvider.status(),
    bad_provider_body: (await badProvider.text()).slice(0, 900),
    run: j8Run,
  })

  // J10 - a first-time user on the New review screen with no documentation.
  await page.goto('/new', { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)
  const newScreen = (await page.locator('body').innerText()).slice(0, 5000)
  const decisionPoints = await page.locator('input, select, textarea').evaluateAll((nodes) =>
    nodes.map((n) => {
      const el = n as HTMLInputElement
      const id = el.id || ''
      const lab = id ? (document.querySelector(`label[for="${id}"]`)?.textContent ?? '') : ''
      const wrap = el.closest('label')?.textContent ?? ''
      return {
        label: (lab || wrap || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '(unlabelled)').trim().slice(0, 80),
        value: String(el.value ?? '').slice(0, 40),
        type: el.tagName === 'SELECT' ? 'select' : el.type,
        required: el.required,
      }
    }),
  )
  const advancedVisible = await page.getByText(/provider|model|engine/i).count()
  results.push({
    journey: 'J10',
    name: 'First-time user',
    user_goal: 'Start a review with no documentation and no training',
    expect_note: 'startable without internal knowledge',
    new_screen_text: newScreen,
    decision_points: decisionPoints,
    decision_point_count: decisionPoints.length,
    presets: await page.locator('button', { hasText: /preset/i }).allInnerTexts(),
    terminology_hits: advancedVisible,
  })

  await fs.mkdir(path.dirname(OUT), { recursive: true })
  await fs.writeFile(OUT, JSON.stringify(results, null, 2))
  console.log(`JOURNEYS_WRITTEN ${OUT} count=${results.length}`)
})
