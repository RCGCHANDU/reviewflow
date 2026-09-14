/** Formatting helpers shared by the workflow screens. */

import type { RunStatus, StageStateName } from '../api/types'

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MiB`
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  return `${minutes}m ${rest}s`
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value.toLocaleString('en-US')
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('en-GB', {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return '—'
  const then = new Date(value).getTime()
  if (Number.isNaN(then)) return value
  const seconds = Math.round((Date.now() - then) / 1000)
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}

/** Terminal states are kept distinct: "blocked" is not a synonym for "failed". */
export const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  queued: 'Queued',
  running: 'Running',
  succeeded: 'Succeeded',
  degraded: 'Degraded',
  blocked: 'Blocked',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

export const RUN_STATUS_MEANING: Record<RunStatus, string> = {
  queued: 'Accepted, waiting for the executor.',
  running: 'Stages are executing; evidence appears as each one reports.',
  succeeded: 'Every mandatory verification check passed.',
  degraded: 'Gates passed, but reviewer coverage was incomplete.',
  blocked: 'A gate refused to pass this run.',
  failed: 'No result could be established.',
  cancelled: 'Stopped at the user’s request; this is not a failure.',
}

export const STAGE_STATE_LABEL: Record<StageStateName, string> = {
  pending: 'Pending',
  running: 'Running',
  completed: 'Completed',
  warning: 'Degraded',
  blocked: 'Blocked',
  failed: 'Failed',
  retried: 'Retried',
  skipped: 'Skipped',
}

export function titleCase(value: string): string {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (character) => character.toUpperCase())
}

export function isTerminal(status: RunStatus): boolean {
  return ['succeeded', 'degraded', 'blocked', 'failed', 'cancelled'].includes(status)
}

export function severityRank(severity: string | undefined): number {
  return { critical: 0, major: 1, minor: 2, info: 3 }[String(severity ?? '').toLowerCase()] ?? 4
}
