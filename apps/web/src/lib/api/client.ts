/**
 * Typed Forge API client (Phase-0 stub).
 *
 * A thin `fetch` wrapper that knows the Forge API surface. The backend routes are
 * still Phase-0 stubs returning HTTP 501; this client therefore surfaces a typed
 * {@link ApiError} (with `notImplemented` set for 501s) so Task 1.6 can build
 * against a stable shape and progressively light up real handlers.
 */

import type {
  ApprovalContext,
  ApprovalCount,
  ApprovalDecisionRecord,
  ApprovalDecisionRequest,
  ApprovalRequest,
  ApprovalResolution,
  ApprovalSummary,
  BulkUpdate,
  EpicDTO,
  HealthResponse,
  IncidentDTO,
  KnowledgeSearchRequest,
  MilestoneDTO,
  Principal,
  RetrievedChunk,
  ServiceInfo,
  SprintDTO,
  TaskDTO,
  TaskStatus,
} from "./types";

export const DEFAULT_API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  /** True when the endpoint exists but is a Phase-0 stub (HTTP 501). */
  readonly notImplemented: boolean;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.notImplemented = status === 501;
  }
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined>;
  signal?: AbortSignal;
  /** Bearer token / API key forwarded as the `Authorization` header. */
  token?: string;
}

export interface ApiClientConfig {
  baseUrl?: string;
  /** Default auth token applied to every request unless overridden per-call. */
  token?: string;
  /** Injectable fetch (defaults to global `fetch`); handy for tests. */
  fetch?: typeof fetch;
}

function buildUrl(
  baseUrl: string,
  path: string,
  query?: RequestOptions["query"],
): string {
  const url = new URL(path.replace(/^\//, ""), baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return url.toString();
}

export class ForgeApiClient {
  readonly baseUrl: string;
  private readonly token?: string;
  private readonly fetchImpl: typeof fetch;

  constructor(config: ApiClientConfig = {}) {
    this.baseUrl = config.baseUrl ?? DEFAULT_API_BASE_URL;
    this.token = config.token;
    this.fetchImpl = config.fetch ?? globalThis.fetch;
  }

  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const url = buildUrl(this.baseUrl, path, options.query);
    const headers: Record<string, string> = { Accept: "application/json" };
    const token = options.token ?? this.token;
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
    let body: string | undefined;
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.body);
    }

    const response = await this.fetchImpl(url, {
      method: options.method ?? "GET",
      headers,
      body,
      signal: options.signal,
    });

    const payload = await parseBody(response);
    if (!response.ok) {
      throw new ApiError(
        response.status,
        `${options.method ?? "GET"} ${path} failed with ${response.status}`,
        payload,
      );
    }
    return payload as T;
  }

  // --- Service ------------------------------------------------------------ //

  info(): Promise<ServiceInfo> {
    return this.request<ServiceInfo>("/");
  }

  health(): Promise<HealthResponse> {
    return this.request<HealthResponse>("/health");
  }

  /** The authenticated principal (used to resolve "assign to me"). */
  me(): Promise<Principal> {
    return this.request<Principal>("/auth/me");
  }

  // --- Board: tasks ------------------------------------------------------- //

  listTasks(query?: RequestOptions["query"]): Promise<TaskDTO[]> {
    return this.request<TaskDTO[]>("/board/tasks", { query });
  }

  getTask(taskId: string): Promise<TaskDTO> {
    return this.request<TaskDTO>(`/board/tasks/${taskId}`);
  }

  createTask(task: TaskDTO): Promise<TaskDTO> {
    return this.request<TaskDTO>("/board/tasks", { method: "POST", body: task });
  }

  updateTask(taskId: string, patch: Partial<TaskDTO>): Promise<TaskDTO> {
    return this.request<TaskDTO>(`/board/tasks/${taskId}`, {
      method: "PATCH",
      body: patch,
    });
  }

  setTaskStatus(taskId: string, status: TaskStatus): Promise<TaskDTO> {
    return this.request<TaskDTO>(`/board/tasks/${taskId}/status`, {
      method: "POST",
      body: { status },
    });
  }

  /** Apply one mutation per entry in a single call (spec: bulk actions). */
  bulkUpdateTasks(updates: BulkUpdate[]): Promise<TaskDTO[]> {
    return this.request<TaskDTO[]>("/board/tasks/bulk", {
      method: "POST",
      body: updates,
    });
  }

  // --- Board: other entities --------------------------------------------- //

  listEpics(query?: RequestOptions["query"]): Promise<EpicDTO[]> {
    return this.request<EpicDTO[]>("/board/epics", { query });
  }

  listSprints(query?: RequestOptions["query"]): Promise<SprintDTO[]> {
    return this.request<SprintDTO[]>("/board/sprints", { query });
  }

  listMilestones(query?: RequestOptions["query"]): Promise<MilestoneDTO[]> {
    return this.request<MilestoneDTO[]>("/board/milestones", { query });
  }

  listIncidents(query?: RequestOptions["query"]): Promise<IncidentDTO[]> {
    return this.request<IncidentDTO[]>("/board/incidents", { query });
  }

  // --- Knowledge ---------------------------------------------------------- //

  searchKnowledge(req: KnowledgeSearchRequest): Promise<RetrievedChunk[]> {
    return this.request<RetrievedChunk[]>("/knowledge/search", {
      method: "POST",
      body: req,
    });
  }

  // --- Approvals (F36 unified /approvals router) -------------------------- //

  /** The approval inbox: workspace-scoped, critical risk first. */
  listApprovals(query?: RequestOptions["query"]): Promise<ApprovalSummary[]> {
    return this.request<ApprovalSummary[]>("/approvals", { query });
  }

  /** Pending-count badge; matches the inbox length by construction. */
  approvalCount(query?: RequestOptions["query"]): Promise<ApprovalCount> {
    return this.request<ApprovalCount>("/approvals/count", { query });
  }

  getApproval(approvalId: string): Promise<ApprovalRequest> {
    return this.request<ApprovalRequest>(`/approvals/${approvalId}`);
  }

  /** The nine "must-show" review items, built by the gate's provider. */
  getApprovalContext(approvalId: string): Promise<ApprovalContext> {
    return this.request<ApprovalContext>(`/approvals/${approvalId}/context`);
  }

  /** The immutable per-approver decision trail. */
  listApprovalDecisions(approvalId: string): Promise<ApprovalDecisionRecord[]> {
    return this.request<ApprovalDecisionRecord[]>(
      `/approvals/${approvalId}/decisions`,
    );
  }

  /** Approve / reject / request changes / escalate a gate. */
  decideApproval(
    approvalId: string,
    body: ApprovalDecisionRequest,
  ): Promise<ApprovalResolution> {
    return this.request<ApprovalResolution>(`/approvals/${approvalId}/decision`, {
      method: "POST",
      body,
    });
  }
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** Process-wide default client (browser/server share the same base URL). */
export const apiClient = new ForgeApiClient();
