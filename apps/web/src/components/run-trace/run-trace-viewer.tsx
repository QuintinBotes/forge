"use client";

import {
  Activity,
  AlertTriangle,
  Clock,
  Coins,
  Copy,
  Hash,
  Layers,
  ListTree,
  Pause,
  Play,
  RotateCcw,
  Search,
  type LucideIcon,
} from "lucide-react";
import { useRouter } from "next/navigation";
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
import { Button } from "@/components/ui/button";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import { useRunTrace } from "@/lib/api/observability";
import type { RunTrace, StepKind } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  KIND_TONE,
  STEP_KIND_META,
  TONE_BADGE_CLASS,
  TONE_NODE_CLASS,
  formatCost,
  formatDuration,
  formatTokens,
  runStatusMeta,
  traceTotals,
} from "./step-meta";
import { TraceStepRow } from "./trace-step";

export interface RunTraceViewerProps {
  /** The run to inspect; when absent the entry form is shown. */
  runId?: string;
  client?: ForgeApiClient;
  /** Navigate to another run (defaults to router push to /runs/{id}). */
  onOpenRun?: (runId: string) => void;
  /** Replay cadence in ms (injectable for deterministic tests). */
  replayIntervalMs?: number;
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable;
}

/**
 * Run-trace viewer — a step-level timeline of one agent run.
 *
 * Reads the redacted `RunTrace` (GET /observability/runs/{id}/trace) and lays
 * its steps out on a vertical spine: kind-coloured nodes, per-step
 * duration / token / cost telemetry, and an expandable detail panel. A replay
 * playhead walks the timeline so you can watch the run unfold. Fully
 * keyboard-driven (j/k to move, o to expand, space to play/pause, r to
 * restart) with the whole surface reachable without a mouse.
 */
export function RunTraceViewer({
  runId,
  client = apiClient,
  onOpenRun,
  replayIntervalMs = 900,
}: RunTraceViewerProps) {
  const router = useRouter();
  const openRun = useCallback(
    (id: string) => {
      const trimmed = id.trim();
      if (!trimmed) return;
      if (onOpenRun) onOpenRun(trimmed);
      else router.push(`/runs/${encodeURIComponent(trimmed)}`);
    },
    [onOpenRun, router],
  );

  const query = useRunTrace(runId, client);
  const trace = query.data;
  const steps = useMemo(() => trace?.steps ?? [], [trace]);
  const totalSteps = steps.length;

  const [activeIndex, setActiveIndex] = useState(0);
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(new Set());
  const [isPlaying, setIsPlaying] = useState(false);

  // Reset transient view state whenever we load a different run.
  useEffect(() => {
    setActiveIndex(0);
    setExpanded(new Set());
    setIsPlaying(false);
  }, [runId, totalSteps]);

  const clampedActive = totalSteps > 0 ? Math.min(activeIndex, totalSteps - 1) : 0;

  // Replay: advance the playhead one step per tick.
  useEffect(() => {
    if (!isPlaying) return;
    const id = setInterval(() => {
      setActiveIndex((i) => (i >= totalSteps - 1 ? i : i + 1));
    }, replayIntervalMs);
    return () => clearInterval(id);
  }, [isPlaying, replayIntervalMs, totalSteps]);

  // Stop when the playhead reaches the final step.
  useEffect(() => {
    if (isPlaying && clampedActive >= totalSteps - 1) {
      setIsPlaying(false);
    }
  }, [isPlaying, clampedActive, totalSteps]);

  const moveActive = useCallback(
    (delta: number) => {
      setIsPlaying(false);
      setActiveIndex((i) => {
        const base = Math.min(i, totalSteps - 1);
        return Math.min(Math.max(base + delta, 0), Math.max(totalSteps - 1, 0));
      });
    },
    [totalSteps],
  );

  const toggleExpand = useCallback((index: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }, []);

  const toggleActiveExpand = useCallback(() => {
    setIsPlaying(false);
    toggleExpand(clampedActive);
  }, [clampedActive, toggleExpand]);

  const selectRow = useCallback((index: number) => {
    setIsPlaying(false);
    setActiveIndex(index);
  }, []);

  const startReplay = useCallback(() => {
    if (totalSteps === 0) return;
    setActiveIndex(0);
    setIsPlaying(true);
  }, [totalSteps]);

  const togglePlay = useCallback(() => {
    if (totalSteps === 0) return;
    setIsPlaying((playing) => {
      if (playing) return false;
      setActiveIndex((i) => (i >= totalSteps - 1 ? 0 : i));
      return true;
    });
  }, [totalSteps]);

  const copyRunId = useCallback(() => {
    if (!runId) return;
    try {
      void navigator.clipboard?.writeText(runId);
    } catch {
      /* clipboard unavailable — non-critical */
    }
  }, [runId]);

  // --- command palette (Cmd+K) ------------------------------------------- //
  const actionsRef = useRef({ startReplay, togglePlay, copyRunId });
  useEffect(() => {
    actionsRef.current = { startReplay, togglePlay, copyRunId };
  }, [startReplay, togglePlay, copyRunId]);
  const commands = useMemo(
    () => [
      {
        id: "run-trace-replay",
        label: "Replay run trace",
        group: "Run trace",
        icon: <Play />,
        shortcut: "R",
        run: () => actionsRef.current.startReplay(),
      },
      {
        id: "run-trace-play-pause",
        label: "Play / pause run trace",
        group: "Run trace",
        run: () => actionsRef.current.togglePlay(),
      },
      {
        id: "run-trace-copy-id",
        label: "Copy run id",
        group: "Run trace",
        icon: <Copy />,
        run: () => actionsRef.current.copyRunId(),
      },
    ],
    [],
  );
  useRegisterCommands("run-trace", commands);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (isEditableTarget(event.target)) return;
      switch (event.key) {
        case "j":
        case "ArrowDown":
          event.preventDefault();
          moveActive(1);
          return;
        case "k":
        case "ArrowUp":
          event.preventDefault();
          moveActive(-1);
          return;
        case "o":
        case "Enter":
          event.preventDefault();
          toggleActiveExpand();
          return;
        case " ":
          event.preventDefault();
          togglePlay();
          return;
        case "r":
          event.preventDefault();
          startReplay();
          return;
        case "Escape":
          setIsPlaying(false);
          return;
        case "Home":
          event.preventDefault();
          selectRow(0);
          return;
        case "End":
          event.preventDefault();
          selectRow(Math.max(totalSteps - 1, 0));
          return;
        default:
          break;
      }
    },
    [moveActive, selectRow, startReplay, togglePlay, toggleActiveExpand, totalSteps],
  );

  // --- states ------------------------------------------------------------ //

  if (!runId) {
    return <EntryScreen onOpenRun={openRun} />;
  }

  if (query.isLoading) {
    return <TraceSkeleton />;
  }

  if (query.isError) {
    const notFound = query.error instanceof ApiError && query.error.status === 404;
    return (
      <ErrorState
        runId={runId}
        notFound={notFound}
        onRetry={() => query.refetch()}
        onOpenRun={openRun}
      />
    );
  }

  if (!trace) {
    return <TraceSkeleton />;
  }

  const totals = traceTotals(steps);
  const status = runStatusMeta(trace.status);

  return (
    <div
      data-testid="run-trace-viewer"
      role="application"
      aria-label="Run trace"
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="flex h-full min-h-0 flex-col gap-5 outline-none"
    >
      <TraceHeader
        runId={runId}
        trace={trace}
        statusLabel={status.label}
        statusTone={status.tone}
        isPlaying={isPlaying}
        onCopyRunId={copyRunId}
        onTogglePlay={togglePlay}
        onRestart={startReplay}
        canReplay={totalSteps > 0}
      />

      <StatRow trace={trace} totals={totals} />

      <KindLegend counts={trace.step_counts} />

      {totalSteps > 0 ? (
        <ReplayControls
          position={clampedActive}
          total={totalSteps}
          isPlaying={isPlaying}
          onScrub={(value) => selectRow(value)}
        />
      ) : null}

      {totalSteps === 0 ? (
        <EmptyTimeline />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto rounded-xl border border-border bg-card/40 p-2">
          <ol className="flex flex-col">
            {steps.map((step, index) => (
              <TraceStepRow
                key={index}
                step={step}
                position={index + 1}
                isActive={index === clampedActive}
                isExpanded={
                  isPlaying ? index === clampedActive : expanded.has(index)
                }
                isPlayhead={isPlaying && index === clampedActive}
                onToggle={() => toggleExpand(index)}
                onFocus={() => selectRow(index)}
              />
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}

// --- header --------------------------------------------------------------- //

interface TraceHeaderProps {
  runId: string;
  trace: RunTrace;
  statusLabel: string;
  statusTone: keyof typeof TONE_BADGE_CLASS;
  isPlaying: boolean;
  canReplay: boolean;
  onCopyRunId: () => void;
  onTogglePlay: () => void;
  onRestart: () => void;
}

function TraceHeader({
  runId,
  trace,
  statusLabel,
  statusTone,
  isPlaying,
  canReplay,
  onCopyRunId,
  onTogglePlay,
  onRestart,
}: TraceHeaderProps) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-4">
      <div className="flex min-w-0 flex-col gap-1.5">
        <div className="flex items-center gap-2">
          <h1 className="font-display text-xl font-semibold tracking-tight">
            Run trace
          </h1>
          <span
            className={cn(
              "rounded-full border px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide",
              TONE_BADGE_CLASS[statusTone],
            )}
          >
            {statusLabel}
          </span>
          {trace.has_subagents ? (
            <span className="inline-flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
              <Layers aria-hidden className="h-3 w-3" />
              multi-agent
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span>run</span>
          <code className="max-w-[22rem] truncate rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-foreground">
            {runId}
          </code>
          <button
            type="button"
            onClick={onCopyRunId}
            aria-label="Copy run id"
            className="rounded p-1 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Copy className="h-3.5 w-3.5" />
          </button>
          {trace.summary ? (
            <span className="truncate">· {trace.summary}</span>
          ) : null}
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onRestart}
          disabled={!canReplay}
          aria-label="Restart replay"
          className="inline-flex h-10 w-10 items-center justify-center rounded-md border border-border text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
        >
          <RotateCcw className="h-4 w-4" />
        </button>
        {/* The single ember action for this view. */}
        <Button
          type="button"
          onClick={onTogglePlay}
          disabled={!canReplay}
          data-testid="replay-toggle"
          data-state={isPlaying ? "playing" : "paused"}
          aria-pressed={isPlaying}
        >
          {isPlaying ? (
            <>
              <Pause className="h-4 w-4" />
              Pause
            </>
          ) : (
            <>
              <Play className="h-4 w-4" />
              Replay
            </>
          )}
        </Button>
      </div>
    </header>
  );
}

// --- stat row ------------------------------------------------------------- //

function StatRow({
  trace,
  totals,
}: {
  trace: RunTrace;
  totals: ReturnType<typeof traceTotals>;
}) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      <StatTile
        icon={ListTree}
        label="Steps"
        value={String(trace.total_steps)}
      />
      <StatTile
        icon={Clock}
        label="Duration"
        value={formatDuration(trace.total_duration_ms)}
      />
      <StatTile
        icon={Hash}
        label="Tokens"
        value={totals.hasTokens ? formatTokens(totals.tokens) : "—"}
      />
      <StatTile
        icon={Coins}
        label="Cost"
        value={totals.hasCost ? formatCost(totals.costUsd) : "—"}
      />
      <StatTile
        icon={Activity}
        label="Confidence"
        value={
          trace.confidence != null
            ? `${Math.round(trace.confidence * 100)}%`
            : "—"
        }
      />
    </div>
  );
}

function StatTile({
  icon: Icon,
  label,
  value,
}: {
  icon: LucideIcon;
  label: string;
  value: string;
}) {
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-border bg-card px-3 py-2.5">
      <span className="inline-flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        <Icon aria-hidden className="h-3.5 w-3.5" />
        {label}
      </span>
      <span className="font-display text-lg font-semibold tabular-nums tracking-tight">
        {value}
      </span>
    </div>
  );
}

// --- replay controls ------------------------------------------------------ //

function ReplayControls({
  position,
  total,
  isPlaying,
  onScrub,
}: {
  position: number;
  total: number;
  isPlaying: boolean;
  onScrub: (value: number) => void;
}) {
  return (
    <div className="flex items-center gap-3">
      <span
        className={cn(
          "inline-flex h-2 w-2 shrink-0 rounded-full",
          isPlaying ? "animate-pulse bg-primary" : "bg-muted-foreground/40",
        )}
        aria-hidden
      />
      <label className="sr-only" htmlFor="replay-scrubber">
        Timeline position
      </label>
      <input
        id="replay-scrubber"
        type="range"
        min={0}
        max={Math.max(total - 1, 0)}
        value={position}
        onChange={(e) => onScrub(Number(e.target.value))}
        aria-valuetext={`Step ${position + 1} of ${total}`}
        className="h-1 flex-1 cursor-pointer appearance-none rounded-full bg-border accent-primary"
      />
      <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">
        {position + 1} / {total}
      </span>
    </div>
  );
}

// --- entry / empty / error / loading ------------------------------------- //

function EntryScreen({ onOpenRun }: { onOpenRun: (id: string) => void }) {
  const [value, setValue] = useState("");
  return (
    <div className="flex h-full flex-col items-center justify-center p-6">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onOpenRun(value);
        }}
        className="w-full max-w-md rounded-xl border border-border bg-card p-6 text-center"
      >
        <div className="mx-auto mb-4 flex h-11 w-11 items-center justify-center rounded-lg border border-border bg-muted">
          <ListTree aria-hidden className="h-5 w-5 text-muted-foreground" />
        </div>
        <h1 className="font-display text-lg font-semibold tracking-tight">
          Inspect a run
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Paste an agent run id to open its step-level trace — every tool call,
          decision and output, with tokens and cost.
        </p>
        <div className="mt-5 flex items-center gap-2">
          <div className="relative flex-1">
            <Search
              aria-hidden
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            />
            <input
              autoFocus
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="Run id (UUID)"
              aria-label="Run id"
              className="h-10 w-full rounded-md border border-input bg-background pl-9 pr-3 font-mono text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
          <Button type="submit" disabled={!value.trim()}>
            Open trace
          </Button>
        </div>
      </form>
    </div>
  );
}

function EmptyTimeline() {
  return (
    <div
      data-testid="empty-timeline"
      className="flex flex-1 flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border p-10 text-center"
    >
      <ListTree className="h-8 w-8 text-muted-foreground" />
      <p className="text-sm font-medium text-foreground">No steps recorded</p>
      <p className="text-xs text-muted-foreground">
        This run finished without emitting any trace steps.
      </p>
    </div>
  );
}

function ErrorState({
  runId,
  notFound,
  onRetry,
  onOpenRun,
}: {
  runId: string;
  notFound: boolean;
  onRetry: () => void;
  onOpenRun: (id: string) => void;
}) {
  return (
    <div
      data-testid="trace-error"
      className="flex h-full flex-col items-center justify-center p-6"
    >
      <div className="w-full max-w-md rounded-xl border border-border bg-card p-6 text-center">
        <div className="mx-auto mb-4 flex h-11 w-11 items-center justify-center rounded-lg border border-danger/40 bg-danger/10">
          <AlertTriangle aria-hidden className="h-5 w-5 text-danger" />
        </div>
        <h1 className="font-display text-lg font-semibold tracking-tight">
          {notFound ? "Run not found" : "Couldn't load the trace"}
        </h1>
        <p className="mt-1 break-words text-sm text-muted-foreground">
          {notFound ? (
            <>
              No trace was recorded for run{" "}
              <code className="font-mono text-foreground">{runId}</code>.
            </>
          ) : (
            "The run trace is temporarily unavailable. Please try again."
          )}
        </p>
        <div className="mt-5 flex items-center justify-center gap-2">
          {notFound ? (
            <Button type="button" variant="outline" onClick={() => onOpenRun("")}>
              Try another run
            </Button>
          ) : (
            <Button type="button" onClick={onRetry}>
              Retry
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

function TraceSkeleton(): ReactNode {
  return (
    <div
      data-testid="trace-skeleton"
      aria-busy="true"
      className="flex h-full flex-col gap-5"
    >
      <div className="flex items-center justify-between gap-4">
        <div className="flex flex-col gap-2">
          <div className="h-6 w-40 animate-pulse rounded bg-muted" />
          <div className="h-3 w-64 animate-pulse rounded bg-muted/60" />
        </div>
        <div className="h-10 w-28 animate-pulse rounded-md bg-muted" />
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {[0, 1, 2, 3, 4].map((i) => (
          <div key={i} className="h-16 animate-pulse rounded-lg bg-muted" />
        ))}
      </div>
      <div className="flex flex-1 flex-col gap-2 rounded-xl border border-border bg-card/40 p-3">
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <div key={i} className="flex items-start gap-3 px-2 py-2">
            <div className="h-6 w-6 shrink-0 animate-pulse rounded-full bg-muted" />
            <div className="flex flex-1 flex-col gap-1.5">
              <div className="h-3 w-1/3 animate-pulse rounded bg-muted/70" />
              <div className="h-3 w-2/3 animate-pulse rounded bg-muted/50" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** A compact per-kind breakdown derived from `step_counts` (pure, testable). */
export function kindLegend(
  counts: Partial<Record<StepKind, number>>,
): { kind: StepKind; label: string; count: number }[] {
  return (Object.keys(counts) as StepKind[])
    .filter((kind) => STEP_KIND_META[kind] && (counts[kind] ?? 0) > 0)
    .map((kind) => ({
      kind,
      label: STEP_KIND_META[kind].label,
      count: counts[kind] ?? 0,
    }));
}

function KindLegend({ counts }: { counts: Partial<Record<StepKind, number>> }) {
  const items = kindLegend(counts);
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="kind-legend">
      {items.map(({ kind, label, count }) => (
        <span
          key={kind}
          className="inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-2.5 py-1 text-xs text-muted-foreground"
        >
          <span
            aria-hidden
            className={cn(
              "inline-flex h-2 w-2 rounded-full border",
              TONE_NODE_CLASS[KIND_TONE[kind]],
            )}
          />
          <span className="text-foreground">{label}</span>
          <span className="font-mono tabular-nums">{count}</span>
        </span>
      ))}
    </div>
  );
}
