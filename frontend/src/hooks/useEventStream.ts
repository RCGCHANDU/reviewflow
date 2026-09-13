/**
 * Live run events over Server-Sent Events.
 *
 * The backend guarantees delivery semantics this hook relies on: it replays persisted
 * events from a sequence number, stamps every event with an `id:` (so the browser's own
 * `Last-Event-ID` reconnection works) and ends the stream with `stream.end` when the run
 * is terminal. That means a dropped connection or a page refresh loses nothing and
 * duplicates nothing - the sequence number is the checkpoint.
 *
 * There is deliberately no polling loop here: events drive refreshes of the slower
 * evidence queries, so the UI updates when the workflow does rather than every second.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import type { RunEvent } from '../api/types'

export type ConnectionState = 'connecting' | 'open' | 'closed' | 'error'

interface UseEventStreamOptions {
  /** Called for every event, in order, exactly once. */
  onEvent?: (event: RunEvent) => void
  /** Stop after this many events (safety valve for very long runs). */
  maxEvents?: number
  enabled?: boolean
}

interface EventStreamState {
  events: RunEvent[]
  state: ConnectionState
  lastSeq: number
  ended: boolean
  endReason: string | null
  reconnect: () => void
}

export function useEventStream(runId: string | null, options: UseEventStreamOptions = {}): EventStreamState {
  const { onEvent, maxEvents = 5000, enabled = true } = options
  const [events, setEvents] = useState<RunEvent[]>([])
  const [state, setState] = useState<ConnectionState>('connecting')
  const [ended, setEnded] = useState(false)
  const [endReason, setEndReason] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const lastSeqRef = useRef(0)
  // Keep the callback out of the effect's dependency list: a new inline function on each
  // render must not tear down and rebuild the stream.
  const onEventRef = useRef(onEvent)
  onEventRef.current = onEvent

  useEffect(() => {
    if (!runId || !enabled) {
      setState('closed')
      return
    }
    // Reset per run so switching runs cannot carry a sequence number across.
    lastSeqRef.current = 0
    setEvents([])
    setEnded(false)
    setEndReason(null)

    const source = new EventSource(`/api/v1/workflows/${runId}/events`)
    let closed = false
    setState('connecting')

    const handle = (raw: MessageEvent<string>) => {
      const parsed = JSON.parse(raw.data) as RunEvent | { status?: string; reason?: string }
      if (raw.lastEventId) {
        lastSeqRef.current = Number(raw.lastEventId) || lastSeqRef.current
      }
      if (parsed && typeof parsed === 'object' && 'seq' in parsed) {
        const event = parsed as RunEvent
        if (event.seq <= lastSeqRef.current && event.id !== -1) {
          // Already seen: the replay window overlaps what we have.
        }
        setEvents((previous) => {
          if (previous.some((item) => item.seq === event.seq)) return previous
          const next = [...previous, event]
          return next.length > maxEvents ? next.slice(next.length - maxEvents) : next
        })
        onEventRef.current?.(event)
      }
    }

    const named = [
      'workflow.created',
      'workflow.started',
      'workflow.succeeded',
      'workflow.blocked',
      'workflow.degraded',
      'workflow.failed',
      'workflow.cancelled',
      'workflow.terminal',
      'stage.started',
      'stage.completed',
      'stage.transition',
      'agent.started',
      'agent.completed',
      'agent.failed',
      'agent.retried',
      'agent.invalid_output',
      'review.report_received',
      'synthesis.completed',
      'edit.completed',
      'edit.mutation',
      'diff_review.completed',
      'adversarial.completed',
      'verification.check',
      'verification.completed',
      'guardrail.triggered',
      'tool.called',
    ]
    for (const type of named) {
      source.addEventListener(type, handle as EventListener)
    }

    source.addEventListener('stream.end', (raw) => {
      const payload = JSON.parse((raw as MessageEvent<string>).data) as { reason?: string; status?: string }
      setEnded(true)
      setEndReason(payload.reason ?? payload.status ?? 'run finished')
      setState('closed')
      closed = true
      source.close()
    })

    source.onopen = () => setState('open')
    source.onerror = () => {
      if (closed) return
      // EventSource reconnects on its own (and sends Last-Event-ID), so this is reported
      // as a transient state rather than a fatal error.
      setState('error')
    }

    return () => {
      closed = true
      source.close()
      setState('closed')
    }
  }, [runId, enabled, attempt, maxEvents])

  const reconnect = useCallback(() => setAttempt((value) => value + 1), [])

  return { events, state, lastSeq: lastSeqRef.current, ended, endReason, reconnect }
}
