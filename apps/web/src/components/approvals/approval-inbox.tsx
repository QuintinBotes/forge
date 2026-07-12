"use client";

import { Inbox, ShieldCheck, User } from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import { useRegisterCommands } from "@/components/command-palette";
import { EmptyState } from "@/components/ui/empty-state";
import { Loading, Skeleton } from "@/components/ui/skeleton";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useApprovalContext,
  useApprovalDecisions,
  useApprovals,
  useDecideApproval,
} from "@/lib/api/approvals";
import type { ApprovalAction, ApprovalSummary } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  ACTION_META,
  actionForKey,
  gateMeta,
  riskBadgeClass,
  riskLabel,
  statusBadgeClass,
  STATUS_LABELS,
} from "./approval-meta";
import { ApprovalList } from "./approval-list";
import { DecisionBar } from "./decision-bar";
import { actorLabel, relativeTime } from "./format";
import { ReviewPanel } from "./review-panel";

const DEFAULT_ACTIONS: ApprovalAction[] = ["approve", "request_changes", "reject"];

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

function decisionErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return "You don't have permission to decide this gate.";
    if (error.status === 409) return "This gate was already resolved.";
  }
  return "Couldn't record the decision. Please try again.";
}

export interface ApprovalInboxProps {
  client?: ForgeApiClient;
}

/**
 * The human-approval inbox: a risk-ranked queue of pending gates beside the
 * nine-item review shell + decision bar. Fully keyboard-driven — `j/k` move the
 * queue selection and `a/x/r/e` approve / reject / request-changes / escalate
 * the focused gate (spec: keyboard-first, no mouse required). Decisions are
 * optimistic (the gate leaves the queue instantly, rolling back on error).
 */
export function ApprovalInbox({ client = apiClient }: ApprovalInboxProps) {
  const [mine, setMine] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [noteAction, setNoteAction] = useState<"reject" | "request_changes" | null>(
    null,
  );
  const [note, setNote] = useState("");
  const [decisionError, setDecisionError] = useState<string | null>(null);

  const filters = useMemo(
    () => ({ status: "pending" as const, mine: mine || undefined }),
    [mine],
  );
  const approvalsQuery = useApprovals(filters, client);
  const items = useMemo(
    () => approvalsQuery.data ?? [],
    [approvalsQuery.data],
  );

  // The *effective* selection is derived during render (no selection-syncing
  // effect): `selectedId` holds the reviewer's explicit pick, and when it is
  // null or has fallen out of the queue (e.g. an optimistic decision removed
  // it, or a filter change) we fall back to the top gate — the highest-risk
  // pending item, since the queue is risk-ranked. So deciding a gate advances
  // focus to the most urgent remaining one.
  const explicitIndex = selectedId
    ? items.findIndex((item) => item.id === selectedId)
    : -1;
  const effectiveIndex =
    explicitIndex >= 0 ? explicitIndex : items.length > 0 ? 0 : -1;
  const selected = effectiveIndex >= 0 ? items[effectiveIndex] : null;
  const effectiveSelectedId = selected?.id ?? null;

  const contextQuery = useApprovalContext(effectiveSelectedId, client);
  const decisionsQuery = useApprovalDecisions(effectiveSelectedId, client);
  const decide = useDecideApproval(client);

  // Clearing the composer + any stale error is tied to *user-initiated*
  // selection changes (below) rather than an effect on `selectedId`: a
  // successful decision already clears it, and a failed one must keep its note
  // and error visible for a retry.
  const resetComposer = useCallback(() => {
    setNoteAction(null);
    setNote("");
    setDecisionError(null);
  }, []);

  const selectGate = useCallback(
    (id: string) => {
      setSelectedId(id);
      resetComposer();
    },
    [resetComposer],
  );

  const availableActions =
    contextQuery.data?.available_actions && contextQuery.data.available_actions.length > 0
      ? contextQuery.data.available_actions
      : DEFAULT_ACTIONS;

  const submitDecision = useCallback(
    (action: ApprovalAction, reason?: string) => {
      if (!selected) return;
      setDecisionError(null);
      decide.mutate(
        { approvalId: selected.id, body: { decision: action, note: reason ?? null } },
        {
          onError: (error) => setDecisionError(decisionErrorMessage(error)),
          onSuccess: () => {
            setNoteAction(null);
            setNote("");
          },
        },
      );
    },
    [decide, selected],
  );

  const triggerAction = useCallback(
    (action: ApprovalAction) => {
      if (!selected || decide.isPending) return;
      if (!availableActions.includes(action)) return;
      if (ACTION_META[action].requiresNote) {
        setDecisionError(null);
        setNote("");
        setNoteAction(action as "reject" | "request_changes");
      } else {
        submitDecision(action);
      }
    },
    [availableActions, decide.isPending, selected, submitDecision],
  );

  const moveSelection = useCallback(
    (delta: number) => {
      if (items.length === 0) return;
      const base = effectiveIndex < 0 ? 0 : effectiveIndex;
      const next = Math.min(Math.max(base + delta, 0), items.length - 1);
      selectGate(items[next].id);
    },
    [items, effectiveIndex, selectGate],
  );

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      // Never hijack typing (the reason composer owns its own keys).
      if (isEditableTarget(event.target) || noteAction) return;
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
        default:
          break;
      }
      const action = actionForKey(event.key);
      if (action) {
        event.preventDefault();
        triggerAction(action);
      }
    },
    [moveSelection, noteAction, triggerAction],
  );

  // Command-palette contributions. `commands` must be a stable reference (per
  // useRegisterCommands), so they read the latest handler through a ref that is
  // refreshed in an effect rather than during render.
  const triggerRef = useRef(triggerAction);
  useEffect(() => {
    triggerRef.current = triggerAction;
  }, [triggerAction]);
  const commands = useMemo(
    () => [
      {
        id: "approve-current",
        label: "Approve current gate",
        group: "Approvals",
        icon: <ShieldCheck />,
        shortcut: "A",
        run: () => triggerRef.current("approve"),
      },
      {
        id: "reject-current",
        label: "Reject current gate",
        group: "Approvals",
        shortcut: "X",
        run: () => triggerRef.current("reject"),
      },
    ],
    [],
  );
  useRegisterCommands("approvals", commands);

  const pendingCount = items.length;
  const isEmpty = !approvalsQuery.isLoading && pendingCount === 0;

  return (
    <div
      data-testid="approval-inbox"
      role="application"
      aria-label="Approval inbox"
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="flex h-full flex-col gap-4 outline-none"
    >
      <header className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <h1 className="font-display text-xl font-semibold tracking-tight">
            Approvals
          </h1>
          <span
            data-testid="pending-count"
            className="rounded-full border border-border bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground"
          >
            {pendingCount} pending
          </span>
        </div>
        <button
          type="button"
          aria-pressed={mine}
          onClick={() => setMine((v) => !v)}
          className={cn(
            "inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm font-medium transition-colors",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            mine
              ? "border-primary/40 bg-accent text-accent-foreground"
              : "border-border text-muted-foreground hover:text-foreground",
          )}
        >
          <User className="h-4 w-4" />
          Assigned to me
        </button>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(18rem,22rem)_1fr]">
        {/* Queue */}
        <div className="flex min-h-0 flex-col overflow-y-auto rounded-lg border border-border bg-card/40 p-2">
          {approvalsQuery.isLoading ? (
            <QueueSkeleton />
          ) : isEmpty ? (
            <EmptyQueue mine={mine} />
          ) : (
            <ApprovalList
              items={items}
              selectedId={effectiveSelectedId}
              onSelect={(item) => selectGate(item.id)}
            />
          )}
          {approvalsQuery.isError ? (
            <p
              role="status"
              className="mt-2 rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground"
            >
              Live approvals are unavailable — showing an empty queue.
            </p>
          ) : null}
        </div>

        {/* Review shell */}
        <div className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-card">
          {selected ? (
            <>
              <DetailHeader
                selected={selected}
                decisionCount={decisionsQuery.data?.length ?? 0}
              />
              <div className="min-h-0 flex-1 overflow-y-auto">
                <ReviewPanel
                  summary={selected}
                  context={contextQuery.data}
                  isLoading={contextQuery.isLoading}
                  isError={contextQuery.isError}
                  onRetry={() => contextQuery.refetch()}
                  client={client}
                />
              </div>
              <DecisionBar
                actions={availableActions}
                activeNote={noteAction}
                note={note}
                onNoteChange={setNote}
                pending={decide.isPending}
                errorMessage={decisionError}
                onTrigger={triggerAction}
                onConfirm={() =>
                  noteAction && submitDecision(noteAction, note.trim() || undefined)
                }
                onCancel={() => {
                  setNoteAction(null);
                  setNote("");
                }}
              />
            </>
          ) : (
            <NoSelection empty={isEmpty} />
          )}
        </div>
      </div>
    </div>
  );
}

// --- Detail header -------------------------------------------------------- //

function DetailHeader({
  selected,
  decisionCount,
}: {
  selected: ApprovalSummary;
  decisionCount: number;
}) {
  const meta = gateMeta(selected.gate_type);
  const Icon = meta.icon;
  const risk = selected.risk_level ?? "info";
  return (
    <div className="flex flex-col gap-2 border-b border-border px-6 py-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted">
            <Icon aria-hidden className="h-4 w-4 text-muted-foreground" />
          </span>
          <div className="min-w-0">
            <h2 className="truncate font-display text-lg font-semibold leading-tight">
              {selected.title || meta.label}
            </h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {meta.label} · requested by {actorLabel(selected.requested_actor)} ·{" "}
              {relativeTime(selected.requested_at)}
            </p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {risk !== "info" ? (
            <span
              className={cn(
                "rounded-full border px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide",
                riskBadgeClass(risk),
              )}
            >
              {riskLabel(risk)}
            </span>
          ) : null}
          <span
            className={cn(
              "rounded-full border px-2 py-0.5 text-[11px] font-medium",
              statusBadgeClass(selected.status),
            )}
          >
            {STATUS_LABELS[selected.status]}
          </span>
        </div>
      </div>
      {decisionCount > 0 ? (
        <p className="text-xs text-muted-foreground">
          {decisionCount} prior decision{decisionCount === 1 ? "" : "s"} recorded
        </p>
      ) : null}
    </div>
  );
}

// --- Empty / placeholder states ------------------------------------------- //

function EmptyQueue({ mine }: { mine: boolean }) {
  return (
    <EmptyState
      data-testid="empty-queue"
      icon={<ShieldCheck />}
      title={mine ? "Nothing waiting on you" : "The queue is clear"}
      description={
        mine
          ? "No gates are currently assigned to you."
          : "New gates from spec, plan, PR, deploy, incident and policy reviews land here."
      }
      className="flex-1 border-none bg-transparent"
    />
  );
}

function NoSelection({ empty }: { empty: boolean }) {
  return (
    <EmptyState
      icon={<Inbox />}
      title={empty ? "No pending approvals" : "Select an approval to review"}
      description={
        empty
          ? "You're all caught up — new gates will appear in the queue on the left."
          : "See its goal, diff, checks and risks before you decide."
      }
      className="h-full border-none bg-transparent"
    />
  );
}

// --- Skeletons ------------------------------------------------------------ //

function QueueSkeleton() {
  return (
    <Loading label="Loading approvals…" data-testid="queue-skeleton" className="flex flex-col gap-1">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="flex items-start gap-3 rounded-md px-4 py-2.5">
          <Skeleton className="mt-0.5 h-4 w-4 shrink-0 rounded-full" />
          <div className="flex flex-1 flex-col gap-1.5">
            <Skeleton className="h-3 w-3/4" />
            <Skeleton className="h-2.5 w-1/2" />
          </div>
        </div>
      ))}
    </Loading>
  );
}
