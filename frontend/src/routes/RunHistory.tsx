import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { RunStatus } from '../api/types'
import { EmptyState, Loading, Panel, RunStatusBadge } from '../components/Status'
import { formatDuration, formatNumber, formatTimestamp, relativeTime } from '../lib/format'

const STATUS_FILTERS: (RunStatus | 'all')[] = ['all', 'succeeded', 'blocked', 'degraded', 'failed', 'cancelled', 'running']

/**
 * Run history.
 *
 * Opening a historical run must never re-execute anything, so this page only reads. Reruns
 * are explicit: the detail page offers a "run again" action that creates a *new* run and
 * leaves this one untouched.
 */
export function RunHistory() {
  const [params, setParams] = useSearchParams()
  const projectId = params.get('project') ?? undefined
  const status = (params.get('status') as RunStatus | null) ?? undefined
  const [limit, setLimit] = useState(50)

  const runs = useQuery({
    queryKey: ['runs', { projectId, status, limit }],
    queryFn: () => api.runs({ projectId, status, limit }),
  })
  const projects = useQuery({ queryKey: ['projects'], queryFn: api.projects })

  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value === '' || value === 'all') next.delete(key)
    else next.set(key, value)
    setParams(next, { replace: true })
  }

  return (
    <>
      <div className="page-head">
        <div className="grow">
          <h1>Runs</h1>
          <p>Every workflow execution, its configuration snapshot and its honest terminal state.</p>
        </div>
        <Link className="button primary" to="/new">
          New review
        </Link>
      </div>

      <div className="row" style={{ marginBottom: 12 }}>
        <label htmlFor="status-filter" style={{ margin: 0 }}>
          Status
        </label>
        <select
          id="status-filter"
          value={status ?? 'all'}
          onChange={(event) => update('status', event.target.value)}
          style={{ width: 160 }}
        >
          {STATUS_FILTERS.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>

        <label htmlFor="project-filter" style={{ margin: 0 }}>
          Project
        </label>
        <select
          id="project-filter"
          value={projectId ?? ''}
          onChange={(event) => update('project', event.target.value)}
          style={{ width: 220 }}
        >
          <option value="">all</option>
          {(projects.data ?? []).map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>

        <label htmlFor="limit-filter" style={{ margin: 0 }}>
          Limit
        </label>
        <select
          id="limit-filter"
          value={String(limit)}
          onChange={(event) => setLimit(Number(event.target.value))}
          style={{ width: 100 }}
        >
          {[25, 50, 100, 200].map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </div>

      <Panel flush>
        {runs.isLoading ? (
          <Loading label="Loading run history…" />
        ) : runs.isError ? (
          <EmptyState title="Could not load runs">{String(runs.error)}</EmptyState>
        ) : (runs.data ?? []).length === 0 ? (
          <EmptyState title="No runs match this filter" />
        ) : (
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Project / source</th>
                  <th>Objective</th>
                  <th>Status</th>
                  <th>Provider</th>
                  <th className="num">Rounds</th>
                  <th className="num">Calls</th>
                  <th className="num">Tokens</th>
                  <th className="num">Duration</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody>
                {(runs.data ?? []).map((run) => (
                  <tr key={run.id}>
                    <td>
                      <Link to={`/runs/${run.id}`} className="mono">
                        {run.id.slice(0, 14)}
                      </Link>
                      <div className="tiny faint">{run.mode}</div>
                    </td>
                    <td className="small">
                      {run.project_name ?? '—'}
                      <div className="tiny faint">{run.source_filename ?? ''}</div>
                    </td>
                    <td className="small" style={{ maxWidth: 320 }}>
                      {run.objective.slice(0, 120)}
                    </td>
                    <td>
                      <RunStatusBadge status={run.status} />
                    </td>
                    <td className="small muted">
                      {String(run.provider_snapshot.provider ?? '—')}
                      <div className="tiny faint">{String(run.provider_snapshot.transport ?? '')}</div>
                    </td>
                    <td className="num">{run.revision_round}</td>
                    <td className="num">{formatNumber(run.usage?.model_calls)}</td>
                    <td className="num">{formatNumber(run.usage?.total_tokens)}</td>
                    <td className="num">{formatDuration(run.usage?.wall_clock_ms)}</td>
                    <td className="small muted" title={formatTimestamp(run.started_at)}>
                      {relativeTime(run.started_at ?? run.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  )
}
