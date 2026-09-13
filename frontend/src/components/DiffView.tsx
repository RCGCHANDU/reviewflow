import { parseUnifiedDiff } from '../lib/diff'

/** Unified diff viewer: renders the engine's own diff text. */

export function DiffView({ diff, label }: { diff: string; label?: string }) {
  const lines = parseUnifiedDiff(diff)
  const added = lines.filter((line) => line.kind === 'added').length
  const removed = lines.filter((line) => line.kind === 'removed').length

  return (
    <div>
      <div className="row small muted" style={{ marginBottom: 8 }}>
        {label ? <strong style={{ color: 'var(--text)' }}>{label}</strong> : null}
        <span className="badge ok">
          <span className="dot" aria-hidden="true" />+{added}
        </span>
        <span className="badge bad">
          <span className="dot" aria-hidden="true" />−{removed}
        </span>
        <span className="faint tiny">{lines.length} lines</span>
      </div>
      <div className="diff" role="table" aria-label="Unified diff">
        {lines.map((line, index) => (
          <div className={`diff-line ${line.kind}`} key={index} role="row">
            <span className="old-no" aria-hidden="true">
              {line.oldNumber ?? ''}
            </span>
            <span className="new-no" aria-hidden="true">
              {line.newNumber ?? ''}
            </span>
            <span className="sign" aria-hidden="true">
              {line.kind === 'added' ? '+' : line.kind === 'removed' ? '−' : ''}
            </span>
            <span className="code">{line.text}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
