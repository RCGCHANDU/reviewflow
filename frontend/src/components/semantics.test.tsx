import { describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'

import { DiffView } from './DiffView'
import { ReviewerPanel } from './Findings'
import { RunStatusBadge } from './Status'
import { renderWithProviders } from '../test/utils'
import type { ReviewerReport } from '../api/types'

/**
 * UI semantics that the product depends on: terminal states stay distinct, failed
 * verification is never dressed up as success, reviewer reports stay separate, and the diff
 * is rendered from the engine's own text.
 */

describe('RunStatusBadge', () => {
  it('labels each terminal state distinctly and explains it', () => {
    const { unmount } = renderWithProviders(<RunStatusBadge status="blocked" />)
    expect(screen.getByText('Blocked')).toBeInTheDocument()
    unmount()

    renderWithProviders(<RunStatusBadge status="succeeded" />)
    expect(screen.getByText('Succeeded')).toBeInTheDocument()
    // The distinction the product must never blur.
    expect(screen.queryByText('Blocked')).not.toBeInTheDocument()
  })

  it('marks a running state as live', () => {
    const { container } = renderWithProviders(<RunStatusBadge status="running" />)
    expect(screen.getByText('Running')).toBeInTheDocument()
    expect(container.querySelector('.badge.running')).not.toBeNull()
  })
})

describe('ReviewerPanel', () => {
  const report: ReviewerReport = {
    reviewer: 'evidence_auditor',
    scope: 'abstract and conclusion',
    overall_assessment: 'Two claims are not supported by the supplied evidence.',
    issues: [
      {
        id: 'ev-1',
        severity: 'critical',
        category: 'scope_mismatch',
        location: 'Abstract, line 7',
        description: 'The 97.3% figure is presented as a test-split result.',
        evidence: 'reviewer note: the test split has not been run',
        evidence_status: 'observed',
        confidence: 0.9,
        recommendation: 'Label the result as development-subset.',
      },
      { id: 'ev-2', severity: 'minor', category: 'clarity', description: 'Wording is ambiguous.' },
    ],
  }

  it("renders one panel per reviewer with that reviewer's findings", () => {
    renderWithProviders(<ReviewerPanel report={report} disposition={{ 'ev-1': 'accepted' }} model="gpt-5.6-luna" />)
    expect(screen.getByText('Evidence Auditor')).toBeInTheDocument()
    expect(screen.getByText('gpt-5.6-luna')).toBeInTheDocument()
    expect(screen.getByText('ev-1')).toBeInTheDocument()
    expect(screen.getByText('ev-2')).toBeInTheDocument()
    // The synthesis decision is shown on the finding rather than replacing it.
    expect(screen.getByText('accepted')).toBeInTheDocument()
    expect(screen.getByText(/97.3% figure/)).toBeInTheDocument()
  })

  it('surfaces critical findings and shows a failed reviewer honestly', () => {
    const { unmount } = renderWithProviders(<ReviewerPanel report={report} disposition={{}} />)
    expect(screen.getByText('critical')).toBeInTheDocument()
    unmount()

    renderWithProviders(
      <ReviewerPanel disposition={{}} failed failureReason="invalid_output: schema mismatch after 2 attempts" />,
    )
    expect(screen.getByText('failed')).toBeInTheDocument()
    expect(screen.getByText(/schema mismatch after 2 attempts/)).toBeInTheDocument()
  })
})

describe('DiffView', () => {
  it('renders the engine diff with addition and deletion counts', () => {
    const diff = [
      '--- a/manuscript.md',
      '+++ b/manuscript.md',
      '@@ -7,3 +7,3 @@',
      ' context line',
      '-the test split reaches 97.3%',
      '+the development subset reaches 97.3%',
    ].join('\n')

    const { container } = renderWithProviders(<DiffView diff={diff} label="manuscript.md" />)
    expect(screen.getByText('manuscript.md')).toBeInTheDocument()
    expect(container.querySelectorAll('.diff-line.added')).toHaveLength(1)
    expect(container.querySelectorAll('.diff-line.removed')).toHaveLength(1)
    expect(screen.getByText(/development subset reaches/)).toBeInTheDocument()
  })
})
