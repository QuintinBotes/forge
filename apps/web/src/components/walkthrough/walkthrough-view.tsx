"use client";

/**
 * In-app guided walkthrough — the first-run product tour that ties the whole
 * Forge loop together: create a spec -> run an agent -> review the PR -> merge.
 *
 * The screen lays the loop out as four real, navigable "stops". A custom tour
 * (built on the Forge design tokens, no third-party dependency) spotlights each
 * stop in turn; it is dismissible, resumable across sessions, and restartable
 * from the on-page Help menu or the ⌘K command palette. Every stop shows the
 * user's live progress, read from the real routers (specs / approvals /
 * deployments) so the tour reflects what they've genuinely accomplished.
 */

import {
  Bot,
  Check,
  Compass,
  FileText,
  GitMerge,
  GitPullRequest,
  LifeBuoy,
  Play,
  RotateCcw,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { useRegisterCommands } from "@/components/command-palette";
import { Button } from "@/components/ui/button";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import { useOnboardingProgress } from "@/lib/api/onboarding";
import type { OnboardingProgress, OnboardingStepKey } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  TourProvider,
  useTour,
  useTourTarget,
  WALKTHROUGH_STORAGE_KEY,
} from "./tour-context";
import { TourOverlay } from "./tour-overlay";

/** Placeholder project until project routing lands (mirrors the spec dashboard). */
export const DEFAULT_PROJECT_ID = "default";

interface StopMeta {
  key: OnboardingStepKey;
  target: string;
  index: number;
  icon: LucideIcon;
  title: string;
  blurb: string;
  href: string;
  cta: string;
  /** Noun for the live count chip ("3 specs", "1 deployment"…). */
  noun: [singular: string, plural: string];
}

const STOPS: readonly StopMeta[] = [
  {
    key: "spec",
    target: "stop-spec",
    index: 1,
    icon: FileText,
    title: "Create a spec",
    blurb:
      "Capture intent as a spec — requirements, acceptance criteria, open questions. It's the contract every agent builds against.",
    href: "/specs",
    cta: "Open Specs",
    noun: ["spec", "specs"],
  },
  {
    key: "run",
    target: "stop-run",
    index: 2,
    icon: Bot,
    title: "Run an agent",
    blurb:
      "Dispatch an agent against the approved spec. It plans, writes code and opens a PR — every step a replayable run trace.",
    href: "/runs",
    cta: "Open Runs",
    noun: ["spec in flight", "specs in flight"],
  },
  {
    key: "review",
    target: "stop-review",
    index: 3,
    icon: GitPullRequest,
    title: "Review the PR",
    blurb:
      "The agent's pull request lands as an approval gate. Review the diff and checks, then approve or request changes.",
    href: "/approvals",
    cta: "Open Approvals",
    noun: ["PR gate", "PR gates"],
  },
  {
    key: "merge",
    target: "stop-merge",
    index: 4,
    icon: GitMerge,
    title: "Merge & ship",
    blurb:
      "Approve to merge and promote the change through the pipeline. That closes the loop — ready for the next spec.",
    href: "/deployments",
    cta: "Open Deployments",
    noun: ["deployment", "deployments"],
  },
];

export interface WalkthroughViewProps {
  client?: ForgeApiClient;
  projectId?: string;
  /** Auto-open the tour on a first-ever visit. Defaults to true. */
  autoStart?: boolean;
  /** Override the persistence key (test isolation). */
  storageKey?: string;
}

export function WalkthroughView({
  client = apiClient,
  projectId = DEFAULT_PROJECT_ID,
  autoStart = true,
  storageKey = WALKTHROUGH_STORAGE_KEY,
}: WalkthroughViewProps) {
  return (
    <TourProvider autoStart={autoStart} storageKey={storageKey}>
      <WalkthroughInner client={client} projectId={projectId} />
      <TourOverlay />
    </TourProvider>
  );
}

function WalkthroughInner({
  client,
  projectId,
}: {
  client: ForgeApiClient;
  projectId: string;
}) {
  const tour = useTour();
  const progressQuery = useOnboardingProgress(projectId, client);
  const progress = progressQuery.data ?? null;
  const loopActive = useTourTarget("loop");

  // ⌘K: start / restart the walkthrough from anywhere (the app's help surface).
  const startRef = useRef(tour.start);
  const restartRef = useRef(tour.restart);
  useEffect(() => {
    startRef.current = tour.start;
    restartRef.current = tour.restart;
  }, [tour.start, tour.restart]);
  const commands = useMemo(
    () => [
      {
        id: "walkthrough-start",
        label: "Start product walkthrough",
        group: "Help",
        icon: <Play />,
        run: () => startRef.current(),
      },
      {
        id: "walkthrough-restart",
        label: "Restart walkthrough from the beginning",
        group: "Help",
        icon: <RotateCcw />,
        run: () => restartRef.current(),
      },
    ],
    [],
  );
  useRegisterCommands("walkthrough", commands);

  return (
    <div
      data-testid="walkthrough"
      role="region"
      aria-label="Guided walkthrough"
      className="mx-auto flex w-full max-w-5xl flex-col gap-8"
    >
      <Hero tour={tour} progress={progress} loading={progressQuery.isLoading} />

      <ProgressPanel query={progressQuery} progress={progress} />

      <section
        data-tour="loop"
        aria-label="The Forge build loop"
        className={cn(
          "relative rounded-2xl border border-border bg-card/40 p-5 transition-all",
          loopActive && "z-50 ring-2 ring-primary shadow-lg",
        )}
        data-tour-active={loopActive ? "true" : undefined}
      >
        <ol className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {STOPS.map((stop) => (
            <li key={stop.key} className="contents">
              <StopCard stop={stop} progress={progress} query={progressQuery} />
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}

// --- Hero ------------------------------------------------------------------ //

function Hero({
  tour,
  progress,
  loading,
}: {
  tour: ReturnType<typeof useTour>;
  progress: OnboardingProgress | null;
  loading: boolean;
}) {
  const canResume = tour.status === "dismissed" && tour.stepIndex > 0;
  const isComplete = tour.status === "completed";
  const primaryLabel = isComplete
    ? "Replay walkthrough"
    : canResume
      ? "Resume walkthrough"
      : "Start walkthrough";
  const onPrimary = () => (isComplete ? tour.restart() : tour.start());

  return (
    <header className="flex flex-col gap-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-border bg-accent text-primary">
            <Compass className="h-6 w-6" aria-hidden />
          </span>
          <div className="flex flex-col gap-1">
            <h1 className="font-display text-2xl font-semibold tracking-tight">
              Get started with Forge
            </h1>
            <p className="max-w-xl text-sm text-muted-foreground">
              A two-minute tour of the loop every change travels through — from a
              spec, to an agent, to a reviewed pull request, to a merge.
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <HelpMenu tour={tour} />
          <Button onClick={onPrimary} data-testid="hero-start">
            {isComplete ? (
              <RotateCcw className="h-4 w-4" aria-hidden />
            ) : (
              <Play className="h-4 w-4" aria-hidden />
            )}
            {primaryLabel}
          </Button>
        </div>
      </div>

      {!loading && progress ? <ProgressBar progress={progress} /> : null}
    </header>
  );
}

function ProgressBar({ progress }: { progress: OnboardingProgress }) {
  const pct = Math.round((progress.completedCount / progress.totalCount) * 100);
  return (
    <div className="flex items-center gap-3" data-testid="progress-bar">
      <div
        className="h-2 flex-1 overflow-hidden rounded-full bg-muted"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={progress.totalCount}
        aria-valuenow={progress.completedCount}
        aria-label="Loop stages completed"
      >
        <div
          className="h-full rounded-full bg-primary transition-all"
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="shrink-0 text-xs font-medium text-muted-foreground">
        {progress.completedCount} of {progress.totalCount} stages
      </span>
    </div>
  );
}

// --- Help menu (restart surface) ------------------------------------------ //

function HelpMenu({ tour }: { tour: ReturnType<typeof useTour> }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const canResume = tour.status === "dismissed" && tour.stepIndex > 0;

  useEffect(() => {
    if (!open) return;
    const onDocDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <Button
        variant="outline"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        data-testid="help-menu-trigger"
      >
        <LifeBuoy className="h-4 w-4" aria-hidden />
        Help
      </Button>
      {open ? (
        <div
          role="menu"
          aria-label="Walkthrough help"
          data-testid="help-menu"
          className="absolute right-0 z-50 mt-2 flex w-60 flex-col gap-0.5 rounded-lg border border-border bg-popover p-1.5 text-popover-foreground shadow-xl"
        >
          <MenuItem
            icon={<Play className="h-4 w-4" aria-hidden />}
            onClick={() => {
              tour.start();
              setOpen(false);
            }}
            testId="help-start"
          >
            {canResume ? "Resume walkthrough" : "Start walkthrough"}
          </MenuItem>
          <MenuItem
            icon={<RotateCcw className="h-4 w-4" aria-hidden />}
            onClick={() => {
              tour.restart();
              setOpen(false);
            }}
            testId="help-restart"
          >
            Restart from the beginning
          </MenuItem>
          <div className="my-1 h-px bg-border" role="separator" />
          <Link
            role="menuitem"
            href="/specs"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Sparkles className="h-4 w-4" aria-hidden />
            Skip to Specs
          </Link>
        </div>
      ) : null}
    </div>
  );
}

function MenuItem({
  icon,
  children,
  onClick,
  testId,
}: {
  icon: ReactNode;
  children: ReactNode;
  onClick: () => void;
  testId: string;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      data-testid={testId}
      className="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-sm text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      {icon}
      {children}
    </button>
  );
}

// --- Progress panel (loading / error / empty / complete states) ----------- //

function ProgressPanel({
  query,
  progress,
}: {
  query: ReturnType<typeof useOnboardingProgress>;
  progress: OnboardingProgress | null;
}) {
  if (query.isLoading) {
    return (
      <div
        data-testid="progress-loading"
        aria-busy="true"
        className="h-14 animate-pulse rounded-xl border border-border bg-card"
      />
    );
  }
  if (query.isError) {
    return (
      <div
        role="status"
        data-testid="progress-error"
        className="flex items-center gap-3 rounded-xl border border-dashed border-border bg-card px-4 py-3 text-sm text-muted-foreground"
      >
        <LifeBuoy className="h-4 w-4 shrink-0" aria-hidden />
        We couldn&apos;t load your progress just now — the tour still works, and
        your progress will appear once the workspace is reachable.
      </div>
    );
  }
  if (progress && progress.allComplete) {
    return (
      <div
        role="status"
        data-testid="progress-complete"
        className="flex items-center gap-3 rounded-xl border border-success/40 bg-success/10 px-4 py-3 text-sm text-foreground"
      >
        <Check className="h-4 w-4 shrink-0 text-success" aria-hidden />
        You&apos;ve shipped a change end to end — the whole loop is complete.
        Draft your next spec to keep the flywheel turning.
      </div>
    );
  }
  if (progress && progress.completedCount === 0) {
    return (
      <div
        data-testid="progress-empty"
        className="flex items-center gap-3 rounded-xl border border-dashed border-border bg-card px-4 py-3 text-sm text-muted-foreground"
      >
        <Sparkles className="h-4 w-4 shrink-0 text-primary" aria-hidden />
        You haven&apos;t started the loop yet. Take the tour, then create your
        first spec to kick things off.
      </div>
    );
  }
  return (
    <div
      data-testid="progress-partial"
      className="flex items-center gap-3 rounded-xl border border-border bg-card px-4 py-3 text-sm text-muted-foreground"
    >
      <Compass className="h-4 w-4 shrink-0 text-primary" aria-hidden />
      You&apos;re on your way — {progress?.completedCount} of{" "}
      {progress?.totalCount} stages done. Pick up the tour any time.
    </div>
  );
}

// --- Stop card ------------------------------------------------------------- //

function StopCard({
  stop,
  progress,
  query,
}: {
  stop: StopMeta;
  progress: OnboardingProgress | null;
  query: ReturnType<typeof useOnboardingProgress>;
}) {
  const active = useTourTarget(stop.target);
  const Icon = stop.icon;
  const stepProgress = progress?.steps.find((s) => s.key === stop.key) ?? null;

  return (
    <div
      data-tour={stop.target}
      data-tour-active={active ? "true" : undefined}
      data-testid={`stop-${stop.key}`}
      aria-current={active ? "step" : undefined}
      className={cn(
        "flex h-full flex-col gap-3 rounded-xl border bg-card p-4 transition-all",
        active
          ? "z-50 border-primary/60 shadow-lg ring-2 ring-primary"
          : "border-border",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span
          className={cn(
            "flex h-9 w-9 items-center justify-center rounded-lg border border-border bg-muted/60",
            stepProgress?.done ? "text-success" : "text-primary",
          )}
        >
          <Icon className="h-5 w-5" aria-hidden />
        </span>
        <StatusChip stop={stop} progress={stepProgress} query={query} />
      </div>

      <div className="flex flex-col gap-1">
        <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          Step {stop.index}
        </span>
        <h3 className="font-display text-base font-semibold leading-tight tracking-tight">
          {stop.title}
        </h3>
        <p className="text-xs leading-relaxed text-muted-foreground">
          {stop.blurb}
        </p>
      </div>

      <Button
        asChild
        variant="ghost"
        size="sm"
        className="mt-auto justify-start px-2 text-primary hover:text-primary"
      >
        <Link href={stop.href} data-testid={`stop-cta-${stop.key}`}>
          {stop.cta}
        </Link>
      </Button>
    </div>
  );
}

function StatusChip({
  stop,
  progress,
  query,
}: {
  stop: StopMeta;
  progress: OnboardingProgress["steps"][number] | null;
  query: ReturnType<typeof useOnboardingProgress>;
}) {
  if (query.isLoading) {
    return (
      <span
        data-testid={`chip-loading-${stop.key}`}
        aria-hidden
        className="h-5 w-16 animate-pulse rounded-full bg-muted"
      />
    );
  }
  if (query.isError || !progress) {
    return (
      <span className="rounded-full border border-border bg-muted/60 px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
        —
      </span>
    );
  }
  if (progress.done) {
    const [one, many] = stop.noun;
    return (
      <span
        data-testid={`chip-done-${stop.key}`}
        className="inline-flex items-center gap-1 rounded-full border border-success/40 bg-success/10 px-2 py-0.5 text-[11px] font-medium text-success"
      >
        <Check className="h-3 w-3" aria-hidden />
        {progress.count} {progress.count === 1 ? one : many}
      </span>
    );
  }
  return (
    <span
      data-testid={`chip-todo-${stop.key}`}
      className="rounded-full border border-border bg-muted/60 px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
    >
      Not started
    </span>
  );
}
