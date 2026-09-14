import { describe, expect, it } from 'vitest'

import type { RunEvent } from '../api/types'
import { reviewerActivity } from './activity'

function event(seq: number, type: string, payload: Record<string, unknown>): RunEvent {
  return {
    id: seq,
    run_id: 'run_test',
    seq,
    timestamp: '2026-01-01T00:00:00.000Z',
    type,
    stage: 'parallel_review',
    payload,
  }
}

describe('reviewerActivity', () => {
  it('shows several reviewers running at the same time', () => {
    // The engine starts reviewers concurrently, so the UI must be able to show more than
    // one in the running state - a sequential display would misrepresent the design.
    const activity = reviewerActivity([
      event(1, 'agent.started', { agent_role: 'evidence_auditor', model: 'm-a' }),
      event(2, 'agent.started', { agent_role: 'technical_reviewer', model: 'm-a' }),
      event(3, 'agent.started', { agent_role: 'structure_reviewer', model: 'm-b' }),
      event(4, 'agent.completed', { agent_role: 'technical_reviewer', latency_ms: 4200 }),
    ])

    const running = activity.filter((item) => item.state === 'running').map((item) => item.role)
    expect(running).toEqual(['evidence_auditor', 'structure_reviewer'])
    const finished = activity.find((item) => item.role === 'technical_reviewer')
    expect(finished?.state).toBe('completed')
    expect(finished?.latencyMs).toBe(4200)
  })

  it('records a reviewer that failed without losing the others', () => {
    const activity = reviewerActivity([
      event(1, 'agent.started', { agent_role: 'evidence_auditor' }),
      event(2, 'agent.started', { agent_role: 'rhetorical_analyst' }),
      event(3, 'agent.failed', { agent_role: 'evidence_auditor' }),
      event(4, 'agent.completed', { agent_role: 'rhetorical_analyst', latency_ms: 10 }),
    ])
    expect(activity.find((item) => item.role === 'evidence_auditor')?.state).toBe('failed')
    expect(activity.find((item) => item.role === 'rhetorical_analyst')?.state).toBe('completed')
  })

  it('reads the key the live sink emits, not only the ledger key', () => {
    // Regression: the running state never appeared in the real application because
    // agent.started carries `role_key` (live sink) while agent_call carries `agent_role`
    // (ledger). The browser E2E run caught it; this pins it.
    const agents = reviewerActivity([
      event(1, 'agent.started', { role_key: 'evidence_auditor', model: 'gpt-5.6-luna' }),
      event(2, 'agent.started', { role_key: 'technical_reviewer', model: 'gpt-5.6-luna' }),
    ])
    const byRole = new Map(agents.map((agent) => [agent.role, agent]))
    expect(byRole.get('evidence_auditor')?.state).toBe('running')
    expect(byRole.get('technical_reviewer')?.state).toBe('running')
  })

  it('ignores events that do not identify an agent', () => {
    const activity = reviewerActivity([
      event(1, 'stage.started', {}),
      event(2, 'workflow.started', { mode: 'scripted' }),
    ])
    expect(activity).toHaveLength(0)
  })
})
