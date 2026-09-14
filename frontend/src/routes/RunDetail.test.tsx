import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { RunDetail } from './RunDetail'
import { MockEventSource, installMockEventSource, mockFetch, renderWithProviders } from '../test/utils'
import type { RunInspection, StageState, StageStateName, WorkflowRunSummary } from '../api/types'

const RUN: WorkflowRunSummary = {
  id: 'run_demo',
  project_id: 'prj_1',
  project_name: 'Long-video paper',
  source_id: 'src_1',
  source_filename: 'manuscript.md',
  objective: 'Identify unsupported claims and make only defensible edits.',
  mode: 'scripted',
  status: 'running',
  current_stage: 'parallel_review',
  created_at: '2026-01-01T00:00:00+00:00',
  started_at: '2026-01-01T00:00:01+00:00',
  finished_at: null,
  revision_round: 0,
  failure_reason: null,
  usage: { model_calls: 6, total_tokens: 1080, wall_clock_ms: 4000 },
  config: { mode: 'scripted' },
  provider_snapshot: { provider: 'scripted', transport: 'standard' },
}

function stages(overrides: Record<string, StageStateName> = {}): StageState[] {
  return [
    'ingest',
    'plan',
    'parallel_review',
    'synthesize',
    'controlled_edit',
    'diff_review',
    'adversarial_review',
    'verify',
    'final_review',
  ].map((stage) => ({
    stage,
    state: overrides[stage] ?? 'pending',
    started_at: null,
    finished_at: null,
    detail: {},
  }))
}

function inspection(overrides: Partial<RunInspection> = {}): RunInspection {
  return {
    run: RUN,
    stages: stages(),
    plan: null,
    reviewers: [],
    failed_reviewers: {},
    synthesis: null,
    edit: null,
    diff: null,
    diff_review: null,
    adversarial: null,
    verification: null,
    final_report: null,
    coverage: null,
    unresolved_issues: [],
    terminal: null,
    usage: RUN.usage,
    artifacts: [],
    status_explanation: '',
    ...overrides,
  }
}

describe('RunDetail', () => {
  it('does not let a passing verification card imply there is nothing unresolved', async () => {
    // A successful run may legitimately carry accepted issues nobody actioned. The screen
    // reported "Nothing failing" next to a banner disclosing an unresolved issue, which
    // read as a contradiction. The card must state the different fact explicitly.
    mockFetch({
      'GET /workflows/run_demo': { body: { run: { ...RUN, status: 'succeeded' }, stages: stages({ verify: 'completed' }), is_running: false } },
      'GET /workflows/run_demo/inspection': {
        body: inspection({
          verification: {
            results: [
              {
                check: 'source_hash_valid',
                command_or_method: 'hash',
                passed: true,
                evidence: 'ok',
                error: null,
                kind: 'deterministic',
                mandatory: true,
              },
            ],
            stage: 'verify',
          },
          unresolved_issues: [{ issue_id: 'ev-2', severity: 'minor', summary: 'unactioned' }],
        }),
      },
      'GET /workflows/run_demo/events': { body: [] },
    })
    renderWithProviders(<RunDetail />, { route: '/runs/run_demo', path: '/runs/:runId' })

    expect(await screen.findByText('No check failed.')).toBeInTheDocument()
    expect(screen.queryByText('Nothing failing.')).not.toBeInTheDocument()
    expect(screen.getByText(/1 accepted issue still\s+unresolved/i)).toBeInTheDocument()
  })

  let fetchStub: ReturnType<typeof mockFetch>

  beforeEach(() => {
    installMockEventSource()
  })

  afterEach(() => {
    fetchStub?.restore()
  })

  it('renders the stage timeline and shows reviewers running from live events', async () => {
    fetchStub = mockFetch({
      'GET /workflows/run_demo/inspection': { body: inspection({ stages: stages({ ingest: 'completed' }) }) },
      'GET /workflows/run_demo': {
        body: { run: RUN, stages: stages({ ingest: 'completed', parallel_review: 'running' }), is_running: true },
      },
    })

    renderWithProviders(<RunDetail />, { route: '/runs/run_demo', path: '/runs/:runId' })

    expect(await screen.findByText('Identify unsupported claims and make only defensible edits.')).toBeInTheDocument()

    // The live stream reports two reviewers executing at the same time. The UI must show
    // both as running rather than serialising them.
    const source = MockEventSource.latest()
    expect(source).toBeDefined()
    // Payload shapes copied from what the server actually emits: the live sink reports
    // `role_key` on agent.started, while the ledger's agent_call event reports `agent_role`.
    // An earlier version of this fixture used `agent_role` for both, which made the mock
    // agree with the frontend and hid a real defect from the browser run.
    source?.emitRunEvent(3, 'agent.started', { role_key: 'evidence_auditor', model: 'gpt-5.6-luna' }, 'parallel_review')
    source?.emitRunEvent(4, 'agent.started', { role_key: 'structure_reviewer', model: 'gpt-5.6-luna' }, 'parallel_review')
    source?.emitRunEvent(5, 'agent.completed', { agent_role: 'evidence_auditor', latency_ms: 3200 }, 'parallel_review')

    await waitFor(() => expect(screen.getAllByText('Evidence Auditor').length).toBeGreaterThan(0))
    const running = document.querySelectorAll('.timeline .agent-row')
    expect(running.length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText('Structure Reviewer').length).toBeGreaterThan(0)
  })

  it('shows a blocked run as blocked, with the failing mandatory check visible', async () => {
    const blockedRun: WorkflowRunSummary = {
      ...RUN,
      status: 'blocked',
      finished_at: '2026-01-01T00:05:00+00:00',
      revision_round: 2,
    }
    fetchStub = mockFetch({
      'GET /workflows/run_demo/inspection': {
        body: inspection({
          run: blockedRun,
          stages: stages({ ingest: 'completed', plan: 'completed', verify: 'blocked' }),
          status_explanation:
            'The workflow used all 2 revision round(s) and verification still refused to pass: edit_addresses_accepted_issues.',
          verification: {
            results: [
              { check: 'target_exists', passed: true, evidence: 'file present' },
              { check: 'edit_addresses_accepted_issues', passed: false, error: 'issue ev-1 remains unresolved' },
            ],
          },
          unresolved_issues: [{ issue_id: 'ev-1', severity: 'critical', summary: 'scope mismatch in abstract' }],
        }),
      },
      'GET /workflows/run_demo': {
        body: { run: blockedRun, stages: stages({ ingest: 'completed', verify: 'blocked' }), is_running: false },
      },
    })

    renderWithProviders(<RunDetail />, { route: '/runs/run_demo', path: '/runs/:runId' })

    // Scoped to the run-level status badge: stage rows now state their own state in words
    // (a blocked stage reads "Blocked" too), so an unscoped query matches several elements.
    expect(await screen.findByText('Blocked', { selector: '[data-status] .badge' })).toBeInTheDocument()
    // Terminal states are not collapsed: a blocked run is never labelled a success.
    expect(screen.queryByText('Succeeded')).not.toBeInTheDocument()
    expect(screen.getByText(/verification still refused to pass/)).toBeInTheDocument()

    const user = userEvent.setup()
    await user.click(screen.getByRole('tab', { name: /Verification/i }))

    const failingRow = await screen.findByText('edit_addresses_accepted_issues')
    const row = failingRow.closest('tr')
    expect(row).not.toBeNull()
    expect(within(row as HTMLElement).getByText('fail')).toBeInTheDocument()
    expect(within(row as HTMLElement).getByText(/ev-1 remains unresolved/)).toBeInTheDocument()
  })

  it('keeps reviewer findings separate and shows the adversarial rejection explicitly', async () => {
    fetchStub = mockFetch({
      'GET /workflows/run_demo/inspection': {
        body: inspection({
          run: { ...RUN, status: 'succeeded' },
          stages: stages({ ingest: 'completed', adversarial_review: 'completed' }),
          reviewers: [
            {
              reviewer: 'evidence_auditor',
              issues: [{ id: 'ev-1', severity: 'critical', description: 'unsupported 97.3% claim' }],
            },
            { reviewer: 'technical_reviewer', issues: [] },
          ],
          adversarial: {
            passed: false,
            rationale: 'The conclusion still overstates the result.',
            // Structured findings, exactly as the engine sends them.
            critical_issues: [
              {
                description: 'sufficiency claim remains',
                location: 'manuscript.md, Conclusion',
                evidence: 'Sparse selection is sufficient for long-video understanding.',
                related_issue_id: 'ev-2',
              },
            ],
            major_issues: [],
            minor_issues: [],
            rejection_reasons: ['unsupported conclusion'],
          },
          diff: '--- a/manuscript.md\n+++ b/manuscript.md\n@@ -7 +7 @@\n-old claim\n+new claim\n',
          edit: { target: 'manuscript.md', changes: [{ issue_ids: ['ev-1'], location: 'Abstract', description: 'narrowed the claim' }] },
        }),
      },
      'GET /workflows/run_demo': {
        body: { run: { ...RUN, status: 'succeeded' }, stages: stages({ ingest: 'completed' }), is_running: false },
      },
    })

    renderWithProviders(<RunDetail />, { route: '/runs/run_demo', path: '/runs/:runId' })
    const user = userEvent.setup()

    await user.click(await screen.findByRole('tab', { name: /Reviewer findings/i }))
    expect(await screen.findByText('Evidence Auditor')).toBeInTheDocument()
    expect(screen.getByText('Technical Reviewer')).toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: /Adversarial/i }))
    expect(await screen.findByText(/REJECT/)).toBeInTheDocument()
    expect(screen.getByText('sufficiency claim remains')).toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: /Edit/i }))
    // The target name legitimately appears both in the edit summary and on the diff viewer.
    expect((await screen.findAllByText('manuscript.md')).length).toBeGreaterThan(0)
    expect(screen.getByText(/new claim/)).toBeInTheDocument()
  })

  it('closes the stream when the server says the run is over and never claims success itself', async () => {
    fetchStub = mockFetch({
      'GET /workflows/run_demo/inspection': { body: inspection({ run: { ...RUN, status: 'running' } }) },
      'GET /workflows/run_demo': {
        body: { run: { ...RUN, status: 'running' }, stages: stages({ ingest: 'completed' }), is_running: true },
      },
    })

    renderWithProviders(<RunDetail />, { route: '/runs/run_demo', path: '/runs/:runId' })
    await screen.findByText(/Identify unsupported claims/)

    const source = MockEventSource.latest()
    source?.emitRunEvent(9, 'workflow.failed', { status: 'failed', reason: 'provider rejected the model', rounds: 0 })
    source?.emit('stream.end', { status: 'failed', reason: 'run finished' }, '10')

    await waitFor(() => expect(screen.getByText(/run finished/)).toBeInTheDocument())
  })
})
