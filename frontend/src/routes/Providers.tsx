import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { Badge, EmptyState, Loading, Notice, Panel, PassBadge } from '../components/Status'
import { titleCase } from '../lib/format'

/**
 * Providers and models.
 *
 * Everything here is *discovered*: provider availability comes from the backend's credential
 * discovery, models from the router's profiles (and, on request, each provider's own
 * catalogue). The page never hard-codes a model name, and it never receives a credential -
 * only masked, identifying metadata.
 *
 * The capability probe makes real, billable model calls, so it is behind an explicit button
 * and states that clearly.
 */
export function Providers() {
  const queryClient = useQueryClient()
  const [doctorResult, setDoctorResult] = useState<Awaited<ReturnType<typeof api.runDoctor>> | null>(null)
  const [doctorError, setDoctorError] = useState<string | null>(null)
  const [showCatalogue, setShowCatalogue] = useState(false)

  const providers = useQuery({ queryKey: ['providers'], queryFn: () => api.providers() })
  const roles = useQuery({ queryKey: ['roles'], queryFn: api.roles })
  const models = useQuery({
    queryKey: ['models', { catalogue: showCatalogue }],
    queryFn: () => api.models({ catalogue: showCatalogue }),
  })

  const refresh = useMutation({
    mutationFn: () => api.refreshProviders(),
    onSuccess: (data) => {
      queryClient.setQueryData(['providers'], data)
      void queryClient.invalidateQueries({ queryKey: ['models'] })
    },
  })

  const doctor = useMutation({
    mutationFn: (provider?: string) => api.runDoctor(provider),
    onSuccess: (data) => {
      setDoctorResult(data)
      setDoctorError(null)
    },
    onError: (error) => {
      setDoctorError(String(error))
      setDoctorResult(null)
    },
  })

  return (
    <>
      <div className="page-head">
        <div className="grow">
          <h1>Providers &amp; models</h1>
          <p>
            Credentials are read from the stores the provider CLIs already wrote. Tokens never reach this page — only
            masked, identifying metadata.
          </p>
        </div>
        <div className="row">
          <button type="button" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
            {refresh.isPending ? 'Refreshing…' : 'Refresh discovery'}
          </button>
          <button
            type="button"
            className="primary"
            disabled={doctor.isPending}
            onClick={() => doctor.mutate(undefined)}
            title="Makes real model calls to both providers"
          >
            {doctor.isPending ? 'Probing…' : 'Run capability probe'}
          </button>
        </div>
      </div>

      {doctor.isPending ? (
        <Notice tone="warn" title="Capability probe running">
          This makes real model calls to every available provider (a normal completion, a structured output and a tool
          call per model). It costs tokens and can take a minute.
        </Notice>
      ) : null}

      {doctorError ? (
        <Notice tone="bad" title="Probe failed">
          {doctorError}
        </Notice>
      ) : null}

      <div className="grid cols-2">
        <Panel title="Discovered providers" flush>
          {providers.isLoading ? (
            <Loading />
          ) : providers.isError ? (
            <EmptyState title="Could not load providers">{String(providers.error)}</EmptyState>
          ) : (
            <div className="scroll-x">
              <table>
                <thead>
                  <tr>
                    <th>Provider</th>
                    <th>State</th>
                    <th>Transport</th>
                    <th>Account / source</th>
                  </tr>
                </thead>
                <tbody>
                  {(providers.data?.providers ?? []).map((provider) => (
                    <tr key={provider.name}>
                      <td>
                        <strong>{provider.name}</strong>
                        {provider.name === providers.data?.active_provider ? (
                          <div className="tiny">
                            <Badge tone="info">active</Badge>
                          </div>
                        ) : null}
                      </td>
                      <td>
                        <Badge tone={provider.available ? 'ok' : 'neutral'}>
                          {provider.available ? 'signed in' : 'not signed in'}
                        </Badge>
                        {!provider.available && provider.hint ? (
                          <div className="tiny faint" style={{ maxWidth: 260 }}>
                            {provider.hint}
                          </div>
                        ) : null}
                      </td>
                      <td className="small muted">
                        {provider.api_mode}
                        {provider.requires_streaming ? (
                          <div className="tiny faint" title="Every agent call, including guardrails and nested runs">
                            requires streaming
                          </div>
                        ) : null}
                      </td>
                      <td className="small">
                        {provider.account ?? '—'}
                        <div className="tiny faint mono">{provider.auth_source ?? ''}</div>
                        {provider.seconds_left !== null && provider.seconds_left !== undefined ? (
                          <div className="tiny faint">
                            token valid for {Math.round(provider.seconds_left / 3600)}h
                            {provider.refreshable ? ' (refreshable)' : ''}
                          </div>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel
          title="Models"
          flush
          actions={
            <label className="small muted row tight" style={{ margin: 0 }}>
              <input
                type="checkbox"
                checked={showCatalogue}
                onChange={(event) => setShowCatalogue(event.target.checked)}
                style={{ width: 'auto' }}
              />
              query provider catalogues
            </label>
          }
        >
          {models.isLoading ? (
            <Loading />
          ) : (models.data ?? []).length === 0 ? (
            <EmptyState title="No models available">No provider credential is configured.</EmptyState>
          ) : (
            <div className="scroll-x" style={{ maxHeight: 420, overflowY: 'auto' }}>
              <table>
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Provider</th>
                    <th>Default for</th>
                  </tr>
                </thead>
                <tbody>
                  {models.data?.map((model) => (
                    <tr key={`${model.provider}:${model.id}`}>
                      <td className="mono small">{model.id}</td>
                      <td className="small muted">{model.provider}</td>
                      <td className="small">
                        {model.default_for.length === 0 ? (
                          <span className="faint tiny">catalogue</span>
                        ) : (
                          model.default_for.map((tier) => (
                            <Badge key={tier} tone="neutral">
                              {tier}
                            </Badge>
                          ))
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>

      {doctorResult ? (
        <div style={{ marginTop: 16 }}>
          <Panel
            title={`Capability probe — ${doctorResult.passed}/${doctorResult.total} checks passed`}
            flush
            actions={<Badge tone={doctorResult.passed === doctorResult.total ? 'ok' : 'warn'}>{doctorResult.active_provider ?? 'n/a'}</Badge>}
          >
            {doctorResult.total === 0 ? (
              <EmptyState title="Nothing probed">{doctorResult.note || 'No provider credential was available.'}</EmptyState>
            ) : (
              <div className="scroll-x">
                <table>
                  <thead>
                    <tr>
                      <th>Model</th>
                      <th>Check</th>
                      <th>Result</th>
                      <th>Evidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {doctorResult.checks.map((check, index) => (
                      <tr key={`${check.provider}-${check.model}-${check.check}-${index}`}>
                        <td className="mono small">{check.model}</td>
                        <td className="small">{check.check}</td>
                        <td>
                          <PassBadge passed={check.ok} />
                        </td>
                        <td className="small muted" style={{ maxWidth: 420 }}>
                          {check.detail}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
      ) : null}

      <div style={{ marginTop: 16 }}>
        <Panel title="Specialist roles" flush>
          {roles.isLoading ? (
            <Loading />
          ) : (
            <div className="scroll-x">
              <table>
                <thead>
                  <tr>
                    <th>Role</th>
                    <th>Responsibility</th>
                    <th>Authority</th>
                    <th>Default model</th>
                  </tr>
                </thead>
                <tbody>
                  {(roles.data ?? []).map((role) => (
                    <tr key={role.role_key}>
                      <td>
                        <strong>{role.agent_name}</strong>
                        <div className="tiny faint mono">{role.role_key}</div>
                      </td>
                      <td className="small muted" style={{ maxWidth: 420 }}>
                        {role.description}
                        <div className="tiny faint">{role.focus.map(titleCase).join(' · ')}</div>
                      </td>
                      <td className="small">
                        {role.may_mutate ? (
                          <Badge tone="warn">write-capable</Badge>
                        ) : (
                          <Badge tone="neutral">read-only</Badge>
                        )}
                        <div className="tiny faint mono">{role.allowed_tools.join(', ')}</div>
                      </td>
                      <td className="mono small">{role.default_model || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>

      <Notice>
        Only the controlled editor holds a mutation tool. Reviewers are built without one, a tool guardrail rejects
        mutating calls, and the write itself demands an application-issued grant for a single path at a known hash.
      </Notice>
    </>
  )
}
