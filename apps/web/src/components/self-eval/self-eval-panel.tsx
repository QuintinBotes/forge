"use client";

import { FlaskConical, Play, ShieldCheck } from "lucide-react";
import type { ReactNode } from "react";

import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import { useRunSelfEval, useSelfEvalStatus } from "@/lib/api/ao-settings";
import type { SelfEvalStatusOut } from "@/lib/api/types";
import { cn } from "@/lib/utils";

export interface SelfEvalPanelProps {
  client?: ForgeApiClient;
}

/**
 * Self-Eval Gate settings panel (trust layer, Phase A): the workspace's
 * private per-repo suite, the frozen baseline the gate defends, the last
 * scoring run, the gate's current posture for pending config changes, and a
 * "run self-eval" action that enqueues the worker-owned `forge.self_eval.run`
 * task via `POST /ao/self-eval/runs`. Every state is backed by
 * `GET /ao/self-eval/status`; the Phase-A limitation is stated inline rather
 * than hidden.
 */
export function SelfEvalPanel({ client = apiClient }: SelfEvalPanelProps) {
  const statusQuery = useSelfEvalStatus(client);
  const runMutation = useRunSelfEval(client);

  if (statusQuery.isLoading) {
    return (
      <section
        data-testid="self-eval-skeleton"
        aria-busy="true"
        className="mx-auto w-full max-w-4xl"
      >
        <div className="h-64 animate-pulse rounded-xl border border-border bg-card" />
      </section>
    );
  }

  if (statusQuery.isError || !statusQuery.data) {
    return (
      <section
        data-testid="self-eval-error"
        role="status"
        className="mx-auto flex w-full max-w-4xl flex-col items-center gap-3 rounded-xl border border-dashed border-border px-6 py-12 text-center"
      >
        <ShieldCheck className="h-8 w-8 text-muted-foreground" aria-hidden />
        <div className="flex flex-col gap-1">
          <p className="text-sm font-medium text-foreground">
            Self-Eval Gate status unavailable
          </p>
          <p className="max-w-sm text-xs text-muted-foreground">
            The status read failed — this is a fetch error, not an empty
            workspace. Try again in a moment.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void statusQuery.refetch()}
          className="inline-flex h-9 items-center rounded-md border border-border px-3 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Retry
        </button>
      </section>
    );
  }

  const status = statusQuery.data;
  const runnable = status.suite !== null && status.suite.published;

  return (
    <section
      data-testid="self-eval-panel"
      className="mx-auto flex w-full max-w-4xl flex-col gap-4 rounded-xl border border-border bg-card p-5"
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-primary">
          <ShieldCheck className="h-5 w-5" aria-hidden />
        </span>
        <div>
          <h2 className="font-display text-base font-semibold tracking-tight">
            Self-Eval Gate
          </h2>
          <p className="text-sm text-muted-foreground">
            Blocks a model/router config change that regresses your private
            per-repo regression suite below its frozen baseline.
          </p>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <SuiteBlock status={status} />
        <BaselineBlock status={status} />
        <GateStatusBlock status={status} />
      </div>

      <LastRunLine status={status} />

      {/* Run action — enqueues the worker task; a run is minutes-long. */}
      <div className="flex flex-col gap-2 border-t border-border pt-4">
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            data-testid="self-eval-run"
            disabled={!runnable || runMutation.isPending}
            onClick={() => runMutation.mutate()}
            className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
          >
            <Play className="h-4 w-4" aria-hidden />
            {runMutation.isPending ? "Queueing…" : "Run self-eval"}
          </button>
          {!runnable ? (
            <span className="text-xs text-muted-foreground">
              Requires a published private suite.
            </span>
          ) : null}
        </div>
        {runMutation.isSuccess ? (
          <p
            data-testid="self-eval-run-accepted"
            role="status"
            className="text-sm text-success"
          >
            Run queued — the <Mono>forge.self_eval.run</Mono> worker task will
            score the private suite and record the baseline. Refresh in a few
            minutes.
          </p>
        ) : null}
        {runMutation.isError ? (
          <p
            data-testid="self-eval-run-error"
            role="alert"
            className="text-sm text-danger"
          >
            Couldn&apos;t queue the self-eval run. It needs a published private
            suite and admin access — please try again.
          </p>
        ) : null}
      </div>

      {/* Phase-A limitation, stated inline (honesty rule). */}
      <p
        data-testid="self-eval-phase-a"
        className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-xs text-muted-foreground"
      >
        <span className="font-medium text-warning">Phase A limitation:</span>{" "}
        the API layer does not re-evaluate a proposed config inline. Without a
        baseline the gate cannot block anything, and even with one, a stock
        deployment no-ops at config-change time until an eval runner is
        injected — baselines are recorded only by the worker-owned{" "}
        <Mono>forge.self_eval.run</Mono> task queued above.
      </p>
    </section>
  );
}

// --- Blocks ------------------------------------------------------------------ //

function SuiteBlock({ status }: { status: SelfEvalStatusOut }) {
  return (
    <FactBlock title="Private suite" testId="self-eval-suite">
      {status.suite ? (
        <div className="flex flex-col gap-1">
          <span className="font-mono text-xs text-foreground">
            {status.suite.slug} @ {status.suite.version}
          </span>
          <span className="text-xs text-muted-foreground">
            {status.suite.title} · {status.suite.task_count} hidden cases
          </span>
          {status.suite.repo_id ? (
            <span className="font-mono text-[11px] text-muted-foreground">
              {status.suite.repo_id}
            </span>
          ) : null}
          <span
            className={cn(
              "w-fit rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wide",
              status.suite.published
                ? "border-success/40 bg-success/10 text-success"
                : "border-warning/40 bg-warning/10 text-warning",
            )}
          >
            {status.suite.published ? "published" : "unpublished — not runnable"}
          </span>
        </div>
      ) : (
        <p data-testid="self-eval-no-suite" className="text-xs text-muted-foreground">
          No private suite yet — one is minted from your merged PRs by the{" "}
          <Mono>forge.self_eval.mint</Mono> worker task.
        </p>
      )}
    </FactBlock>
  );
}

function BaselineBlock({ status }: { status: SelfEvalStatusOut }) {
  return (
    <FactBlock title="Baseline" testId="self-eval-baseline">
      {status.baseline ? (
        <div className="flex flex-col gap-1">
          <span className="font-display text-xl font-semibold text-foreground">
            {formatRate(status.baseline.baseline_rate)}
          </span>
          <span className="text-xs text-muted-foreground">
            {status.baseline.resolved}/{status.baseline.total} cases resolved
          </span>
          <span className="text-xs text-muted-foreground">
            recorded {formatWhen(status.baseline.recorded_at)}
          </span>
        </div>
      ) : (
        <p
          data-testid="self-eval-no-baseline"
          className="text-xs text-muted-foreground"
        >
          No baseline recorded — the Self-Eval Gate cannot block any config
          change until a baseline exists.
        </p>
      )}
    </FactBlock>
  );
}

function GateStatusBlock({ status }: { status: SelfEvalStatusOut }) {
  const gate = deriveGate(status);
  return (
    <FactBlock title="Gate status" testId="self-eval-gate-status">
      <div className="flex flex-col gap-1">
        <span
          className={cn(
            "w-fit rounded-full border px-2.5 py-0.5 text-xs font-medium",
            gate.tone,
          )}
        >
          {gate.label}
        </span>
        <p className="text-xs text-muted-foreground">{gate.description}</p>
      </div>
    </FactBlock>
  );
}

function LastRunLine({ status }: { status: SelfEvalStatusOut }) {
  return (
    <p data-testid="self-eval-last-run" className="text-xs text-muted-foreground">
      <FlaskConical className="mr-1 inline h-3.5 w-3.5 align-text-bottom" aria-hidden />
      {status.baseline ? (
        <>
          Last scoring run: {status.baseline.resolved}/{status.baseline.total}{" "}
          resolved ({formatRate(status.baseline.baseline_rate)}) on{" "}
          {formatWhen(status.baseline.recorded_at)}.
        </>
      ) : (
        <>No scored runs recorded.</>
      )}{" "}
      Phase A keeps no separate run history — a run that scores updates the
      baseline; a run that cannot score (missing provisioning) leaves no record
      here.
    </p>
  );
}

// --- Derivations & primitives ------------------------------------------------ //

function deriveGate(status: SelfEvalStatusOut): {
  label: string;
  description: string;
  tone: string;
} {
  if (!status.enforced) {
    return {
      label: "Enforcement off",
      description:
        "self_eval_enforce is disabled — pending config changes are not checked against the baseline.",
      tone: "border-border bg-muted text-muted-foreground",
    };
  }
  if (!status.baseline) {
    return {
      label: "Enforcement on — cannot block yet",
      description:
        "Enforcement is on, but with no baseline there is nothing to regress against, so every pending config change passes.",
      tone: "border-warning/40 bg-warning/10 text-warning",
    };
  }
  return {
    label: "Enforcement on — baseline recorded",
    description:
      "A pending config change that regresses below the baseline is refused (409) unless forced — once an eval runner is wired (see the Phase A note).",
    tone: "border-success/40 bg-success/10 text-success",
  };
}

function formatRate(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

function formatWhen(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function FactBlock({
  title,
  testId,
  children,
}: {
  title: string;
  testId: string;
  children: ReactNode;
}) {
  return (
    <div
      data-testid={testId}
      className="flex flex-col gap-2 rounded-lg border border-border bg-muted/30 p-4"
    >
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </h3>
      {children}
    </div>
  );
}

function Mono({ children }: { children: ReactNode }) {
  return <span className="font-mono text-[11px]">{children}</span>;
}
