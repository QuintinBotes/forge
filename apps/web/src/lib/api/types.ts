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
