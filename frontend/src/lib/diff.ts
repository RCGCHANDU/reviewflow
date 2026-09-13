/**
 * Unified diff parsing.
 *
 * The engine already produces a real unified diff (computed from bytes on disk), so this
 * only interprets that text — it never computes a second diff, and so cannot disagree
 * with the artifact the verifier hashed.
 */

interface DiffLine {
  kind: 'context' | 'added' | 'removed' | 'hunk' | 'meta'
  text: string
  oldNumber: number | null
  newNumber: number | null
}

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/

export function parseUnifiedDiff(diff: string): DiffLine[] {
  const lines: DiffLine[] = []
  let oldNumber = 0
  let newNumber = 0

  for (const raw of diff.split('\n')) {
    const hunk = HUNK.exec(raw)
    if (hunk) {
      oldNumber = Number(hunk[1])
      newNumber = Number(hunk[2])
      lines.push({ kind: 'hunk', text: raw, oldNumber: null, newNumber: null })
      continue
    }
    if (raw.startsWith('---') || raw.startsWith('+++') || raw.startsWith('diff ') || raw.startsWith('index ')) {
      lines.push({ kind: 'meta', text: raw, oldNumber: null, newNumber: null })
      continue
    }
    if (raw.startsWith('+')) {
      lines.push({ kind: 'added', text: raw.slice(1), oldNumber: null, newNumber: newNumber++ })
      continue
    }
    if (raw.startsWith('-')) {
      lines.push({ kind: 'removed', text: raw.slice(1), oldNumber: oldNumber++, newNumber: null })
      continue
    }
    if (raw.startsWith('\\')) {
      lines.push({ kind: 'meta', text: raw, oldNumber: null, newNumber: null })
      continue
    }
    // A trailing empty line from the split is not a context line.
    if (raw === '' && lines.length > 0) {
      lines.push({ kind: 'context', text: '', oldNumber: oldNumber++, newNumber: newNumber++ })
      continue
    }
    lines.push({ kind: 'context', text: raw.startsWith(' ') ? raw.slice(1) : raw, oldNumber: oldNumber++, newNumber: newNumber++ })
  }
  return lines
}
