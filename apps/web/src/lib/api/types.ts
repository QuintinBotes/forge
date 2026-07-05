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
