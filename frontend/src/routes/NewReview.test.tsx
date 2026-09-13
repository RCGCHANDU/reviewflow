import { afterEach, describe, expect, it } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { NewReview } from './NewReview'
import { mockFetch, renderWithProviders } from '../test/utils'

const PROJECTS = [{ id: 'prj_1', name: 'Long-video paper', description: '', created_at: '', updated_at: '', default_objective: '' }]
const SOURCES = [
  {
    id: 'src_1',
    project_id: 'prj_1',
    filename: 'manuscript.md',
    content_type: 'text/markdown',
    size_bytes: 781,
    sha256: 'a'.repeat(64),
    created_at: '',
    preview: '# Sparse Retrieval',
  },
]
const ROLES = [
  {
    role_key: 'evidence_auditor',
    agent_name: 'Evidence auditor',
    description: 'Checks whether every statement is supported.',
    focus: ['unsupported_claim'],
    may_mutate: false,
    allowed_tools: ['read_file'],
    mandatory: true,
    planner_selectable: true,
    default_model: 'gpt-5.6-luna',
    tier: 'reviewer',
  },
  {
    role_key: 'controlled_editor',
    agent_name: 'Controlled editor',
    description: 'Applies the smallest defensible set of changes.',
    focus: ['minimal_edit'],
    may_mutate: true,
    allowed_tools: ['apply_edit'],
    mandatory: false,
    planner_selectable: false,
    default_model: 'gpt-5.6-sol',
    tier: 'editor',
  },
]

function providers(available: boolean) {
  return {
    active_provider: available ? 'commandcode' : null,
    requested_provider: 'auto',
    providers: [
      {
        name: 'commandcode',
        available,
        api_mode: 'chat_completions',
        requires_streaming: false,
        auth_source: available ? '/home/.commandcode/auth.json' : null,
        account: available ? 'RCGCHANDU' : null,
        expires_at: null,
        seconds_left: null,
        refreshable: false,
        base_url: 'https://api.commandcode.ai/provider/v1',
        hint: available ? '' : 'Sign in with the commandcode CLI so ~/.commandcode/auth.json exists.',
      },
    ],
    routing: {},
    role_defaults: { reviewer: 'gpt-5.6-luna' },
  }
}

const SAMPLES = [{ id: 'scenario:successful_end_to_end', name: 'manuscript.md', description: 'scripted scenario' }]

describe('NewReview', () => {
  let stub: ReturnType<typeof mockFetch>

  afterEach(() => stub?.restore())

  it('offers the scripted engine when no provider credential is available, and says why', async () => {
    stub = mockFetch({
      'GET /projects': { body: PROJECTS },
      'GET /roles': { body: ROLES },
      'GET /providers': { body: providers(false) },
      'GET /models': { body: [] },
      'GET /samples': { body: SAMPLES },
      'GET /projects/prj_1': { body: { project: PROJECTS[0], sources: SOURCES, runs: [] } },
    })

    renderWithProviders(<NewReview />, { route: '/new', path: '/new' })

    expect(await screen.findByRole('heading', { name: 'New review' })).toBeInTheDocument()
    // The live option is disabled rather than silently failing later.
    await waitFor(() => {
      const mode = screen.getByLabelText('Engine') as HTMLSelectElement
      const live = Array.from(mode.options).find((option) => option.value === 'live')
      expect(live?.disabled).toBe(true)
    })
    expect(await screen.findByText(/Scripted runs expect the target to be named/)).toBeInTheDocument()
  })

  it('lists the reviewers as read-only and the editor as the only write-capable agent', async () => {
    stub = mockFetch({
      'GET /projects': { body: PROJECTS },
      'GET /roles': { body: ROLES },
      'GET /providers': { body: providers(true) },
      'GET /models': { body: [{ id: 'gpt-5.6-sol', provider: 'commandcode', label: '', default_for: ['editor'], capabilities: {} }] },
      'GET /samples': { body: SAMPLES },
    })

    renderWithProviders(<NewReview />, { route: '/new', path: '/new' })

    expect(await screen.findByText('Evidence auditor')).toBeInTheDocument()
    // The editor appears both in the reviewer list and in the per-role model table.
    expect(screen.getAllByText('Controlled editor').length).toBeGreaterThan(0)
    expect(screen.getAllByText('read-only').length).toBeGreaterThan(0)
    expect(screen.getByText('write-capable')).toBeInTheDocument()
  })

  it('refuses to start before preflight passes', async () => {
    stub = mockFetch({
      'GET /projects': { body: PROJECTS },
      'GET /roles': { body: ROLES },
      'GET /providers': { body: providers(true) },
      'GET /models': { body: [] },
      'GET /samples': { body: SAMPLES },
      'GET /projects/prj_1': { body: { project: PROJECTS[0], sources: SOURCES, runs: [] } },
      'POST /workflows/preflight': {
        body: {
          ok: false,
          checks: [
            { name: 'provider', ok: false, detail: 'provider "codex" has no usable credential', blocking: true },
            { name: 'objective', ok: true, detail: 'objective is set', blocking: true },
          ],
          blocking_failures: ['provider'],
        },
      },
    })

    const user = userEvent.setup()
    renderWithProviders(<NewReview />, { route: '/new?project=prj_1', path: '/new' })

    // Pick the source and give an objective, so only the provider blocks the run.
    const sourceSelect = await screen.findByLabelText('Source material')
    await user.selectOptions(sourceSelect, 'src_1')
    await user.type(screen.getByLabelText(/What must this review achieve/), 'Find unsupported claims.')

    const start = screen.getByRole('button', { name: 'Start review' })
    expect(start).toBeDisabled()

    await user.click(screen.getByRole('button', { name: 'Run preflight' }))
    expect(await screen.findByText(/has no usable credential/)).toBeInTheDocument()
    expect(screen.getByText('Blocking problems')).toBeInTheDocument()
    expect(start).toBeDisabled()
    // No run was created.
    expect(stub.calls.some((call) => call.method === 'POST' && call.url.endsWith('/workflows'))).toBe(false)
  })
})
