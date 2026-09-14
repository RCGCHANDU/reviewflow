import type { ReactNode } from 'react'

import type { RunStatus, StageStateName } from '../api/types'
import { RUN_STATUS_LABEL, RUN_STATUS_MEANING, STAGE_STATE_LABEL } from '../lib/format'

/**
 * Status is the primary semantic in this product, so it gets one component with one
 * vocabulary. Terminal states never collapse into a single "done": blocked, degraded,
 * failed and cancelled each keep their own colour and wording, and each carries a title
 * explaining what it means - the UI must not imply success a run did not achieve.
 */

type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'neutral'

const RUN_STATUS_TONE: Record<RunStatus, Tone> = {
  queued: 'neutral',
  running: 'info',
  succeeded: 'ok',
  degraded: 'warn',
  blocked: 'bad',
  failed: 'bad',
  cancelled: 'neutral',
}

const STAGE_TONE: Record<StageStateName, Tone> = {
  pending: 'neutral',
  running: 'info',
  completed: 'ok',
  warning: 'warn',
  blocked: 'bad',
  failed: 'bad',
  retried: 'info',
  skipped: 'neutral',
}

export function Badge({ tone, children, pulse = false }: { tone: Tone; children: ReactNode; pulse?: boolean }) {
  return (
    <span className={`badge ${tone}${pulse ? ' running' : ''}`}>
      <span className="dot" aria-hidden="true" />
      {children}
    </span>
  )
}

export function RunStatusBadge({ status }: { status: RunStatus }) {
  return (
    <span title={RUN_STATUS_MEANING[status]} data-status={status}>
      <Badge tone={RUN_STATUS_TONE[status]} pulse={status === 'running'}>
        {RUN_STATUS_LABEL[status]}
      </Badge>
    </span>
  )
}

export function StageStateBadge({ state }: { state: StageStateName }) {
  return (
    <Badge tone={STAGE_TONE[state]} pulse={state === 'running'}>
      {STAGE_STATE_LABEL[state]}
    </Badge>
  )
}

export function SeverityBadge({ severity }: { severity: string | undefined }) {
  const value = String(severity ?? 'unknown').toLowerCase()
  const tone: Tone = value === 'critical' ? 'bad' : value === 'major' ? 'warn' : 'neutral'
  return <Badge tone={tone}>{value}</Badge>
}

export function PassBadge({ passed }: { passed: boolean }) {
  return <Badge tone={passed ? 'ok' : 'bad'}>{passed ? 'pass' : 'fail'}</Badge>
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="row tight">
      <span className="spinner" aria-hidden="true" />
      {label ? <span className="muted small">{label}</span> : null}
      <span className="sr-only" role="status">
        {label ?? 'Loading'}
      </span>
    </span>
  )
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="skeleton-stack" aria-hidden="true">
      {Array.from({ length: rows }).map((_, index) => (
        <div className="skeleton" key={index} />
      ))}
    </div>
  )
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="loading">
      <Spinner label={label} />
    </div>
  )
}

export function Notice({
  tone = 'neutral',
  title,
  role,
  children,
}: {
  tone?: 'neutral' | 'ok' | 'warn' | 'bad'
  title?: string
  role?: 'status' | 'alert'
  children?: ReactNode
}) {
  return (
    <div className={`notice ${tone === 'neutral' ? '' : tone}`} role={role ?? (tone === 'bad' ? 'alert' : undefined)}>
      <div className="notice-body">
        {title ? <strong>{title}</strong> : null}
        {children}
      </div>
    </div>
  )
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {children}
    </div>
  )
}

export function Panel({
  title,
  actions,
  children,
  flush = false,
}: {
  title?: string
  actions?: ReactNode
  children: ReactNode
  flush?: boolean
}) {
  return (
    <section className="panel">
      {title || actions ? (
        <header>
          {title ? <h2>{title}</h2> : null}
          <span className="spacer" />
          {actions}
        </header>
      ) : null}
      <div className={`panel-body${flush ? ' flush' : ''}`}>{children}</div>
    </section>
  )
}

export function KeyValues({ items }: { items: { label: string; value: ReactNode }[] }) {
  return (
    <dl className="kv">
      {items.map((item) => (
        <div key={item.label} style={{ display: 'contents' }}>
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  )
}
