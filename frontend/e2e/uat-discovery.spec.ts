/**
 * UAT Phase 1 - product discovery.
 *
 * Records what a user actually sees on every screen, with no reference to the implementation.
 * The output is the raw material for the persona/terminology/friction analysis.
 */
import { test, expect } from '@playwright/test'
import fs from 'node:fs/promises'
import path from 'node:path'

const OUT = path.resolve('..', 'artifacts', 'uat', 'discovery.json')

type Screen = {
  route: string
  label: string
  title: string
  headings: string[]
  buttons: string[]
  fields: { label: string; type: string; required: boolean }[]
  body_text: string
  visible_controls: number
}

async function dump(page: import('@playwright/test').Page, route: string, label: string): Promise<Screen> {
  await page.goto(route, { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)
  const headings = await page.locator('h1, h2, h3').allInnerTexts()
  const buttons = (await page.locator('button, [role=button], a.button, [role=tab]').allInnerTexts())
    .map((t) => t.trim())
    .filter(Boolean)
  const fields = await page.locator('input, select, textarea').evaluateAll((nodes) =>
    nodes.map((n) => {
      const el = n as HTMLInputElement
      const id = el.id || ''
      const lab = id ? (document.querySelector(`label[for="${id}"]`)?.textContent ?? '') : ''
      const wrap = el.closest('label')?.textContent ?? ''
      return {
        label: (lab || wrap || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '(unlabelled)').trim().slice(0, 90),
        type: el.tagName === 'SELECT' ? 'select' : el.type || el.tagName.toLowerCase(),
        required: el.required || el.getAttribute('aria-required') === 'true',
      }
    }),
  )
  return {
    route,
    label,
    title: await page.title(),
    headings: headings.map((h) => h.trim()).filter(Boolean),
    buttons,
    fields,
    body_text: (await page.locator('body').innerText()).slice(0, 6000),
    visible_controls: await page.locator('button, input, select, textarea, [role=tab], a[href]').count(),
  }
}

const BASE_URL = 'http://127.0.0.1:8012'

async function createRun(page: import('@playwright/test').Page, scenario: string, name: string): Promise<string> {
  const project = await (
    await page.request.post(`${BASE_URL}/api/v1/projects`, {
      data: { name, description: 'UAT discovery', default_objective: 'UAT discovery run.' },
    })
  ).json()
  const source = await (
    await page.request.post(`${BASE_URL}/api/v1/projects/${project.id}/samples?sample_id=scenario:${scenario}`)
  ).json()
  const run = await (
    await page.request.post(`${BASE_URL}/api/v1/workflows`, {
      data: { project_id: project.id, source_id: source.id, objective: `UAT discovery ${scenario}`, config: { mode: 'scripted', scripted_scenario: scenario } },
    })
  ).json()
  return run.id as string
}

async function settle(page: import('@playwright/test').Page, runId: string): Promise<string> {
  let status = ''
  for (let i = 0; i < 80; i++) {
    status = (await (await page.request.get(`${BASE_URL}/api/v1/workflows/${runId}`)).json()).run.status
    if (!['running', 'queued', 'pending'].includes(status)) return status
    await page.waitForTimeout(400)
  }
  return status
}

test('UAT discovery: every screen a user can reach', async ({ page }) => {
  // Scripted runs give us populated screens without spending provider tokens.
  const runId = await createRun(page, 'unsupported_claim_detected_and_fixed', 'UAT Discovery Run')
  await settle(page, runId)

  const screens: Screen[] = []
  for (const [route, label] of [
    ['/', 'Dashboard (home)'],
    ['/new', 'New review'],
    ['/runs', 'Run history'],
    ['/projects', 'Projects'],
    ['/providers', 'Providers & models'],
  ] as const) {
    screens.push(await dump(page, route, label))
  }

  // Run detail: the tab set is the core inspection surface.
  await page.goto(`/runs/${runId}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(400)
  screens.push(await dump(page, `/runs/${runId}`, 'Run detail (default tab)'))
  const tabs = page.locator('[role=tab]')
  const tabNames = (await tabs.allInnerTexts()).map((t) => t.trim()).filter(Boolean)
  const tabTexts: Record<string, string> = {}
  for (const name of tabNames) {
    try {
      await page.getByRole('tab', { name, exact: true }).click()
      await page.waitForTimeout(250)
      tabTexts[name] = (await page.locator('body').innerText()).slice(0, 5000)
    } catch (error) {
      tabTexts[name] = `TAB_UNREACHABLE: ${String(error).slice(0, 160)}`
    }
  }

  // A blocked run, because the terminal vocabulary is the highest-stakes screen.
  const blockedId = await createRun(page, 'failed_verification_blocks_success', 'UAT Discovery Blocked')
  await settle(page, blockedId)
  await page.goto(`/runs/${blockedId}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(400)
  screens.push(await dump(page, `/runs/${blockedId}`, 'Run detail (blocked run)'))

  await fs.mkdir(path.dirname(OUT), { recursive: true })
  await fs.writeFile(OUT, JSON.stringify({ run_id: runId, blocked_run_id: blockedId, screens, run_detail_tabs: tabTexts }, null, 2))
  console.log(`DISCOVERY_WRITTEN ${OUT}`)
})
