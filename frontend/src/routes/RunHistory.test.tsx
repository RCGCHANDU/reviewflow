import { afterEach, describe, expect, it } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

import { RunHistory } from './RunHistory'
import { mockFetch, renderWithProviders } from '../test/utils'
import type { WorkflowRunSummary } from '../api/types'

function run(id: string, status: WorkflowRunSummary['status']): WorkflowRunSummary {
  return {
    id,
    project_id: 'prj_1',
    project_name: 'Long-video paper',
    source_id: 'src_1',
    source_filename: 'manuscript.md',
    objective: 'Identify unsupported claims.',
    mode: 'scripted',
    status,
    current_stage: null,
    created_at: '2026-01-01T00:00:00+00:00',
    started_at: '2026-01-01T00:00:01+00:00',
    finished_at: '2026-01-01T00:04:00+00:00',
    revision_round: 2,
    failure_reason: null,
    usage: { model_calls: 19, total_tokens: 99048, wall_clock_ms: 412047 },
    config: {},
    provider_snapshot: { provider: 'commandcode', transport: 'standard' },
  }
}

describe('RunHistory', () => {
  let stub: ReturnType<typeof mockFetch>

  afterEach(() => stub?.restore())

  it('lists runs with their distinct terminal states and usage', async () => {
    stub = mockFetch({
      'GET /workflows': {
        body: [run('run_a', 'succeeded'), run('run_b', 'blocked'), run('run_c', 'degraded'), run('run_d', 'cancelled')],
      },
      'GET /projects': { body: [] },
    })

    renderWithProviders(<RunHistory />, { route: '/runs', path: '/runs' })

    expect(await screen.findByText('Succeeded')).toBeInTheDocument()
    expect(screen.getByText('Blocked')).toBeInTheDocument()
    expect(screen.getByText('Degraded')).toBeInTheDocument()
    expect(screen.getByText('Cancelled')).toBeInTheDocument()
    expect(screen.getAllByText('99,048').length).toBeGreaterThan(0)
  })

  it('does not start or rerun anything when merely listing history', async () => {
    stub = mockFetch({
      'GET /workflows': { body: [run('run_a', 'succeeded')] },
      'GET /projects': { body: [] },
    })

    renderWithProviders(<RunHistory />, { route: '/runs', path: '/runs' })
    await screen.findByText('Succeeded')

    // Reading history must never have side effects on the engine.
    const mutations = stub.calls.filter((call) => call.method !== 'GET')
    expect(mutations).toHaveLength(0)
  })

  it('filters by status through the API rather than client-side only', async () => {
    stub = mockFetch({
      'GET /workflows': { body: [run('run_b', 'blocked')] },
      'GET /projects': { body: [] },
    })

    renderWithProviders(<RunHistory />, { route: '/runs?status=blocked', path: '/runs' })
    await screen.findByText('Blocked')

    await waitFor(() => {
      const statusCall = stub.calls.find((call) => call.url.includes('status=blocked'))
      expect(statusCall).toBeDefined()
    })
  })
})
