import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { LimitsConfig, PreflightResult, RunConfigSnapshot, RunMode } from '../api/types'
import { Badge, EmptyState, Loading, Notice, Panel } from '../components/Status'
import { titleCase } from '../lib/format'

const DEFAULT_LIMITS: LimitsConfig = {
  max_revision_rounds: 2,
  max_turns_per_agent: 8,
  max_reviewer_concurrency: 4,
  agent_timeout_s: 120,
  max_transient_retries: 2,
  max_invalid_output_retries: 1,
  max_total_tokens: 400000,
  min_successful_reviewers: 2,
  semantic_checks: null,
}

const OBJECTIVE_PRESETS = [
  'Identify unsupported or overstated claims and fix the smallest defensible set without weakening verified content.',
  'Technically review this design: methodology, assumptions, comparisons and whether the conclusions are warranted. Make only defensible edits.',
  'Improve clarity and information flow without changing any factual claim.',
  'Find internal inconsistencies and cross-section contradictions, and reconcile only what the material supports.',
]

/**
 * New review.
 *
 * One focused form rather than a wizard: the decisions are few and interdependent (the
 * provider choice determines which models exist; the limits determine how much the run may
 * spend), and hiding them behind steps makes them harder to reason about.
 *
 * Two rules this screen enforces:
 *   * every option comes from the backend - providers, models, roles and even the list of
 *     choices are discovered, so the UI cannot offer something the engine will reject;
 *   * a run is not started until preflight passes, so an impossible or unaffordable run is
 *     never submitted.
 */
export function NewReview() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [projectId, setProjectId] = useState(params.get('project') ?? '')
  const [sourceId, setSourceId] = useState('')
  const [objective, setObjective] = useState('')
  const [mode, setMode] = useState<RunMode>('scripted')
  const [provider, setProvider] = useState('auto')
  const [roleModels, setRoleModels] = useState<Record<string, string>>({})
  const [limits, setLimits] = useState<LimitsConfig>(DEFAULT_LIMITS)
  const [scenario, setScenario] = useState('successful_end_to_end')
  const [preflight, setPreflight] = useState<PreflightResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const projects = useQuery({ queryKey: ['projects'], queryFn: api.projects })
  const projectDetail = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.project(projectId),
    enabled: Boolean(projectId),
  })
  const roles = useQuery({ queryKey: ['roles'], queryFn: api.roles })
  const providers = useQuery({ queryKey: ['providers'], queryFn: () => api.providers() })
  const models = useQuery({ queryKey: ['models', { catalogue: false }], queryFn: () => api.models() })
  const samples = useQuery({ queryKey: ['samples'], queryFn: api.samples })

  const scenarios = useMemo(
    () => (samples.data ?? []).filter((sample) => sample.id.startsWith('scenario:')).map((sample) => sample.id.replace('scenario:', '')),
    [samples.data],
  )

  // Default the objective from the project, and the mode from what is actually available.
  useEffect(() => {
    const detail = projectDetail.data
    if (detail?.project.default_objective && !objective) setObjective(detail.project.default_objective)
  }, [projectDetail.data, objective])

  useEffect(() => {
    const available = (providers.data?.providers ?? []).filter((item) => item.available)
    if (available.length === 0 && providers.isSuccess) setMode('scripted')
    else if (available.length > 0 && mode === 'scripted' && !params.get('scripted')) setMode('live')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [providers.isSuccess, providers.data])

  const sources = projectDetail.data?.sources ?? []
  const reviewerRoles = (roles.data ?? []).filter((role) => role.planner_selectable)
  const providerOptions = providers.data?.providers ?? []
  const modelOptions = (models.data ?? []).filter((model) => provider === 'auto' || model.provider === provider)

  const config: RunConfigSnapshot = {
    mode,
    provider,
    role_models: roleModels,
    limits,
    reviewers: null,
    scripted_scenario: scenario,
  }

  const runPreflight = useMutation({
    mutationFn: (deep: boolean) =>
      api.preflight({ project_id: projectId || undefined, source_id: sourceId || undefined, objective, config, deep }),
    onSuccess: (result) => {
      setPreflight(result)
      setError(null)
    },
    onError: (mutationError) => {
      setPreflight(null)
      setError(String(mutationError))
    },
  })

  const startRun = useMutation({
    mutationFn: () =>
      api.createRun({ project_id: projectId, source_id: sourceId, objective, config, constraints: [] }),
    onSuccess: (run) => navigate(`/runs/${run.id}`),
    onError: (mutationError) => setError(String(mutationError)),
  })

  const canStart = Boolean(projectId && sourceId && objective.trim()) && (preflight?.ok ?? false) && !startRun.isPending
  const source = sources.find((item) => item.id === sourceId)

  return (
    <>
      <div className="page-head">
        <div className="grow">
          <h1>New review</h1>
          <p>Choose the material, state the objective, then decide which specialists and models should work on it.</p>
        </div>
      </div>

      {error ? (
        <Notice tone="bad" title="Could not continue">
          {error}
        </Notice>
      ) : null}

      <div className="grid cols-2">
        <div className="grid" style={{ gap: 16 }}>
          <Panel title="1. Source">
            {projects.isLoading ? (
              <Loading />
            ) : projects.isError ? (
              <Notice tone="bad" title="Could not load projects">
                This is not an empty workspace: the request failed.{' '}
                <button type="button" className="linklike" onClick={() => void projects.refetch()}>
                  Try again
                </button>
              </Notice>
            ) : (projects.data ?? []).length === 0 ? (
              <EmptyState title="No projects yet">
                <Link to="/projects">Create a project</Link> and upload material to review.
              </EmptyState>
            ) : (
              <>
                <div className="field">
                  <label htmlFor="project">Project</label>
                  <select
                    id="project"
                    value={projectId}
                    onChange={(event) => {
                      setProjectId(event.target.value)
                      setSourceId('')
                      setPreflight(null)
                    }}
                  >
                    <option value="">Select a project…</option>
                    {projects.data?.map((project) => (
                      <option key={project.id} value={project.id}>
                        {project.name}
                      </option>
                    ))}
                  </select>
                </div>

                {projectId ? (
                  <div className="field">
                    <label htmlFor="source">Source material</label>
                    {sources.length === 0 ? (
                      <div className="small muted">
                        This project has no sources yet. <Link to={`/projects/${projectId}`}>Upload one</Link>.
                      </div>
                    ) : (
                      <select
                        id="source"
                        value={sourceId}
                        onChange={(event) => {
                          setSourceId(event.target.value)
                          setPreflight(null)
                        }}
                      >
                        <option value="">Select a source…</option>
                        {sources.map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.filename} ({item.size_bytes} bytes)
                          </option>
                        ))}
                      </select>
                    )}
                    {source ? (
                      <div className="help mono">
                        sha256 {source.sha256.slice(0, 24)}… · {source.content_type}
                      </div>
                    ) : null}
                  </div>
                ) : null}

                {source?.preview ? (
                  <div>
                    <label>Preview</label>
                    <pre
                      className="small"
                      style={{
                        maxHeight: 160,
                        overflow: 'auto',
                        background: 'var(--surface-sunken)',
                        border: '1px solid var(--border)',
                        borderRadius: 'var(--radius)',
                        padding: 8,
                        margin: 0,
                        whiteSpace: 'pre-wrap',
                      }}
                    >
                      {source.preview}
                    </pre>
                  </div>
                ) : null}
              </>
            )}
          </Panel>

          <Panel title="2. Objective">
            <div className="field">
              <label htmlFor="objective">What must this review achieve?</label>
              <textarea
                id="objective"
                value={objective}
                onChange={(event) => {
                  setObjective(event.target.value)
                  setPreflight(null)
                }}
                placeholder={OBJECTIVE_PRESETS[0]}
                rows={4}
              />
            </div>
            <div className="row tight">
              {OBJECTIVE_PRESETS.map((preset, index) => (
                <button
                  key={index}
                  type="button"
                  className="tiny"
                  onClick={() => {
                    setObjective(preset)
                    setPreflight(null)
                  }}
                >
                  Preset {index + 1}
                </button>
              ))}
            </div>
            <div className="help" style={{ marginTop: 8 }}>
              The objective steers the planner and is carried into every stage. It cannot override the safety rules:
              verification gates and the read/write policy are not prompt-controlled.
            </div>
          </Panel>

          <Panel title="3. Reviewers" flush>
            {roles.isLoading ? (
              <Loading />
            ) : (
              <>
                <div style={{ padding: 14 }}>
                  <p className="small muted" style={{ margin: 0 }}>
                    Every run uses these independent reviewers. The engine requires all four: a run that skipped one
                    would be reported as <em>degraded</em> rather than verified, so they cannot be disabled here.
                  </p>
                </div>
                <div className="scroll-x">
                  <table>
                    <thead>
                      <tr>
                        <th>Reviewer</th>
                        <th>Focus</th>
                        <th>Authority</th>
                      </tr>
                    </thead>
                    <tbody>
                      {reviewerRoles.map((role) => (
                        <tr key={role.role_key}>
                          <td>
                            <strong>{role.agent_name}</strong>
                            <div className="tiny faint mono">{role.role_key}</div>
                          </td>
                          <td className="small muted">{role.focus.slice(0, 5).map(titleCase).join(' · ')}</td>
                          <td>
                            <Badge tone="neutral">read-only</Badge>
                          </td>
                        </tr>
                      ))}
                {(roles.data ?? []).some((role) => role.may_mutate) ? (
                  <tr>
                    <td>
                      <strong>{(roles.data ?? []).find((role) => role.may_mutate)?.agent_name}</strong>
                      <div className="tiny faint mono">
                        {(roles.data ?? []).find((role) => role.may_mutate)?.role_key}
                      </div>
                    </td>
                    <td className="small muted">Applies the smallest defensible set of accepted changes.</td>
                    <td>
                      <Badge tone="warn">write-capable</Badge>
                    </td>
                  </tr>
                ) : null}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </Panel>

          <Panel title="4. Provider &amp; models">
            <div className="field">
              <label htmlFor="mode">Engine</label>
              <select
                id="mode"
                value={mode}
                onChange={(event) => {
                  setMode(event.target.value as RunMode)
                  setPreflight(null)
                }}
              >
                <option value="live" disabled={!(providers.data?.providers ?? []).some((item) => item.available)}>
                  Live providers (real model calls, real cost)
                </option>
                <option value="scripted">Scripted engine (deterministic, no cost)</option>
              </select>
              <div className="help">
                {mode === 'scripted'
                  ? 'Replays a canned evaluation scenario through the real workflow engine: same stages, same safety gates, no model calls. It is for trying the application, not for reviewing your document.'
                  : 'Uses the discovered providers. Reviewers, editor, adversarial reviewer and verifier each make real calls.'}
              </div>
            </div>

            {mode === 'scripted' ? (
              <div className="field">
                <label htmlFor="scenario">Scenario</label>
                <select id="scenario" value={scenario} onChange={(event) => setScenario(event.target.value)}>
                  {(scenarios.length ? scenarios : ['successful_end_to_end']).map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
                <div className="help">
                  Scripted runs expect the target to be named <code>manuscript.md</code>; load a sample source to match.
                </div>
              </div>
            ) : (
              <>
                <div className="field">
                  <label htmlFor="provider">Provider</label>
                  <select
                    id="provider"
                    value={provider}
                    onChange={(event) => {
                      setProvider(event.target.value)
                      setPreflight(null)
                    }}
                  >
                    <option value="auto">auto — first credentialed provider</option>
                    {providerOptions.map((item) => (
                      <option key={item.name} value={item.name} disabled={!item.available}>
                        {item.name}
                        {item.available ? '' : ' (not signed in)'}
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label>Per-role model</label>
                  <p className="help" style={{ marginTop: 0 }}>
                    Leave a role on <code>auto</code> to use the provider&rsquo;s default for its tier. Anything you set
                    here overrides it for this run only.
                  </p>
                  <div className="scroll-x">
                    <table>
                      <thead>
                        <tr>
                          <th>Role</th>
                          <th>Default (auto)</th>
                          <th>Selection</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(roles.data ?? []).map((role) => (
                          <tr key={role.role_key}>
                            <td className="small">
                              {role.agent_name}
                              <div className="tiny faint mono">{role.tier}</div>
                            </td>
                            <td className="mono tiny muted">{role.default_model || '—'}</td>
                            <td>
                              <select
                                aria-label={`Model for ${role.agent_name}`}
                                value={roleModels[role.tier] ?? 'auto'}
                                onChange={(event) => {
                                  const value = event.target.value
                                  setRoleModels((previous) => {
                                    const next = { ...previous }
                                    if (value === 'auto') delete next[role.tier]
                                    else next[role.tier] = value
                                    return next
                                  })
                                  setPreflight(null)
                                }}
                              >
                                <option value="auto">auto</option>
                                {modelOptions.map((model) => (
                                  <option key={`${model.provider}:${model.id}`} value={`${model.provider}:${model.id}`}>
                                    {model.provider}:{model.id}
                                  </option>
                                ))}
                              </select>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </>
            )}
          </Panel>

          <Panel title="5. Limits">
            <div className="grid cols-3">
              <div className="field">
                <label htmlFor="rounds">Max revision rounds</label>
                <input
                  id="rounds"
                  type="number"
                  min={0}
                  max={10}
                  value={limits.max_revision_rounds}
                  onChange={(event) => setLimits({ ...limits, max_revision_rounds: Number(event.target.value) })}
                />
              </div>
              <div className="field">
                <label htmlFor="concurrency">Reviewer concurrency</label>
                <input
                  id="concurrency"
                  type="number"
                  min={1}
                  max={8}
                  value={limits.max_reviewer_concurrency}
                  onChange={(event) => setLimits({ ...limits, max_reviewer_concurrency: Number(event.target.value) })}
                />
              </div>
              <div className="field">
                <label htmlFor="timeout">Agent timeout (s)</label>
                <input
                  id="timeout"
                  type="number"
                  min={5}
                  max={1800}
                  value={limits.agent_timeout_s}
                  onChange={(event) => setLimits({ ...limits, agent_timeout_s: Number(event.target.value) })}
                />
              </div>
              <div className="field">
                <label htmlFor="turns">Max turns per agent</label>
                <input
                  id="turns"
                  type="number"
                  min={1}
                  max={40}
                  value={limits.max_turns_per_agent}
                  onChange={(event) => setLimits({ ...limits, max_turns_per_agent: Number(event.target.value) })}
                />
              </div>
              <div className="field">
                <label htmlFor="tokens">Max total tokens</label>
                <input
                  id="tokens"
                  type="number"
                  min={1000}
                  step={1000}
                  value={limits.max_total_tokens}
                  onChange={(event) => setLimits({ ...limits, max_total_tokens: Number(event.target.value) })}
                />
              </div>
              <div className="field">
                <label htmlFor="min-reviewers">Min successful reviewers</label>
                <input
                  id="min-reviewers"
                  type="number"
                  min={1}
                  max={8}
                  value={limits.min_successful_reviewers}
                  onChange={(event) => setLimits({ ...limits, min_successful_reviewers: Number(event.target.value) })}
                />
              </div>
            </div>
            <div className="help">
              Exceeding a budget stops the run; it never truncates a result silently. Retries are bounded: transient
              failures are retried, invalid structured output is retried once with corrective feedback, and a failed
              verification routes back to a revision rather than repeating a call.
            </div>
          </Panel>
        </div>

        <div>
          <Panel
            title="6. Preflight &amp; start"
            actions={
              <button
                type="button"
                onClick={() => runPreflight.mutate(false)}
                disabled={runPreflight.isPending || !objective.trim() || !sourceId}
              >
                {runPreflight.isPending ? 'Checking…' : 'Run preflight'}
              </button>
            }
          >
            <p className="small muted">
              Preflight checks the source, the objective, the provider credentials and every model you selected. A run is
              only started when nothing blocking is found.
            </p>

            <div className="row" style={{ marginBottom: 10 }}>
              <button
                type="button"
                className="small"
                onClick={() => runPreflight.mutate(true)}
                disabled={runPreflight.isPending || mode !== 'live'}
                title={mode === 'live' ? 'Also performs live capability checks (real model calls)' : 'Live probe needs the live engine'}
              >
                Deep probe (real calls)
              </button>
              {preflight ? (
                <Badge tone={preflight.ok ? 'ok' : 'bad'}>{preflight.ok ? 'clear to start' : 'blocked'}</Badge>
              ) : (
                <span className="small faint">not run yet</span>
              )}
            </div>

            {preflight ? (
              <table>
                <tbody>
                  {preflight.checks.map((check, index) => (
                    <tr key={`${check.name}-${index}`}>
                      <td className="small" style={{ width: 26 }}>
                        {check.ok ? '✓' : check.blocking ? '✕' : '!'}
                      </td>
                      <td className="small">
                        {check.name}
                        <div className="tiny faint">{check.detail}</div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : null}

            {preflight && !preflight.ok ? (
              <Notice tone="bad" title="Blocking problems">
                {preflight.blocking_failures.join(', ')}
              </Notice>
            ) : null}

            <div className="row" style={{ marginTop: 14 }}>
              <button type="button" className="primary" disabled={!canStart} onClick={() => startRun.mutate()}>
                {startRun.isPending ? 'Starting…' : 'Start review'}
              </button>
              {!preflight?.ok ? <span className="small faint">Run preflight first</span> : null}
            </div>
          </Panel>
        </div>
      </div>
    </>
  )
}
