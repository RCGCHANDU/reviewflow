import type { RunEvent, StageState } from '../api/types'
import { reviewerActivity } from '../lib/activity'
import { formatDuration, formatTimestamp, titleCase } from '../lib/format'

import { EmptyState, StageStateBadge } from './Status'

/**
 * The stage timeline.
 *
 * Stages come from the backend in engine order, with a derived state. Under PARALLEL
 * REVIEW the individual reviewer executions are shown from the event stream, because the
 * four reviewers genuinely run at once - showing them as a single sequential step would
 * misrepresent the design that makes their findings independent.
 */

function summarise(detail: Record<string, unknown>): string {
  const parts: string[] = []
  for (const [key, value] of Object.entries(detail)) {
    if (value === null || value === undefined) continue
    if (Array.isArray(value)) {
      if (value.length === 0) continue
      parts.push(`${titleCase(key)}: ${value.slice(0, 4).join(', ')}${value.length > 4 ? ` +${value.length - 4}` : ''}`)
    } else if (typeof value === 'object') {
      const nested = Object.entries(value as Record<string, unknown>)
        .filter(([, item]) => item !== null && item !== undefined)
        .map(([k, item]) => `${k}=${String(item)}`)
      if (nested.length) parts.push(`${titleCase(key)}: ${nested.join(' ')}`)
    } else {
      parts.push(`${titleCase(key)}: ${String(value)}`)
    }
  }
  return parts.join(' · ')
}

const STATE_MARK: Record<string, string> = {
  retried: '↻',
  pending: '·',
  running: '▶',
  completed: '✓',
  warning: '!',
  blocked: '■',
  failed: '✕',
  skipped: '–',
}

export function StageTimeline({
  stages,
  events,
  showReviewers = true,
}: {
  stages: StageState[]
  events: RunEvent[]
  showReviewers?: boolean
}) {
  if (stages.length === 0) {
    return <EmptyState title="No stages recorded yet">The run has not reported any stage activity.</EmptyState>
  }
  const reviewStage = stages.find((stage) => stage.stage === 'parallel_review')
  const agents = reviewerActivity(events)

  return (
    <ol className="timeline">
      {stages.map((stage) => {
        const isReview = stage.stage === 'parallel_review'
        const showAgents = showReviewers && isReview && agents.length > 0
        return (
          <li key={stage.stage} className={stage.state}>
            <span className="marker" aria-hidden="true">
              {STATE_MARK[stage.state] ?? '·'}
            </span>
            <div>
              <div className="stage-name">
                {titleCase(stage.stage)} {showAgents ? <span className="faint tiny">({agents.length} agents)</span> : null}
              </div>
              <div className="stage-detail">{summarise(stage.detail)}</div>
              {showAgents ? (
                <div style={{ marginTop: 8, border: '1px solid var(--border)', borderRadius: 'var(--radius)' }}>
                  {agents.map((agent) => (
                    <div className="agent-row" key={agent.role}>
                      <span>{titleCase(agent.role)}</span>
                      <span className="faint tiny">{agent.model ?? ''}</span>
                      <span>
                        <StageStateBadge
                          state={
                            agent.state === 'running' ? 'running' : agent.state === 'failed' ? 'failed' : 'completed'
                          }
                        />
                        {agent.latencyMs !== undefined ? (
                          <span className="faint tiny" style={{ marginLeft: 8 }}>
                            {formatDuration(agent.latencyMs)}
                          </span>
                        ) : null}
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
            <div className="stage-meta">
              {stage.started_at ? <div>{formatTimestamp(stage.started_at)}</div> : null}
              <div>{stage.state === 'running' ? 'in progress' : ''}</div>
              {reviewStage && showAgents && stage.state !== 'running' ? (
                <div className="faint">concurrent</div>
              ) : null}
            </div>
          </li>
        )
      })}
    </ol>
  )
}
