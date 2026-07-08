/**
 * Typed Forge API client (Phase-0 stub).
 *
 * A thin `fetch` wrapper that knows the Forge API surface. The backend routes are
 * still Phase-0 stubs returning HTTP 501; this client therefore surfaces a typed
 * {@link ApiError} (with `notImplemented` set for 501s) so Task 1.6 can build
 * against a stable shape and progressively light up real handlers.
 */

import { deriveOnboardingProgress } from "./onboarding-progress";
import type {
  AgentRole,
  AoSettingsOut,
  AoSettingsUpdateRequest,
  ApprovalContext,
  ApprovalCount,
  ApprovalDecisionRecord,
  ApprovalDecisionRequest,
  ApprovalRequest,
  ApprovalResolution,
  ApprovalSummary,
  AuditEntry,
  AuditListResponse,
  AuditQuery,
  AuditVocabulary,
  BulkUpdate,
  ChainVerifyResult,
  BurndownSeries,
  CompleteSprintRequest,
  DeploymentDecisionRequest,
  DeploymentDetail,
  DeploymentListQuery,
  DeploymentRead,
  DeploymentRequestBody,
  CostSummary,
  CostSummaryQuery,
  CostTimeseries,
  CostTimeseriesQuery,
  EpicDTO,
  HealthResponse,
  IncidentDeclareRequest,
  IncidentDetailView,
  IncidentDTO,
  IncidentEventRequest,
  IncidentEventView,
  IncidentView,
  Installation,
  InstallPlan,
  InstallRequest,
  InstallResult,
  KnowledgeSearchRequest,
  Listing,
  ListingDetail,
  MilestoneDTO,
  OnboardingProgress,
  PipelineRead,
  PostmortemView,
  PmConnection,
  PmConnectionConfigInput,
  PmConnectionDetail,
  PmConnectionPatch,
  PmHealthResult,
  PmLink,
  PmSyncState,
  Principal,
  ProjectAccess,
  ProjectTeamAccess,
  ProjectTeamAccessInput,
  ProjectVisibilityInput,
  RemediationPlanView,
  RetrievedChunk,
  RoleConfigListResponse,
  RoleConfigOut,
  RoleConfigUpsertRequest,
  RoleGrant,
  RoleGrantInput,
  RoleGrantQuery,
  RoutingPreviewRequest,
  RoutingPreviewResponse,
  RunTrace,
  ServiceInfo,
  Team,
  TeamInput,
  TeamMember,
  TeamMemberInput,
  TeamRole,
  SpecDashboard,
  SpecManifest,
  Sprint,
  SprintDTO,
  SprintReport,
  TaskDTO,
  TaskStatus,
  VelocityDashboard,
  HrdDiscoverRequest,
  HrdDiscoverResponse,
  SamlTestResult,
  ScimTokenCreated,
  ScimTokenCreateRequest,
  ScimTokenInfo,
  SsoConfig,
  SsoConfigInput,
  CreateWorkflowDefinition,
  SaveWorkflowDraftRequest,
  WorkflowCatalog,
  WorkflowDefinitionDetail,
  WorkflowDefinitionSummary,
  WorkflowRevisionDetail,
  WorkflowRevisionSummary,
  WorkflowValidationIssue,
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

  // --- Incidents (F17 /incidents workflow surface) ------------------------ //

  /** Declare a manual incident (FSM starts at `incident_created`). */
  declareIncident(body: IncidentDeclareRequest): Promise<IncidentView> {
    return this.request<IncidentView>("/incidents", { method: "POST", body });
  }

  /** The incident queue (workspace-scoped; filter by project/state/severity). */
  listIncidentRecords(query?: RequestOptions["query"]): Promise<IncidentView[]> {
    return this.request<IncidentView[]>("/incidents", { query });
  }

  /** One incident's detail: summary + latest plan + event count. */
  getIncident(incidentId: string): Promise<IncidentDetailView> {
    return this.request<IncidentDetailView>(
      `/incidents/${encodeURIComponent(incidentId)}`,
    );
  }

  /** The ordered incident timeline (state changes, notes, remediation). */
  getIncidentTimeline(incidentId: string): Promise<IncidentEventView[]> {
    return this.request<IncidentEventView[]>(
      `/incidents/${encodeURIComponent(incidentId)}/timeline`,
    );
  }

  /** Drive the incident FSM with an event (WRITE-gated: human-in-the-loop). */
  sendIncidentEvent(
    incidentId: string,
    body: IncidentEventRequest,
  ): Promise<IncidentDetailView> {
    return this.request<IncidentDetailView>(
      `/incidents/${encodeURIComponent(incidentId)}/events`,
      { method: "POST", body },
    );
  }

  /** The latest proposed remediation runbook (404 when none proposed yet). */
  getRemediationPlan(incidentId: string): Promise<RemediationPlanView> {
    return this.request<RemediationPlanView>(
      `/incidents/${encodeURIComponent(incidentId)}/remediation`,
    );
  }

  /** The rendered postmortem + action items (404 until one is generated). */
  getPostmortem(incidentId: string): Promise<PostmortemView> {
    return this.request<PostmortemView>(
      `/incidents/${encodeURIComponent(incidentId)}/postmortem`,
    );
  }

  /** Publish the incident's postmortem (advances its status to published). */
  publishPostmortem(incidentId: string): Promise<PostmortemView> {
    return this.request<PostmortemView>(
      `/incidents/${encodeURIComponent(incidentId)}/postmortem/publish`,
      { method: "POST" },
    );
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

  // --- Spec engine / SDD lifecycle (F02 /spec + F23 dashboard) ------------ //

  /**
   * The spec-validation dashboard projection for a project: the constitution
   * plus every spec manifest with its rolled-up validation report. Backs the
   * SDD lifecycle view, gates and requirement->task->test traceability matrix.
   */
  getProjectSpecOverview(projectId: string): Promise<SpecDashboard> {
    return this.request<SpecDashboard>(
      `/projects/${encodeURIComponent(projectId)}/specs`,
    );
  }

  /** Read a single spec manifest by its deterministic uuid. */
  getSpecManifest(specId: string): Promise<SpecManifest> {
    return this.request<SpecManifest>(
      `/spec/specs/${encodeURIComponent(specId)}`,
    );
  }

  /** Approve a spec — the human gate that advances it out of clarification. */
  approveSpec(specId: string): Promise<SpecManifest> {
    return this.request<SpecManifest>(
      `/spec/specs/${encodeURIComponent(specId)}/approve`,
      { method: "POST" },
    );
  }

  // --- Onboarding / guided walkthrough ------------------------------------ //

  /**
   * Derived progress for the first-run guided walkthrough: how far the user has
   * advanced through the "spec -> run -> review PR -> merge" loop. Composed from
   * three existing router reads (specs, approvals, deployments) so the tour can
   * reflect real workspace state without a bespoke backend endpoint.
   */
  async getOnboardingProgress(projectId: string): Promise<OnboardingProgress> {
    const [dashboard, approvals, deployments] = await Promise.all([
      this.getProjectSpecOverview(projectId),
      this.listApprovals({ project_id: projectId }),
      this.listProjectDeployments(projectId),
    ]);
    return deriveOnboardingProgress(projectId, {
      specs: dashboard.specs ?? [],
      approvals,
      deployments,
    });
  }

  // --- Observability (run-trace viewer) ----------------------------------- //

  /** Assemble a step-level trace for one agent run (redacted, ordered). */
  getRunTrace(runId: string): Promise<RunTrace> {
    return this.request<RunTrace>(
      `/observability/runs/${encodeURIComponent(runId)}/trace`,
    );
  }

  // --- Cost & observability metrics (F38) --------------------------------- //

  /** Aggregate spend for a scope with a grouped breakdown (phase/provider/model). */
  getCostSummary(query?: CostSummaryQuery): Promise<CostSummary> {
    return this.request<CostSummary>("/cost/summary", {
      query: query as RequestOptions["query"],
    });
  }

  /** Bucketed spend over time, one series per group key. */
  getCostTimeseries(query?: CostTimeseriesQuery): Promise<CostTimeseries> {
    return this.request<CostTimeseries>("/cost/timeseries", {
      query: query as RequestOptions["query"],
    });
  }

  // --- Adaptive Orchestration settings (ao-settings-api) ------------------ //

  /** Every role's effective `{model_or_tier, effort}` (optionally project-scoped). */
  listAoRoleConfig(projectId?: string): Promise<RoleConfigListResponse> {
    return this.request<RoleConfigListResponse>("/ao/role-config", {
      query: projectId ? { project_id: projectId } : undefined,
    });
  }

  /** Pin a workspace- or project-scoped override for one role (admin). */
  upsertAoRoleConfig(
    role: AgentRole,
    body: RoleConfigUpsertRequest,
    projectId?: string,
  ): Promise<RoleConfigOut> {
    return this.request<RoleConfigOut>(
      `/ao/role-config/${encodeURIComponent(role)}`,
      {
        method: "PUT",
        body,
        query: projectId ? { project_id: projectId } : undefined,
      },
    );
  }

  /** Remove an override for one role, reverting to the next fallback (admin). */
  deleteAoRoleConfig(role: AgentRole, projectId?: string): Promise<RoleConfigOut> {
    return this.request<RoleConfigOut>(
      `/ao/role-config/${encodeURIComponent(role)}`,
      {
        method: "DELETE",
        query: projectId ? { project_id: projectId } : undefined,
      },
    );
  }

  /** Workspace-wide auto-route toggle, tier-model map, complexity thresholds. */
  getAoSettings(): Promise<AoSettingsOut> {
    return this.request<AoSettingsOut>("/ao/settings");
  }

  /** Update the workspace-wide Adaptive Orchestration settings (admin). */
  updateAoSettings(body: AoSettingsUpdateRequest): Promise<AoSettingsOut> {
    return this.request<AoSettingsOut>("/ao/settings", { method: "PUT", body });
  }

  /** What tier/model/strategy a sample task would get under this workspace's settings. */
  previewAoRouting(body: RoutingPreviewRequest): Promise<RoutingPreviewResponse> {
    return this.request<RoutingPreviewResponse>("/ao/routing-preview", {
      method: "POST",
      body,
    });
  }

  /**
   * The in-process F38 metric registry as Prometheus text exposition. Returns
   * an empty string when observability is disabled (the registry is a no-op).
   * The caller parses it (see `observability-metrics.ts`).
   */
  getMetricsExposition(): Promise<string> {
    return this.request<string>("/observability/metrics");
  }

  // --- Marketplace (F32 integration marketplace) -------------------------- //

  /** The catalog: community skill profiles + MCP connectors (workspace-scoped). */
  listListings(query?: RequestOptions["query"]): Promise<Listing[]> {
    return this.request<Listing[]>("/marketplace/listings", { query });
  }

  /** One package with its full version history (manifest + provenance). */
  getListing(registrySlug: string, slug: string): Promise<ListingDetail> {
    return this.request<ListingDetail>(
      `/marketplace/listings/${encodeURIComponent(registrySlug)}/${encodeURIComponent(slug)}`,
    );
  }

  /** Dry-run an install: verification result, warnings, admin follow-ups. */
  previewInstall(body: InstallRequest): Promise<InstallPlan> {
    return this.request<InstallPlan>("/marketplace/preview", {
      method: "POST",
      body,
    });
  }

  /** Install a package into the workspace (admin). */
  installPackage(body: InstallRequest): Promise<InstallResult> {
    return this.request<InstallResult>("/marketplace/install", {
      method: "POST",
      body,
    });
  }

  /** Installed packages, with any available update surfaced per row. */
  listInstallations(): Promise<Installation[]> {
    return this.request<Installation[]>("/marketplace/installations");
  }

  /** Update an installation to `version` (or the latest compatible). */
  updateInstallation(
    installationId: string,
    version?: string,
  ): Promise<InstallResult> {
    return this.request<InstallResult>(
      `/marketplace/installations/${encodeURIComponent(installationId)}/update`,
      { method: "POST", query: version ? { version } : undefined },
    );
  }

  // --- Audit log (F39 canonical /audit query surface) --------------------- //

  /** A cursor-paginated, redacted page of the immutable audit log. */
  listAudit(query?: AuditQuery): Promise<AuditListResponse> {
    return this.request<AuditListResponse>("/audit", {
      query: query as RequestOptions["query"],
    });
  }

  /** The filter vocabulary (actions / actor types / resources / outcomes). */
  getAuditVocabulary(): Promise<AuditVocabulary> {
    return this.request<AuditVocabulary>("/audit/actions");
  }

  /** One audit entry by id (workspace-isolated; foreign ids 404). */
  getAuditEntry(entryId: string): Promise<AuditEntry> {
    return this.request<AuditEntry>(`/audit/${encodeURIComponent(entryId)}`);
  }

  /** Re-walk the workspace's audit hash chain and return the integrity verdict. */
  verifyAuditChain(body?: {
    from_seq?: number;
    to_seq?: number;
  }): Promise<ChainVerifyResult> {
    return this.request<ChainVerifyResult>("/audit/verify", {
      method: "POST",
      body: body ?? {},
    });
  }

  /**
   * Stream the audit log as NDJSON (chain hashes included; re-verifiable
   * offline). Returns the raw body text — each line is one JSON entry.
   */
  exportAuditNdjson(query?: { from?: string; to?: string }): Promise<string> {
    return this.request<string>("/audit/export", {
      query: query as RequestOptions["query"],
    });
  }

  // --- Sprints & velocity (F26 sprint router) ----------------------------- //

  /** Every sprint for a project, newest scope first (GET /projects/{id}/sprints). */
  listProjectSprints(
    projectId: string,
    query?: RequestOptions["query"],
  ): Promise<Sprint[]> {
    return this.request<Sprint[]>(
      `/projects/${encodeURIComponent(projectId)}/sprints`,
      { query },
    );
  }

  /** Committed-vs-completed velocity + forecast over the last `n` sprints. */
  getVelocityDashboard(
    projectId: string,
    last?: number,
  ): Promise<VelocityDashboard> {
    return this.request<VelocityDashboard>(
      `/projects/${encodeURIComponent(projectId)}/velocity`,
      { query: last ? { last } : undefined },
    );
  }

  /** A sprint's day-by-day burndown (remaining vs ideal). */
  getSprintBurndown(sprintId: string): Promise<BurndownSeries> {
    return this.request<BurndownSeries>(
      `/sprints/${encodeURIComponent(sprintId)}/burndown`,
    );
  }

  /** The sprint report: velocity rollup + tasks bucketed by outcome. */
  getSprintReport(sprintId: string): Promise<SprintReport> {
    return this.request<SprintReport>(
      `/sprints/${encodeURIComponent(sprintId)}/report`,
    );
  }

  /** Start a planned sprint (baselines committed scope). WRITE-gated. */
  startSprint(sprintId: string): Promise<Sprint> {
    return this.request<Sprint>(
      `/sprints/${encodeURIComponent(sprintId)}/start`,
      { method: "POST" },
    );
  }

  /** Complete an active sprint, routing carryover. Returns its report. */
  completeSprint(
    sprintId: string,
    body: CompleteSprintRequest = {},
  ): Promise<SprintReport> {
    return this.request<SprintReport>(
      `/sprints/${encodeURIComponent(sprintId)}/complete`,
      { method: "POST", body },
    );
  }

  // --- Deployments & gates (F31 deployment-gates) ------------------------- //

  /** A project's promotion pipeline: ranked environments + what's live on each. */
  getDeploymentPipeline(projectId: string): Promise<PipelineRead> {
    return this.request<PipelineRead>(
      `/projects/${encodeURIComponent(projectId)}/pipeline`,
    );
  }

  /** Recent deployments for a project (optionally by environment / state). */
  listProjectDeployments(
    projectId: string,
    query?: DeploymentListQuery,
  ): Promise<DeploymentRead[]> {
    return this.request<DeploymentRead[]>(
      `/projects/${encodeURIComponent(projectId)}/deployments`,
      { query: query as RequestOptions["query"] },
    );
  }

  /** One deployment's detail: gate evaluation, per-check results, transitions. */
  getDeployment(deploymentId: string): Promise<DeploymentDetail> {
    return this.request<DeploymentDetail>(
      `/deployments/${encodeURIComponent(deploymentId)}`,
    );
  }

  /** Request a promotion of a commit to an environment (WRITE-gated). */
  requestDeployment(
    projectId: string,
    body: DeploymentRequestBody,
  ): Promise<DeploymentRead> {
    return this.request<DeploymentRead>(
      `/projects/${encodeURIComponent(projectId)}/deployments`,
      { method: "POST", body },
    );
  }

  /** Approve / reject / request-changes on a gated deployment (WRITE-gated). */
  decideDeployment(
    deploymentId: string,
    body: DeploymentDecisionRequest,
  ): Promise<DeploymentRead> {
    return this.request<DeploymentRead>(
      `/deployments/${encodeURIComponent(deploymentId)}/decision`,
      { method: "POST", body },
    );
  }

  /** Cancel an in-flight deployment (WRITE-gated). */
  cancelDeployment(deploymentId: string): Promise<DeploymentRead> {
    return this.request<DeploymentRead>(
      `/deployments/${encodeURIComponent(deploymentId)}/cancel`,
      { method: "POST" },
    );
  }

  /** Roll back a succeeded deployment (WRITE-gated). */
  rollbackDeployment(deploymentId: string): Promise<DeploymentRead> {
    return this.request<DeploymentRead>(
      `/deployments/${encodeURIComponent(deploymentId)}/rollback`,
      { method: "POST" },
    );
  }

  // --- Enterprise SSO + SCIM (F33 auth/sso admin routers) ----------------- //

  /** The workspace SAML configuration (admin-only; 404 when unconfigured). */
  getSsoConfig(workspaceId: string): Promise<SsoConfig> {
    return this.request<SsoConfig>(
      `/workspaces/${encodeURIComponent(workspaceId)}/sso`,
    );
  }

  /** Create or replace the workspace SAML configuration (admin-only). */
  putSsoConfig(workspaceId: string, body: SsoConfigInput): Promise<SsoConfig> {
    return this.request<SsoConfig>(
      `/workspaces/${encodeURIComponent(workspaceId)}/sso`,
      { method: "PUT", body },
    );
  }

  /** Enable SSO for the workspace. */
  enableSso(workspaceId: string): Promise<SsoConfig> {
    return this.request<SsoConfig>(
      `/workspaces/${encodeURIComponent(workspaceId)}/sso/enable`,
      { method: "POST" },
    );
  }

  /** Disable SSO (break-glass guarded: 409 without a local admin). */
  disableSso(workspaceId: string): Promise<SsoConfig> {
    return this.request<SsoConfig>(
      `/workspaces/${encodeURIComponent(workspaceId)}/sso/disable`,
      { method: "POST" },
    );
  }

  /** Delete the workspace SAML configuration entirely. */
  deleteSsoConfig(workspaceId: string): Promise<void> {
    return this.request<void>(
      `/workspaces/${encodeURIComponent(workspaceId)}/sso`,
      { method: "DELETE" },
    );
  }

  /** Validation-only SAML round trip (never mints a session). */
  testSsoConfig(
    workspaceId: string,
    samlResponse: string,
  ): Promise<SamlTestResult> {
    return this.request<SamlTestResult>(
      `/workspaces/${encodeURIComponent(workspaceId)}/sso/test`,
      { method: "POST", body: { saml_response: samlResponse } },
    );
  }

  /** SCIM provisioning tokens for the workspace (redacted). */
  listScimTokens(workspaceId: string): Promise<ScimTokenInfo[]> {
    return this.request<ScimTokenInfo[]>(
      `/workspaces/${encodeURIComponent(workspaceId)}/scim/tokens`,
    );
  }

  /** Issue a SCIM bearer token; the raw value is returned exactly once. */
  createScimToken(
    workspaceId: string,
    body: ScimTokenCreateRequest,
  ): Promise<ScimTokenCreated> {
    return this.request<ScimTokenCreated>(
      `/workspaces/${encodeURIComponent(workspaceId)}/scim/tokens`,
      { method: "POST", body },
    );
  }

  /** Revoke a SCIM token. */
  revokeScimToken(workspaceId: string, tokenId: string): Promise<void> {
    return this.request<void>(
      `/workspaces/${encodeURIComponent(workspaceId)}/scim/tokens/${encodeURIComponent(tokenId)}`,
      { method: "DELETE" },
    );
  }

  /** Home-realm discovery: does this email's domain route to SSO? */
  discoverSso(body: HrdDiscoverRequest): Promise<HrdDiscoverResponse> {
    return this.request<HrdDiscoverResponse>("/auth/saml/discover", {
      method: "POST",
      body,
    });
  }

  // --- External PM adapters (F18 /integrations/pm router) ----------------- //

  /** Every external PM connection in the workspace (redaction-safe). */
  listPmConnections(): Promise<PmConnection[]> {
    return this.request<PmConnection[]>("/integrations/pm/connections");
  }

  /** One connection with its per-state link tallies. */
  getPmConnection(connectionId: string): Promise<PmConnectionDetail> {
    return this.request<PmConnectionDetail>(
      `/integrations/pm/connections/${encodeURIComponent(connectionId)}`,
    );
  }

  /** Create (connect) an external PM adapter (admin-only). */
  createPmConnection(body: PmConnectionConfigInput): Promise<PmConnection> {
    return this.request<PmConnection>("/integrations/pm/connections", {
      method: "POST",
      body,
    });
  }

  /** Patch a connection's mapping / policy / enabled flag (admin-only). */
  patchPmConnection(
    connectionId: string,
    body: PmConnectionPatch,
  ): Promise<PmConnection> {
    return this.request<PmConnection>(
      `/integrations/pm/connections/${encodeURIComponent(connectionId)}`,
      { method: "PATCH", body },
    );
  }

  /** Disconnect (disable + best-effort webhook unregister); links are retained. */
  disconnectPmConnection(connectionId: string): Promise<PmConnection> {
    return this.request<PmConnection>(
      `/integrations/pm/connections/${encodeURIComponent(connectionId)}`,
      { method: "DELETE" },
    );
  }

  /** Live health probe against the provider (admin-only). */
  testPmConnection(connectionId: string): Promise<PmHealthResult> {
    return this.request<PmHealthResult>(
      `/integrations/pm/connections/${encodeURIComponent(connectionId)}/test`,
      { method: "POST" },
    );
  }

  /** Durable task ↔ issue links, optionally filtered to a sync state. */
  listPmLinks(connectionId: string, state?: PmSyncState): Promise<PmLink[]> {
    return this.request<PmLink[]>(
      `/integrations/pm/connections/${encodeURIComponent(connectionId)}/links`,
      { query: state ? { state } : undefined },
    );
  }

  // --- Multi-team & RBAC (F30 authz routers) ------------------------------ //
  // Every route is implicitly scoped to the authenticated principal's
  // workspace (no workspace path param); the server enforces the escalation +
  // last-admin-lockout invariants and returns 403/409 on violation.

  /** Role grants in the workspace (filterable by principal / scope). */
  listRoleGrants(query?: RoleGrantQuery): Promise<RoleGrant[]> {
    return this.request<RoleGrant[]>("/access/grants", {
      query: query as RequestOptions["query"],
    });
  }

  /** Grant a role to a principal at a scope (WRITE-gated: `role.grant`). */
  createRoleGrant(body: RoleGrantInput): Promise<RoleGrant> {
    return this.request<RoleGrant>("/access/grants", { method: "POST", body });
  }

  /** Revoke a role grant (409 when it would remove the last workspace admin). */
  revokeRoleGrant(grantId: string): Promise<void> {
    return this.request<void>(
      `/access/grants/${encodeURIComponent(grantId)}`,
      { method: "DELETE" },
    );
  }

  /** Every team in the workspace (excludes archived by default server-side). */
  listTeams(query?: RequestOptions["query"]): Promise<Team[]> {
    return this.request<Team[]>("/teams", { query });
  }

  /** Create a team (WRITE-gated: `team.manage`). */
  createTeam(body: TeamInput): Promise<Team> {
    return this.request<Team>("/teams", { method: "POST", body });
  }

  /** A team's members with their team roles. */
  listTeamMembers(teamId: string): Promise<TeamMember[]> {
    return this.request<TeamMember[]>(
      `/teams/${encodeURIComponent(teamId)}/members`,
    );
  }

  /** Add a member to a team (WRITE-gated: `team.member.manage`). */
  addTeamMember(teamId: string, body: TeamMemberInput): Promise<TeamMember> {
    return this.request<TeamMember>(
      `/teams/${encodeURIComponent(teamId)}/members`,
      { method: "POST", body },
    );
  }

  /** Change a member's team role (lead / member). */
  setTeamMemberRole(
    teamId: string,
    userId: string,
    teamRole: TeamRole,
  ): Promise<TeamMember> {
    return this.request<TeamMember>(
      `/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(userId)}`,
      { method: "PATCH", body: { team_role: teamRole } },
    );
  }

  /** Remove a member from a team. */
  removeTeamMember(teamId: string, userId: string): Promise<void> {
    return this.request<void>(
      `/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(userId)}`,
      { method: "DELETE" },
    );
  }

  /** A project's visibility + per-team access (404 when invisible/foreign). */
  getProjectAccess(projectId: string): Promise<ProjectAccess> {
    return this.request<ProjectAccess>(
      `/projects/${encodeURIComponent(projectId)}/access`,
    );
  }

  /** Set a project's visibility (WRITE-gated: `project.admin`). */
  setProjectVisibility(
    projectId: string,
    body: ProjectVisibilityInput,
  ): Promise<ProjectAccess> {
    return this.request<ProjectAccess>(
      `/projects/${encodeURIComponent(projectId)}/visibility`,
      { method: "PUT", body },
    );
  }

  /** Grant or update a team's access level on a project. */
  upsertProjectTeamAccess(
    projectId: string,
    body: ProjectTeamAccessInput,
  ): Promise<ProjectTeamAccess> {
    return this.request<ProjectTeamAccess>(
      `/projects/${encodeURIComponent(projectId)}/team-access`,
      { method: "POST", body },
    );
  }

  /** Remove a team's access on a project. */
  removeProjectTeamAccess(projectId: string, teamId: string): Promise<void> {
    return this.request<void>(
      `/projects/${encodeURIComponent(projectId)}/team-access/${encodeURIComponent(teamId)}`,
      { method: "DELETE" },
    );
  }

  // --- Workflow visual editor (F28 /workflow/editor router) --------------- //

  /** The registry palette: states, events, guards, effects, skills, modes. */
  getWorkflowCatalog(): Promise<WorkflowCatalog> {
    return this.request<WorkflowCatalog>("/workflow/editor/catalog");
  }

  /** Every workflow definition in the workspace (bundled + custom + forks). */
  listWorkflowDefinitions(): Promise<WorkflowDefinitionSummary[]> {
    return this.request<WorkflowDefinitionSummary[]>(
      "/workflow/editor/definitions",
    );
  }

  /** One definition with its published + draft revisions (graph + issues). */
  getWorkflowDefinition(name: string): Promise<WorkflowDefinitionDetail> {
    return this.request<WorkflowDefinitionDetail>(
      `/workflow/editor/definitions/${encodeURIComponent(name)}`,
    );
  }

  /** Author a new custom workflow (admin). Seeds an initial draft. */
  createWorkflowDefinition(
    body: CreateWorkflowDefinition,
  ): Promise<WorkflowDefinitionDetail> {
    return this.request<WorkflowDefinitionDetail>(
      "/workflow/editor/definitions",
      { method: "POST", body },
    );
  }

  /** Fork a read-only bundled workflow into an editable copy (admin). */
  forkBundledWorkflow(name: string): Promise<WorkflowDefinitionDetail> {
    return this.request<WorkflowDefinitionDetail>(
      `/workflow/editor/definitions/${encodeURIComponent(name)}/fork`,
      { method: "POST" },
    );
  }

  /** Save the working graph as the draft; returns it re-validated server-side. */
  saveWorkflowDraft(
    name: string,
    body: SaveWorkflowDraftRequest,
  ): Promise<WorkflowRevisionDetail> {
    return this.request<WorkflowRevisionDetail>(
      `/workflow/editor/definitions/${encodeURIComponent(name)}/draft`,
      { method: "PUT", body },
    );
  }

  /** Re-run validation on the saved draft; returns every issue (errors + warnings). */
  validateWorkflowDraft(name: string): Promise<WorkflowValidationIssue[]> {
    return this.request<WorkflowValidationIssue[]>(
      `/workflow/editor/definitions/${encodeURIComponent(name)}/draft/validate`,
      { method: "POST" },
    );
  }

  /** Publish the draft as the new active revision (admin). 409 when errors remain. */
  publishWorkflow(name: string): Promise<WorkflowRevisionDetail> {
    return this.request<WorkflowRevisionDetail>(
      `/workflow/editor/definitions/${encodeURIComponent(name)}/publish`,
      { method: "POST" },
    );
  }

  /** A definition's revision history (newest edits last; drafts + published). */
  listWorkflowRevisions(name: string): Promise<WorkflowRevisionSummary[]> {
    return this.request<WorkflowRevisionSummary[]>(
      `/workflow/editor/definitions/${encodeURIComponent(name)}/revisions`,
    );
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
