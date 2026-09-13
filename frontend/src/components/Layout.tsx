import { NavLink, Outlet } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'

/** Application shell: navigation plus the operational facts worth always having visible. */
export function Layout() {
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 30_000 })

  const running = health.data?.running_runs?.length ?? 0
  const providers = health.data?.providers_available ?? []

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          Review<span>Flow</span>
        </div>
        <nav aria-label="Main">
          <NavLink to="/" end>
            Dashboard
          </NavLink>
          <NavLink to="/runs">Runs</NavLink>
          <NavLink to="/projects">Projects</NavLink>
          <NavLink to="/providers">Providers</NavLink>
          <NavLink to="/new">New review</NavLink>
        </nav>
        <div className="topbar-right">
          {running > 0 ? (
            <span className="badge info running">
              <span className="dot" aria-hidden="true" />
              {running} running
            </span>
          ) : null}
          <span className="tiny" title="Model providers discovered on this machine">
            {providers.length > 0 ? providers.join(', ') : 'no provider credentials'}
          </span>
        </div>
      </header>
      <main className="content">
        <Outlet />
      </main>
    </div>
  )
}
