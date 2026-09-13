import type { Issue, ReviewerReport } from '../api/types'
import { severityRank, titleCase } from '../lib/format'

import { Badge, EmptyState, KeyValues, Panel } from './Status'

/**
 * Reviewer findings.
 *
 * Each reviewer keeps its own panel, in the order the engine reported them, and nothing is
 * cross-merged here: the workflow's whole premise is that four independent reviewers
 * inspect the same material without seeing each other's conclusions. Reconciliation is the
 * synthesis agent's job and is shown on its own tab, so the UI cannot imply a consensus
 * that was never reached.
 */

export function IssueCard({ issue, disposition }: { issue: Issue; disposition?: string }) {
  const severity = String(issue.severity ?? 'unknown')
  return (
    <article className={`issue ${severity}`}>
      <div className="issue-head">
        <span className="issue-id">{issue.id}</span>
        <Badge tone={severity === 'critical' ? 'bad' : severity === 'major' ? 'warn' : 'neutral'}>{severity}</Badge>
        {issue.category ? <span className="tiny faint">{titleCase(issue.category)}</span> : null}
        <span className="spacer" />
        {disposition ? (
          <Badge tone={disposition === 'accepted' ? 'ok' : disposition === 'merged' ? 'info' : 'neutral'}>
            {disposition}
          </Badge>
        ) : null}
        {issue.confidence !== undefined ? <span className="tiny faint">confidence {issue.confidence}</span> : null}
      </div>
      {issue.location ? <div className="small muted">{issue.location}</div> : null}
      <p className="small" style={{ marginTop: 6 }}>
        {issue.description}
      </p>
      {issue.evidence ? <blockquote>{issue.evidence}</blockquote> : null}
      {issue.evidence_status ? (
        <div className="tiny faint" style={{ marginTop: 4 }}>
          evidence status: {issue.evidence_status}
        </div>
      ) : null}
      {issue.recommendation ? (
        <div className="small" style={{ marginTop: 6 }}>
          <strong>Recommendation:</strong> {issue.recommendation}
        </div>
      ) : null}
    </article>
  )
}

export function ReviewerPanel({
  report,
  disposition,
  failed,
  failureReason,
  model,
  latencyMs,
}: {
  report?: ReviewerReport
  disposition: Record<string, string>
  failed?: boolean
  failureReason?: string
  model?: string
  latencyMs?: number
}) {
  const role = report?.reviewer ?? '(unknown reviewer)'
  const issues = [...(report?.issues ?? [])].sort(
    (a, b) => severityRank(a.severity) - severityRank(b.severity),
  )
  const critical = issues.filter((issue) => String(issue.severity) === 'critical').length

  return (
    <Panel
      title={titleCase(role)}
      actions={
        <>
          {model ? <span className="tiny faint mono">{model}</span> : null}
          {latencyMs !== undefined ? <span className="tiny faint">{(latencyMs / 1000).toFixed(1)}s</span> : null}
          {failed ? (
            <Badge tone="bad">failed</Badge>
          ) : (
            <Badge tone={critical > 0 ? 'bad' : issues.length > 0 ? 'warn' : 'ok'}>
              {issues.length} issue{issues.length === 1 ? '' : 's'}
            </Badge>
          )}
        </>
      }
    >
      {failed ? (
        <p className="small muted">This reviewer failed and produced no report: {failureReason}</p>
      ) : !report ? (
        <EmptyState title="No report">This reviewer has not reported yet.</EmptyState>
      ) : (
        <>
          {report.scope ? (
            <div className="small muted" style={{ marginBottom: 10 }}>
              Scope: {report.scope}
            </div>
          ) : null}
          {report.overall_assessment ? (
            <p className="small" style={{ marginBottom: 10 }}>
              {report.overall_assessment}
            </p>
          ) : null}
          {issues.length === 0 ? (
            <p className="small muted">No issues found in the material reviewed.</p>
          ) : (
            issues.map((issue) => <IssueCard key={issue.id} issue={issue} disposition={disposition[issue.id]} />)
          )}
          {(report.unresolved_questions?.length ?? 0) > 0 ? (
            <div style={{ marginTop: 10 }}>
              <h3 className="small" style={{ marginBottom: 4 }}>
                Unresolved questions
              </h3>
              <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
                {report.unresolved_questions?.map((question, index) => <li key={index}>{question}</li>)}
              </ul>
            </div>
          ) : null}
          {(report.coverage_notes?.length ?? 0) > 0 ? (
            <KeyValues
              items={[
                {
                  label: 'Coverage notes',
                  value: (
                    <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
                      {report.coverage_notes?.map((note, index) => <li key={index}>{note}</li>)}
                    </ul>
                  ),
                },
              ]}
            />
          ) : null}
        </>
      )}
    </Panel>
  )
}
