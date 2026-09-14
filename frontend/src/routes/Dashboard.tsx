import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { EmptyState, Loading, Notice, Panel, RunStatusBadge } from '../components/Status'
import { formatDuration, formatNumber, relativeTime } from '../lib/format'

const ATTENTION_STATUSES = ['blocked', 'degraded', 'failed']

/**
 * Dashboard: an operational launchpad, not an analytics page.
 *
 * Order is the order a user needs: what is waiting on a decision, what is running right
 * now, then history. No aggregate "healthy" figure appears anywhere - a blocked run, a
 * failed run and a degraded run stay distinguishable, and each attention item states the
 * reason it is there.
 */
export function Dashboard() {
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 15_000 })
  const runs = useQuery({ queryKey: ['runs', { limit: 20 }], queryFn: () => api.runs({ limit: 20 }), refetchInterval: 10_000 })
  const projects = useQuery({ queryKey: ['projects'], queryFn: api.projects })
  const providers = useQuery({ queryKey: ['providers'], queryFn: () => api.providers() })

  const all = runs.data ?? []
  const running = all.filter((run) => run.status === 'running' || run.status === 'queued')
  const finished = all.filter((run) => !['running', 'queued'].includes(run.status))
  const attentionAll = finished.filter((run) => ATTENTION_STATUSES.includes(run.status))
  const attention = attentionAll.slice(0, 5)
  const attentionIds = new Set(attention.map((run) => run.id))
  const recent = finished.filter((run) => !attentionIds.has(run.id)).slice(0, 10)
  const unavailable = (providers.data?.providers ?? []).filter((provider) => !provider.available)
  const noProvider = providers.data ? (providers.data.providers ?? []).every((provider) => !provider.available) : false

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

      {runs.isError ? (
        // Silence is not an empty state: with the runs request failing, an empty dashboard
        // hid two blocked runs and said nothing was wrong (A8-01).
        <Notice tone="bad" title="Could not load runs">
          The run history could not be fetched, so this page cannot show what is running or what needs a decision.{' '}
          <button type="button" className="linklike" onClick={() => void runs.refetch()}>
            Try again
          </button>
        </Notice>
      ) : null}

      {noProvider ? (
        <Notice tone="bad" title="No model provider is available">
          A live run cannot start. Sign in to a provider CLI, or use the scripted engine to try the application.{' '}
          <Link to="/providers">Open provider settings</Link>
        </Notice>
      ) : null}

      {!runs.isError && attention.length > 0 ? (
        <section style={{ marginBottom: 16 }}>
          <div className="row" style={{ marginBottom: 10 }}>
            <h2>Waiting on you</h2>
            <span className="small muted">
              {attentionAll.length} {attentionAll.length === 1 ? 'run needs' : 'runs need'} a decision
              {attentionAll.length > attention.length ? ` · showing the ${attention.length} most recent` : ''}
            </span>
          </div>
          <div className="attention">
            {attention.map((run) => (
              <article key={run.id} className="attention-item" data-status={run.status}>
                <div className="grow">
                  <div className="row tight" style={{ marginBottom: 4 }}>
                    <RunStatusBadge status={run.status} />
                    <Link className="mono small" to={`/runs/${run.id}`}>
                      {run.id.slice(0, 14)}
                    </Link>
                    <span className="tiny faint">{run.project_name ?? '—'}</span>
                    <span className="tiny faint">{relativeTime(run.finished_at ?? run.created_at)}</span>
                  </div>
                  <div className="small" style={{ marginBottom: 2 }}>
                    {run.objective.slice(0, 140)}
                  </div>
                  {run.failure_reason ? <div className="why tiny">{run.failure_reason}</div> : null}
                </div>
                <Link className="button" to={`/runs/${run.id}`}>
                  Inspect
                </Link>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {running.length > 0 ? (
        <div style={{ marginBottom: 16 }}>
          <Panel title="Running now" flush>
            <div className="scroll-x">
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
            </div>
          </Panel>
        </div>
      ) : null}

      <div className="dash-grid">
        <Panel
          title="Recent activity"
          flush
          actions={
            <Link className="small" to="/runs">
              All runs →
            </Link>
          }
        >
          {runs.isLoading ? (
            <Loading label="Loading runs…" />
          ) : runs.isError ? (
            <EmptyState title="Run history unavailable">
              The request for runs failed. This is not an empty history — reload or try again above.
            </EmptyState>
          ) : recent.length === 0 ? (
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
                  {recent.map((run) => (
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

        <div className="grid">
          <Panel title="Provider health">
            <dl className="kv">
              <dt>Active</dt>
              <dd>{providers.data?.active_provider ?? 'none'}</dd>
              <dt>Requested</dt>
              <dd>{providers.data?.requested_provider ?? '—'}</dd>
            </dl>
            <div className="row tight" style={{ marginTop: 10 }}>
              {(providers.data?.providers ?? []).map((provider) => (
                <span
                  key={provider.name}
                  className={`badge ${provider.available ? 'ok' : 'warn'}`}
                  title={provider.hint}
                >
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

          <Panel title="Executor">
            <dl className="kv">
              <dt>Runs in flight</dt>
              <dd>{health.data?.running_runs.length ?? 0}</dd>
              <dt>Live runs</dt>
              <dd>
                <span className={`badge ${health.data?.live_runs_enabled ? 'ok' : 'neutral'}`}>
                  {health.data?.live_runs_enabled ? 'enabled' : 'disabled'}
                </span>
              </dd>
              <dt>Scripted engine</dt>
              <dd>
                <span className={`badge ${health.data?.scripted_runs_enabled ? 'ok' : 'neutral'}`}>
                  {health.data?.scripted_runs_enabled ? 'enabled' : 'disabled'}
                </span>
              </dd>
            </dl>
          </Panel>

          <Panel title="Projects">
            <dl className="kv">
              <dt>Projects</dt>
              <dd>{projects.data?.length ?? 0}</dd>
            </dl>
            <div className="small" style={{ marginTop: 8 }}>
              <Link to="/projects">Manage projects and sources</Link>
            </div>
          </Panel>
        </div>
      </div>
    </>
  )
}
