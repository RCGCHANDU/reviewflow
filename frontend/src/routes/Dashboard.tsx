import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { EmptyState, Loading, Notice, Panel, RunStatusBadge } from '../components/Status'
import { formatDuration, formatNumber, relativeTime } from '../lib/format'

/**
 * Dashboard: operational facts, not vanity metrics.
 *
 * What a user needs on opening the app is (a) what is running right now, (b) what recently
 * finished and how it ended, and (c) whether the machine can actually run a review. Status
 * is never summarised into a single "healthy" figure - a blocked run is not a failure, and
 * the dashboard must not blur them.
 */
export function Dashboard() {
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 15_000 })
  const runs = useQuery({ queryKey: ['runs', { limit: 12 }], queryFn: () => api.runs({ limit: 12 }), refetchInterval: 10_000 })
  const projects = useQuery({ queryKey: ['projects'], queryFn: api.projects })
  const providers = useQuery({ queryKey: ['providers'], queryFn: () => api.providers() })

  const running = (runs.data ?? []).filter((run) => run.status === 'running' || run.status === 'queued')
  const finished = (runs.data ?? []).filter((run) => !['running', 'queued'].includes(run.status))
  const unavailable = (providers.data?.providers ?? []).filter((provider) => !provider.available)

  return (
    <>
      <div className="page-head">
        <div className="grow">
          <h1>Dashboard</h1>
          <p>
            Independent reviewers, one controlled editor, adversarial review and verification — with the evidence for
            every stage kept.
          </p>
        </div>
        <Link className="button primary" to="/new">
          New review
        </Link>
      </div>

      {providers.data && (providers.data.providers ?? []).every((provider) => !provider.available) ? (
        <Notice tone="bad" title="No model provider is available">
          A live run cannot start. Sign in to a provider CLI, or use the scripted engine to try the application.{' '}
          <Link to="/providers">Open provider settings</Link>
        </Notice>
      ) : null}

      <div className="grid cols-3" style={{ marginBottom: 16 }}>
        <Panel title="Executor">
          <div className="row">
            <span className="mono" style={{ fontSize: 26, fontWeight: 600 }}>
              {health.data?.running_runs.length ?? 0}
            </span>
            <span className="muted small">runs in flight</span>
          </div>
          <div className="small muted" style={{ marginTop: 6 }}>
            Scripted engine {health.data?.scripted_runs_enabled ? 'enabled' : 'disabled'} · live runs{' '}
            {health.data?.live_runs_enabled ? 'enabled' : 'disabled'}
          </div>
        </Panel>

        <Panel title="Active provider">
          <div className="row">
            <strong>{providers.data?.active_provider ?? 'none'}</strong>
            <span className="muted small">requested: {providers.data?.requested_provider ?? '—'}</span>
          </div>
          <div className="row tight" style={{ marginTop: 8 }}>
            {(providers.data?.providers ?? []).map((provider) => (
              <span key={provider.name} className={`badge ${provider.available ? 'ok' : 'neutral'}`} title={provider.hint}>
                <span className="dot" aria-hidden="true" />
                {provider.name}
              </span>
            ))}
          </div>
          {unavailable.length > 0 ? (
            <div className="tiny faint" style={{ marginTop: 8 }}>
              Not signed in: {unavailable.map((provider) => provider.name).join(', ')}
            </div>
          ) : null}
        </Panel>

        <Panel title="Projects">
          <div className="row">
            <span className="mono" style={{ fontSize: 26, fontWeight: 600 }}>
              {projects.data?.length ?? 0}
            </span>
            <span className="muted small">projects</span>
          </div>
          <div className="small muted" style={{ marginTop: 6 }}>
            <Link to="/projects">Manage projects and sources</Link>
          </div>
        </Panel>
      </div>

      {running.length > 0 ? (
        <div style={{ marginBottom: 16 }}>
          <Panel title="Running now" flush>
            <table>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Objective</th>
                  <th>Stage</th>
                  <th>Status</th>
                  <th className="num">Round</th>
                </tr>
              </thead>
              <tbody>
                {running.map((run) => (
                  <tr key={run.id}>
                    <td>
                      <Link to={`/runs/${run.id}`} className="mono">
                        {run.id.slice(0, 12)}
                      </Link>
                      <div className="tiny faint">{run.project_name}</div>
                    </td>
                    <td className="small">{run.objective.slice(0, 90)}</td>
                    <td className="small muted">{run.current_stage ?? '—'}</td>
                    <td>
                      <RunStatusBadge status={run.status} />
                    </td>
                    <td className="num">{run.revision_round}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>
        </div>
      ) : null}

      <Panel
        title="Recent runs"
        flush
        actions={
          <Link className="small" to="/runs">
            All runs →
          </Link>
        }
      >
        {runs.isLoading ? (
          <Loading label="Loading runs…" />
        ) : finished.length === 0 ? (
          <EmptyState title="No finished runs yet">
            Start a review to see its stages, reviewer findings, diff, verdict and verification here.
          </EmptyState>
        ) : (
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Project</th>
                  <th>Status</th>
                  <th className="num">Rounds</th>
                  <th className="num">Calls</th>
                  <th className="num">Tokens</th>
                  <th className="num">Duration</th>
                  <th>Finished</th>
                </tr>
              </thead>
              <tbody>
                {finished.map((run) => (
                  <tr key={run.id}>
                    <td>
                      <Link to={`/runs/${run.id}`} className="mono">
                        {run.id.slice(0, 12)}
                      </Link>
                      <div className="tiny faint">{run.mode}</div>
                    </td>
                    <td className="small">{run.project_name ?? '—'}</td>
                    <td>
                      <RunStatusBadge status={run.status} />
                    </td>
                    <td className="num">{run.revision_round}</td>
                    <td className="num">{formatNumber(run.usage?.model_calls)}</td>
                    <td className="num">{formatNumber(run.usage?.total_tokens)}</td>
                    <td className="num">{formatDuration(run.usage?.wall_clock_ms)}</td>
                    <td className="small muted">{relativeTime(run.finished_at ?? run.created_at)}</td>
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
