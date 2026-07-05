"use client";

import {
  Activity,
  ArrowRight,
  Cable,
  Check,
  GitMerge,
  Inbox,
  Loader2,
  Plug,
  PlugZap,
  Plus,
  RefreshCw,
  Save,
  ShieldAlert,
  SlidersHorizontal,
  Trash2,
  Unplug,
} from "lucide-react";
import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";

import { useRegisterCommands } from "@/components/command-palette";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useCreatePmConnection,
  useDisconnectPmConnection,
  usePatchPmConnection,
  usePmConnection,
  usePmConnections,
  usePmLinks,
  useTestPmConnection,
} from "@/lib/api/pm";
import {
  PM_CONFLICT_POLICIES,
  PM_PROVIDERS,
  PM_STATUS_CATEGORIES,
  PM_SYNC_DIRECTIONS,
  PM_SYNC_STATES,
  type PmConflictPolicy,
  type PmConnection,
  type PmConnectionConfigInput,
  type PmHealthResult,
  type PmProvider,
  type PmSyncDirection,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  conflictPolicyHint,
  conflictPolicyLabel,
  connectionStatusMeta,
  isConnectionEnabled,
  providerLabel,
  relativeTime,
  rowsToStatusMap,
  statusCategoryLabel,
  statusMapToRows,
  summarizeLinks,
  syncDirectionHint,
  syncDirectionLabel,
  syncStateMeta,
  type StatusMapRow,
  type Tone,
} from "./pm-meta";

const FIELD =
  "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

const TONE_BADGE: Record<Tone, string> = {
  success: "border-success/40 bg-success/10 text-success",
  warning: "border-warning/40 bg-warning/10 text-warning",
  danger: "border-danger/40 bg-danger/10 text-danger",
  info: "border-spark/40 bg-spark/15 text-foreground",
  muted: "border-border bg-muted text-muted-foreground",
};

const TONE_DOT: Record<Tone, string> = {
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
  info: "bg-spark",
  muted: "bg-muted-foreground",
};

function mutationMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return "You don't have permission to change PM integrations.";
    if (error.status === 409)
      return "A connection for that project already exists.";
    if (error.status === 404) return "That connection no longer exists.";
    if (error.status === 400)
      return "Some details look invalid. Check the fields and try again.";
  }
  return "Something went wrong. Please try again.";
}

export interface PmIntegrationsViewProps {
  client?: ForgeApiClient;
}

type ViewMode = "detail" | "new";

/**
 * PM integrations (F18) — the workspace's external project-management control
 * plane. A rail of connected Jira / Linear adapters on the left; on the right,
 * the selected connection's connect details, live sync health, a status-map
 * editor, and a conflict inbox — or the connect form when adding one. Ember is
 * spent on a single primary per view: "Connect a PM tool" in the empty state,
 * "Connect" in the form, and "Save mapping" once the status map is edited.
 */
export function PmIntegrationsView({
  client = apiClient,
}: PmIntegrationsViewProps) {
  const connectionsQuery = usePmConnections(client);
  const connections = useMemo(
    () => connectionsQuery.data ?? [],
    [connectionsQuery.data],
  );

  const [mode, setMode] = useState<ViewMode>("detail");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const effectiveId =
    mode === "new" ? null : (selectedId ?? connections[0]?.id ?? null);

  const select = useCallback((id: string) => {
    setSelectedId(id);
    setMode("detail");
  }, []);

  const openNew = useCallback(() => setMode("new"), []);
  const closeNew = useCallback(() => setMode("detail"), []);

  const onConnected = useCallback((conn: PmConnection) => {
    setSelectedId(conn.id);
    setMode("detail");
  }, []);

  // Keyboard-first: open the connect form from the command palette.
  const openNewRef = useRef(openNew);
  openNewRef.current = openNew;
  const commands = useMemo(
    () => [
      {
        id: "pm-new-connection",
        label: "Connect a PM tool",
        group: "Integrations",
        icon: <Plus />,
        run: () => openNewRef.current(),
      },
    ],
    [],
  );
  useRegisterCommands("pm-integrations", commands);

  if (connectionsQuery.isLoading) {
    return <ScreenSkeleton />;
  }
  if (connectionsQuery.isError) {
    return <ScreenError onRetry={() => void connectionsQuery.refetch()} />;
  }

  const empty = connections.length === 0;

  return (
    <div
      data-testid="pm-view"
      className="mx-auto flex w-full max-w-6xl flex-col gap-6"
    >
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-border bg-muted/60 text-primary">
            <Cable className="h-5 w-5" aria-hidden />
          </span>
          <div>
            <h1 className="font-display text-xl font-semibold tracking-tight">
              PM integrations
            </h1>
            <p className="text-sm text-muted-foreground">
              Sync Forge tasks with external project management — Jira and Linear.
            </p>
          </div>
        </div>
        {!empty ? (
          <button
            type="button"
            data-testid="pm-new-open"
            onClick={openNew}
            className="inline-flex h-9 items-center gap-2 rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Plus className="h-4 w-4" aria-hidden />
            New connection
          </button>
        ) : null}
      </header>

      {empty && mode !== "new" ? (
        <EmptyState onConnect={openNew} />
      ) : mode === "new" ? (
        <ConnectionForm
          client={client}
          onCancel={empty ? undefined : closeNew}
          onConnected={onConnected}
        />
      ) : (
        <div className="grid gap-6 lg:grid-cols-[18rem_minmax(0,1fr)]">
          <ConnectionRail
            connections={connections}
            activeId={effectiveId}
            onSelect={select}
          />
          {effectiveId ? (
            <ConnectionDetail
              key={effectiveId}
              connectionId={effectiveId}
              client={client}
            />
          ) : null}
        </div>
      )}
    </div>
  );
}

// --- Connection rail ------------------------------------------------------- //

function ConnectionRail({
  connections,
  activeId,
  onSelect,
}: {
  connections: PmConnection[];
  activeId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <nav
      aria-label="PM connections"
      data-testid="pm-rail"
      className="flex flex-col gap-1.5"
    >
      {connections.map((conn) => {
        const active = conn.id === activeId;
        const meta = connectionStatusMeta(conn.status);
        return (
          <button
            key={conn.id}
            type="button"
            data-testid={`pm-conn-${conn.id}`}
            data-active={active}
            aria-current={active ? "true" : undefined}
            onClick={() => onSelect(conn.id)}
            className={cn(
              "flex items-center gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              active
                ? "border-border bg-card shadow-sm"
                : "border-transparent hover:border-border hover:bg-accent/50",
            )}
          >
            <ProviderTile provider={conn.provider} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium text-foreground">
                {conn.name}
              </span>
              <span className="block truncate font-mono text-[11px] text-muted-foreground">
                {conn.external_project_key}
              </span>
            </span>
            <span
              className={cn("h-2 w-2 shrink-0 rounded-full", TONE_DOT[meta.tone])}
              aria-label={meta.label}
              title={meta.label}
            />
          </button>
        );
      })}
    </nav>
  );
}

function ProviderTile({
  provider,
  className,
}: {
  provider: PmProvider;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 font-display text-sm font-semibold text-primary",
        className,
      )}
      aria-hidden
    >
      {provider === "jira" ? "J" : "L"}
    </span>
  );
}

// --- Connection detail ----------------------------------------------------- //

function ConnectionDetail({
  connectionId,
  client,
}: {
  connectionId: string;
  client: ForgeApiClient;
}) {
  const detailQuery = usePmConnection(connectionId, client);
  const patch = usePatchPmConnection(client);
  const disconnect = useDisconnectPmConnection(client);
  const test = useTestPmConnection(client);

  const detail = detailQuery.data;

  const [rows, setRows] = useState<StatusMapRow[]>([]);
  const [seeded, setSeeded] = useState(false);
  const [mapError, setMapError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Seed the status-map editor from the loaded config exactly once, so a
  // background refetch never clobbers an in-progress edit.
  if (!seeded && detail) {
    setSeeded(true);
    setRows(statusMapToRows(detail.status_map));
  }

  const savedMap = detail?.status_map ?? {};
  const draftMap = useMemo(() => rowsToStatusMap(rows), [rows]);
  const mapDirty = useMemo(
    () => JSON.stringify(draftMap) !== JSON.stringify(savedMap),
    [draftMap, savedMap],
  );

  const saveMap = useCallback(() => {
    if (!mapDirty || patch.isPending) return;
    setMapError(null);
    patch.mutate(
      { connectionId, body: { status_map: draftMap } },
      { onError: (err) => setMapError(mutationMessage(err)) },
    );
  }, [mapDirty, patch, connectionId, draftMap]);

  const runTest = useCallback(() => {
    if (test.isPending) return;
    test.mutate(connectionId);
  }, [test, connectionId]);

  // Keyboard-first detail actions.
  const saveRef = useRef(saveMap);
  saveRef.current = saveMap;
  const testRef = useRef(runTest);
  testRef.current = runTest;
  const commands = useMemo(
    () => [
      {
        id: `pm-test-${connectionId}`,
        label: "Test PM connection",
        group: "Integrations",
        icon: <RefreshCw />,
        run: () => testRef.current(),
      },
      {
        id: `pm-save-map-${connectionId}`,
        label: "Save PM status mapping",
        group: "Integrations",
        icon: <Save />,
        run: () => saveRef.current(),
      },
    ],
    [connectionId],
  );
  useRegisterCommands(`pm-detail-${connectionId}`, commands);

  if (detailQuery.isLoading) {
    return <DetailSkeleton />;
  }
  if (detailQuery.isError || !detail) {
    return (
      <DetailError onRetry={() => void detailQuery.refetch()} />
    );
  }

  const meta = connectionStatusMeta(detail.status);
  const enabled = isConnectionEnabled(detail.status);
  const rollup = summarizeLinks(detail.link_counts);

  const onPatch = (
    body: { sync_direction?: PmSyncDirection; conflict_policy?: PmConflictPolicy; enabled?: boolean },
  ) => {
    setActionError(null);
    patch.mutate(
      { connectionId, body },
      { onError: (err) => setActionError(mutationMessage(err)) },
    );
  };

  return (
    <div data-testid="pm-detail" className="flex min-w-0 flex-col gap-6">
      {/* Header */}
      <section className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex items-start gap-3">
            <ProviderTile provider={detail.provider} className="h-10 w-10" />
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="font-display text-lg font-semibold tracking-tight">
                  {detail.name}
                </h2>
                <StatusBadge
                  testid="pm-status-badge"
                  tone={meta.tone}
                  label={meta.label}
                />
              </div>
              <p className="mt-0.5 text-sm text-muted-foreground">
                {providerLabel(detail.provider)}
                <span aria-hidden> · </span>
                <span className="font-mono text-xs">
                  {detail.external_project_key}
                </span>
                {detail.account_label ? (
                  <>
                    <span aria-hidden> · </span>
                    {detail.account_label}
                  </>
                ) : null}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Toggle
              testid="pm-enable-toggle"
              checked={enabled}
              disabled={patch.isPending}
              onChange={(next) => onPatch({ enabled: next })}
              label={enabled ? "Sync enabled" : "Sync paused"}
            />
            <button
              type="button"
              data-testid="pm-test"
              onClick={runTest}
              disabled={test.isPending}
              className="inline-flex h-9 items-center gap-2 rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
            >
              {test.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              ) : (
                <RefreshCw className="h-4 w-4" aria-hidden />
              )}
              Test connection
            </button>
            <button
              type="button"
              data-testid="pm-disconnect"
              onClick={() => {
                setActionError(null);
                disconnect.mutate(connectionId, {
                  onError: (err) => setActionError(mutationMessage(err)),
                });
              }}
              disabled={disconnect.isPending || detail.status === "disabled"}
              className="inline-flex h-9 items-center gap-2 rounded-md border border-danger/40 px-3 text-sm font-medium text-danger transition-colors hover:bg-danger/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
            >
              <Unplug className="h-4 w-4" aria-hidden />
              Disconnect
            </button>
          </div>
        </div>

        {actionError ? (
          <p role="alert" className="text-sm text-danger">
            {actionError}
          </p>
        ) : null}

        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 border-t border-border pt-4 sm:grid-cols-4">
          <Meta label="Last checked" value={relativeTime(detail.last_health_at)} />
          <Meta
            label="Last full sync"
            value={relativeTime(detail.last_full_sync_at)}
          />
          <Meta
            label="Credential"
            value={detail.has_credential ? "Stored" : "Missing"}
            tone={detail.has_credential ? undefined : "danger"}
          />
          <Meta
            label="Webhook"
            value={detail.has_webhook_secret ? "Verified" : "Not set"}
            tone={detail.has_webhook_secret ? undefined : "muted"}
          />
        </dl>
      </section>

      {/* Sync health */}
      <Card
        icon={<Activity className="h-5 w-5" aria-hidden />}
        title="Sync health"
        description="Live link state, direction and conflict policy for this connection."
      >
        <div
          data-testid="pm-health-strip"
          className="grid grid-cols-2 gap-2 sm:grid-cols-5"
        >
          {PM_SYNC_STATES.map((state) => {
            const stateMeta = syncStateMeta(state);
            const count = detail.link_counts?.[state] ?? 0;
            return (
              <div
                key={state}
                data-testid={`pm-health-${state}`}
                className="flex flex-col gap-1 rounded-lg border border-border bg-muted/30 px-3 py-2.5"
              >
                <span className="flex items-center gap-1.5">
                  <span
                    className={cn(
                      "h-1.5 w-1.5 rounded-full",
                      TONE_DOT[stateMeta.tone],
                    )}
                    aria-hidden
                  />
                  <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    {stateMeta.label}
                  </span>
                </span>
                <span className="font-display text-xl font-semibold tabular-nums text-foreground">
                  {count}
                </span>
              </div>
            );
          })}
        </div>

        <p className="text-xs text-muted-foreground">
          {rollup.total === 0
            ? "No tasks linked yet. Links appear as tasks sync with the provider."
            : `${rollup.synced} of ${rollup.total} linked ${rollup.total === 1 ? "task" : "tasks"} in sync${rollup.conflicts > 0 ? ` · ${rollup.conflicts} awaiting resolution` : ""}.`}
        </p>

        {test.isSuccess && test.data ? (
          <HealthResult data={test.data} />
        ) : test.isError ? (
          <p
            role="alert"
            data-testid="pm-health-error"
            className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger"
          >
            The health probe failed. The provider may be unreachable — try again.
          </p>
        ) : null}

        <div className="grid gap-4 border-t border-border pt-4 sm:grid-cols-2">
          <Field label="Sync direction" hint={syncDirectionHint(detail.sync_direction)}>
            <select
              aria-label="Sync direction"
              value={detail.sync_direction}
              disabled={patch.isPending}
              onChange={(e) =>
                onPatch({ sync_direction: e.target.value as PmSyncDirection })
              }
              className={FIELD}
            >
              {PM_SYNC_DIRECTIONS.map((d) => (
                <option key={d} value={d}>
                  {syncDirectionLabel(d)}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="Conflict policy"
            hint={conflictPolicyHint(detail.conflict_policy)}
          >
            <select
              aria-label="Conflict policy"
              value={detail.conflict_policy}
              disabled={patch.isPending}
              onChange={(e) =>
                onPatch({ conflict_policy: e.target.value as PmConflictPolicy })
              }
              className={FIELD}
            >
              {PM_CONFLICT_POLICIES.map((p) => (
                <option key={p} value={p}>
                  {conflictPolicyLabel(p)}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </Card>

      {/* Status map editor */}
      <Card
        icon={<SlidersHorizontal className="h-5 w-5" aria-hidden />}
        title="Status mapping"
        description="Map the provider's workflow states onto Forge's board categories."
      >
        <div data-testid="pm-statusmap" className="flex flex-col gap-2">
          {rows.length === 0 ? (
            <p
              data-testid="pm-statusmap-empty"
              className="rounded-md border border-dashed border-border bg-muted/40 px-3 py-4 text-sm text-muted-foreground"
            >
              No status mappings yet. Add a row to map a provider state (e.g.{" "}
              <span className="font-mono text-xs">In Review</span>) onto a Forge
              category.
            </p>
          ) : (
            <ul className="flex flex-col gap-2">
              {rows.map((row, index) => (
                <li
                  key={index}
                  data-testid="pm-map-row"
                  className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_auto] items-center gap-2"
                >
                  <input
                    aria-label={`Provider status ${index + 1}`}
                    value={row.external}
                    onChange={(e) =>
                      setRows((prev) =>
                        prev.map((r, i) =>
                          i === index ? { ...r, external: e.target.value } : r,
                        ),
                      )
                    }
                    placeholder="Provider status"
                    className={cn(FIELD, "font-mono text-xs")}
                  />
                  <ArrowRight
                    className="h-4 w-4 shrink-0 text-muted-foreground"
                    aria-hidden
                  />
                  <select
                    aria-label={`Forge category ${index + 1}`}
                    value={row.category}
                    onChange={(e) =>
                      setRows((prev) =>
                        prev.map((r, i) =>
                          i === index ? { ...r, category: e.target.value } : r,
                        ),
                      )
                    }
                    className={FIELD}
                  >
                    {PM_STATUS_CATEGORIES.map((c) => (
                      <option key={c} value={c}>
                        {statusCategoryLabel(c)}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    aria-label={`Remove mapping ${index + 1}`}
                    onClick={() =>
                      setRows((prev) => prev.filter((_, i) => i !== index))
                    }
                    className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border text-muted-foreground transition-colors hover:border-danger/40 hover:text-danger focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <Trash2 className="h-4 w-4" aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
            <button
              type="button"
              data-testid="pm-map-add"
              onClick={() =>
                setRows((prev) => [...prev, { external: "", category: "unstarted" }])
              }
              className="inline-flex h-9 items-center gap-2 rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Plus className="h-4 w-4" aria-hidden />
              Add mapping
            </button>
            <div className="flex items-center gap-3">
              <span
                data-testid="pm-map-dirty"
                role="status"
                aria-live="polite"
                className="text-xs text-muted-foreground"
              >
                {patch.isPending
                  ? "Saving…"
                  : mapDirty
                    ? "Unsaved changes"
                    : "All changes saved"}
              </span>
              <button
                type="button"
                data-testid="pm-map-save"
                onClick={saveMap}
                disabled={!mapDirty || patch.isPending}
                className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
              >
                <Save className="h-4 w-4" aria-hidden />
                Save mapping
              </button>
            </div>
          </div>
          {mapError ? (
            <p role="alert" className="text-sm text-danger">
              {mapError}
            </p>
          ) : null}
        </div>
      </Card>

      {/* Conflict inbox */}
      <ConflictInbox
        connectionId={connectionId}
        conflictPolicy={detail.conflict_policy}
        client={client}
      />
    </div>
  );
}

function HealthResult({ data }: { data: PmHealthResult }) {
  const ok = data.status === "connected";
  return (
    <div
      data-testid="pm-health-result"
      className={cn(
        "flex flex-col gap-1 rounded-md border px-3 py-2 text-xs",
        ok
          ? "border-success/40 bg-success/10 text-foreground"
          : "border-danger/40 bg-danger/10 text-foreground",
      )}
    >
      <span className="flex items-center gap-2 font-medium">
        {ok ? (
          <Check className="h-3.5 w-3.5 text-success" aria-hidden />
        ) : (
          <ShieldAlert className="h-3.5 w-3.5 text-danger" aria-hidden />
        )}
        {ok
          ? `Connected in ${Math.round(data.latency_ms)}ms`
          : "Probe failed"}
        {data.account ? (
          <span className="text-muted-foreground">· {data.account}</span>
        ) : null}
      </span>
      {ok && data.granted_scopes.length > 0 ? (
        <span className="font-mono text-[11px] text-muted-foreground">
          {data.granted_scopes.join(" · ")}
        </span>
      ) : null}
      {!ok && data.error ? (
        <span className="text-muted-foreground">{data.error}</span>
      ) : null}
    </div>
  );
}

// --- Conflict inbox -------------------------------------------------------- //

function ConflictInbox({
  connectionId,
  conflictPolicy,
  client,
}: {
  connectionId: string;
  conflictPolicy: PmConflictPolicy;
  client: ForgeApiClient;
}) {
  const conflictsQuery = usePmLinks(connectionId, "conflict", client);
  const conflicts = conflictsQuery.data ?? [];

  return (
    <Card
      icon={<GitMerge className="h-5 w-5" aria-hidden />}
      title="Conflict inbox"
      description={`Links that changed on both sides. Policy: ${conflictPolicyLabel(conflictPolicy)}.`}
    >
      {conflictsQuery.isLoading ? (
        <div
          data-testid="pm-conflicts-loading"
          aria-busy="true"
          className="flex flex-col gap-2"
        >
          <div className="h-14 animate-pulse rounded-lg border border-border bg-muted/40" />
          <div className="h-14 animate-pulse rounded-lg border border-border bg-muted/40" />
        </div>
      ) : conflictsQuery.isError ? (
        <div
          data-testid="pm-conflicts-error"
          role="status"
          className="flex items-center justify-between gap-3 rounded-md border border-dashed border-border px-3 py-4 text-sm text-muted-foreground"
        >
          <span>Couldn&apos;t load conflicts.</span>
          <button
            type="button"
            onClick={() => void conflictsQuery.refetch()}
            className="inline-flex h-8 items-center rounded-md border border-border px-3 text-xs font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Retry
          </button>
        </div>
      ) : conflicts.length === 0 ? (
        <div
          data-testid="pm-conflicts-empty"
          className="flex flex-col items-center gap-2 rounded-lg border border-dashed border-border px-6 py-10 text-center"
        >
          <Inbox className="h-7 w-7 text-muted-foreground" aria-hidden />
          <p className="text-sm font-medium text-foreground">Inbox zero</p>
          <p className="max-w-sm text-xs text-muted-foreground">
            No conflicts to resolve. Both-sides edits that clash under the{" "}
            <span className="font-medium">manual</span> policy land here for a
            human call.
          </p>
        </div>
      ) : (
        <ul data-testid="pm-conflict-list" className="flex flex-col gap-2">
          {conflicts.map((link) => {
            const fields = Object.keys(link.conflict_detail ?? {});
            return (
              <li
                key={link.id}
                data-testid="pm-conflict-row"
                className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-warning/30 bg-warning/5 px-3 py-2.5"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-medium text-foreground">
                      {link.external_key}
                    </span>
                    <span className="rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-warning">
                      Conflict
                    </span>
                  </div>
                  <p className="mt-0.5 truncate text-xs text-muted-foreground">
                    Forge task{" "}
                    <span className="font-mono">
                      {link.forge_task_id.slice(0, 8)}
                    </span>
                    <span aria-hidden> · </span>
                    detected {relativeTime(link.last_synced_at)}
                    {fields.length > 0 ? (
                      <>
                        <span aria-hidden> · </span>
                        {fields.length} field{fields.length === 1 ? "" : "s"}:{" "}
                        <span className="font-mono">{fields.join(", ")}</span>
                      </>
                    ) : null}
                  </p>
                </div>
                <a
                  href={link.external_url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md border border-border px-3 text-xs font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  Open in {providerLabel(link.provider)}
                  <ArrowRight className="h-3.5 w-3.5" aria-hidden />
                </a>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

// --- Connect form ---------------------------------------------------------- //

interface ConnectForm {
  provider: PmProvider;
  name: string;
  externalProjectKey: string;
  externalBaseUrl: string;
  projectId: string;
  authType: "oauth" | "api_token";
  apiToken: string;
  apiTokenEmail: string;
  syncDirection: PmSyncDirection;
  conflictPolicy: PmConflictPolicy;
}

function emptyForm(): ConnectForm {
  return {
    provider: "jira",
    name: "",
    externalProjectKey: "",
    externalBaseUrl: "",
    projectId: "",
    authType: "api_token",
    apiToken: "",
    apiTokenEmail: "",
    syncDirection: "bidirectional",
    conflictPolicy: "newest_wins",
  };
}

function ConnectionForm({
  client,
  onCancel,
  onConnected,
}: {
  client: ForgeApiClient;
  onCancel?: () => void;
  onConnected: (conn: PmConnection) => void;
}) {
  const create = useCreatePmConnection(client);
  const [form, setForm] = useState<ConnectForm>(emptyForm);
  const [error, setError] = useState<string | null>(null);

  const patch = useCallback(
    <K extends keyof ConnectForm>(key: K, value: ConnectForm[K]) =>
      setForm((prev) => ({ ...prev, [key]: value })),
    [],
  );

  const isJira = form.provider === "jira";
  const usesToken = form.authType === "api_token";

  const canConnect =
    form.name.trim().length > 0 &&
    form.externalProjectKey.trim().length > 0 &&
    form.projectId.trim().length > 0 &&
    (!isJira || form.externalBaseUrl.trim().length > 0) &&
    (!usesToken || form.apiToken.trim().length > 0) &&
    (!usesToken || !isJira || form.apiTokenEmail.trim().length > 0) &&
    !create.isPending;

  const submit = useCallback(() => {
    if (!canConnect) return;
    setError(null);
    const body: PmConnectionConfigInput = {
      provider: form.provider,
      name: form.name.trim(),
      project_id: form.projectId.trim(),
      external_project_key: form.externalProjectKey.trim(),
      external_base_url: form.externalBaseUrl.trim() || null,
      auth_type: form.authType,
      api_token: usesToken ? form.apiToken.trim() : null,
      api_token_email:
        usesToken && isJira ? form.apiTokenEmail.trim() : null,
      sync_direction: form.syncDirection,
      conflict_policy: form.conflictPolicy,
    };
    create.mutate(body, {
      onSuccess: (conn) => onConnected(conn),
      onError: (err) => setError(mutationMessage(err)),
    });
  }, [canConnect, form, usesToken, isJira, create, onConnected]);

  return (
    <Card
      icon={<PlugZap className="h-5 w-5" aria-hidden />}
      title="Connect a PM tool"
      description="Link a Jira or Linear project so its issues stay in sync with a Forge board."
    >
      <form
        data-testid="pm-form"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          submit();
        }}
        className="flex flex-col gap-4"
      >
        <Field label="Provider">
          <SegmentedProvider
            value={form.provider}
            onChange={(p) => patch("provider", p)}
          />
        </Field>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Connection name" required>
            <input
              aria-label="Connection name"
              value={form.name}
              onChange={(e) => patch("name", e.target.value)}
              placeholder="Platform · Jira"
              className={FIELD}
            />
          </Field>
          <Field label="External project key" required hint="e.g. ENG">
            <input
              aria-label="External project key"
              value={form.externalProjectKey}
              onChange={(e) => patch("externalProjectKey", e.target.value)}
              placeholder="ENG"
              className={cn(FIELD, "font-mono text-xs")}
            />
          </Field>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="Forge project"
            required
            hint="the board project to sync"
          >
            <input
              aria-label="Forge project"
              value={form.projectId}
              onChange={(e) => patch("projectId", e.target.value)}
              placeholder="project id"
              className={cn(FIELD, "font-mono text-xs")}
            />
          </Field>
          {isJira ? (
            <Field label="Jira site URL" required>
              <input
                aria-label="Jira site URL"
                value={form.externalBaseUrl}
                onChange={(e) => patch("externalBaseUrl", e.target.value)}
                placeholder="https://acme.atlassian.net"
                className={cn(FIELD, "font-mono text-xs")}
              />
            </Field>
          ) : (
            <div className="hidden sm:block" aria-hidden />
          )}
        </div>

        <Field label="Authentication">
          <SegmentedAuth
            value={form.authType}
            onChange={(a) => patch("authType", a)}
          />
        </Field>

        {usesToken ? (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="API token" required>
              <input
                aria-label="API token"
                type="password"
                value={form.apiToken}
                onChange={(e) => patch("apiToken", e.target.value)}
                placeholder="•••••••••••••"
                className={cn(FIELD, "font-mono text-xs")}
              />
            </Field>
            {isJira ? (
              <Field label="Account email" required hint="for Jira basic auth">
                <input
                  aria-label="Account email"
                  type="email"
                  value={form.apiTokenEmail}
                  onChange={(e) => patch("apiTokenEmail", e.target.value)}
                  placeholder="you@acme.com"
                  className={FIELD}
                />
              </Field>
            ) : null}
          </div>
        ) : (
          <p
            data-testid="pm-oauth-note"
            className="rounded-md border border-dashed border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
          >
            OAuth saves a pending connection; you&apos;ll finish authorization
            with the provider before the first sync runs.
          </p>
        )}

        <div className="grid gap-4 border-t border-border pt-4 sm:grid-cols-2">
          <Field label="Sync direction">
            <select
              aria-label="Sync direction"
              value={form.syncDirection}
              onChange={(e) =>
                patch("syncDirection", e.target.value as PmSyncDirection)
              }
              className={FIELD}
            >
              {PM_SYNC_DIRECTIONS.map((d) => (
                <option key={d} value={d}>
                  {syncDirectionLabel(d)}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Conflict policy">
            <select
              aria-label="Conflict policy"
              value={form.conflictPolicy}
              onChange={(e) =>
                patch("conflictPolicy", e.target.value as PmConflictPolicy)
              }
              className={FIELD}
            >
              {PM_CONFLICT_POLICIES.map((p) => (
                <option key={p} value={p}>
                  {conflictPolicyLabel(p)}
                </option>
              ))}
            </select>
          </Field>
        </div>

        {error ? (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        ) : null}

        <div className="flex items-center justify-end gap-3">
          {onCancel ? (
            <button
              type="button"
              data-testid="pm-form-cancel"
              onClick={onCancel}
              className="inline-flex h-9 items-center rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Cancel
            </button>
          ) : null}
          <button
            type="submit"
            data-testid="pm-connect"
            disabled={!canConnect}
            className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
          >
            {create.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <Plug className="h-4 w-4" aria-hidden />
            )}
            Connect
          </button>
        </div>
      </form>
    </Card>
  );
}

function SegmentedProvider({
  value,
  onChange,
}: {
  value: PmProvider;
  onChange: (p: PmProvider) => void;
}) {
  return (
    <div
      role="radiogroup"
      aria-label="Provider"
      className="inline-flex items-center gap-1 self-start rounded-md border border-border bg-muted/40 p-0.5"
    >
      {PM_PROVIDERS.map((p) => {
        const active = p === value;
        return (
          <button
            key={p}
            type="button"
            role="radio"
            aria-checked={active}
            data-testid={`pm-provider-${p}`}
            onClick={() => onChange(p)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded px-3 py-1 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              active
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {providerLabel(p)}
          </button>
        );
      })}
    </div>
  );
}

function SegmentedAuth({
  value,
  onChange,
}: {
  value: "oauth" | "api_token";
  onChange: (a: "oauth" | "api_token") => void;
}) {
  const options: { value: "oauth" | "api_token"; label: string }[] = [
    { value: "api_token", label: "API token" },
    { value: "oauth", label: "OAuth" },
  ];
  return (
    <div
      role="radiogroup"
      aria-label="Authentication"
      className="inline-flex items-center gap-1 self-start rounded-md border border-border bg-muted/40 p-0.5"
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={active}
            data-testid={`pm-auth-${o.value}`}
            onClick={() => onChange(o.value)}
            className={cn(
              "inline-flex items-center rounded px-3 py-1 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              active
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// --- Shared primitives ----------------------------------------------------- //

function Toggle({
  checked,
  disabled,
  onChange,
  label,
  testid,
}: {
  checked: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
  label: string;
  testid?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      data-testid={testid}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "inline-flex h-9 items-center gap-2 rounded-md border px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50",
        checked
          ? "border-success/40 bg-success/10 text-success"
          : "border-border text-muted-foreground hover:bg-accent hover:text-accent-foreground",
      )}
    >
      <span
        className={cn(
          "h-2 w-2 rounded-full",
          checked ? "bg-success" : "bg-muted-foreground",
        )}
        aria-hidden
      />
      {label}
    </button>
  );
}

function StatusBadge({
  tone,
  label,
  testid,
}: {
  tone: Tone;
  label: string;
  testid?: string;
}) {
  return (
    <span
      data-testid={testid}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
        TONE_BADGE[tone],
      )}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", TONE_DOT[tone])} aria-hidden />
      {label}
    </span>
  );
}

function Meta({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: Tone;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd
        className={cn(
          "text-sm text-foreground",
          tone === "danger" && "text-danger",
          tone === "muted" && "text-muted-foreground",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

function Card({
  icon,
  title,
  description,
  children,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
          {icon}
        </span>
        <div>
          <h2 className="font-display text-base font-semibold tracking-tight">
            {title}
          </h2>
          <p className="text-sm text-muted-foreground">{description}</p>
        </div>
      </div>
      {children}
    </section>
  );
}

function Field({
  label,
  required,
  hint,
  children,
}: {
  label: string;
  required?: boolean;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="flex items-baseline justify-between gap-2">
        <span className="text-sm font-medium text-foreground">
          {label}
          {required ? (
            <span className="ml-0.5 text-danger" aria-hidden>
              *
            </span>
          ) : null}
        </span>
        {hint ? (
          <span className="text-[11px] text-muted-foreground">{hint}</span>
        ) : null}
      </span>
      {children}
    </label>
  );
}

// --- Top-level + inline states --------------------------------------------- //

function EmptyState({ onConnect }: { onConnect: () => void }) {
  return (
    <div
      data-testid="pm-empty"
      className="flex flex-col items-center justify-center gap-4 rounded-xl border border-dashed border-border px-6 py-20 text-center"
    >
      <span className="flex h-12 w-12 items-center justify-center rounded-xl border border-border bg-muted/60 text-primary">
        <Cable className="h-6 w-6" aria-hidden />
      </span>
      <div className="flex flex-col gap-1">
        <p className="font-display text-lg font-semibold tracking-tight">
          No PM tools connected
        </p>
        <p className="max-w-md text-sm text-muted-foreground">
          Connect Jira or Linear to keep issues in lockstep with your Forge
          board — two-way sync, status mapping and conflict resolution.
        </p>
      </div>
      <button
        type="button"
        data-testid="pm-empty-connect"
        onClick={onConnect}
        className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Plus className="h-4 w-4" aria-hidden />
        Connect a PM tool
      </button>
    </div>
  );
}

function ScreenSkeleton() {
  return (
    <div
      data-testid="pm-skeleton"
      aria-busy="true"
      className="mx-auto flex w-full max-w-6xl flex-col gap-6"
    >
      <div className="h-12 w-72 animate-pulse rounded-lg bg-muted" />
      <div className="grid gap-6 lg:grid-cols-[18rem_minmax(0,1fr)]">
        <div className="flex flex-col gap-2">
          <div className="h-14 animate-pulse rounded-lg border border-border bg-card" />
          <div className="h-14 animate-pulse rounded-lg border border-border bg-card" />
          <div className="h-14 animate-pulse rounded-lg border border-border bg-card" />
        </div>
        <div className="flex flex-col gap-6">
          <div className="h-40 animate-pulse rounded-xl border border-border bg-card" />
          <div className="h-56 animate-pulse rounded-xl border border-border bg-card" />
        </div>
      </div>
    </div>
  );
}

function ScreenError({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      data-testid="pm-error"
      role="status"
      className="mx-auto flex w-full max-w-6xl flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-border px-6 py-16 text-center"
    >
      <ShieldAlert className="h-8 w-8 text-muted-foreground" aria-hidden />
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-foreground">
          PM integrations unavailable
        </p>
        <p className="max-w-sm text-xs text-muted-foreground">
          The integrations service is unreachable. Your connections are safe —
          try again in a moment.
        </p>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="inline-flex h-9 items-center rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Retry
      </button>
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div
      data-testid="pm-detail-skeleton"
      aria-busy="true"
      className="flex flex-col gap-6"
    >
      <div className="h-40 animate-pulse rounded-xl border border-border bg-card" />
      <div className="h-56 animate-pulse rounded-xl border border-border bg-card" />
    </div>
  );
}

function DetailError({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      data-testid="pm-detail-error"
      role="status"
      className="flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-border px-6 py-16 text-center"
    >
      <ShieldAlert className="h-7 w-7 text-muted-foreground" aria-hidden />
      <p className="text-sm font-medium text-foreground">
        Couldn&apos;t load this connection
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="inline-flex h-9 items-center rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Retry
      </button>
    </div>
  );
}
