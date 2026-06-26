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
