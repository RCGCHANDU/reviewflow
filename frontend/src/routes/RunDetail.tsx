import { useMemo } from 'react'
import type { KeyboardEvent } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { RunInspection } from '../api/types'
import { DiffView } from '../components/DiffView'
import { EventLog } from '../components/EventLog'
import { ReviewerPanel } from '../components/Findings'
import { StageTimeline } from '../components/StageTimeline'
import { Badge, EmptyState, KeyValues, Loading, Notice, Panel, PassBadge, RunStatusBadge } from '../components/Status'
import { useEventStream } from '../hooks/useEventStream'
import { reviewerActivity } from '../lib/activity'
import { dispositionMap } from '../lib/findings'
import { RUN_STATUS_MEANING, formatDuration, formatNumber, formatTimestamp, isTerminal, titleCase } from '../lib/format'

type Tab = 'timeline' | 'reviewers' | 'synthesis' | 'edit' | 'adversarial' | 'verification' | 'result'

const TAB_KEYS: Tab[] = ['timeline', 'reviewers', 'synthesis', 'edit', 'adversarial', 'verification', 'result']

/**
 * Live workflow run.
 *
 * The most important screen: what is happening, which agent is doing it, on which model,
 * what it found, what changed, what verification decided and why the run ended the way it
 * did. Live progress comes from the server's event stream (which replays from a sequence
 * number, so a refresh or a reconnect loses nothing) rather than from polling.
 *
 * The screen never flattens the outcome: a failed mandatory check stays visible, and a run
 * that did not succeed is never given a green header.
 */
export function RunDetail() {
  const { runId } = useParams<{ runId: string }>()
  const id = runId as string
  const queryClient = useQueryClient()
  // The active tab lives in the URL, so a section can be linked, shared and survives a
  // refresh. Before this, every cross-reference ("see the Result tab") was dead text and
  // a reload silently reset the reader to the process log.
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedTab = searchParams.get('tab')
  const tab: Tab = TAB_KEYS.includes((requestedTab ?? '') as Tab) ? (requestedTab as Tab) : 'timeline'
  const setTab = (next: Tab) => {
    const params = new URLSearchParams(searchParams)
    params.set('tab', next)
    setSearchParams(params, { replace: true })
  }

  const runQuery = useQuery({
    queryKey: ['run', id],
    queryFn: () => api.run(id),
    enabled: Boolean(id),
    // A slow fallback only: the event stream is what actually drives updates.
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return 5_000
      return isTerminal(data.run.status) ? false : 10_000
    },
  })

  const status = runQuery.data?.run.status
  const terminal = status ? isTerminal(status) : false

  const inspection = useQuery({
    queryKey: ['inspection', id],
    queryFn: () => api.inspection(id),
    enabled: Boolean(id),
    // Evidence is refetched as the stream reports progress, and once more when the run
    // finishes so the terminal state is read from the database rather than inferred.
    refetchInterval: terminal ? false : 20_000,
  })

  const stream = useEventStream(id, {
    onEvent: (event) => {
      if (event.type.startsWith('stage.') || event.type.startsWith('agent.') || event.type.startsWith('workflow.')) {
        void queryClient.invalidateQueries({ queryKey: ['run', id] })
      }
      if (
        event.type === 'workflow.terminal' ||
        event.type.startsWith('workflow.succeeded') ||
        event.type.startsWith('workflow.blocked') ||
        event.type.startsWith('workflow.degraded') ||
        event.type.startsWith('workflow.failed') ||
        event.type.startsWith('workflow.cancelled') ||
        event.type === 'verification.completed' ||
        event.type === 'edit.completed' ||
        event.type === 'adversarial.completed' ||
        event.type === 'synthesis.completed'
      ) {
        void queryClient.invalidateQueries({ queryKey: ['inspection', id] })
      }
    },
  })

  const cancel = useMutation({
    mutationFn: () => api.cancelRun(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['run', id] })
      void queryClient.invalidateQueries({ queryKey: ['inspection', id] })
    },
  })

  const rerun = useMutation({
    mutationFn: () => api.rerun(id),
  })

  const data: RunInspection | undefined = inspection.data
  const run = runQuery.data?.run ?? data?.run
  const stages = runQuery.data?.stages ?? data?.stages ?? []
  const dispositions = useMemo(() => dispositionMap(data?.synthesis ?? null), [data?.synthesis])
  const agents = useMemo(() => reviewerActivity(stream.events), [stream.events])
  const agentByRole = useMemo(() => new Map(agents.map((agent) => [agent.role, agent])), [agents])

  const verificationResults = data?.verification?.results ?? []
  const failedChecks = verificationResults.filter((result) => !result.passed)
  const issueCount = (data?.reviewers ?? []).reduce((total, report) => total + (report.issues?.length ?? 0), 0)
  // Findings accepted by the synthesis that the run finished without resolving. Distinct
  // from a failed check: the engine permits a successful run to carry them, and the screen
  // must say so plainly rather than let a full green summary imply none exist.
  const unresolvedCount = data?.unresolved_issues?.length ?? 0

  if (runQuery.isError) {
    return (
      <>
        <Notice tone="bad" title="Run not found">
          {String(runQuery.error)} <Link to="/runs">Back to runs</Link>
        </Notice>
      </>
    )
  }

  if (!run) {
    return <Loading label="Loading run…" />
  }

  const tabs: { key: Tab; label: string; count?: number }[] = [
    { key: 'timeline', label: 'Timeline' },
    { key: 'reviewers', label: 'Reviewer findings', count: issueCount },
    { key: 'synthesis', label: 'Synthesis', count: data?.synthesis?.conflicts?.length },
    { key: 'edit', label: 'Edit & diff' },
    { key: 'adversarial', label: 'Adversarial' },
    { key: 'verification', label: 'Verification', count: failedChecks.length },
    { key: 'result', label: 'Result' },
  ]

  // One sourced count for the decision strip; severity is the engine's own, not inferred.
  const criticalUnresolved = (data?.unresolved_issues ?? []).filter((item) => item.severity === 'critical').length
  // Evidence comes from a second request; when it fails the page must say so rather than
  // render zeroes that read as measured facts (A8-02).
  const evidenceFailed = inspection.isError || (!inspection.isLoading && !data)

  const onTabKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const order = tabs.map((item) => item.key)
    const current = order.indexOf(tab)
    if (current < 0) return
    let next: Tab | null = null
    if (event.key === 'ArrowRight') next = order[(current + 1) % order.length]
    else if (event.key === 'ArrowLeft') next = order[(current - 1 + order.length) % order.length]
    else if (event.key === 'Home') next = order[0]
    else if (event.key === 'End') next = order[order.length - 1]
    if (!next) return
    event.preventDefault()
    setTab(next)
    document.getElementById(`tab-${next}`)?.focus()
  }

  return (
    <><div className="breadcrumb">
        <Link to="/runs">Runs</Link> / <span className="mono">{run.id}</span>
      </div>

      <div className="page-head">
        <div className="grow">
          <div className="row tight">
            <RunStatusBadge status={run.status} />
            <Badge tone="neutral">{run.mode}</Badge>
            {run.provider_snapshot.provider ? (
              <Badge tone="neutral">
                {String(run.provider_snapshot.provider)}
                {run.provider_snapshot.transport === 'streaming' ? ' · streaming' : ''}
              </Badge>
            ) : null}
            {stream.state === 'open' && !terminal ? (
              <span className="badge info running">
                <span className="dot" aria-hidden="true" />
                live
              </span>
            ) : null}
            {stream.state === 'error' ? <Badge tone="warn">reconnecting</Badge> : null}
            {terminal ? <span className="tiny faint">stream closed</span> : null}
          </div>
          <h1 style={{ marginTop: 6 }}>{run.objective}</h1>
          <p>
            {run.project_name} · {run.source_filename} · started {formatTimestamp(run.started_at ?? run.created_at)} ·{' '}
            {run.revision_round} revision round{run.revision_round === 1 ? '' : 's'}
          </p>
        </div>
        <div className="row">
          {!terminal ? (
            <button
              type="button"
              className="danger"
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending}
              title="Stops scheduling new stages and cancels safe in-flight work; recorded as cancelled, not failed"
            >
              {cancel.isPending ? 'Cancelling…' : 'Cancel run'}
            </button>
          ) : (
            <button type="button" onClick={() => rerun.mutate()} disabled={rerun.isPending}>
              {rerun.isPending ? 'Starting…' : 'Run again with this config'}
            </button>
          )}
        </div>
      </div>

      <Notice
        tone={run.status === 'succeeded' ? 'ok' : run.status === 'failed' ? 'bad' : 'warn'}
        title={RUN_STATUS_MEANING[run.status]}
        role={terminal ? 'status' : undefined}
      >
        {data?.status_explanation ?? run.failure_reason ?? ''}
      </Notice>

      {rerun.isSuccess && rerun.data ? (
        <Notice tone="ok" title="A new run was created">
          This run is unchanged. <Link to={`/runs/${rerun.data.id}`}>Open the new run →</Link>
        </Notice>
      ) : null}
      {cancel.isError ? (
        <Notice tone="bad" title="Could not cancel">
          {String(cancel.error)}
        </Notice>
      ) : null}
      {rerun.isError ? (
        // Measured: a failing rerun changed nothing on screen and only logged to the console.
        <Notice tone="bad" title="Could not start a new run">
          {String(rerun.error)}
        </Notice>
      ) : null}

      {evidenceFailed ? (
        <Notice tone="bad" title="Could not load this run's evidence">
          The status above comes from the run record. The reviewer findings, verification results, diff and artifacts
          could not be fetched, so nothing on this page may be read as a complete account of the run.{' '}
          <button type="button" className="linklike" onClick={() => void inspection.refetch()}>
            Try again
          </button>
        </Notice>
      ) : (
        <>
          {/* Decision-first: the facts that decide what to do next belong above the evidence,
          and every count is labelled so "10 reviewer findings" and "23 unresolved at
          completion" cannot read as two contradictory totals for the same thing. */}
      <div className="decision-strip">
        <span className="mono">
          <strong>{verificationResults.length - failedChecks.length}</strong>/{verificationResults.length} checks
          passed
        </span>
        {failedChecks.length > 0 ? (
          <span className="decision-fail">failing: {failedChecks.map((check) => check.check).join(', ')}</span>
        ) : null}
        <span>
          <strong className="mono">{unresolvedCount}</strong> unresolved at completion
          {criticalUnresolved > 0 ? (
            <span className="decision-fail"> · {criticalUnresolved} critical</span>
          ) : null}
        </span>
        {run.status === 'blocked' && (data?.terminal?.reason ?? run.failure_reason) ? (
          <span className="decision-fail">
            blocked: {String(data?.terminal?.reason ?? run.failure_reason).replace(/\.$/, '')}
          </span>
        ) : null}
        <button type="button" className="button primary" onClick={() => setTab('result')}>
          See the decision →
        </button>
      </div>

      <div className="grid cols-3" style={{ marginBottom: 16 }}>
        <Panel title="Progress">
          <div className="row small muted" style={{ gap: 16 }}>
            <span>
              <strong className="mono" style={{ color: 'var(--text)' }}>
                {stages.filter((stage) => stage.state === 'completed').length}
              </strong>
              /{stages.length} stages
            </span>
            <span>
              <strong className="mono" style={{ color: 'var(--text)' }}>
                {run.usage?.model_calls ?? 0}
              </strong>{' '}
              calls
            </span>
            <span>
              <strong className="mono" style={{ color: 'var(--text)' }}>
                {formatNumber(run.usage?.total_tokens)}
              </strong>{' '}
              tokens
            </span>
            <span>{formatDuration(run.usage?.wall_clock_ms)}</span>
          </div>
          <div className="meter" style={{ marginTop: 10 }}>
            <div
              style={{
                width: `${stages.length ? (stages.filter((stage) => stage.state === 'completed').length / stages.length) * 100 : 0}%`,
              }}
            />
          </div>
        </Panel>

        <Panel title="Reviewer coverage">
          {data?.coverage ? (
            <>
              <div className="row tight">
                {(data.coverage.expected ?? []).map((role) => {
                  const failed = (data.coverage?.failed ?? []).includes(role)
                  return (
                    <Badge key={role} tone={failed ? 'bad' : 'ok'}>
                      {titleCase(role)}
                    </Badge>
                  )
                })}
              </div>
              {Object.keys(data.failed_reviewers ?? {}).length > 0 ? (
                <div className="small" style={{ marginTop: 8, color: 'var(--bad)' }}>
                  {Object.entries(data.failed_reviewers).map(([role, reason]) => (
                    <div key={role}>
                      {titleCase(role)} failed: {reason}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="small muted" style={{ marginTop: 8 }}>
                  All expected reviewers reported.
                </div>
              )}
            </>
          ) : (
            <span className="small muted">Coverage appears when the reviewers report.</span>
          )}
        </Panel>

        <Panel title="Verification">
          {verificationResults.length === 0 ? (
            <span className="small muted">No checks have run yet.</span>
          ) : (
            <>
              <div className="row">
                <span className="mono" style={{ fontSize: 22, fontWeight: 600 }}>
                  {verificationResults.length - failedChecks.length}/{verificationResults.length}
                </span>
                <span className="muted small">mandatory checks passed</span>
              </div>
              {failedChecks.length > 0 ? (
                <div className="small" style={{ marginTop: 8, color: 'var(--bad)' }}>
                  Failing: {failedChecks.map((check) => check.check).join(', ')}{' '}
                  <button type="button" className="linklike" onClick={() => setTab('verification')}>
                    Show the checks
                  </button>
                </div>
              ) : (
                <div className="small muted" style={{ marginTop: 8 }}>
                  No check failed.
                </div>
              )}
              {/* Verification checks and unresolved findings are different facts. A run can
                  pass every check and still carry accepted issues nobody actioned, and the
                  screen must not let "no check failed" read as "nothing is wrong". */}
              {unresolvedCount > 0 ? (
                <div className="small" style={{ marginTop: 6, color: 'var(--warn, var(--text))' }}>
                  {unresolvedCount} accepted {unresolvedCount === 1 ? 'issue' : 'issues'} still
                  unresolved.{' '}
                  <button type="button" className="linklike" onClick={() => setTab('result')}>
                    Open the unresolved list
                  </button>
                </div>
              ) : null}
            </>
          )}
        </Panel>
      </div>

      <div className="tabs" role="tablist" aria-label="Run sections" onKeyDown={onTabKeyDown}>
        {tabs.map((item) => (
          <button
            key={item.key}
            id={`tab-${item.key}`}
            role="tab"
            aria-controls="run-panel"
            tabIndex={tab === item.key ? 0 : -1}
            aria-label={item.count ? `${item.label}, ${item.count} ${item.count === 1 ? 'item' : 'items'}` : item.label}
            aria-selected={tab === item.key}
            onClick={() => setTab(item.key)}
            type="button"
          >
            {item.label}
            {item.count ? (
              <span className="tab-count" aria-hidden="true">
                {item.count}
              </span>
            ) : null}
          </button>
        ))}
      </div>

      {/* The panel the tabs control: aria-controls on each tab points at this id, so a
          reader that follows a tab always lands on the content it names. */}
      <div role="tabpanel" id="run-panel" aria-labelledby={`tab-${tab}`} tabIndex={-1}>
      {inspection.isLoading && !data ? (
        <Loading label="Loading run evidence…" />
      ) : (
        <>
          {tab === 'timeline' ? (
            <div className="grid cols-2">
              <Panel title="Stages">
                <StageTimeline stages={stages} events={stream.events} />
              </Panel>
              <Panel
                title="Workflow activity"
                flush
                actions={<span className="tiny faint">{stream.events.length} events{stream.endReason ? ` · ${stream.endReason}` : ''}</span>}
              >
                <EventLog events={stream.events} live={stream.state === 'open' && !terminal} />
              </Panel>
            </div>
          ) : null}

          {tab === 'reviewers' ? (
            <div className="grid cols-2">
              {(data?.reviewers ?? []).length === 0 && Object.keys(data?.failed_reviewers ?? {}).length === 0 ? (
                <Panel title="Reviewer findings">
                  <EmptyState title="No reviewer reports yet">
                    Each reviewer reports independently; their findings appear here as they finish.
                  </EmptyState>
                </Panel>
              ) : (
                <>
                  {(data?.reviewers ?? []).map((report) => {
                    const activity = agentByRole.get(report.reviewer)
                    return (
                      <ReviewerPanel
                        key={report.reviewer}
                        report={report}
                        disposition={dispositions}
                        model={activity?.model}
                        latencyMs={activity?.latencyMs}
                      />
                    )
                  })}
                  {Object.entries(data?.failed_reviewers ?? {}).map(([role, reason]) => (
                    <ReviewerPanel key={role} disposition={dispositions} failed failureReason={reason} />
                  ))}
                </>
              )}
            </div>
          ) : null}

          {tab === 'synthesis' ? (
            <div className="grid cols-2">
              <Panel title="Decisions">
                {!data?.synthesis ? (
                  <EmptyState title="Synthesis has not run yet" />
                ) : (
                  <>
                    <KeyValues
                      items={[
                        {
                          label: 'Change required',
                          value: data.synthesis.change_required ? <Badge tone="warn">yes</Badge> : <Badge tone="ok">no</Badge>,
                        },
                        { label: 'Rationale', value: data.synthesis.change_rationale ?? '—' },
                        {
                          label: 'Accepted',
                          value: (
                            <div className="row tight">
                              {(data.synthesis.accepted_issues ?? []).map((item) => (
                                <Badge key={item.issue_id} tone="ok">
                                  {item.issue_id}
                                </Badge>
                              ))}
                              {(data.synthesis.accepted_issues ?? []).length === 0 ? '—' : null}
                            </div>
                          ),
                        },
                        {
                          label: 'Rejected',
                          value: (
                            <div className="row tight">
                              {(data.synthesis.rejected_issues ?? []).map((item) => (
                                <span key={item.issue_id} title={item.rationale}>
                                  <Badge tone="neutral">{item.issue_id}</Badge>
                                </span>
                              ))}
                              {(data.synthesis.rejected_issues ?? []).length === 0 ? '—' : null}
                            </div>
                          ),
                        },
                        {
                          label: 'Merged',
                          value:
                            (data.synthesis.merged_issues ?? []).length === 0
                              ? '—'
                              : (data.synthesis.merged_issues ?? []).map((merged, index) => (
                                  <div key={index} className="small">
                                    {merged.issue_ids.join(', ')} — {merged.rationale}
                                  </div>
                                )),
                        },
                      ]}
                    />
                    {(data.synthesis.unresolved_questions?.length ?? 0) > 0 ? (
                      <div style={{ marginTop: 12 }}>
                        <h3 style={{ marginBottom: 4 }}>Unresolved questions</h3>
                        <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
                          {data.synthesis.unresolved_questions?.map((question, index) => (
                            <li key={index}>{question}</li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                  </>
                )}
              </Panel>

              <Panel title="Conflicts">
                {(data?.synthesis?.conflicts ?? []).length === 0 ? (
                  <EmptyState title="No conflicts recorded">
                    The reviewers either agreed or their findings did not overlap.
                  </EmptyState>
                ) : (
                  (data?.synthesis?.conflicts ?? []).map((conflict, index) => (
                    <article className="issue major" key={index}>
                      <div className="issue-head">
                        <Badge tone="warn">conflict</Badge>
                        {conflict.adjudicated ? <Badge tone="info">adjudicated</Badge> : <Badge tone="neutral">open</Badge>}
                      </div>
                      <p className="small">{conflict.summary}</p>
                      {conflict.reason ? (
                        <div className="small muted">
                          <strong>Reason:</strong> {conflict.reason}
                        </div>
                      ) : null}
                      {conflict.resolution_note ? (
                        <div className="small">
                          <strong>Resolution:</strong> {conflict.resolution_note}
                        </div>
                      ) : null}
                      {conflict.issue_ids?.length ? <div className="tiny faint mono">{conflict.issue_ids.join(', ')}</div> : null}
                    </article>
                  ))
                )}
              </Panel>
            </div>
          ) : null}

          {tab === 'edit' ? (
            <div className="grid" style={{ gap: 16 }}>
              <Panel title="Controlled edit">
                <Notice>
                  The controlled editor is the only agent with a mutation tool. It receives the consolidated issue map
                  and a single grant: one path, at one hash, with a line budget. Reviewers cannot write, and a tool
                  guardrail refuses mutating calls even if one tried.
                </Notice>
                {!data?.edit ? (
                  <EmptyState title="No edit result yet">
                    Either the run has not reached the edit stage, or it finished without editing.
                  </EmptyState>
                ) : (
                  <KeyValues
                    items={[
                      { label: 'Target', value: <span className="mono">{data.edit.target ?? '—'}</span> },
                      {
                        label: 'Changes',
                        value: `${data.edit.changes?.length ?? 0} change${(data.edit.changes?.length ?? 0) === 1 ? '' : 's'}`,
                      },
                      {
                        label: 'No-change reason',
                        value: data.edit.no_change_reason ?? '—',
                      },
                      {
                        label: 'Unresolved items',
                        value:
                          (data.edit.unresolved_items?.length ?? 0) === 0 ? (
                            '—'
                          ) : (
                            <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                              {data.edit.unresolved_items?.map((item, index) => <li key={index}>{item}</li>)}
                            </ul>
                          ),
                      },
                    ]}
                  />
                )}
                {(data?.edit?.changes ?? []).length > 0 ? (
                  <div style={{ marginTop: 14 }}>
                    <h3 style={{ marginBottom: 6 }}>Declared changes</h3>
                    {data?.edit?.changes?.map((change, index) => (
                      <article className="issue minor" key={index}>
                        <div className="issue-head">
                          <span className="issue-id">{change.location ?? `change ${index + 1}`}</span>
                          {(change.issue_ids ?? []).map((issueId) => (
                            <Badge key={issueId} tone="info">
                              {issueId}
                            </Badge>
                          ))}
                        </div>
                        <p className="small">{change.rationale ?? change.description ?? change.kind ?? ''}</p>
                        {change.after ? <blockquote>{change.after}</blockquote> : null}
                      </article>
                    ))}
                  </div>
                ) : null}
              </Panel>

              <Panel title="Diff (computed from bytes on disk)">
                {!data?.diff ? (
                  <EmptyState title="No diff">
                    The material was not modified. The decision not to edit is itself reviewed and verified.
                  </EmptyState>
                ) : (
                  <DiffView diff={data.diff} label={data.edit?.target ?? undefined} />
                )}
              </Panel>

              {data?.diff_review ? (
                <Panel title="Diff review">
                  <div className="row" style={{ marginBottom: 8 }}>
                    <Badge tone={data.diff_review.approved ? 'ok' : 'bad'}>
                      {data.diff_review.approved ? 'approved' : 'not approved'}
                    </Badge>
                  </div>
                  <KeyValues
                    items={[
                      {
                        label: 'Out-of-scope changes',
                        value: (data.diff_review.out_of_scope_changes ?? []).length ? (
                          <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                            {data.diff_review.out_of_scope_changes?.map((item, index) => <li key={index}>{item}</li>)}
                          </ul>
                        ) : (
                          'none'
                        ),
                      },
                      {
                        label: 'Unsupported assertions',
                        value: (data.diff_review.unsupported_assertions ?? []).length ? (
                          <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                            {data.diff_review.unsupported_assertions?.map((item, index) => <li key={index}>{item}</li>)}
                          </ul>
                        ) : (
                          'none'
                        ),
                      },
                      {
                        label: 'Semantic drift',
                        value: (data.diff_review.semantic_drift ?? []).length ? (
                          <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                            {data.diff_review.semantic_drift?.map((item, index) => <li key={index}>{item}</li>)}
                          </ul>
                        ) : (
                          'none'
                        ),
                      },
                    ]}
                  />
                </Panel>
              ) : null}
            </div>
          ) : null}

          {tab === 'adversarial' ? (
            <Panel title="Adversarial review">
              <Notice tone={data?.adversarial?.passed ? 'warn' : 'bad'}>
                This agent runs after editing and its purpose is to find reasons the revision should be <em>rejected</em>.
                It does not see the editor&rsquo;s reasoning as authoritative, and it is not shown the other reviewers&apos;
                reports as a consensus.
              </Notice>
              {!data?.adversarial ? (
                <EmptyState title="No verdict yet">The adversarial reviewer has not reported.</EmptyState>
              ) : (
                <>
                  <div className="row" style={{ marginBottom: 10 }}>
                    <Badge tone={data.adversarial.passed ? 'ok' : 'bad'}>
                      {data.adversarial.passed ? 'PASS — revision survived' : 'REJECT — revision did not survive'}
                    </Badge>
                  </div>
                  {data.adversarial.rationale ? <p className="small">{data.adversarial.rationale}</p> : null}
                  {(
                    [
                      ['Critical', data.adversarial.critical_issues, 'bad'],
                      ['Major', data.adversarial.major_issues, 'warn'],
                      ['Minor', data.adversarial.minor_issues, 'neutral'],
                    ] as const
                  ).map(([label, items, tone]) => (
                    <div key={label} style={{ marginTop: 10 }}>
                      <h3 style={{ marginBottom: 4 }}>
                        <Badge tone={tone}>{label}</Badge> <span className="faint small">{(items ?? []).length}</span>
                      </h3>
                      {(items ?? []).length === 0 ? (
                        <p className="small muted">None.</p>
                      ) : (
                        <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                          {items?.map((item, index) => (
                            <li key={index} style={{ marginBottom: 6 }}>
                              <div>{item.description}</div>
                              <div className="faint small mono">{item.location}</div>
                              <div className="muted small">&ldquo;{item.evidence}&rdquo;</div>
                              {item.related_issue_id ? (
                                <div className="faint small">relates to {item.related_issue_id}</div>
                              ) : null}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  ))}
                  {(data.adversarial.rejection_reasons?.length ?? 0) > 0 ? (
                    <div style={{ marginTop: 10 }}>
                      <h3 style={{ marginBottom: 4 }}>Rejection reasons</h3>
                      <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                        {data.adversarial.rejection_reasons?.map((reason, index) => <li key={index}>{reason}</li>)}
                      </ul>
                    </div>
                  ) : null}
                </>
              )}
            </Panel>
          ) : null}

          {tab === 'verification' ? (
            <Panel
              title="Verification"
              flush
              actions={
                failedChecks.length > 0 ? (
                  <Badge tone="bad">{failedChecks.length} failing</Badge>
                ) : verificationResults.length > 0 ? (
                  <Badge tone="ok">all passing</Badge>
                ) : null
              }
            >
              {verificationResults.length === 0 ? (
                <EmptyState title="No verification results yet" />
              ) : (
                <div className="scroll-x">
                  <table>
                    <thead>
                      <tr>
                        <th style={{ width: 70 }}>Result</th>
                        <th>Check</th>
                        <th style={{ width: 110 }}>Kind</th>
                        <th style={{ width: 90 }}>Required</th>
                        <th>Method</th>
                        <th>Evidence / error</th>
                      </tr>
                    </thead>
                    <tbody>
                      {verificationResults.map((result) => (
                        <tr key={result.check}>
                          <td>
                            <PassBadge passed={result.passed} />
                          </td>
                          <td className="small">
                            <strong>{result.check}</strong>
                          </td>
                          {/* The engine distinguishes deterministic from semantic checks and
                              marks which ones are mandatory. Showing that is the difference
                              between "a check failed" and "the run could not have succeeded". */}
                          <td className="small muted">{result.kind ?? '—'}</td>
                          <td className="small muted">
                            {result.mandatory === false ? 'advisory' : result.mandatory ? 'yes' : '—'}
                          </td>
                          <td className="small muted">{result.command_or_method ?? '—'}</td>
                          <td className="small" style={{ maxWidth: 520, color: result.passed ? 'var(--text-muted)' : 'var(--bad)' }}>
                            {result.error ?? result.evidence ?? '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Panel>
          ) : null}

          {tab === 'result' ? (
            <div className="grid cols-2">
              <Panel title="Final report">
                {!data?.final_report ? (
                  <EmptyState title="No final report yet" />
                ) : (
                  <>
                    <p>{data.final_report.executive_summary}</p>
                    {(data.final_report.disclosures?.length ?? 0) > 0 ? (
                      <div style={{ marginTop: 10 }}>
                        <h3 style={{ marginBottom: 4 }}>Disclosures</h3>
                        <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                          {data.final_report.disclosures?.map((item, index) => (
                            <li key={index}>{item}</li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                    {(data.final_report.recommendations?.length ?? 0) > 0 ? (
                      <div style={{ marginTop: 10 }}>
                        <h3 style={{ marginBottom: 4 }}>Recommendations</h3>
                        <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                          {data.final_report.recommendations?.map((item, index) => (
                            <li key={index}>{item}</li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                  </>
                )}
              </Panel>

              <Panel title="Artifacts" flush>
                {(data?.artifacts ?? []).length === 0 ? (
                  <EmptyState title="No artifacts">A finished run lists its document, ledger and evidence here.</EmptyState>
                ) : (
                  <table>
                    <thead>
                      <tr>
                        <th>Artifact</th>
                        <th>Name</th>
                        <th className="num">Size</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {data?.artifacts?.map((artifact) => (
                        <tr key={artifact.id}>
                          <td className="small">{titleCase(artifact.type)}</td>
                          <td className="mono small">{artifact.filename}</td>
                          <td className="num small">{artifact.size_bytes}</td>
                          <td className="num">
                            <a className="small" href={artifact.download_url} download>
                              download
                            </a>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Panel>

              <Panel title="Usage">
                <KeyValues
                  items={[
                    { label: 'Model calls', value: formatNumber(run.usage?.model_calls) },
                    { label: 'Input tokens', value: formatNumber(run.usage?.input_tokens) },
                    { label: 'Output tokens', value: formatNumber(run.usage?.output_tokens) },
                    { label: 'Total tokens', value: formatNumber(run.usage?.total_tokens) },
                    { label: 'Wall clock', value: formatDuration(run.usage?.wall_clock_ms) },
                    { label: 'Rounds executed', value: String(data?.terminal?.rounds_executed ?? run.revision_round) },
                    {
                      label: 'Cost',
                      value: (
                        <span className="muted">
                          unknown — these providers do not expose per-token pricing to the application
                        </span>
                      ),
                    },
                  ]}
                />
                {(run.usage?.by_role ?? []).length > 0 ? (
                  <div className="scroll-x" style={{ marginTop: 12 }}>
                    <table>
                      <thead>
                        <tr>
                          <th>Role</th>
                          <th className="num">Calls</th>
                          <th className="num">In</th>
                          <th className="num">Out</th>
                          <th className="num">Latency</th>
                        </tr>
                      </thead>
                      <tbody>
                        {run.usage?.by_role?.map((role) => (
                          <tr key={role.role}>
                            <td className="small">{titleCase(role.role)}</td>
                            <td className="num">{role.calls}</td>
                            <td className="num">{formatNumber(role.input_tokens)}</td>
                            <td className="num">{formatNumber(role.output_tokens)}</td>
                            <td className="num">{formatDuration(role.latency_ms)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : null}
              </Panel>

              {(data?.unresolved_issues?.length ?? 0) > 0 ? (
                <Panel title="Unresolved issues" flush>
                  <table>
                    <thead>
                      <tr>
                        <th>Issue</th>
                        <th>Severity</th>
                        <th>Summary</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data?.unresolved_issues?.map((issue, index) => (
                        <tr key={`${issue.issue_id}-${index}`}>
                          <td className="mono small">{issue.issue_id}</td>
                          <td>
                            <Badge tone={issue.severity === 'critical' ? 'bad' : 'warn'}>{issue.severity ?? '—'}</Badge>
                          </td>
                          <td className="small">{issue.summary}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Panel>
              ) : null}
            </div>
          ) : null}
        </>
      )}
      </div>
        </>
      )}
    </>
  )
}
