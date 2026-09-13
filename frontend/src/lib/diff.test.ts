import { describe, expect, it } from 'vitest'

import { parseUnifiedDiff } from './diff'

const DIFF = [
  '--- a/manuscript.md',
  '+++ b/manuscript.md',
  '@@ -7,7 +7,7 @@',
  ' We introduce a sparse retrieval front-end.',
  '-On the Haystack-LVBench split, our pipeline reaches 97.3% accuracy.',
  '+On the development subset of the split, our pipeline reaches 97.3% accuracy.',
  ' ',
  ' ## Method',
  '',
].join('\n')

describe('parseUnifiedDiff', () => {
  it('classifies headers, hunks, additions, deletions and context', () => {
    const lines = parseUnifiedDiff(DIFF)
    const kinds = lines.map((line) => line.kind)
    expect(kinds[0]).toBe('meta')
    expect(kinds[1]).toBe('meta')
    expect(kinds[2]).toBe('hunk')
    expect(kinds).toContain('removed')
    expect(kinds).toContain('added')
  })

  it('numbers the two sides independently and correctly', () => {
    const lines = parseUnifiedDiff(DIFF)
    const removed = lines.find((line) => line.kind === 'removed')
    const added = lines.find((line) => line.kind === 'added')
    // The hunk header says old line 7, new line 7. One context line precedes the change,
    // so the removed line is old-8 and the added line is new-8.
    expect(removed?.oldNumber).toBe(8)
    expect(removed?.newNumber).toBeNull()
    expect(added?.newNumber).toBe(8)
    expect(added?.oldNumber).toBeNull()
  })

  it('strips the diff marker from the rendered text', () => {
    const lines = parseUnifiedDiff(DIFF)
    expect(lines.find((line) => line.kind === 'added')?.text.startsWith('+')).toBe(false)
    expect(lines.find((line) => line.kind === 'added')?.text).toContain('development subset')
  })

  it('handles an empty diff without inventing lines', () => {
    expect(parseUnifiedDiff('').filter((line) => line.kind !== 'context')).toHaveLength(0)
  })
})
