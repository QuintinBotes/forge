/**
 * TypeScript mirrors of the Python `forge_contracts` DTOs (Pydantic v2).
 *
 * These are hand-maintained against `packages/contracts/forge_contracts` and
 * kept intentionally minimal in Phase 0 (the board surface Task 1.6 needs). Enum
 * string values match the Python `StrEnum` members verbatim.
 */

// --- Enums (string unions matching forge_contracts.enums) ----------------- //

export const TASK_STATUSES = [
  "backlog",
  "ready",
  "ready_for_agent",
  "in_progress",
  "in_review",
  "blocked",
  "done",
  "cancelled",
] as const;
export type TaskStatus = (typeof TASK_STATUSES)[number];

export const TASK_PRIORITIES = ["low", "medium", "high", "urgent"] as const;
export type Priority = (typeof TASK_PRIORITIES)[number];

export const TASK_KINDS = [
  "feature",
  "bug",
  "chore",
  "spike",
  "incident",
  "change_request",
  "doc",
] as const;
export type TaskKind = (typeof TASK_KINDS)[number];

export type ExecutionMode = "single_agent" | "supervised_multi_agent";

export const INCIDENT_SEVERITIES = ["low", "medium", "high", "critical"] as const;
export type IncidentSeverity = (typeof INCIDENT_SEVERITIES)[number];

// --- DTOs ----------------------------------------------------------------- //

export interface KnowledgeScope {
  source_ids?: string[];
  kinds?: string[];
  path_globs?: string[];
}

export interface EpicDTO {
  id?: string | null;
  key?: string | null;
  project_id?: string | null;
  title: string;
  description?: string | null;
  status?: string;
  spec_id?: string | null;
  labels?: string[];
  created_at?: string | null;
  updated_at?: string | null;
}

export interface TaskDTO {
  id?: string | null;
  key?: string | null;
  project_id?: string | null;
  epic_id?: string | null;
  spec_id?: string | null;
  kind?: TaskKind;
  title: string;
  description?: string | null;
  status?: TaskStatus;
  priority?: Priority;
  estimate?: number | null;
  execution_mode?: ExecutionMode;
  instructions_profile?: string | null;
  skill_profile?: string | null;
  labels?: string[];
  assignee_id?: string | null;
  sprint_id?: string | null;
  milestone_id?: string | null;
  depends_on?: string[];
  created_at?: string | null;
  updated_at?: string | null;
}

export interface SprintDTO {
  id?: string | null;
  project_id?: string | null;
  name: string;
  goal?: string | null;
  starts_at?: string | null;
  ends_at?: string | null;
  task_ids?: string[];
}

export interface MilestoneDTO {
  id?: string | null;
  project_id?: string | null;
  name: string;
  description?: string | null;
  due_at?: string | null;
}

export interface IncidentDTO {
  id?: string | null;
  key?: string | null;
  project_id?: string | null;
  title: string;
  description?: string | null;
  severity?: IncidentSeverity;
  state?: string;
  created_at?: string | null;
  updated_at?: string | null;
}

/** One entry in a bulk board mutation (POST /board/tasks/bulk). */
export interface BulkUpdate {
  task_id: string;
  status?: TaskStatus;
  priority?: Priority;
  assignee_id?: string | null;
  sprint_id?: string | null;
  labels?: string[];
}

/** The authenticated principal (GET /auth/me) — drives "assign to me". */
export interface Principal {
  user_id: string;
  workspace_id: string;
  role?: string;
  email?: string | null;
  auth_method?: string;
}

// --- Knowledge -------------------------------------------------------------- //

export interface RetrievedChunk {
  id?: string | null;
  source_id?: string | null;
  path?: string | null;
  content: string;
  score: number;
  start_line?: number | null;
  end_line?: number | null;
}

export interface KnowledgeSearchRequest {
  query: string;
  scope?: KnowledgeScope;
  k?: number;
}

// --- Service / stub envelopes (apps/api) ----------------------------------- //

export interface ServiceInfo {
  name: string;
  version: string;
  environment: string;
  docs_url?: string | null;
}

export interface HealthResponse {
  status: string;
  [key: string]: unknown;
}

/** Shape returned by every not-yet-implemented Phase-0 stub route (HTTP 501). */
export interface NotImplementedResponse {
  status: "not_implemented";
  detail: string;
  router: string;
  operation: string;
}

// --- Approvals (F36 unified /approvals router) ---------------------------- //
// Hand-maintained mirror of `forge_approval.models` (Pydantic v2). Enum string
// values match the Python StrEnum members verbatim.

/** The six approval gate types (forge_contracts.enums.ApprovalGate). */
export const GATE_TYPES = [
  "spec",
  "plan",
  "pr",
  "deploy",
  "incident_remediation",
  "policy_override",
] as const;
export type GateType = (typeof GATE_TYPES)[number];

/** Gate lifecycle (ApprovalStatus + the SLA sweeper's `expired`). */
export const GATE_STATUSES = [
  "pending",
  "approved",
  "rejected",
  "changes_requested",
  "expired",
] as const;
export type GateStatus = (typeof GATE_STATUSES)[number];

/** The decisions a reviewer can record on a gate. */
export const APPROVAL_ACTIONS = [
  "approve",
  "reject",
  "request_changes",
  "escalate",
] as const;
export type ApprovalAction = (typeof APPROVAL_ACTIONS)[number];

/** Ascending severity — drives the inbox sort + risk styling. */
export const RISK_LEVELS = ["info", "warning", "critical"] as const;
export type RiskLevel = (typeof RISK_LEVELS)[number];

/** One inbox row (GET /approvals). */
export interface ApprovalSummary {
  id: string;
  gate_type: GateType;
  status: GateStatus;
  title: string;
  project_id?: string | null;
  risk_level?: string;
  requested_actor?: string;
  requested_at?: string | null;
}

/** One full approval-request row (GET /approvals/{id}). */
export interface ApprovalRequest {
  id: string;
  workspace_id: string;
  project_id?: string | null;
  gate_type: GateType;
  status: GateStatus;
  subject_type?: string;
  subject_id?: string | null;
  workflow_run_id?: string | null;
  agent_run_id?: string | null;
  task_id?: string | null;
  required_approvals?: number;
  risk_level?: RiskLevel;
  title?: string | null;
  gate_payload?: Record<string, unknown>;
  requested_actor?: string;
  escalated?: boolean;
  decision_note?: string | null;
  expires_at?: string | null;
  requested_at?: string | null;
  resolved_at?: string | null;
}

/** One entry in the "Risks flagged" panel (must-show item 7). */
export interface RiskFlag {
  severity?: RiskLevel;
  category?: string;
  message: string;
  source?: string | null;
}

/**
 * The spec's nine "must-show" review items (GET /approvals/{id}/context).
 * Nullable sections are hidden when a gate type does not apply them.
 */
export interface ApprovalContext {
  approval_id: string;
  gate_type: GateType;
  goal?: string; // 1 — goal & requirements
  requirements?: Record<string, unknown>[]; // 1
  diff?: Record<string, unknown> | null; // 2 — changed files
  verification?: Record<string, unknown> | null; // 3 — lint/type/test/coverage
  traceability?: Record<string, unknown>[] | null; // 4 — spec traceability
  knowledge_refs?: Record<string, unknown>[] | null; // 5 — provenance
  confidence?: Record<string, unknown> | null; // 6 — {score, rationale}
  risk_flags?: RiskFlag[]; // 7 — always shown
  run_trace_ref?: Record<string, unknown> | null; // 8 — {workflow_run_id, agent_run_id}
  available_actions?: ApprovalAction[]; // 9
  gate_payload?: Record<string, unknown>;
}

/** One immutable per-approver decision row (GET /approvals/{id}/decisions). */
export interface ApprovalDecisionRecord {
  approval_request_id: string;
  approver_user_id: string;
  decision: ApprovalAction;
  note?: string | null;
  created_at?: string | null;
}

/** Body of POST /approvals/{id}/decision. */
export interface ApprovalDecisionRequest {
  decision: ApprovalAction;
  note?: string | null;
}

/** What the gate's resolution hook did (or could not yet do). */
export interface ResolutionOutcome {
  completed?: boolean;
  blocking_reasons?: string[];
  follow_up_state?: string | null;
  details?: Record<string, unknown>;
}

/** Result of a decision — gate status + hook outcome. */
export interface ApprovalResolution {
  approval_id: string;
  status: GateStatus;
  outcome: ResolutionOutcome;
}

/** Body of GET /approvals/count (the nav badge). */
export interface ApprovalCount {
  count: number;
}

// --- Spec engine / SDD (F02 /spec + F23 spec-validation) ------------------ //
// Hand-maintained mirror of the spec DTOs in `forge_contracts.dtos` (Pydantic
// v2). Enum string values match the Python `SpecStatus` StrEnum verbatim.

/** The SDD lifecycle stages (forge_contracts.enums.SpecStatus), in order. */
export const SPEC_STATUSES = [
  "draft",
  "clarifying",
  "approved",
  "implementing",
  "validated",
  "closed",
] as const;
export type SpecStatus = (typeof SPEC_STATUSES)[number];

export interface Requirement {
  id: string;
  text: string;
}

/** An acceptance criterion; `req_refs` links it back to requirements. */
export interface AcceptanceCriterion {
  id: string;
  text: string;
  req_refs?: string[];
  spec_ref?: string | null;
}

export interface OpenQuestion {
  id: string;
  text: string;
  resolution?: string | null;
}

/** An architecture decision record (spec manifest `decisions[]`). */
export interface ADR {
  id: string;
  title: string;
  status?: string;
  context?: string | null;
  decision?: string | null;
  consequences?: string | null;
}

/** Engineering principles / architecture guardrails for a project. */
export interface Constitution {
  id?: string | null;
  project_id?: string | null;
  principles?: string[];
  architecture_guardrails?: string[];
  content?: string | null;
}

/** Machine-readable spec metadata (GET /spec/specs/{id}). */
export interface SpecManifest {
  id: string;
  name: string;
  status?: SpecStatus;
  constitution_refs?: string[];
  repos?: string[];
  requirements?: Requirement[];
  acceptance_criteria?: AcceptanceCriterion[];
  open_questions?: OpenQuestion[];
  constraints?: string[];
  decisions?: ADR[];
  plan_ref?: string | null;
  tasks_ref?: string | null;
  validation_ref?: string | null;
  execution_mode?: ExecutionMode;
  skill_profile?: string | null;
}

/** One requirement -> acceptance -> task -> test traceability row. */
export interface RequirementTrace {
  requirement_id: string;
  text?: string | null;
  acceptance_criteria_ids?: string[];
  task_refs?: string[];
  test_refs?: string[];
  satisfied?: boolean;
}

/** A single verification check outcome (lint / type / tests / coverage). */
export interface CheckResult {
  name: string;
  passed: boolean;
  details?: string | null;
}

/** Output of spec validation (spec: validation / traceability + gates). */
export interface ValidationReport {
  task_id?: string;
  spec_id?: string | null;
  passed?: boolean;
  traceability?: RequirementTrace[];
  checks?: CheckResult[];
  coverage?: number | null;
  notes?: string[];
}

/**
 * A spec manifest enriched with its latest validation report — the row shape of
 * the F23 spec-validation dashboard projection (GET /projects/{id}/specs).
 */
export interface SpecOverview extends SpecManifest {
  validation?: ValidationReport | null;
}

/**
 * The spec-validation dashboard payload for a project: the project constitution
 * plus every spec with its rolled-up validation (GET /projects/{id}/specs).
 */
export interface SpecDashboard {
  project_id: string;
  constitution?: Constitution | null;
  specs: SpecOverview[];
}

/**
 * Token/cost accounting for one model call (`forge_agent.providers`'s
 * `UsageAccumulator.to_artifact` shape — mirrored here, not reimplemented).
 */
export interface ModelUsage {
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number;
  calls?: number;
  cache_read_input_tokens?: number;
}

/**
 * The draft-only result of `POST /spec/draft` (ss-draft / ss-ai-panel): a BYOK
 * model turns a one-line goal into a `spec.md`, seeded with the project
 * constitution. Nothing is persisted — `manifest` is a parsed *preview* (or
 * `null` with `parse_error` set when the drafted markdown didn't parse) for a
 * human to refine before saving via the normal spec-editing endpoints.
 */
export interface SpecDraft {
  goal: string;
  epic_id?: string | null;
  model: string;
  spec_md: string;
  manifest?: SpecManifest | null;
  parse_error?: string | null;
  usage?: ModelUsage;
}

// --- Observability: run traces -------------------------------------------- //
// Mirrors forge_api.observability.trace.RunTrace + forge_contracts.Step, the
// response shape of GET /observability/runs/{run_id}/trace.

export const STEP_KINDS = [
  "plan",
  "tool_call",
  "observation",
  "decision",
  "message",
  "output",
  "error",
  "handoff",
] as const;
export type StepKind = (typeof STEP_KINDS)[number];

export const RUN_STATUSES = [
  "pending",
  "running",
  "succeeded",
  "failed",
  "escalated",
  "cancelled",
] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];

export type DecisionEffect = "allow" | "deny" | "requires_approval";

/** A request to invoke a tool — the unit a step's policy evaluation acts on. */
export interface TraceToolCall {
  tool: string;
  action?: string | null;
  arguments?: Record<string, unknown>;
  path?: string | null;
  resource?: string | null;
  connection_id?: string | null;
  metadata?: Record<string, unknown>;
}

/** The result of evaluating a {@link TraceToolCall} against policy. */
export interface TraceDecision {
  effect: DecisionEffect;
  reason?: string | null;
  matched_rule?: string | null;
  requires_approval?: boolean;
  approval_gate?: GateType | null;
  severity?: string;
}

/** One step in an agent run trace (plan / tool call / observation / …). */
export interface TraceStep {
  index?: number | null;
  kind: StepKind;
  thought?: string | null;
  tool_call?: TraceToolCall | null;
  observation?: string | null;
  output?: string | null;
  decision?: TraceDecision | null;
  confidence?: number | null;
  duration_ms?: number | null;
  timestamp?: string | null;
  /** Free-form; token/cost/model live here (input_tokens, cost_usd, …). */
  metadata?: Record<string, unknown>;
}

/** An ordered, redacted, summarised view of a single run's steps. */
export interface RunTrace {
  run_id?: string | null;
  status?: RunStatus | null;
  steps: TraceStep[];
  total_steps: number;
  step_counts: Partial<Record<StepKind, number>>;
  total_duration_ms: number;
  started_at?: string | null;
  completed_at?: string | null;
  confidence?: number | null;
  has_subagents: boolean;
  summary?: string | null;
}

// --- Marketplace (F32 integration marketplace) ---------------------------- //
// Hand-maintained mirror of `forge_marketplace.models` + the marketplace router
// DTOs (Pydantic v2). Enum string values match the Python `StrEnum` verbatim.

/** The distributable artifact kinds a registry can advertise (ArtifactKind). */
export const ARTIFACT_KINDS = [
  "mcp_connector",
  "skill_profile",
  "workflow_template",
  "policy_template",
] as const;
export type ArtifactKind = (typeof ARTIFACT_KINDS)[number];

/** Registry provenance, most-trusted first (TrustLevel). */
export const TRUST_LEVELS = [
  "official",
  "trusted",
  "community",
  "unverified",
] as const;
export type TrustLevel = (typeof TRUST_LEVELS)[number];

/**
 * The cryptographic verification outcome for a version/installation.
 * `signature_invalid` / `hash_mismatch` are hard blocks; `unsigned` /
 * `untrusted_registry` are soft-gated (require an explicit admin acknowledgement).
 */
export const VERIFICATION_STATUSES = [
  "verified",
  "unsigned",
  "untrusted_registry",
  "signature_invalid",
  "hash_mismatch",
] as const;
export type VerificationStatus = (typeof VERIFICATION_STATUSES)[number];

/** Installation lifecycle (InstallStatus). */
export const INSTALL_STATUSES = [
  "pending",
  "installed",
  "update_available",
  "failed",
  "uninstalled",
] as const;
export type InstallStatus = (typeof INSTALL_STATUSES)[number];

export type RegistryType = "git" | "http_index";

/** One published version of a listing (GET /marketplace/listings/.../...). */
export interface ListingVersion {
  version: string;
  content_hash: string;
  signed: boolean;
  min_forge_version?: string | null;
  published_at: string;
  yanked?: boolean;
  yanked_reason?: string | null;
}

/** One catalog row (GET /marketplace/listings). */
export interface Listing {
  id: string;
  registry_id: string;
  registry_slug: string;
  trust_level: TrustLevel;
  kind: ArtifactKind;
  slug: string;
  name: string;
  summary: string;
  tags: string[];
  latest_version: string;
  homepage?: string | null;
  repository?: string | null;
  license: string;
  cached_at: string;
}

/** A listing enriched with its full version history (package detail). */
export interface ListingDetail extends Listing {
  versions: ListingVersion[];
}

/** One installed package (GET /marketplace/installations). */
export interface Installation {
  id: string;
  registry_slug: string;
  listing_slug: string;
  kind: string;
  installed_version: string;
  pinned: boolean;
  target_kind: string;
  target_object_id?: string | null;
  content_hash: string;
  verification_status: VerificationStatus;
  status: InstallStatus;
  available_version?: string | null;
  yanked_reason?: string | null;
  created_at: string;
}

/** The result of verifying a version's content hash + signature. */
export interface VerificationResult {
  status: VerificationStatus;
  content_hash_ok: boolean;
  signature_ok?: boolean | null;
  detail?: string | null;
}

/** Body of POST /marketplace/preview and /marketplace/install. */
export interface InstallRequest {
  registry_id: string;
  kind: ArtifactKind;
  slug: string;
  version?: string | null;
  /** Required to install an unsigned / untrusted-registry package. */
  acknowledge_unverified?: boolean;
  override_name?: string | null;
}

/** The dry-run plan a preview returns (POST /marketplace/preview). */
export interface InstallPlan {
  registry_id?: string | null;
  kind: ArtifactKind;
  slug: string;
  version: string;
  verification: VerificationResult;
  resolved_config: Record<string, unknown>;
  warnings: string[];
  requires_admin_followup: string[];
  overrides_builtin: boolean;
  blocked: boolean;
  block_reason?: string | null;
}

/** The outcome of a completed install/update (POST /marketplace/install). */
export interface InstallResult {
  installation_id: string;
  target_kind: string;
  target_object_id: string;
  status: InstallStatus;
  version: string;
  verification: VerificationResult;
  warnings: string[];
}

/** Catalog query params (GET /marketplace/listings). */
export interface MarketplaceListingQuery {
  kind?: ArtifactKind;
  tag?: string;
  registry_id?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

// --- Incidents (F17 /incidents workflow surface) -------------------------- //

/** The ten forward incident lifecycle states (matches IncidentState). */
export const INCIDENT_STATES = [
  "alert_received",
  "incident_created",
  "context_gathering",
  "impact_assessed",
  "remediation_proposed",
  "awaiting_approval",
  "executing_runbook",
  "monitoring",
  "resolved",
  "postmortem_created",
] as const;
export type IncidentState = (typeof INCIDENT_STATES)[number];

/** Declared blast radius of a remediation step / plan (matches BlastRadius). */
export const BLAST_RADII = ["low", "medium", "high"] as const;
export type BlastRadius = (typeof BLAST_RADII)[number];

export type RemediationStepStatus =
  | "proposed"
  | "approved"
  | "skipped"
  | "running"
  | "succeeded"
  | "failed";

/** An incident summary (GET /incidents, POST /incidents). */
export interface IncidentView {
  id: string;
  key: string;
  project_id: string;
  title: string;
  description?: string | null;
  severity: IncidentSeverity;
  state: IncidentState;
  /** The raw FSM lifecycle state (may be an error/terminal state too). */
  lifecycle_state: string;
  source: string;
  dedup_key?: string | null;
  commander_id?: string | null;
  blast_radius?: string | null;
  impact_summary?: string | null;
  created_at: string;
  detected_at?: string | null;
  acknowledged_at?: string | null;
  resolved_at?: string | null;
  /** FSM events valid from the current lifecycle state (drives the action bar). */
  allowed_events: string[];
}

/** One incident timeline event (GET /incidents/{id}/timeline). */
export interface IncidentEventView {
  id: string;
  incident_id: string;
  sequence: number;
  kind: string;
  actor: string;
  summary: string;
  data: Record<string, unknown>;
  created_at: string;
}

/** One ordered remediation step with its declared blast radius. */
export interface RemediationStepView {
  id: string;
  order: number;
  title: string;
  action: string;
  blast_radius: BlastRadius;
  rationale: string;
  status: RemediationStepStatus;
  /** True when this step is outside the incident's blast-radius policy. */
  blocked: boolean;
}

/** The latest proposed remediation runbook (GET /incidents/{id}/remediation). */
export interface RemediationPlanView {
  id: string;
  incident_id: string;
  attempt: number;
  max_blast_radius: BlastRadius;
  status: string;
  steps: RemediationStepView[];
  offending_step_ids: string[];
}

/** Incident detail: summary + latest plan + event count (GET /incidents/{id}). */
export interface IncidentDetailView extends IncidentView {
  remediation_plan?: RemediationPlanView | null;
  event_count: number;
}

/** A rendered postmortem with its extracted action items. */
export interface PostmortemView {
  id: string;
  incident_id: string;
  status: string;
  content_md: string;
  root_cause?: string | null;
  action_item_task_keys: string[];
}

/** Body of POST /incidents (manual declaration). */
export interface IncidentDeclareRequest {
  project_id: string;
  title: string;
  severity?: IncidentSeverity;
  description?: string | null;
  repo_id?: string | null;
  commander_id?: string | null;
}

/** Body of POST /incidents/{id}/events — drive the incident FSM. */
export interface IncidentEventRequest {
  event: string;
  context?: Record<string, boolean>;
  note?: string | null;
}

// --- Observability & cost (F38) ------------------------------------------- //
// Mirrors `forge_obs.cost.models` (Pydantic v2). Money is a `Decimal` on the
// server and serialises to a JSON **string** (e.g. "0.43"); token counts are
// integers. Consumers coerce with `toNum` before arithmetic.

/** Aggregation scope for a cost query. */
export type CostScope = "workspace" | "project" | "task";
/** Breakdown dimension for a summary/timeseries. */
export type CostGroupBy =
  | "phase"
  | "provider"
  | "model"
  | "tier"
  | "strategy"
  | "none";
/** Time-bucket granularity for a timeseries. */
export type CostBucketSize = "hour" | "day" | "week";

/** One breakdown bucket (keyed by phase | provider | model | tier | strategy, per `group_by`). */
export interface CostBucket {
  key: string;
  cost_usd: string | number;
  prompt_tokens: number;
  completion_tokens: number;
  /** Number of priced calls folded into this bucket. */
  request_count?: number;
}

/** Aggregate spend for a scope, with a grouped breakdown (GET /cost/summary). */
export interface CostSummary {
  scope: string;
  scope_id: string;
  total_cost_usd: string | number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  group_by: string;
  buckets: CostBucket[];
  from?: string | null;
  to?: string | null;
}

/** Bucketed spend over time, one series per group key (GET /cost/timeseries). */
export interface CostTimeseries {
  scope: string;
  scope_id: string;
  bucket: string;
  group_by: string;
  /** `{ "<key>": [[iso_timestamp, cost_usd_string], …] }`. */
  series: Record<string, [string, string | number][]>;
}

/** Query params for GET /cost/summary. */
export interface CostSummaryQuery {
  scope?: CostScope;
  scope_id?: string;
  group_by?: CostGroupBy;
  from?: string;
  to?: string;
}

/** Query params for GET /cost/timeseries. */
export interface CostTimeseriesQuery {
  scope?: CostScope;
  scope_id?: string;
  bucket?: CostBucketSize;
  group_by?: CostGroupBy;
  from?: string;
  to?: string;
}

// --- Sprints & velocity (F26 sprint router) ------------------------------- //
// Hand-maintained mirror of `forge_board.sprint_service` view models (Pydantic
// v2). These back the /projects/{id}/sprints, /velocity, /sprints/{id}/burndown
// and /report routes. Distinct from the Phase-0 board `SprintDTO` above.

/** Sprint lifecycle states (forge_contracts.enums.SprintState), in order. */
export const SPRINT_STATES = [
  "planned",
  "active",
  "completed",
  "cancelled",
] as const;
export type SprintState = (typeof SPRINT_STATES)[number];

/** Where incomplete tasks go when a sprint is completed (CarryoverTarget). */
export const CARRYOVER_TARGETS = ["backlog", "next_sprint", "leave"] as const;
export type CarryoverTarget = (typeof CARRYOVER_TARGETS)[number];

/** One sprint with its rolled-up velocity metrics (GET /sprints/{id}). */
export interface Sprint {
  id: string;
  project_id: string;
  workspace_id: string;
  name: string;
  goal?: string | null;
  state: SprintState;
  start_date?: string | null;
  end_date?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  capacity_points?: number | null;
  committed_points: number;
  committed_task_count: number;
  completed_points: number;
  added_points: number;
  removed_points: number;
  carryover_points: number;
  remaining_points: number;
  /** completed / committed, 0..1 (0 when nothing committed). */
  predictability: number;
  /** (added + removed) / committed, 0..1. */
  scope_change_ratio: number;
  velocity_version: number;
}

/** One task in a sprint report, bucketed by outcome. */
export interface SprintReportTask {
  task_id: string;
  key: string;
  title: string;
  points: number;
  bucket: string;
}

/** The per-sprint velocity rollup (report.velocity). */
export interface VelocityResult {
  committed_points: number;
  completed_points: number;
  added_points: number;
  removed_points: number;
  carryover_points: number;
  committed_task_count: number;
  completed_task_count: number;
  carryover_task_count: number;
  predictability: number;
  scope_change_ratio: number;
}

/** A completed sprint's report (GET /sprints/{id}/report, POST .../complete). */
export interface SprintReport {
  sprint: Sprint;
  velocity: VelocityResult;
  completed: SprintReportTask[];
  carryover: SprintReportTask[];
  added: SprintReportTask[];
  removed: SprintReportTask[];
}

/** One calendar day of a sprint's burndown (GET /sprints/{id}/burndown). */
export interface BurndownPoint {
  snapshot_date: string;
  scope_points: number;
  remaining_points: number;
  completed_points: number;
  ideal_points: number;
  completed_task_count: number;
  remaining_task_count: number;
}

/** A sprint's burndown series (GET /sprints/{id}/burndown). */
export interface BurndownSeries {
  sprint_id: string;
  start_date?: string | null;
  end_date?: string | null;
  committed_points: number;
  points: BurndownPoint[];
}

/** One bar in the velocity dashboard (committed vs completed per sprint). */
export interface VelocitySprintBar {
  sprint_id: string;
  name: string;
  end_date?: string | null;
  committed_points: number;
  completed_points: number;
  predictability: number;
}

/** Aggregate velocity over a window of completed sprints. */
export interface VelocitySummary {
  sprint_count: number;
  average_velocity: number;
  rolling_3_velocity: number;
  predictability_avg: number;
  scope_change_avg: number;
  forecast_low: number;
  forecast_avg: number;
  forecast_high: number;
}

/** The velocity dashboard for a project (GET /projects/{id}/velocity). */
export interface VelocityDashboard {
  project_id: string;
  sprints: VelocitySprintBar[];
  summary: VelocitySummary;
}

/** Body of POST /sprints/{id}/complete. */
export interface CompleteSprintRequest {
  carryover?: CarryoverTarget;
  next_sprint_id?: string | null;
}

// --- Audit log (F39 — immutable, hash-chained audit trail) --------------- //

export const AUDIT_OUTCOMES = [
  "success",
  "denied",
  "error",
  "blocked",
] as const;
export type AuditOutcome = (typeof AUDIT_OUTCOMES)[number];

export const AUDIT_SEVERITIES = [
  "info",
  "notice",
  "warning",
  "critical",
] as const;
export type AuditSeverity = (typeof AUDIT_SEVERITIES)[number];

export const AUDIT_ACTOR_TYPES = [
  "user",
  "agent_runner",
  "system",
  "integration",
  "api_key",
] as const;
export type AuditActorType = (typeof AUDIT_ACTOR_TYPES)[number];

/**
 * One persisted, redacted audit row (GET /audit) including its hash-chain
 * fields. Mirrors `forge_contracts.audit.AuditEntry`. The server redacts
 * secrets before persisting; the viewer redacts again defensively on render.
 */
export interface AuditEntry {
  id: string;
  workspace_id: string;
  seq?: number | null;
  action: string;
  actor_id?: string | null;
  actor_type: string;
  actor_label?: string | null;
  target_type?: string | null;
  target_id?: string | null;
  scope_type?: string | null;
  scope_id?: string | null;
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
  result: string;
  severity: string;
  reason?: string | null;
  details: Record<string, unknown>;
  detail_ref?: Record<string, unknown> | null;
  request_id?: string | null;
  payload_hash?: string | null;
  prev_hash?: string | null;
  entry_hash?: string | null;
  created_at: string;
}

/** Cursor-paginated audit page (GET /audit). */
export interface AuditListResponse {
  items: AuditEntry[];
  next_cursor?: string | null;
}

/** Filter vocabulary for the audit viewer (GET /audit/actions). */
export interface AuditVocabulary {
  actions: string[];
  actor_types: string[];
  resource_types: string[];
  outcomes: string[];
  severities: string[];
}

/** Verdict of re-walking the workspace's audit hash chain (POST /audit/verify). */
export interface ChainVerifyResult {
  workspace_id: string;
  ok: boolean;
  entries_checked: number;
  broken_at_seq?: number | null;
  detail?: string | null;
}

/** Query parameters accepted by GET /audit (all optional). */
export interface AuditQuery {
  actor_id?: string;
  actor_type?: string;
  /** Single action value (FastAPI accepts one member of its `list[str]`). */
  action?: string;
  target_type?: string;
  target_id?: string;
  outcome?: string;
  severity?: string;
  /** ISO-8601 lower bound (sent as the `from` query alias). */
  from?: string;
  /** ISO-8601 upper bound. */
  to?: string;
  /** Free-text search across actor label / reason / details. */
  q?: string;
  cursor?: string;
  limit?: number;
}

// --- Deployments / gates (F31 deployment-gates) --------------------------- //

/** The 12 states of the deployment promotion FSM (forge_contracts.DeploymentState). */
export const DEPLOYMENT_STATES = [
  "requested",
  "gate_evaluating",
  "awaiting_approval",
  "approved",
  "deploying",
  "verifying",
  "succeeded",
  "failed",
  "gate_rejected",
  "rolling_back",
  "rolled_back",
  "cancelled",
] as const;
export type DeploymentState = (typeof DEPLOYMENT_STATES)[number];

/** Terminal states — no further transitions possible. */
export const TERMINAL_DEPLOYMENT_STATES = [
  "succeeded",
  "failed",
  "gate_rejected",
  "rolled_back",
  "cancelled",
] as const satisfies readonly DeploymentState[];

export const DEPLOYMENT_KINDS = ["promotion", "rollback", "redeploy"] as const;
export type DeploymentKind = (typeof DEPLOYMENT_KINDS)[number];

export const DEPLOYMENT_TRIGGERS = [
  "manual",
  "auto_promote",
  "agent",
  "automation",
  "rollback",
] as const;
export type DeploymentTrigger = (typeof DEPLOYMENT_TRIGGERS)[number];

export const GATE_CHECK_NAMES = [
  "policy_allows",
  "predecessor_succeeded",
  "ci_green",
  "spec_validated",
  "security_clean",
  "not_frozen",
] as const;
export type GateCheckName = (typeof GATE_CHECK_NAMES)[number];

export const GATE_CHECK_STATUSES = [
  "passed",
  "failed",
  "pending",
  "skipped",
] as const;
export type GateCheckStatus = (typeof GATE_CHECK_STATUSES)[number];

export const HEALTH_STATUSES = ["passing", "failing", "unknown"] as const;
export type HealthStatus = (typeof HEALTH_STATUSES)[number];

/** A read-model of a single deployment run (forge_contracts.DeploymentDTO). */
export interface DeploymentRead {
  id: string;
  project_id: string;
  environment_name: string;
  repo_id: string;
  commit_sha: string;
  artifact_ref?: string | null;
  from_environment_name?: string | null;
  kind: DeploymentKind;
  rollback_of?: string | null;
  state: DeploymentState;
  trigger: DeploymentTrigger;
  initiated_by: string;
  provider_name?: string | null;
  provider_url?: string | null;
  health_status?: HealthStatus | null;
  failure_reason?: string | null;
  requested_at: string;
  finished_at?: string | null;
}

/** One pipeline stage (environment) with what is currently deployed to it. */
export interface EnvironmentRead {
  id: string;
  name: string;
  rank: number;
  is_restricted: boolean;
  requires_approval: boolean;
  gate_config: Record<string, unknown>;
  provider_config: Record<string, unknown>;
  health_check: Record<string, unknown>;
  currently_deployed?: DeploymentRead | null;
}

/** A project's promotion pipeline (ranked dev -> staging -> prod stages). */
export interface PipelineRead {
  id: string;
  project_id: string;
  repo_id: string;
  enabled: boolean;
  version: number;
  environments: EnvironmentRead[];
}

/** One gate check's verdict (name + status + detail). */
export interface GateCheckResult {
  name: GateCheckName;
  status: GateCheckStatus;
  detail: string;
  metrics: Record<string, string>;
}

/** A deployment's gate evaluation: can it proceed, and why not. */
export interface GateEvaluation {
  deployment_id: string;
  environment: string;
  can_proceed: boolean;
  requires_human_approval: boolean;
  checks: GateCheckResult[];
  blocking_reasons: string[];
}

/** One recorded FSM transition (state change) for a deployment. */
export interface DeploymentTransition {
  sequence: number;
  from_state: string;
  to_state: string;
  event: string;
  actor: string;
  created_at?: string | null;
}

/** A deployment plus its gate evaluation, checks and transition history. */
export interface DeploymentDetail extends DeploymentRead {
  gate?: GateEvaluation | null;
  checks: GateCheckResult[];
  transitions: DeploymentTransition[];
  diff_since?: Record<string, unknown> | null;
}

/** Approve / reject / request-changes on a gated deployment. */
export const DEPLOYMENT_DECISIONS = [
  "approve",
  "reject",
  "changes_requested",
] as const;
export type DeploymentDecision = (typeof DEPLOYMENT_DECISIONS)[number];

export interface DeploymentDecisionRequest {
  decision: DeploymentDecision;
  note?: string | null;
}

/** Request a promotion of an artifact to an environment. */
export interface DeploymentRequestBody {
  environment: string;
  commit_sha: string;
  artifact_ref?: string | null;
  kind?: DeploymentKind;
  trigger?: DeploymentTrigger;
}

/** Optional filters accepted by GET /projects/{id}/deployments. */
export interface DeploymentListQuery {
  environment?: string;
  state?: DeploymentState;
  limit?: number;
}

// --- Enterprise SSO / SCIM (F33 auth/sso admin routers) ------------------- //

/** Roles an SSO-provisioned identity can be granted (matches UserRole). */
export const SSO_ROLES = ["admin", "member", "viewer", "agent-runner"] as const;
export type SsoRole = (typeof SSO_ROLES)[number];

/** Where in the SAML assertion each Forge identity field is read from. */
export interface SamlAttributeMapping {
  email: string;
  name?: string | null;
  first_name?: string | null;
  last_name?: string | null;
  groups?: string | null;
}

/** The identity-provider half of a SAML federation. */
export interface SamlIdpConfig {
  entity_id: string;
  sso_url: string;
  slo_url?: string | null;
  x509_certs: string[];
  name_id_format: string;
}

/**
 * Public view of a workspace SAML configuration (`SsoConfigOut`). The SP private
 * key is never exposed — only the public signing certificate (`sp_cert_pem`).
 */
export interface SsoConfig {
  id: string;
  workspace_id: string;
  protocol: "saml";
  enabled: boolean;
  idp: SamlIdpConfig;
  sp_entity_id: string;
  sp_acs_url: string;
  sp_slo_url: string;
  sp_metadata_url: string;
  sp_cert_pem: string;
  domains: string[];
  allow_idp_initiated: boolean;
  sign_authn_requests: boolean;
  want_assertions_signed: boolean;
  attribute_mapping: SamlAttributeMapping;
  default_role: string;
  group_role_map: Record<string, string>;
  jit_provisioning: boolean;
  last_metadata_refresh_at?: string | null;
}

/** Create/replace payload for a workspace SAML configuration (`SsoConfigIn`). */
export interface SsoConfigInput {
  protocol?: "saml";
  enabled?: boolean;
  metadata_url?: string | null;
  metadata_xml?: string | null;
  idp?: SamlIdpConfig | null;
  domains?: string[];
  allow_idp_initiated?: boolean;
  sign_authn_requests?: boolean;
  want_assertions_signed?: boolean;
  want_name_id_encrypted?: boolean;
  attribute_mapping?: SamlAttributeMapping;
  default_role?: SsoRole;
  group_role_map?: Record<string, string>;
  jit_provisioning?: boolean;
}

/** Redacted SCIM-token view — never the raw token or its hash. */
export interface ScimTokenInfo {
  id: string;
  name: string;
  token_prefix: string;
  created_at: string;
  last_used_at?: string | null;
  expires_at?: string | null;
  revoked_at?: string | null;
}

/** Mint response — carries the plaintext `token` exactly once. */
export interface ScimTokenCreated extends ScimTokenInfo {
  token: string;
}

/** Body for issuing a SCIM bearer token. */
export interface ScimTokenCreateRequest {
  name: string;
  expires_at?: string | null;
}

/** Parsed result of a validation-only SAML round trip. */
export interface SamlTestResult {
  name_id: string;
  name_id_format: string;
  issuer: string;
  attributes: Record<string, string[]>;
}

/** Home-realm-discovery probe (POST /auth/saml/discover). */
export interface HrdDiscoverRequest {
  email: string;
}

export interface HrdDiscoverResponse {
  sso: boolean;
  redirect?: string | null;
}

// --- Multi-team & RBAC (F30) ---------------------------------------------- //
// Mirrors `forge_contracts.authz` + the `/teams`, `/access` and
// `/projects/{id}/access` router schemas. Enum string values match the Python
// StrEnum members verbatim (note the HYPHEN in "agent-runner").

/** A workspace-scoped role. Order = descending capability (drives the census). */
export const WORKSPACE_ROLES = [
  "admin",
  "member",
  "viewer",
  "agent-runner",
] as const;
export type WorkspaceRole = (typeof WORKSPACE_ROLES)[number];

/** A member's role within a team (`lead` confers team-member management). */
export const TEAM_ROLES = ["lead", "member"] as const;
export type TeamRole = (typeof TEAM_ROLES)[number];

/** The scope at which a role grant applies. */
export const SCOPE_TYPES = ["workspace", "team", "project"] as const;
export type ScopeType = (typeof SCOPE_TYPES)[number];

/** The kind of identity a grant binds to. */
export const PRINCIPAL_TYPES = ["user", "api_key", "service"] as const;
export type PrincipalType = (typeof PRINCIPAL_TYPES)[number];

/** A team's access level on a project. */
export const ACCESS_LEVELS = ["read", "write", "admin"] as const;
export type AccessLevel = (typeof ACCESS_LEVELS)[number];

/** Whether a project is visible workspace-wide or walled off to teams. */
export const PROJECT_VISIBILITIES = ["workspace", "team_restricted"] as const;
export type ProjectVisibility = (typeof PROJECT_VISIBILITIES)[number];

export interface PrincipalRef {
  type: PrincipalType;
  id: string;
}

export interface ScopeRef {
  type: ScopeType;
  id: string;
}

/** A single `(principal, scope, role)` grant, optionally time-bounded. */
export interface RoleGrant {
  id: string;
  workspace_id: string;
  principal: PrincipalRef;
  scope: ScopeRef;
  role: WorkspaceRole;
  granted_by?: string | null;
  expires_at?: string | null;
  created_at?: string | null;
}

/** Body for `POST /access/grants`. */
export interface RoleGrantInput {
  principal: PrincipalRef;
  scope: ScopeRef;
  role: WorkspaceRole;
  expires_at?: string | null;
}

/** Query filters for `GET /access/grants`. */
export interface RoleGrantQuery {
  principal_id?: string;
  scope_type?: ScopeType;
  scope_id?: string;
}

export interface Team {
  id: string;
  key: string;
  name: string;
  description?: string | null;
  parent_team_id?: string | null;
  archived_at?: string | null;
  created_at: string;
}

/** Body for `POST /teams`. */
export interface TeamInput {
  key: string;
  name: string;
  description?: string | null;
  parent_team_id?: string | null;
}

export interface TeamMember {
  user_id: string;
  team_role: TeamRole;
  created_at: string;
}

/** Body for `POST /teams/{id}/members`. */
export interface TeamMemberInput {
  user_id: string;
  team_role?: TeamRole;
}

export interface ProjectTeamAccess {
  project_id: string;
  team_id: string;
  access_level: AccessLevel;
}

/** Body for `POST /projects/{id}/team-access`. */
export interface ProjectTeamAccessInput {
  team_id: string;
  access_level: AccessLevel;
}

/** Body for `PUT /projects/{id}/visibility`. */
export interface ProjectVisibilityInput {
  visibility: ProjectVisibility;
  owner_team_id?: string | null;
}

/** The visibility + per-team access for one project (`GET /projects/{id}/access`). */
export interface ProjectAccess {
  project_id: string;
  visibility: ProjectVisibility;
  owner_team_id?: string | null;
  team_access: ProjectTeamAccess[];
}

// --- External PM adapters (F18 `/integrations/pm` routers) ----------------- //

export const PM_PROVIDERS = ["jira", "linear"] as const;
export type PmProvider = (typeof PM_PROVIDERS)[number];

export const PM_AUTH_TYPES = ["oauth", "api_token"] as const;
export type PmAuthType = (typeof PM_AUTH_TYPES)[number];

export const PM_SYNC_DIRECTIONS = [
  "bidirectional",
  "inbound_only",
  "outbound_only",
] as const;
export type PmSyncDirection = (typeof PM_SYNC_DIRECTIONS)[number];

export const PM_CONFLICT_POLICIES = [
  "forge_wins",
  "external_wins",
  "newest_wins",
  "manual",
] as const;
export type PmConflictPolicy = (typeof PM_CONFLICT_POLICIES)[number];

export const PM_CONNECTION_STATUSES = [
  "pending",
  "connected",
  "error",
  "disabled",
] as const;
export type PmConnectionStatus = (typeof PM_CONNECTION_STATUSES)[number];

/** Ordered so the health strip reads healthy → degraded left to right. */
export const PM_SYNC_STATES = [
  "synced",
  "pending_out",
  "pending_in",
  "conflict",
  "error",
] as const;
export type PmSyncState = (typeof PM_SYNC_STATES)[number];

/** Normalized status categories a mapping row can target (board grain). */
export const PM_STATUS_CATEGORIES = [
  "backlog",
  "unstarted",
  "started",
  "completed",
  "canceled",
] as const;
export type PmStatusCategory = (typeof PM_STATUS_CATEGORIES)[number];

/**
 * Redaction-safe view of a `pm_connection` row. Secrets are never returned —
 * only the `has_credential` / `has_webhook_secret` booleans and the connected
 * account label.
 */
export interface PmConnection {
  id: string;
  provider: PmProvider;
  name: string;
  project_id: string;
  external_base_url?: string | null;
  external_project_key: string;
  external_project_id: string;
  auth_type: PmAuthType;
  account_label?: string | null;
  granted_scopes: string[];
  sync_direction: PmSyncDirection;
  conflict_policy: PmConflictPolicy;
  status_map: Record<string, string>;
  priority_map: Record<string, string>;
  field_map: Record<string, unknown>;
  status: PmConnectionStatus;
  last_health_at?: string | null;
  last_full_sync_at?: string | null;
  has_credential: boolean;
  has_webhook_secret: boolean;
  created_at: string;
  updated_at: string;
}

/** A connection plus its per-state link tallies (keyed by {@link PmSyncState}). */
export interface PmConnectionDetail extends PmConnection {
  link_counts: Partial<Record<PmSyncState, number>>;
}

/** Request body for `POST /connections` (the connect form). */
export interface PmConnectionConfigInput {
  provider: PmProvider;
  name: string;
  project_id: string;
  external_base_url?: string | null;
  external_project_key: string;
  auth_type?: PmAuthType;
  api_token?: string | null;
  api_token_email?: string | null;
  sync_direction?: PmSyncDirection;
  conflict_policy?: PmConflictPolicy;
  status_map?: Record<string, string>;
  priority_map?: Record<string, string>;
  field_map?: Record<string, unknown>;
  on_external_delete?: "unlink" | "archive";
}

/** Partial update for `PATCH /connections/{id}`. */
export interface PmConnectionPatch {
  name?: string | null;
  status_map?: Record<string, string> | null;
  priority_map?: Record<string, string> | null;
  field_map?: Record<string, unknown> | null;
  sync_direction?: PmSyncDirection | null;
  conflict_policy?: PmConflictPolicy | null;
  enabled?: boolean | null;
}

/** A durable Forge-task ↔ external-issue link. */
export interface PmLink {
  id: string;
  forge_task_id: string;
  provider: PmProvider;
  external_id: string;
  external_key: string;
  external_url: string;
  sync_state: PmSyncState;
  last_synced_at?: string | null;
  conflict_detail?: Record<string, unknown> | null;
}

/** Result of `POST /connections/{id}/test` (the health probe). */
export interface PmHealthResult {
  status: "connected" | "error";
  provider: PmProvider;
  latency_ms: number;
  account?: string | null;
  granted_scopes: string[];
  error?: string | null;
}

// --- Workflow visual editor (F28 /workflow/editor router) ----------------- //

export type WorkflowRevisionStatus = "draft" | "published" | "archived";
export type WorkflowValidationState = "valid" | "invalid" | "unvalidated";
export type WorkflowDefinitionOrigin = "bundled" | "bundled_fork" | "custom";
export type WorkflowNodeKind = "normal" | "initial" | "terminal" | "human_gate";
export type WorkflowIssueSeverity = "error" | "warning";

/** Canvas position of a state node (UI-only; persisted in `graph_json`). */
export interface WorkflowNodeLayout {
  x: number;
  y: number;
}

/** A workflow state rendered as a graph node. */
export interface WorkflowStateNode {
  id: string;
  label?: string | null;
  kind: WorkflowNodeKind;
  layout: WorkflowNodeLayout;
}

/** A workflow transition rendered as a graph edge (mirrors `WorkflowTransition`). */
export interface WorkflowTransitionEdge {
  id: string;
  from_state: string;
  to_state: string;
  action?: string | null;
  when?: string | string[] | null;
  condition?: string | null;
  preconditions: string[];
  checks: string[];
  record?: string | null;
  skill?: string | null;
}

export interface WorkflowRetryPolicy {
  max_retries: number;
  backoff: string;
  initial_delay_seconds: number;
}

export interface WorkflowEscalationPolicy {
  confidence_threshold: number;
  on_low_confidence: string;
  on_policy_conflict: string;
}

/** The full editable graph: metadata + nodes + edges (+ layout). */
export interface WorkflowGraph {
  name: string;
  version: string;
  title: string;
  description?: string | null;
  modes: Record<string, unknown>;
  retry_policy: WorkflowRetryPolicy;
  escalation_policy: WorkflowEscalationPolicy;
  nodes: WorkflowStateNode[];
  edges: WorkflowTransitionEdge[];
}

/** One server-authoritative validation problem, anchored to a node or edge. */
export interface WorkflowValidationIssue {
  code: string;
  severity: WorkflowIssueSeverity;
  message: string;
  node_id?: string | null;
  edge_id?: string | null;
  invariant_id?: string | null;
}

export interface WorkflowRevisionSummary {
  id: string;
  revision: number;
  status: WorkflowRevisionStatus;
  validation_status: WorkflowValidationState;
  error_count: number;
  warning_count: number;
  notes?: string | null;
  created_by?: string | null;
  created_at?: string | null;
  published_at?: string | null;
}

export interface WorkflowRevisionDetail extends WorkflowRevisionSummary {
  graph: WorkflowGraph;
  dsl_yaml: string;
  validation_issues: WorkflowValidationIssue[];
}

export interface WorkflowDefinitionSummary {
  name: string;
  title: string;
  description?: string | null;
  origin: WorkflowDefinitionOrigin;
  base_bundled_name?: string | null;
  is_active: boolean;
  published_revision?: number | null;
  has_draft: boolean;
}

export interface WorkflowDefinitionDetail extends WorkflowDefinitionSummary {
  editable: boolean;
  current_published?: WorkflowRevisionDetail | null;
  draft?: WorkflowRevisionDetail | null;
}

/** Catalog (palette) entry for a guard / precondition predicate. */
export interface WorkflowGuardMeta {
  name: string;
  description: string;
  takes_arg: boolean;
  arg_hint?: string | null;
  is_precondition: boolean;
}

/** Catalog entry for an effect (a DSL `action` step). */
export interface WorkflowEffectMeta {
  name: string;
  description: string;
  provided_by?: string | null;
}

/** The registry palette the editor composes transitions from. */
export interface WorkflowCatalog {
  states: string[];
  events: string[];
  guards: WorkflowGuardMeta[];
  preconditions: WorkflowGuardMeta[];
  effects: WorkflowEffectMeta[];
  skills: string[];
  modes: string[];
}

export interface CreateWorkflowDefinition {
  name: string;
  title: string;
  description?: string | null;
  graph?: WorkflowGraph | null;
}

export interface SaveWorkflowDraftRequest {
  graph: WorkflowGraph;
  notes?: string | null;
}

/** The 409 body shape when a publish is blocked by validation errors. */
export interface WorkflowPublishBlocked {
  detail: string;
  errors: WorkflowValidationIssue[];
}

// --- Onboarding / guided walkthrough progress ----------------------------- //
// A derived, read-only projection that grounds the first-run product tour in
// real workspace state. Each step of the "spec -> run -> review PR -> merge"
// loop is backed by an existing router read (specs, approvals, deployments), so
// the walkthrough can reflect what the user has genuinely already accomplished.

/** The four stages of the Forge build loop, in order. */
export const ONBOARDING_STEP_KEYS = [
  "spec",
  "run",
  "review",
  "merge",
] as const;

export type OnboardingStepKey = (typeof ONBOARDING_STEP_KEYS)[number];

/** One loop stage's real-data completion signal. */
export interface OnboardingStepProgress {
  key: OnboardingStepKey;
  /** True once at least one real artifact backs this stage. */
  done: boolean;
  /** How many real artifacts back this stage (specs, PR gates, deploys…). */
  count: number;
}

/** The whole loop's completion, derived from live router reads. */
export interface OnboardingProgress {
  projectId: string;
  steps: OnboardingStepProgress[];
  /** Number of stages with `done === true`. */
  completedCount: number;
  /** Total number of stages (always {@link ONBOARDING_STEP_KEYS}.length). */
  totalCount: number;
  /** True when every stage has been completed at least once. */
  allComplete: boolean;
}

// --- Adaptive Orchestration settings (ao-settings-api, /ao/*) ------------- //

/** The five Adaptive Orchestration roles configured independently. */
export type AgentRole = "planner" | "coder" | "reviewer" | "spec_author" | "coordinator";

/** Model "thinking effort" a role runs at (provider-agnostic). */
export type AoEffort = "low" | "medium" | "high" | "max";

/** Where an effective role config came from, in resolution order. */
export type RoleConfigSource = "default" | "workspace" | "project";

/** The effective `{model_or_tier, effort}` for one role, plus its source. */
export interface RoleConfigOut {
  role: AgentRole;
  model_or_tier: string;
  effort: AoEffort;
  source: RoleConfigSource;
}

/** Body for `GET /ao/role-config`. */
export interface RoleConfigListResponse {
  items: RoleConfigOut[];
}

/** Body for `PUT /ao/role-config/{role}`: pin a human override. */
export interface RoleConfigUpsertRequest {
  model_or_tier: string;
  effort: AoEffort;
}

/**
 * The effective workspace-wide Adaptive Orchestration settings. `junior_max`/
 * `medior_max` are always the *effective* threshold (workspace override, or
 * the hardcoded default when unset).
 */
export interface AoSettingsOut {
  workspace_id: string;
  auto_route: boolean;
  /** `{provider: {tier: model}}` overrides layered onto the model router's defaults. */
  tier_model_overrides: Record<string, Record<string, string>>;
  junior_max: number;
  medior_max: number;
  junior_max_is_default: boolean;
  medior_max_is_default: boolean;
}

/** `PUT /ao/settings` — every field is optional (partial update). */
export interface AoSettingsUpdateRequest {
  auto_route?: boolean;
  tier_model_overrides?: Record<string, Record<string, string>>;
  junior_max?: number;
  medior_max?: number;
  /** Reset the corresponding threshold back to the hardcoded default. */
  clear_junior_max?: boolean;
  clear_medior_max?: boolean;
}

/** Adaptive Orchestration seniority tier a task/spec sizes into. */
export type AoTier = "junior" | "medior" | "senior";

/** Whether a sized task runs single-agent or as a supervised swarm. */
export type AoStrategy = "single" | "swarm";

/** A sample task's sizing signals for `POST /ao/routing-preview` (all optional). */
export interface RoutingPreviewRequest {
  kind?: string;
  priority?: string;
  blast_radius?: "low" | "medium" | "high" | null;
  file_count?: number;
  repo_count?: number;
  requirement_count?: number;
  acceptance_criteria_count?: number;
  touches_contracts?: boolean;
  touches_security?: boolean;
  dependency_count?: number;
  open_questions_count?: number;
  underspecified?: boolean;
  provider?: "anthropic" | "openai";
}

/** What tier/model/strategy the sample task in `RoutingPreviewRequest` gets. */
export interface RoutingPreviewResponse {
  tier: AoTier;
  strategy: AoStrategy;
  score: number;
  reasons: string[];
  model: string;
  provider: "anthropic" | "openai";
  junior_max: number;
  medior_max: number;
  auto_route_enabled: boolean;
}
