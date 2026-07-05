"use client";

import {
  GitBranch,
  PackageOpen,
  Rocket,
  ServerCog,
  ShieldAlert,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { useRegisterCommands } from "@/components/command-palette";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useCancelDeployment,
  useDecideDeployment,
  useDeploymentDetail,
  useDeploymentPipeline,
  useProjectDeployments,
  useRollbackDeployment,
} from "@/lib/api/deployments";
import type { DeploymentDecision } from "@/lib/api/types";

import { DeploymentList } from "./deployment-list";
import {
  nextEnvironmentName,
  sortDeploymentsForQueue,
} from "./deployment-meta";
import { GatePanel } from "./gate-panel";
import { PipelineStages } from "./pipeline-stages";
import { PromoteDialog, type PromoteTarget } from "./promote-dialog";

/** Placeholder project until project routing lands (mirrors the sprints screen). */
export const DEFAULT_PROJECT_ID = "default";

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

function actionErrorMessage(error: unknown, verb: string): string {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return `You don't have permission to ${verb} this deployment.`;
    if (error.status === 409) {
      const detail =
        error.body && typeof error.body === "object"
          ? (error.body as { detail?: unknown }).detail
          : undefined;
      if (detail && typeof detail === "object" && "blocking_reasons" in detail) {
        const reasons = (detail as { blocking_reasons?: unknown }).blocking_reasons;
        if (Array.isArray(reasons) && reasons.length > 0) {
          return `Gate blocked: ${reasons.join("; ")}`;
        }
      }
      return "That action isn't valid from the current state.";
    }
  }
  return `Couldn't ${verb} the deployment. Please try again.`;
}

export interface DeploymentsViewProps {
  projectId?: string;
  client?: ForgeApiClient;
}

/**
 * Deployment gates (F31) — the promotion control plane for one project. The
 * ranked pipeline (dev → staging → prod) sits above an attention-ordered list of
 * recent deployments and the focused deployment's gate detail: its verdict, the
 * per-check breakdown, history, and the approve / reject / cancel / roll-back
 * controls. Keyboard-first — `j/k` move the list, `p` promotes — with a single
 * ember primary action (Promote) and optimistic cache invalidation on every gate
 * decision so the pipeline's live markers advance the moment a gate clears.
 */
export function DeploymentsView({
  projectId = DEFAULT_PROJECT_ID,
  client = apiClient,
}: DeploymentsViewProps) {
  const [picked, setPicked] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [promoteOpen, setPromoteOpen] = useState(false);
  const [promoteTarget, setPromoteTarget] = useState<PromoteTarget | null>(null);

  const pipelineQuery = useDeploymentPipeline(projectId, client);
  const deploymentsQuery = useProjectDeployments(projectId, { limit: 50 }, client);

  const environments = useMemo(
    () => pipelineQuery.data?.environments ?? [],
    [pipelineQuery.data],
  );
  const deployments = useMemo(
    () => deploymentsQuery.data ?? [],
    [deploymentsQuery.data],
  );
  const ordered = useMemo(
    () => sortDeploymentsForQueue(deployments),
    [deployments],
  );

  const selectedId = picked ?? ordered[0]?.id ?? null;
  const detailQuery = useDeploymentDetail(selectedId, client);

  const decide = useDecideDeployment(client);
  const cancel = useCancelDeployment(client);
  const rollback = useRollbackDeployment(client);

  const select = useCallback((id: string) => {
    setPicked(id);
    setActionError(null);
  }, []);

  const moveSelection = useCallback(
    (delta: number) => {
      if (ordered.length === 0) return;
      const base = Math.max(
        0,
        ordered.findIndex((d) => d.id === selectedId),
      );
      const next = Math.min(Math.max(base + delta, 0), ordered.length - 1);
      select(ordered[next].id);
    },
    [ordered, selectedId, select],
  );

  // --- actions ------------------------------------------------------------ //
  const openPromote = useCallback((target: PromoteTarget | null = null) => {
    setPromoteTarget(target);
    setPromoteOpen(true);
  }, []);

  const onPromoteFrom = useCallback(
    (fromEnvName: string) => {
      const env = environments.find((e) => e.name === fromEnvName);
      const target = nextEnvironmentName(environments, fromEnvName);
      if (!target) return;
      openPromote({
        environment: target,
        commitSha: env?.currently_deployed?.commit_sha ?? "",
      });
    },
    [environments, openPromote],
  );

  const onDecision = useCallback(
    (decision: DeploymentDecision, note?: string) => {
      if (!selectedId || decide.isPending) return;
      setActionError(null);
      decide.mutate(
        { deploymentId: selectedId, body: { decision, note: note ?? null } },
        {
          onError: (err) =>
            setActionError(actionErrorMessage(err, "decide on")),
        },
      );
    },
    [selectedId, decide],
  );

  const onCancel = useCallback(() => {
    if (!selectedId || cancel.isPending) return;
    setActionError(null);
    cancel.mutate(selectedId, {
      onError: (err) => setActionError(actionErrorMessage(err, "cancel")),
    });
  }, [selectedId, cancel]);

  const onRollback = useCallback(() => {
    if (!selectedId || rollback.isPending) return;
    setActionError(null);
    rollback.mutate(selectedId, {
      onError: (err) => setActionError(actionErrorMessage(err, "roll back")),
    });
  }, [selectedId, rollback]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (isEditableTarget(event.target) || promoteOpen) return;
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
        case "p":
          event.preventDefault();
          openPromote(null);
          return;
        default:
          return;
      }
    },
    [promoteOpen, moveSelection, openPromote],
  );

  // Command-palette contribution (stable ref → latest handler).
  const promoteRef = useRef(openPromote);
  useEffect(() => {
    promoteRef.current = openPromote;
  }, [openPromote]);
  const commands = useMemo(
    () => [
      {
        id: "promote-deployment",
        label: "Promote a deployment",
        group: "Deployments",
        icon: <Rocket />,
        shortcut: "P",
        run: () => promoteRef.current(null),
      },
    ],
    [],
  );
  useRegisterCommands("deployments", commands);

  const awaitingCount = useMemo(
    () => deployments.filter((d) => d.state === "awaiting_approval").length,
    [deployments],
  );

  const actionPending = decide.isPending || cancel.isPending || rollback.isPending;
  const pipeline = pipelineQuery.data ?? null;

  // --- top-level states --------------------------------------------------- //
  if (pipelineQuery.isLoading && !pipeline) {
    return <ScreenSkeleton />;
  }
  if (pipelineQuery.isError && !pipeline) {
    return <ScreenError />;
  }
  if (!pipeline) {
    return <NoPipeline />;
  }

  return (
    <div
      data-testid="deployments-view"
      role="application"
      aria-label="Deployment gates"
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="flex h-full flex-col gap-5 outline-none"
    >
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
            <ServerCog className="h-5 w-5" aria-hidden />
          </span>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="font-display text-xl font-semibold tracking-tight">
                Deployment gates
              </h1>
              {awaitingCount > 0 ? (
                <span
                  data-testid="awaiting-count"
                  className="inline-flex items-center rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning"
                >
                  {awaitingCount} awaiting approval
                </span>
              ) : null}
            </div>
            <p className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-sm text-muted-foreground">
              <span className="inline-flex items-center gap-1">
                <GitBranch className="h-3.5 w-3.5" aria-hidden />
                <span className="font-mono text-xs">{pipeline.repo_id}</span>
              </span>
              <span>
                Promote a commit across the pipeline once its gate clears.
              </span>
            </p>
          </div>
        </div>

        <button
          type="button"
          data-testid="promote-button"
          onClick={() => openPromote(null)}
          className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Rocket className="h-4 w-4" aria-hidden />
          Promote
        </button>
      </header>

      <span data-testid="deployments-status" role="status" aria-live="polite" className="sr-only">
        {actionPending ? "Applying deployment action…" : ""}
      </span>

      {/* Pipeline */}
      <section className="flex flex-col gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Promotion pipeline
        </h2>
        <PipelineStages
          environments={environments}
          selectedDeploymentId={selectedId}
          onSelectDeployment={select}
          onPromoteFrom={onPromoteFrom}
        />
      </section>

      {/* List + gate detail */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(17rem,21rem)_1fr]">
        <div className="flex min-h-0 flex-col gap-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Recent deployments
          </h2>
          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto rounded-lg border border-border bg-card/40 p-2">
            {deploymentsQuery.isLoading ? (
              <ListSkeleton />
            ) : ordered.length === 0 ? (
              <EmptyDeployments onPromote={() => openPromote(null)} />
            ) : (
              <DeploymentList
                deployments={ordered}
                selectedId={selectedId}
                onSelect={(d) => select(d.id)}
              />
            )}
            {deploymentsQuery.isError ? (
              <p
                role="status"
                className="mt-2 rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground"
              >
                Live deployments are unavailable — showing an empty list.
              </p>
            ) : null}
          </div>
        </div>

        <div className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-card">
          {selectedId ? (
            <GatePanel
              detail={detailQuery.data}
              isLoading={detailQuery.isLoading}
              isError={detailQuery.isError}
              onRetry={() => detailQuery.refetch()}
              actions={{
                onDecision,
                onCancel,
                onRollback,
                pending: actionPending,
                error: actionError,
              }}
            />
          ) : (
            <NoSelection empty={ordered.length === 0} />
          )}
        </div>
      </div>

      <PromoteDialog
        open={promoteOpen}
        onOpenChange={setPromoteOpen}
        projectId={projectId}
        environments={environments}
        initialTarget={promoteTarget}
        onPromoted={(deployment) => select(deployment.id)}
        client={client}
      />
    </div>
  );
}

// --- Empty / placeholder states ------------------------------------------- //

function NoSelection({ empty }: { empty: boolean }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-10 text-center">
      <ShieldAlert className="h-8 w-8 text-muted-foreground" aria-hidden />
      <p className="text-sm text-muted-foreground">
        {empty
          ? "No deployments to review yet."
          : "Select a deployment to review its gate."}
      </p>
    </div>
  );
}

function EmptyDeployments({ onPromote }: { onPromote: () => void }) {
  return (
    <div
      data-testid="deployments-empty"
      className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center"
    >
      <PackageOpen className="h-8 w-8 text-muted-foreground" aria-hidden />
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-foreground">No deployments yet</p>
        <p className="text-xs text-muted-foreground">
          Promote a commit into the first environment to start the pipeline.
        </p>
      </div>
      <button
        type="button"
        onClick={onPromote}
        className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-3 text-xs font-medium text-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Rocket className="h-3.5 w-3.5" aria-hidden />
        Promote
      </button>
    </div>
  );
}

function NoPipeline() {
  return (
    <div
      data-testid="deployments-no-pipeline"
      className="flex h-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border px-6 py-16 text-center"
    >
      <ServerCog className="h-8 w-8 text-muted-foreground" aria-hidden />
      <p className="text-sm font-medium text-foreground">No deployment pipeline</p>
      <p className="max-w-sm text-xs text-muted-foreground">
        This project has no promotion pipeline yet. Configure its environments
        (dev → staging → prod) to gate and promote deployments here.
      </p>
    </div>
  );
}

function ScreenError() {
  return (
    <div
      data-testid="deployments-error"
      role="status"
      className="flex h-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border px-6 py-16 text-center"
    >
      <ServerCog className="h-8 w-8 text-muted-foreground" aria-hidden />
      <p className="text-sm font-medium text-foreground">Deployments unavailable</p>
      <p className="max-w-sm text-xs text-muted-foreground">
        The deployment service is unreachable — the pipeline and gates will return
        once it is back.
      </p>
    </div>
  );
}

function ScreenSkeleton() {
  return (
    <div data-testid="deployments-skeleton" aria-busy="true" className="flex flex-col gap-5">
      <div className="h-10 w-64 animate-pulse rounded-lg bg-muted" />
      <div className="flex gap-2">
        {Array.from({ length: 3 }, (_, i) => (
          <div key={i} className="h-40 w-64 animate-pulse rounded-lg border border-border bg-card" />
        ))}
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(17rem,21rem)_1fr]">
        <div className="h-64 animate-pulse rounded-lg border border-border bg-card" />
        <div className="h-64 animate-pulse rounded-lg border border-border bg-card" />
      </div>
    </div>
  );
}

function ListSkeleton(): ReactNode {
  return (
    <div className="flex flex-col gap-1" data-testid="list-skeleton" aria-busy="true">
      {Array.from({ length: 5 }, (_, i) => (
        <div key={i} className="flex flex-col gap-2 rounded-md px-3 py-2.5">
          <div className="h-3 w-2/3 animate-pulse rounded bg-muted" />
          <div className="h-4 w-28 animate-pulse rounded-full bg-muted/60" />
        </div>
      ))}
    </div>
  );
}
