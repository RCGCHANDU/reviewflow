/**
 * Reviewer activity derived from the live event stream.
 *
 * Under PARALLEL REVIEW the four reviewers genuinely execute at once (bounded by the
 * configured concurrency), so which ones are running is read from their own start/complete
 * events rather than inferred from the stage being active.
 */

import type { RunEvent } from '../api/types'

interface AgentActivity {
  role: string
  state: 'running' | 'completed' | 'failed'
  model?: string
  latencyMs?: number
  attempts?: number
}

const REVIEWER_ROLES = new Set([
  'evidence_auditor',
  'technical_reviewer',
  'rhetorical_analyst',
  'structure_reviewer',
])

export function reviewerActivity(events: RunEvent[]): AgentActivity[] {
  const byRole = new Map<string, AgentActivity>()
  for (const event of events) {
    // Scoped to the stage that actually runs reviewers concurrently. Without this
    // filter every agent in the run (orchestrator, editor, diff reviewer,
    // adversarial reviewer, verifier) was listed as a member of the parallel
    // review board, so "Parallel Review (8 agents)" counted agents that never ran
    // in that stage.
    const stage = typeof event.stage === 'string' ? event.stage : ''
    const roleKey = String(event.payload.agent_role ?? event.payload.role_key ?? '')
    if (stage && stage !== 'parallel_review') continue
    if (!stage && !REVIEWER_ROLES.has(roleKey)) continue
    // The engine's live sink reports `role_key` on agent.started; the ledger's agent_call
    // event reports `agent_role`. Reading only one of them meant the running state never
    // appeared in the real application while the unit test (whose fixture used the other
    // key) passed.
    const role = String(event.payload.agent_role ?? event.payload.role_key ?? '')
    if (!role) continue
    if (event.type === 'agent.started') {
      byRole.set(role, {
        role,
        state: 'running',
        model: typeof event.payload.model === 'string' ? event.payload.model : undefined,
        attempts: typeof event.payload.attempt === 'number' ? event.payload.attempt : undefined,
      })
    } else if (event.type === 'agent.completed') {
      const existing = byRole.get(role)
      byRole.set(role, {
        role,
        // A reviewer whose report was rejected still completed its model call; the report
        // level failure is reported separately below.
        state: 'completed',
        model: typeof event.payload.model === 'string' ? event.payload.model : existing?.model,
        latencyMs: typeof event.payload.latency_ms === 'number' ? event.payload.latency_ms : undefined,
        attempts: typeof event.payload.attempt === 'number' ? event.payload.attempt : undefined,
      })
    } else if (event.type === 'agent.failed') {
      const existing = byRole.get(role)
      byRole.set(role, {
        role,
        state: 'failed',
        model: existing?.model,
        attempts: typeof event.payload.attempt === 'number' ? event.payload.attempt : existing?.attempts,
      })
    }
  }
  return [...byRole.values()].sort((a, b) => a.role.localeCompare(b.role))
}
