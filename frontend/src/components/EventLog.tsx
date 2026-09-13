import { useEffect, useRef } from 'react'

import type { RunEvent } from '../api/types'
import { titleCase } from '../lib/format'

/**
 * Raw workflow event log.
 *
 * Structured workflow facts, not chat: one dense row per event with the sequence number
 * (the same checkpoint the server replays from), its type, the stage, and a one-line
 * summary of the payload. This is the screen that makes "what actually happened" auditable
 * without reading a transcript.
 */

const ICON: Record<string, string> = {
  'workflow.created': '◦',
  'workflow.started': '▶',
  'workflow.succeeded': '✓',
  'workflow.blocked': '■',
  'workflow.degraded': '!',
  'workflow.failed': '✕',
  'workflow.cancelled': '⏹',
  'stage.started': '▶',
  'stage.completed': '✓',
  'stage.transition': '→',
  'agent.started': '▷',
  'agent.completed': '✓',
  'agent.failed': '✕',
  'agent.retried': '↻',
  'review.report_received': '📄',
  'synthesis.completed': '⚖',
  'edit.completed': '✎',
  'adversarial.completed': '⚔',
  'verification.completed': '☑',
  'verification.check': '☑',
  'guardrail.triggered': '⛔',
  'tool.called': '⚙',
}

function summarise(event: RunEvent): string {
  const payload = event.payload
  const parts: string[] = []
  const push = (label: string, value: unknown) => {
    if (value === null || value === undefined || value === '') return
    if (Array.isArray(value)) {
      if (value.length === 0) return
      parts.push(`${label}: ${value.length}`)
      return
    }
    if (typeof value === 'object') {
      const entries = Object.entries(value as Record<string, unknown>).filter(([, item]) => item !== null && item !== undefined)
      if (entries.length) parts.push(`${label}: ${entries.map(([k, item]) => `${k}=${String(item)}`).join(' ')}`)
      return
    }
    parts.push(`${label}: ${String(value)}`)
  }
  if (typeof payload.agent_role === 'string' && payload.agent_role) parts.push(payload.agent_role)
  push('model', payload.model)
  push('latency_ms', payload.latency_ms)
  if (typeof payload.input_tokens === 'number' || typeof payload.output_tokens === 'number') {
    parts.push(`${Number(payload.input_tokens ?? 0) + Number(payload.output_tokens ?? 0)} tok`)
  }
  push('errors', payload.errors)
  push('reason', payload.reason)
  push('detail', payload.detail)
  return parts.join(' · ')
}

export function EventLog({ events, follow = true }: { events: RunEvent[]; follow?: boolean }) {
  const container = useRef<HTMLDivElement>(null)
  const atBottom = useRef(true)

  useEffect(() => {
    if (!follow || !container.current || !atBottom.current) return
    container.current.scrollTop = container.current.scrollHeight
  }, [events, follow])

  return (
    <div
      className="event-log"
      ref={container}
      onScroll={(event) => {
        const element = event.currentTarget
        atBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 40
      }}
      aria-label="Workflow events"
      role="log"
    >
      {events.map((event) => (
        <div className={`event-row${event.type.startsWith('workflow.') ? ' terminal' : ''}`} key={event.seq}>
          <span className="seq">{event.seq}</span>
          <span className="type">
            <span aria-hidden="true">{ICON[event.type] ?? '•'}</span> {event.type}
          </span>
          <span className="stage">{event.stage ? titleCase(event.stage) : ''}</span>
          <span className="summary" title={summarise(event)}>
            {summarise(event)}
          </span>
        </div>
      ))}
    </div>
  )
}
