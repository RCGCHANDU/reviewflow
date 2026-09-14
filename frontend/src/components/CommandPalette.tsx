import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'

/**
 * Command palette (⌘K / Ctrl-K).
 *
 * An established pattern for an operations tool: reach any screen, run or project without
 * hunting through navigation. It is a real dialog - labelled, modal, arrow-key navigable,
 * Escape to close, focus moved into the input on open and returned by the caller on close.
 */

type Item = { id: string; label: string; hint?: string; go: () => void }

export function CommandPalette({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [index, setIndex] = useState(0)
  const input = useRef<HTMLInputElement>(null)

  const runs = useQuery({ queryKey: ['runs', { limit: 20 }], queryFn: () => api.runs({ limit: 20 }) })
  const projects = useQuery({ queryKey: ['projects'], queryFn: api.projects })

  const items = useMemo<Item[]>(() => {
    const screens: Item[] = [
      { id: 's-dashboard', label: 'Dashboard', hint: 'screen', go: () => navigate('/') },
      { id: 's-new', label: 'New review', hint: 'screen', go: () => navigate('/new') },
      { id: 's-runs', label: 'Runs', hint: 'screen', go: () => navigate('/runs') },
      { id: 's-projects', label: 'Projects', hint: 'screen', go: () => navigate('/projects') },
      { id: 's-providers', label: 'Providers', hint: 'screen', go: () => navigate('/providers') },
    ]
    const runItems: Item[] = (runs.data ?? []).map((run) => ({
      id: run.id,
      label: run.objective.slice(0, 72) || run.id,
      hint: `${run.status} · ${run.id.slice(0, 14)}`,
      go: () => navigate(`/runs/${run.id}`),
    }))
    const projectItems: Item[] = (projects.data ?? []).map((project) => ({
      id: `p-${project.id}`,
      label: project.name,
      hint: 'project',
      go: () => navigate('/projects'),
    }))
    return [...screens, ...runItems, ...projectItems]
  }, [navigate, runs.data, projects.data])

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase()
    const pool = needle
      ? items.filter((item) => `${item.label} ${item.hint ?? ''}`.toLowerCase().includes(needle))
      : items
    return pool.slice(0, 9)
  }, [items, query])

  useEffect(() => {
    input.current?.focus()
  }, [])
  useEffect(() => {
    setIndex(0)
  }, [query])

  const choose = (item: Item | undefined) => {
    if (!item) return
    item.go()
    onClose()
  }

  const active = matches[index]

  return (
    <div className="cmdk-overlay" onMouseDown={onClose}>
      <div
        className="cmdk"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <input
          ref={input}
          className="cmdk-input"
          value={query}
          placeholder="Jump to a screen, run or project…"
          aria-label="Search screens, runs and projects"
          aria-controls="cmdk-list"
          aria-activedescendant={active ? `cmdk-${active.id}` : undefined}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'ArrowDown') {
              event.preventDefault()
              setIndex((i) => Math.min(i + 1, Math.max(matches.length - 1, 0)))
            } else if (event.key === 'ArrowUp') {
              event.preventDefault()
              setIndex((i) => Math.max(i - 1, 0))
            } else if (event.key === 'Enter') {
              event.preventDefault()
              choose(active)
            } else if (event.key === 'Escape') {
              event.preventDefault()
              onClose()
            }
          }}
        />
        <ul className="cmdk-list" id="cmdk-list" role="listbox" aria-label="Results">
          {matches.map((item, i) => (
            <li
              key={item.id}
              id={`cmdk-${item.id}`}
              role="option"
              aria-selected={i === index}
              className="cmdk-item"
              onMouseEnter={() => setIndex(i)}
              onMouseDown={() => choose(item)}
            >
              <span className="cmdk-label">{item.label}</span>
              {item.hint ? <span className="tiny faint">{item.hint}</span> : null}
            </li>
          ))}
          {matches.length === 0 ? <li className="cmdk-empty tiny faint">No matches</li> : null}
        </ul>
        <div className="cmdk-foot tiny faint">
          <kbd>↑</kbd>
          <kbd>↓</kbd> navigate · <kbd>↵</kbd> open · <kbd>esc</kbd> close
        </div>
      </div>
    </div>
  )
}
