/**
 * API client.
 *
 * One place that knows how to talk to the backend, including the error envelope the
 * backend guarantees (code, message, retryable, safe detail). Errors are surfaced with
 * that message rather than a generic failure, because the backend is written to give
 * actionable ones.
 */

import type {
  ArtifactRef,
  DoctorResponse,
  Health,
  ModelInfo,
  PreflightResult,
  Project,
  ProjectDetail,
  ProvidersResponse,
  RoleInfo,
  RunConfigSnapshot,
  RunCreateRequest,
  RunEvent,
  RunInspection,
  SampleInfo,
  SourceArtifact,
  WorkflowRunSummary,
} from './types'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly retryable: boolean
  readonly detail: Record<string, unknown>

  constructor(status: number, code: string, message: string, retryable: boolean, detail: Record<string, unknown> = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.retryable = retryable
    this.detail = detail
  }
}

const BASE = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
    })
  } catch (cause) {
    // A transport failure is retryable and worth saying so plainly.
    throw new ApiError(0, 'network_error', `Could not reach the ReviewFlow API: ${String(cause)}`, true)
  }

  if (response.status === 204) return undefined as T

  const text = await response.text()
  const body: unknown = text ? safeJson(text) : null

  if (!response.ok) {
    const envelope = body as { error?: { code?: string; message?: string; retryable?: boolean; detail?: Record<string, unknown> } }
    const error = envelope?.error
    throw new ApiError(
      response.status,
      error?.code ?? 'http_error',
      error?.message ?? `Request failed with status ${response.status}`,
      error?.retryable ?? false,
      error?.detail ?? {},
    )
  }
  return body as T
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

export interface PreflightRequestish {
  project_id?: string
  source_id?: string
  objective: string
  config: RunConfigSnapshot
  deep?: boolean
}

export const api = {
  health: () => request<Health>('/health'),

  providers: (provider?: string) =>
    request<ProvidersResponse>(`/providers${provider ? `?provider=${encodeURIComponent(provider)}` : ''}`),
  refreshProviders: () => request<ProvidersResponse>('/providers/refresh', { method: 'POST' }),
  runDoctor: (provider?: string) =>
    request<DoctorResponse>(`/providers/doctor${provider ? `?provider=${encodeURIComponent(provider)}` : ''}`, {
      method: 'POST',
    }),
  models: (options?: { provider?: string; catalogue?: boolean }) => {
    const params = new URLSearchParams()
    if (options?.provider) params.set('provider', options.provider)
    if (options?.catalogue) params.set('catalogue', 'true')
    const query = params.toString()
    return request<ModelInfo[]>(`/models${query ? `?${query}` : ''}`)
  },
  roles: () => request<RoleInfo[]>('/roles'),

  projects: () => request<Project[]>('/projects'),
  createProject: (payload: { name: string; description?: string; default_objective?: string }) =>
    request<Project>('/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  project: (projectId: string) => request<ProjectDetail>(`/projects/${projectId}`),
  sources: (projectId: string) => request<SourceArtifact[]>(`/projects/${projectId}/sources`),
  uploadSource: async (projectId: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    // No Content-Type header: the browser must set the multipart boundary itself.
    return request<SourceArtifact>(`/projects/${projectId}/sources`, { method: 'POST', body: form })
  },
  samples: () => request<SampleInfo[]>('/samples'),
  loadSample: (projectId: string, sampleId: string) =>
    request<SourceArtifact>(`/projects/${projectId}/samples?sample_id=${encodeURIComponent(sampleId)}`, {
      method: 'POST',
    }),

  runs: (options?: { projectId?: string; status?: string; limit?: number }) => {
    const params = new URLSearchParams()
    if (options?.projectId) params.set('project_id', options.projectId)
    if (options?.status) params.set('status', options.status)
    if (options?.limit) params.set('limit', String(options.limit))
    const query = params.toString()
    return request<WorkflowRunSummary[]>(`/workflows${query ? `?${query}` : ''}`)
  },
  preflight: (payload: PreflightRequestish) =>
    request<PreflightResult>('/workflows/preflight', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  createRun: (payload: RunCreateRequest) =>
    request<WorkflowRunSummary>('/workflows', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  run: (runId: string) =>
    request<{ run: WorkflowRunSummary; stages: RunInspection['stages']; is_running: boolean }>(`/workflows/${runId}`),
  inspection: (runId: string) => request<RunInspection>(`/workflows/${runId}/inspection`),
  events: (runId: string, after = 0) => request<RunEvent[]>(`/workflows/${runId}/events?after=${after}`),
  issues: (runId: string) =>
    request<{
      reports: RunInspection['reviewers']
      failed_reviewers: Record<string, string>
      conflicts: Record<string, unknown>[]
      unresolved_questions: string[]
      dispositions: Record<string, Record<string, unknown>>
      unresolved_issues: RunInspection['unresolved_issues']
      total_issues: number
    }>(`/workflows/${runId}/issues`),
  artifacts: (runId: string) => request<ArtifactRef[]>(`/workflows/${runId}/artifacts`),
  cancelRun: (runId: string) => request<{ run_id: string; cancelled: boolean }>(`/workflows/${runId}/cancel`, { method: 'POST' }),
  rerun: (runId: string, config?: RunConfigSnapshot) =>
    request<WorkflowRunSummary>(`/workflows/${runId}/rerun`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config ?? null),
    }),
}
