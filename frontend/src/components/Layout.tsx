import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { CommandPalette } from './CommandPalette'

const NAV = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/new', label: 'New review', end: false },
  { to: '/runs', label: 'Runs', end: false },
  { to: '/projects', label: 'Projects', end: false },
  { to: '/providers', label: 'Providers', end: false },
]

/**
 * Application shell.
 *
 * A persistent sidebar rather than a top bar: navigation stays put while run evidence
 * scrolls, and the operational facts worth always seeing (what is running, which
 * providers are reachable) live where they cannot collide with the workflow content.
 */
export function Layout() {
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 30_000 })

  const running = health.data?.running_runs?.length ?? 0
  const providers = health.data?.providers_available ?? []
  const [paletteOpen, setPaletteOpen] = useState(false)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setPaletteOpen((open) => !open)
      } else if (event.key === 'Escape') {
        setPaletteOpen(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div className="shell">
      <a className="skip" href="#content">
        Skip to content
      </a>
      <aside className="sidebar">
        <div className="brand">
          Review<span>Flow</span>
        </div>
        <button type="button" className="side-search" onClick={() => setPaletteOpen(true)}>
          <span>Search…</span>
          <kbd>⌘K</kbd>
        </button>
        <nav className="side-nav" aria-label="Main">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="side-foot">
          {running > 0 ? (
            <span className="badge info running">
              <span className="dot" aria-hidden="true" />
              {running} running
            </span>
          ) : null}
          <span className="who" title="Model providers discovered on this machine">
            {health.isError
              ? 'provider status unavailable'
              : providers.length > 0
                ? `providers · ${providers.join(', ')}`
                : 'no provider credentials'}
          </span>
        </div>
      </aside>
      <main className="content" id="content" tabIndex={-1}>
        <Outlet />
      </main>
      {paletteOpen ? <CommandPalette onClose={() => setPaletteOpen(false)} /> : null}
    </div>
  )
}
