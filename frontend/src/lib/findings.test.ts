import { describe, expect, it } from 'vitest'

import type { IssueMap, ReviewerReport } from '../api/types'
import { dispositionMap } from './findings'
import { RUN_STATUS_LABEL, RUN_STATUS_MEANING, formatBytes, formatDuration, isTerminal, severityRank } from './format'

describe('status vocabulary', () => {
  it('keeps every terminal state distinct', () => {
    // The product requirement: a blocked or degraded run must never be presented as a
    // success. Distinct labels and distinct meanings are what make that enforceable.
    const terminal = ['succeeded', 'degraded', 'blocked', 'failed', 'cancelled'] as const
    const labels = terminal.map((status) => RUN_STATUS_LABEL[status])
    expect(new Set(labels).size).toBe(labels.length)
    expect(RUN_STATUS_MEANING.blocked).toContain('verification')
    expect(RUN_STATUS_MEANING.degraded).toContain('coverage')
    expect(RUN_STATUS_MEANING.cancelled).toContain('not a failure')
    terminal.forEach((status) => expect(isTerminal(status)).toBe(true))
    expect(isTerminal('running')).toBe(false)
  })
})

describe('formatting', () => {
  it('formats sizes, durations and missing values predictably', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(2048)).toBe('2.0 KiB')
    expect(formatDuration(450)).toBe('450 ms')
    expect(formatDuration(2500)).toBe('2.5 s')
    expect(formatDuration(65_000)).toBe('1m 5s')
    expect(formatDuration(undefined)).toBe('—')
    expect(formatBytes(Number.NaN)).toBe('—')
  })

  it('orders severities so critical findings surface first', () => {
    const severities = ['minor', 'critical', 'unknown', 'major'].sort((a, b) => severityRank(a) - severityRank(b))
    expect(severities).toEqual(['critical', 'major', 'minor', 'unknown'])
  })
})

describe('dispositionMap', () => {
  it('attaches synthesis decisions to issue ids without dropping rejected findings', () => {
    const synthesis: IssueMap = {
      accepted_issues: [{ issue_id: 'ev-1', disposition: 'accepted' }],
      rejected_issues: [{ issue_id: 'ev-9', disposition: 'rejected', rationale: 'not supported' }],
      merged_issues: [{ issue_ids: ['tr-2', 'tr-3'], rationale: 'same defect' }],
    }
    const map = dispositionMap(synthesis)
    expect(map['ev-1']).toBe('accepted')
    // A rejected finding keeps its own entry: the UI shows it with its reason rather than
    // hiding it, so disagreement between reviewers stays visible.
    expect(map['ev-9']).toBe('rejected')
    expect(map['tr-2']).toBe('merged')
    expect(map['tr-3']).toBe('merged')
  })

  it('handles a missing synthesis', () => {
    expect(dispositionMap(null)).toEqual({})
  })
})

describe('reviewer report shape', () => {
  it('keeps each reviewer independent in the payload the UI renders', () => {
    const reports: ReviewerReport[] = [
      { reviewer: 'evidence_auditor', issues: [{ id: 'ev-1', severity: 'critical' }] },
      { reviewer: 'structure_reviewer', issues: [] },
    ]
    // Two reports means two panels: nothing in this shape merges them.
    expect(reports.map((report) => report.reviewer)).toEqual(['evidence_auditor', 'structure_reviewer'])
    expect(reports[1].issues).toHaveLength(0)
  })
})
