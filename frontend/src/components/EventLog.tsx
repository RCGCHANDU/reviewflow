import { useEffect, useMemo, useRef, useState } from 'react'

import type { RunEvent } from '../api/types'
import { titleCase } from '../lib/format'

/**
 * Workflow activity console.
 *
 * This is an audit surface, not a chat transcript: the user can follow live activity,
 * pause it to inspect history, filter by event family, search the rendered facts, and
 * open one event to inspect its complete payload. The event stream remains the source
 * of truth; the component adds no inferred progress.
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
  'review.report_received': '▤',
  'synthesis.completed': '⚖',
  'edit.completed': '✎',
  'adversarial.completed': '⚔',
  'verification.completed': '☑',
  'verification.check': '☑',
  'guardrail.triggered': '⛔',
  'tool.called': '⚙',
}

const FAMILY: Record<string, string> = {
  workflow: 'Workflow',
  stage: 'Stages',
  agent: 'Agents',
  verification: 'Verification',
  review: 'Review',
  synthesis: 'Synthesis',
  edit: 'Editing',
  adversarial: 'Adversarial',
  guardrail: 'Safety',
  tool: 'Tools',
}

type EventFamily = 'all' | 'workflow' | 'stage' | 'agent' | 'verification' | 'review' | 'synthesis' | 'edit' | 'adversarial' | 'guardrail' | 'tool'

function familyOf(type: string): EventFamily {
  const prefix = type.split('.')[0]
  return prefix in FAMILY ? (prefix as EventFamily) : 'all'
}

function summarise(event: RunEvent): string {
  const payload = event.payload
  const parts: string[] = []
  const push = (label: string, value: unknown) => {
    if (value === null || value === undefined || value === '') return
    if (Array.isArray(value)) {
      if (value.length) parts.push(`${label}: ${value.length}`)
      return
    }
    if (typeof value === 'object') {
      const entries = Object.entries(value as Record<string, unknown>).filter(([, item]) => item !== null && item !== undefined)
      if (entries.length) parts.push(`${label}: ${entries.map(([k, item]) => `${k}=${String(item)}`).join(' ')}`)
      return
    }
    parts.push(`${label}: ${String(value)}`)
  }
  if (typeof payload.agent_role === 'string' && payload.agent_role) parts.push(titleCase(payload.agent_role))
  push('model', payload.model)
  push('latency', payload.latency_ms ? `${payload.latency_ms}ms` : undefined)
  if (typeof payload.input_tokens === 'number' || typeof payload.output_tokens === 'number') {
    parts.push(`${Number(payload.input_tokens ?? 0) + Number(payload.output_tokens ?? 0)} tok`)
  }
  push('errors', payload.errors)
  push('reason', payload.reason)
  push('detail', payload.detail)
  return parts.join(' · ')
}

function isTerminal(type: string): boolean {
  return type === 'workflow.terminal' || type.startsWith('workflow.') || type === 'guardrail.triggered'
}

export function EventLog({ events, follow: initialFollow = true }: { events: RunEvent[]; follow?: boolean }) {
  const container = useRef<HTMLDivElement>(null)
  const [follow, setFollow] = useState(initialFollow)
  const [family, setFamily] = useState<EventFamily>('all')
  const [query, setQuery] = useState('')
  const [selectedSeq, setSelectedSeq] = useState<number | null>(null)
  const [newestSeq, setNewestSeq] = useState<number | null>(null)
  const previousLast = useRef<number | null>(null)

  useEffect(() => {
    const last = events.at(-1)?.seq ?? null
    if (last !== null && previousLast.current !== null && last !== previousLast.current) {
      setNewestSeq(last)
      const timer = window.setTimeout(() => setNewestSeq(null), 1200)
      return () => window.clearTimeout(timer)
    }
    previousLast.current = last
    return undefined
  }, [events])

  useEffect(() => {
    if (!follow || !container.current) return
    container.current.scrollTop = container.current.scrollHeight
  }, [events, follow])

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return events.filter((event) => {
      if (family !== 'all' && familyOf(event.type) !== family) return false
      if (!needle) return true
      return `${event.type} ${event.stage ?? ''} ${summarise(event)}`.toLowerCase().includes(needle)
    })
  }, [events, family, query])

  const selected = events.find((event) => event.seq === selectedSeq) ?? null
  const familyCounts = useMemo(() => {
    const counts: Record<string, number> = { all: events.length }
    events.forEach((event) => {
      const key = familyOf(event.type)
      counts[key] = (counts[key] ?? 0) + 1
    })
    return counts
  }, [events])

  return (
    <section className="activity-console" aria-label="Workflow activity">
      <div className="activity-toolbar">
        <div className="activity-live">
          <span className="activity-signal" aria-hidden="true" />
          <div>
            <strong>Activity</strong>
            <span className="tiny faint">{events.length} recorded events</span>
          </div>
        </div>
        <button
          type="button"
          className={`activity-follow ${follow ? 'is-on' : ''}`}
          aria-pressed={follow}
          onClick={() => setFollow((value) => !value)}
        >
          <span aria-hidden="true">{follow ? '◉' : '○'}</span> {follow ? 'Following live' : 'Follow paused'}
        </button>
      </div>

      <div className="activity-controls">
        <label className="activity-search">
          <span className="sr-only">Search workflow events</span>
          <span aria-hidden="true">⌕</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search events…" />
          {query ? <button type="button" aria-label="Clear event search" onClick={() => setQuery('')}>×</button> : null}
        </label>
        <div className="activity-filters" role="group" aria-label="Filter workflow events">
          {(Object.keys(FAMILY) as Array<Exclude<EventFamily, 'all'>>).map((key) => (
            <button
              key={key}
              type="button"
              className={family === key ? 'is-active' : ''}
              aria-pressed={family === key}
              onClick={() => setFamily((current) => (current === key ? 'all' : key))}
            >
              {FAMILY[key]} <span>{familyCounts[key] ?? 0}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="activity-body">
        <div
          className="activity-list"
          ref={container}
          role="log"
          aria-label="Workflow event stream"
          aria-live="polite"
          aria-busy={follow && events.length === 0}
        >
          {filtered.map((event) => {
            const terminal = isTerminal(event.type)
            const active = selectedSeq === event.seq
            const familyKey = familyOf(event.type)
            return (
              <button
                type="button"
                className={`activity-row ${terminal ? 'is-terminal' : ''} ${active ? 'is-selected' : ''} ${newestSeq === event.seq ? 'is-new' : ''}`}
                key={event.seq}
                onClick={() => setSelectedSeq(event.seq)}
                aria-pressed={active}
              >
                <span className="activity-seq">{event.seq}</span>
                <span className={`activity-icon family-${familyKey}`} aria-hidden="true">{ICON[event.type] ?? '•'}</span>
                <span className="activity-main">
                  <span className="activity-type">{event.type}</span>
                  <span className="activity-summary">{summarise(event) || 'Event recorded'}</span>
                </span>
                <span className="activity-stage">{event.stage ? titleCase(event.stage) : FAMILY[familyKey]}</span>
              </button>
            )
          })}
          {filtered.length === 0 ? <div className="activity-empty">No events match this view.</div> : null}
        </div>

        {selected ? (
          <aside className="activity-inspector" aria-label={`Event ${selected.seq} details`}>
            <div className="activity-inspector-head">
              <div>
                <span className="tiny faint">EVENT {selected.seq}</span>
                <h3>{selected.type}</h3>
              </div>
              <button type="button" aria-label="Close event details" onClick={() => setSelectedSeq(null)}>×</button>
            </div>
            <dl className="activity-meta">
              <dt>Stage</dt><dd>{selected.stage ? titleCase(selected.stage) : '—'}</dd>
              <dt>Sequence</dt><dd>{selected.seq}</dd>
              <dt>Timestamp</dt><dd>{selected.timestamp}</dd>
            </dl>
            <pre className="activity-payload">{JSON.stringify(selected.payload, null, 2)}</pre>
          </aside>
        ) : (
          <aside className="activity-inspector activity-inspector-empty">
            <span className="activity-inspector-mark" aria-hidden="true">⌁</span>
            <strong>Select an event</strong>
            <span className="tiny muted">Inspect the recorded payload without leaving the run.</span>
          </aside>
        )}
      </div>
    </section>
  )
}
