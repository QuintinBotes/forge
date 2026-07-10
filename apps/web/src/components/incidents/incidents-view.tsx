"use client";

import { AlertTriangle, Siren } from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import { useRegisterCommands } from "@/components/command-palette";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Loading, Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toast";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useIncidentDetail,
  useIncidentTimeline,
  useIncidents,
  usePostmortem,
  usePublishPostmortem,
  useRemediationPlan,
  useSendIncidentEvent,
} from "@/lib/api/incidents";
import type { IncidentSeverity } from "@/lib/api/types";
import { INCIDENT_SEVERITIES } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { DeclareIncidentDialog } from "./declare-incident-dialog";
import { IncidentDetail } from "./incident-detail";
import { IncidentQueue } from "./incident-queue";
import { eventMeta, severityMeta } from "./incident-meta";

const OPEN_TERMINAL = new Set([
  "resolved",
  "postmortem_created",
  "closed",
  "cancelled",
  "failed",
]);

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable
  );
}

function eventErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return "You don't have permission to drive this incident.";
    if (error.status === 409) {
      const detail =
        error.body && typeof error.body === "object"
          ? (error.body as { detail?: unknown }).detail
          : undefined;
      if (detail && typeof detail === "object" && "error" in detail) {
        return "Remediation exceeds the blast-radius policy — scope it down first.";
      }
      return "That transition isn't valid from the current state.";
    }
  }
  return "Couldn't apply that action. Please try again.";
}

export interface IncidentsViewProps {
  client?: ForgeApiClient;
}

/**
 * The incident command center — a severity-ranked queue beside the selected
 * incident's lifecycle detail: badges, FSM action bar, timeline, remediation
 * plan and postmortem. Keyboard-first: `j/k` move the queue, `c` declares.
 * Declaring is the single ember primary action.
 */
export function IncidentsView({ client = apiClient }: IncidentsViewProps) {
  const [severityFilter, setSeverityFilter] = useState<IncidentSeverity | "all">(
    "all",
  );
  const [openOnly, setOpenOnly] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [declareOpen, setDeclareOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const filters = useMemo(
    () => ({ severity: severityFilter === "all" ? undefined : severityFilter }),
    [severityFilter],
  );
  const incidentsQuery = useIncidents(filters, client);

  // Sort by severity (most urgent first) then most-recent, and optionally hide
  // incidents that have reached a resolved/terminal state.
  const items = useMemo(() => {
    const all = incidentsQuery.data ?? [];
    const visible = openOnly
      ? all.filter((i) => !OPEN_TERMINAL.has(i.lifecycle_state))
      : all;
    return [...visible].sort((a, b) => {
      const w = severityMeta(b.severity).weight - severityMeta(a.severity).weight;
      if (w !== 0) return w;
      return (b.created_at ?? "").localeCompare(a.created_at ?? "");
    });
  }, [incidentsQuery.data, openOnly]);

  // Effective selection: the reviewer's explicit pick, falling back to the top
  // (most urgent) incident when unset or filtered out of the queue.
  const explicitIndex = selectedId
    ? items.findIndex((i) => i.id === selectedId)
    : -1;
  const effectiveIndex =
    explicitIndex >= 0 ? explicitIndex : items.length > 0 ? 0 : -1;
  const selected = effectiveIndex >= 0 ? items[effectiveIndex] : null;
  const selectedIncidentId = selected?.id ?? null;

  const detailQuery = useIncidentDetail(selectedIncidentId, client);
  const timelineQuery = useIncidentTimeline(selectedIncidentId, client);
  const remediationQuery = useRemediationPlan(selectedIncidentId, client);
  const postmortemQuery = usePostmortem(selectedIncidentId, client);
  const sendEvent = useSendIncidentEvent(client);
  const publish = usePublishPostmortem(client);

  const selectIncident = useCallback((id: string) => {
    setSelectedId(id);
    setActionError(null);
  }, []);

  const moveSelection = useCallback(
    (delta: number) => {
      if (items.length === 0) return;
      const base = effectiveIndex < 0 ? 0 : effectiveIndex;
      const next = Math.min(Math.max(base + delta, 0), items.length - 1);
      selectIncident(items[next].id);
    },
    [items, effectiveIndex, selectIncident],
  );

  const onSendEvent = useCallback(
    (event: string) => {
      if (!selectedIncidentId || sendEvent.isPending) return;
      setActionError(null);
      sendEvent.mutate(
        { incidentId: selectedIncidentId, body: { event } },
        {
          onSuccess: () => toast.success(eventMeta(event).label),
          onError: (err) => setActionError(eventErrorMessage(err)),
        },
      );
    },
    [selectedIncidentId, sendEvent],
  );

  const onPublish = useCallback(() => {
    if (!selectedIncidentId || publish.isPending) return;
    publish.mutate(selectedIncidentId, {
      onSuccess: () => toast.success("Postmortem published"),
    });
  }, [selectedIncidentId, publish]);

  const openDeclare = useCallback(() => setDeclareOpen(true), []);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (isEditableTarget(event.target) || declareOpen) return;
      switch (event.key) {
        case "j":
        case "ArrowDown":
          event.preventDefault();
          moveSelection(1);
          return;
        case "k":
        case "ArrowUp":
          event.preventDefault();
          moveSelection(-1);
          return;
        case "c":
          event.preventDefault();
          openDeclare();
          return;
        default:
          return;
      }
    },
    [declareOpen, moveSelection, openDeclare],
  );

  // Command-palette contribution (stable ref → latest handler via a ref).
  const declareRef = useRef(openDeclare);
  useEffect(() => {
    declareRef.current = openDeclare;
  }, [openDeclare]);
  const commands = useMemo(
    () => [
      {
        id: "declare-incident",
        label: "Declare incident",
        group: "Incidents",
        icon: <Siren />,
        shortcut: "C",
        run: () => declareRef.current(),
      },
    ],
    [],
  );
  useRegisterCommands("incidents", commands);

  const openCount = useMemo(
    () =>
      (incidentsQuery.data ?? []).filter(
        (i) => !OPEN_TERMINAL.has(i.lifecycle_state),
      ).length,
    [incidentsQuery.data],
  );

  const defaultProjectId =
    selected?.project_id ?? incidentsQuery.data?.[0]?.project_id ?? "";

  const postmortem = postmortemQuery.data ?? null;
  const canPublish = Boolean(postmortem && postmortem.status !== "published");

  const isEmpty = !incidentsQuery.isLoading && items.length === 0;

  return (
    <div
      data-testid="incidents-view"
      role="application"
      aria-label="Incidents"
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="flex h-full flex-col gap-4 outline-none"
    >
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <h1 className="font-display text-xl font-semibold tracking-tight">
            Incidents
          </h1>
          <span
            data-testid="open-count"
            className="rounded-full border border-border bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground"
          >
            {openCount} open
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            aria-pressed={openOnly}
            onClick={() => setOpenOnly((v) => !v)}
            className={cn(
              "inline-flex h-9 items-center rounded-md border px-3 text-sm font-medium transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              openOnly
                ? "border-primary/40 bg-accent text-accent-foreground"
                : "border-border text-muted-foreground hover:text-foreground",
            )}
          >
            Open only
          </button>
          <button
            type="button"
            onClick={openDeclare}
            className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Siren className="h-4 w-4" />
            Declare incident
          </button>
        </div>
      </header>

      {/* Severity filter chips */}
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by severity">
        <FilterChip
          active={severityFilter === "all"}
          onClick={() => setSeverityFilter("all")}
        >
          All severities
        </FilterChip>
        {INCIDENT_SEVERITIES.map((s) => (
          <FilterChip
            key={s}
            active={severityFilter === s}
            onClick={() => setSeverityFilter(s)}
          >
            {severityMeta(s).label}
          </FilterChip>
        ))}
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(18rem,22rem)_1fr]">
        {/* Queue */}
        <div className="flex min-h-0 flex-col overflow-y-auto rounded-lg border border-border bg-card/40 p-2">
          {incidentsQuery.isLoading ? (
            <QueueSkeleton />
          ) : isEmpty ? (
            <EmptyQueue openOnly={openOnly} onDeclare={openDeclare} />
          ) : (
            <IncidentQueue
              items={items}
              selectedId={selectedIncidentId}
              onSelect={(i) => selectIncident(i.id)}
            />
          )}
          {incidentsQuery.isError ? (
            <ErrorState
              data-testid="incidents-error"
              title="Live incidents are unavailable"
              description="The incident service may be offline — showing an empty queue."
              onRetry={() => incidentsQuery.refetch()}
              className="mt-2 border-none bg-transparent p-3 text-left"
            />
          ) : null}
        </div>

        {/* Detail */}
        <div className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-card">
          {selected ? (
            <IncidentDetail
              detail={detailQuery.data}
              isLoading={detailQuery.isLoading}
              isError={detailQuery.isError}
              onRetry={() => detailQuery.refetch()}
              timeline={{
                data: timelineQuery.data,
                isLoading: timelineQuery.isLoading,
                isError: timelineQuery.isError,
                onRetry: () => timelineQuery.refetch(),
              }}
              remediation={{
                data: remediationQuery.data,
                isLoading: remediationQuery.isLoading,
                isError: remediationQuery.isError,
                onRetry: () => remediationQuery.refetch(),
              }}
              postmortem={{
                data: postmortem,
                isLoading: postmortemQuery.isLoading,
                isError: postmortemQuery.isError,
                onRetry: () => postmortemQuery.refetch(),
                canPublish,
                onPublish,
                publishing: publish.isPending,
              }}
              actions={{
                allowedEvents: detailQuery.data?.allowed_events ?? [],
                onEvent: onSendEvent,
                pending: sendEvent.isPending,
                error: actionError,
              }}
            />
          ) : (
            <NoSelection empty={isEmpty} />
          )}
        </div>
      </div>

      <DeclareIncidentDialog
        open={declareOpen}
        onOpenChange={setDeclareOpen}
        defaultProjectId={defaultProjectId}
        onDeclared={(incident) => selectIncident(incident.id)}
        client={client}
      />
    </div>
  );
}

// --- Filter chip ---------------------------------------------------------- //

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        active
          ? "border-primary/40 bg-accent text-accent-foreground"
          : "border-border text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

// --- Empty / placeholder states ------------------------------------------- //

function EmptyQueue({
  openOnly,
  onDeclare,
}: {
  openOnly: boolean;
  onDeclare: () => void;
}) {
  return (
    <EmptyState
      data-testid="empty-queue"
      icon={<Siren />}
      title={openOnly ? "No open incidents" : "No incidents yet"}
      description={
        openOnly
          ? "Everything's resolved. Declare one if something's on fire."
          : "Declare an incident to start a response timeline."
      }
      action={
        <Button size="sm" variant="outline" onClick={onDeclare}>
          <Siren className="h-3.5 w-3.5" aria-hidden />
          Declare incident
        </Button>
      }
      className="flex-1 border-none bg-transparent"
    />
  );
}

function NoSelection({ empty }: { empty: boolean }) {
  return (
    <EmptyState
      icon={<AlertTriangle />}
      title={empty ? "No incidents to review" : "Select an incident"}
      description={empty ? undefined : "See its lifecycle, timeline and remediation."}
      className="h-full border-none bg-transparent"
    />
  );
}

function QueueSkeleton() {
  return (
    <Loading data-testid="queue-skeleton" label="Loading incidents…" className="flex flex-col gap-1">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="flex flex-col gap-2 rounded-md px-3 py-2.5">
          <Skeleton className="h-2.5 w-1/3" />
          <Skeleton className="h-3 w-3/4" />
          <Skeleton className="h-4 w-24 rounded-full" />
        </div>
      ))}
    </Loading>
  );
}
