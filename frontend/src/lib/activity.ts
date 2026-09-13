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

export function reviewerActivity(events: RunEvent[]): AgentActivity[] {
  const byRole = new Map<string, AgentActivity>()
  for (const event of events) {
    const role = String(event.payload.agent_role ?? '')
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
