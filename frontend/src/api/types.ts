/**
 * API types.
 *
 * These mirror the backend's Pydantic contracts. The engine-owned parts of a run
 * (reviewer reports, the issue map, the adversarial verdict, verification results) are
 * intentionally typed as structured-but-open shapes: the engine is the single definition
 * of their fields, and re-declaring them here would create a second schema to drift.
 */

export type RunMode = 'scripted' | 'live'

export type RunStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'degraded'
  | 'blocked'
  | 'failed'
  | 'cancelled'

export type StageStateName =
  | 'pending'
  | 'running'
  | 'completed'
  | 'warning'
  | 'blocked'
  | 'failed'
  | 'skipped'

export interface Project {
  id: string
  name: string
  description: string
  created_at: string
  updated_at: string
  default_objective: string
}

export interface SourceArtifact {
  id: string
  project_id: string
  filename: string
  content_type: string
  size_bytes: number
  sha256: string
  created_at: string
  preview: string | null
}

export interface UsageByRole {
  role: string
  calls: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  latency_ms: number
}

export interface RunUsage {
  requests?: number
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
  model_calls?: number
  wall_clock_ms?: number
  by_role?: UsageByRole[]
  cost?: { amount: number | null; currency: string | null; label: string }
}

export interface WorkflowRunSummary {
  id: string
  project_id: string
  project_name: string | null
  source_id: string
  source_filename: string | null
  objective: string
  mode: RunMode
  status: RunStatus
  current_stage: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  revision_round: number
  failure_reason: string | null
  usage: RunUsage
  config: Record<string, unknown>
  provider_snapshot: Record<string, unknown>
}

export interface StageState {
  stage: string
  state: StageStateName
  started_at: string | null
  finished_at: string | null
  detail: Record<string, unknown>
}

export interface RunEvent {
  id: number
  run_id: string
  seq: number
  timestamp: string
  type: string
  stage: string | null
  payload: Record<string, unknown>
}

export interface ArtifactRef {
  id: string
  run_id: string
  type: string
  filename: string
  sha256: string
  size_bytes: number
  created_at: string
  download_url: string
}

/** A reviewer's finding, as the engine's own contract defines it. */
export interface Issue {
  id: string
  reviewer?: string
  location?: string
  category?: string
  severity?: string
  description?: string
  evidence?: string
  evidence_status?: string
  recommendation?: string
  confidence?: number
}

export interface ReviewerReport {
  reviewer: string
  scope?: string
  issues?: Issue[]
  unresolved_questions?: string[]
  overall_assessment?: string
  coverage_notes?: string[]
}

export interface IssueMap {
  accepted_issues?: { issue_id: string; disposition?: string; rationale?: string; action_required?: string }[]
  rejected_issues?: { issue_id: string; disposition?: string; rationale?: string }[]
  merged_issues?: { issue_ids: string[]; rationale?: string }[]
  conflicts?: { issue_ids?: string[]; summary?: string; reason?: string; adjudicated?: boolean; resolution_note?: string }[]
  unresolved_questions?: string[]
  change_required?: boolean
  change_rationale?: string
}

export interface EditResult {
  target?: string
  changes?: { issue_ids?: string[]; location?: string; description?: string; rationale?: string; kind?: string; before?: string; after?: string }[]
  unresolved_items?: string[]
  verification_requested?: string[]
  no_change_reason?: string
}

export interface DiffReview {
  reviewer?: string
  out_of_scope_changes?: string[]
  unsupported_assertions?: string[]
  removed_content_risks?: string[]
  semantic_drift?: string[]
  notes?: string[]
  approved?: boolean
}

/**
 * One concrete reason the revision should be rejected.
 *
 * The engine sends structured findings here, not strings: rendering one directly is a React
 * error ("objects are not valid as a React child"), which is exactly what happened when this
 * was typed as `string[]`.
 */
export interface AdversarialFinding {
  description: string
  location: string
  evidence: string
  related_issue_id?: string | null
}

export interface AdversarialVerdict {
  passed?: boolean
  critical_issues?: AdversarialFinding[]
  major_issues?: AdversarialFinding[]
  minor_issues?: AdversarialFinding[]
  rationale?: string
  rejection_reasons?: string[]
}

export interface VerificationResult {
  check: string
  command_or_method?: string
  passed: boolean
  evidence?: string
  error?: string | null
  /** 'deterministic' or 'semantic': which class of check this is. */
  kind?: string
  /** A mandatory check failing blocks success; others are advisory. */
  mandatory?: boolean
  duration_ms?: number
}

export interface VerificationReport {
  results?: VerificationResult[]
  /** The stage the report was produced in (the engine's own field name). */
  stage?: string
}

export interface FinalReport {
  executive_summary?: string
  accepted_issues_summary?: string[]
  rejected_issues_summary?: string[]
  reviewer_disagreements?: { issue_id?: string; reviewers?: string[]; nature?: string; resolution?: string }[]
  changes_applied?: { issue_id?: string; change_id?: string; summary?: string }[]
  unresolved_issues?: { issue_id?: string; severity?: string; summary?: string }[]
  verification_summary?: { passed?: number; failed?: number; failing_checks?: string[] } | string
  disclosures?: string[]
  claimed_status?: string
  status?: string
  recommendations?: string[]
}

export interface Coverage {
  expected?: string[]
  succeeded?: string[]
  failed?: string[]
}

export interface RunInspection {
  run: WorkflowRunSummary
  stages: StageState[]
  plan: Record<string, unknown> | null
  reviewers: ReviewerReport[]
  failed_reviewers: Record<string, string>
  synthesis: IssueMap | null
  edit: EditResult | null
  diff: string | null
  diff_review: DiffReview | null
  adversarial: AdversarialVerdict | null
  verification: VerificationReport | null
  final_report: FinalReport | null
  coverage: Coverage | null
  unresolved_issues: { issue_id?: string; severity?: string; summary?: string }[]
  terminal: { status?: string; reason?: string; rounds_executed?: number; failures?: string[] } | null
  usage: RunUsage
  artifacts: ArtifactRef[]
  status_explanation: string
}

export interface ProviderInfo {
  name: string
  available: boolean
  api_mode: string
  requires_streaming: boolean
  auth_source: string | null
  account: string | null
  expires_at: number | null
  seconds_left: number | null
  refreshable: boolean
  base_url: string | null
  hint: string
}

export interface ProvidersResponse {
  active_provider: string | null
  requested_provider: string
  providers: ProviderInfo[]
  routing: Record<string, unknown>
  role_defaults: Record<string, string>
}

export interface ModelInfo {
  id: string
  provider: string
  label: string
  default_for: string[]
  capabilities: Record<string, boolean>
}

export interface DoctorCheck {
  provider: string
  model: string
  check: string
  ok: boolean
  detail: string
}

export interface DoctorResponse {
  requested_provider: string
  active_provider: string | null
  passed: number
  total: number
  checks: DoctorCheck[]
  note: string
}

export interface RoleInfo {
  role_key: string
  agent_name: string
  description: string
  focus: string[]
  may_mutate: boolean
  allowed_tools: string[]
  mandatory: boolean
  planner_selectable: boolean
  default_model: string
  tier: string
}

export interface LimitsConfig {
  max_revision_rounds: number
  max_turns_per_agent: number
  max_reviewer_concurrency: number
  agent_timeout_s: number
  max_transient_retries: number
  max_invalid_output_retries: number
  max_total_tokens: number
  min_successful_reviewers: number
  semantic_checks: string[] | null
}

export interface RunConfigSnapshot {
  mode: RunMode
  provider: string
  role_models: Record<string, string>
  limits: LimitsConfig
  reviewers: string[] | null
  scripted_scenario: string
}

export interface RunCreateRequest {
  project_id: string
  source_id: string
  objective: string
  config: RunConfigSnapshot
  constraints?: string[]
}

export interface PreflightCheck {
  name: string
  ok: boolean
  detail: string
  blocking: boolean
}

export interface PreflightResult {
  ok: boolean
  checks: PreflightCheck[]
  blocking_failures: string[]
}

export interface ProjectDetail {
  project: Project
  sources: SourceArtifact[]
  runs: WorkflowRunSummary[]
}

export interface Health {
  status: string
  data_root: string
  active_provider: string | null
  providers_available: string[]
  running_runs: string[]
  scripted_runs_enabled: boolean
  live_runs_enabled: boolean
}

export interface SampleInfo {
  id: string
  name: string
  description: string
}

export interface ApiErrorPayload {
  error: { code: string; message: string; retryable: boolean; detail: Record<string, unknown> }
}
