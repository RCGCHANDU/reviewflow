/**
 * Frontend test utilities.
 *
 * The application talks to the API over `fetch` and to the run's event stream over
 * `EventSource`. Both are stubbed here so component tests exercise the real screens against
 * canned backend payloads - including the streaming path, which jsdom does not implement.
 */

import type { ReactElement, ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { render, type RenderResult } from '@testing-library/react'

/**
 * Render a component with the providers the app supplies.
 *
 * Pass ``path`` when the component reads route params (``useParams``): without a matching
 * route the params are empty and the screen would be tested in a state it can never be in.
 */
export function renderWithProviders(
  ui: ReactElement,
  { route = '/', path }: { route?: string; path?: string } = {},
): RenderResult {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0, gcTime: 0 } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        {path ? (
          <Routes>
            <Route path={path} element={ui} />
          </Routes>
        ) : (
          <Wrapper>{ui}</Wrapper>
        )}
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** Element passthrough so providers wrap the component under test without extra markup. */
function Wrapper({ children }: { children: ReactNode }) {
  return <>{children}</>
}

export interface RouteResponse {
  status?: number
  body: unknown
}

/** Install a fetch stub. The most specific matching route wins. */
export interface FetchStub {
  calls: { url: string; method: string }[]
  restore: () => void
}

export function mockFetch(
  routes: Record<string, RouteResponse | ((url: string) => RouteResponse)>,
): FetchStub {
  const calls: { url: string; method: string }[] = []
  const original = globalThis.fetch

  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    const method = (init?.method ?? 'GET').toUpperCase()
    calls.push({ url, method })

    // Longest path first: `/projects/prj_1` must beat `/projects`, or the detail endpoint
    // silently receives the list payload and the screen is tested in an impossible state.
    const candidates = Object.entries(routes)
      .map(([key, value]) => {
        const [routeMethod, routePath] = key.includes(' ') ? key.split(' ') : ['GET', key]
        return { method: routeMethod.toUpperCase(), path: routePath, value }
      })
      .filter((route) => route.method === method && url.includes(route.path))
      .sort((a, b) => b.path.length - a.path.length)

    if (candidates.length > 0) {
      const chosen = candidates[0]
      const response = typeof chosen.value === 'function' ? chosen.value(url) : chosen.value
      const status = response.status ?? 200
      return new Response(JSON.stringify(response.body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      })
    }

    // An unmatched request is a test bug, and it must be loud rather than silently empty.
    return new Response(
      JSON.stringify({ error: { code: 'unmatched', message: `no stub for ${method} ${url}`, retryable: false } }),
      { status: 404, headers: { 'Content-Type': 'application/json' } },
    )
  }) as typeof globalThis.fetch

  return {
    calls,
    restore: () => {
      globalThis.fetch = original
    },
  }
}

interface Listener {
  type: string
  handler: (event: MessageEvent<string>) => void
}

/** A controllable EventSource stand-in for the server-sent event stream. */
export class MockEventSource {
  static instances: MockEventSource[] = []

  readonly url: string
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  private listeners: Listener[] = []

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }

  addEventListener(type: string, handler: (event: MessageEvent<string>) => void): void {
    this.listeners.push({ type, handler })
  }

  close(): void {
    this.closed = true
  }

  /** Deliver a named event, exactly as the server would frame it. */
  emit(type: string, payload: unknown, lastEventId = ''): void {
    const message = { data: JSON.stringify(payload), lastEventId } as MessageEvent<string>
    for (const listener of this.listeners) {
      if (listener.type === type) listener.handler(message)
    }
  }

  /** Deliver a complete run event envelope, as the API actually frames it. */
  emitRunEvent(seq: number, type: string, payload: Record<string, unknown>, stage: string | null = null): void {
    this.emit(
      type,
      {
        id: seq,
        run_id: this.url.split('/').filter(Boolean).at(-2) ?? 'run_test',
        seq,
        timestamp: '2026-01-01T00:00:01.000Z',
        type,
        stage,
        payload,
      },
      String(seq),
    )
  }

  static reset(): void {
    MockEventSource.instances = []
  }

  static latest(): MockEventSource | undefined {
    return MockEventSource.instances.at(-1)
  }
}

export function installMockEventSource(): void {
  MockEventSource.reset()
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(globalThis as any).EventSource = MockEventSource
}
