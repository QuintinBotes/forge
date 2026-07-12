"use client";

import { GitCompareArrows } from "lucide-react";
import { useCallback, useState } from "react";

import { Button } from "@/components/ui/button";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import { useReplayRun } from "@/lib/api/observability";
import type { ReplayRunResult } from "@/lib/api/types";
import { cn } from "@/lib/utils";

export interface TimeTravelReplayProps {
  /**
   * The `RunRecording` id to replay — a separate id space from the
   * `AgentRunResult.run_id` this viewer otherwise shows (see
   * `forge_api.routers.agent.replay_run`); only populated for runs taped
   * under `FORGE_RECORD_RUNS=1`.
   */
  runId: string;
  client?: ForgeApiClient;
}

/**
 * Time-Travel Runs — the deterministic-replay control for the run-trace view.
 *
 * Distinct from the timeline "Replay" button in the header above (which just
 * scrubs the already-fetched trace): this re-runs the recorded cassette *by
 * substitution* (`POST /agent/runs/{run_id}/replay`) and reports whether
 * today's runtime still reproduces the exact call sequence taped for this
 * run. A cassette holds no objective, so the same one that produced the run
 * is supplied here.
 */
export function TimeTravelReplay({ runId, client = apiClient }: TimeTravelReplayProps) {
  const [open, setOpen] = useState(false);
  const [objective, setObjective] = useState("");
  const replay = useReplayRun(runId, client);

  const onRun = useCallback(() => {
    const trimmed = objective.trim();
    if (!trimmed || replay.isPending) return;
    replay.mutate({ objective: trimmed });
  }, [objective, replay]);

  return (
    <div className="flex flex-col items-end gap-2">
      <Button
        type="button"
        variant="outline"
        size="sm"
        data-testid="time-travel-toggle"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <GitCompareArrows aria-hidden className="h-4 w-4" />
        Time-travel replay
      </Button>

      {open ? (
        <div
          data-testid="time-travel-panel"
          className="w-full max-w-sm rounded-lg border border-border bg-card p-3 text-sm"
        >
          <label
            htmlFor="replay-objective"
            className="mb-1.5 block text-xs font-medium text-muted-foreground"
          >
            Objective that produced this run
          </label>
          <div className="flex gap-2">
            <input
              id="replay-objective"
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
              placeholder="e.g. Fix the flaky retry test"
              className="h-9 flex-1 rounded-md border border-input bg-background px-2.5 text-xs placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <Button
              type="button"
              size="sm"
              onClick={onRun}
              disabled={!objective.trim() || replay.isPending}
            >
              {replay.isPending ? "Replaying…" : "Run"}
            </Button>
          </div>

          {replay.data ? <ReplayResultBanner result={replay.data} /> : null}
          {replay.isError ? (
            <p role="alert" className="mt-2 text-xs text-danger">
              Couldn&apos;t replay this run — please try again.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ReplayResultBanner({ result }: { result: ReplayRunResult }) {
  const matched = result.steps.filter((step) => step.matched).length;
  return (
    <div
      data-testid="replay-result"
      className={cn(
        "mt-2 rounded-md border px-2.5 py-2 text-xs",
        result.diverged
          ? "border-danger/40 bg-danger/10 text-danger"
          : "border-success/40 bg-success/10 text-success",
      )}
    >
      <p className="font-medium">
        {result.diverged ? "Diverged from the recorded run" : "Reproduced the recorded run"}
      </p>
      <p className="mt-0.5 text-muted-foreground">
        {matched}/{result.steps.length} calls matched
        {result.divergence
          ? ` — first drift at ${result.divergence.boundary} call #${result.divergence.index}`
          : ""}
      </p>
    </div>
  );
}
