import { Check, Flame } from "lucide-react";

import { cn } from "@/lib/utils";
import type { SpecStatus } from "@/lib/api/types";

import { LIFECYCLE_STAGES, stageIndex, stageState } from "./spec-meta";

export interface LifecycleRailProps {
  status: SpecStatus | undefined;
}

/**
 * The SDD lifecycle as a forge heat rail. Ember heat has travelled up to the
 * spec's current stage — filled ember segments and a glowing, spark-ringed
 * current node — with cold, dashed steel ahead. The rail encodes real progress
 * (the lifecycle is a genuine sequence), so it doubles as the spec-status view.
 */
export function LifecycleRail({ status }: LifecycleRailProps) {
  const current = stageIndex(status);
  return (
    <div className="overflow-x-auto">
      <ol
        data-testid="lifecycle-rail"
        aria-label="SDD lifecycle"
        className="flex min-w-[34rem] items-start"
      >
        {LIFECYCLE_STAGES.map((stage, index) => {
          const state = stageState(index, status);
          const heated = index <= current;
          return (
            <li
              key={stage.status}
              data-testid={`stage-${stage.status}`}
              data-state={state}
              aria-current={state === "current" ? "step" : undefined}
              className="relative flex flex-1 flex-col items-center gap-2 px-1 text-center"
            >
              {index > 0 ? (
                <span
                  aria-hidden
                  className={cn(
                    "absolute left-[-50%] top-[13px] -z-0 h-0.5 w-full",
                    heated
                      ? "bg-primary"
                      : "border-t-2 border-dashed border-border bg-transparent",
                  )}
                />
              ) : null}

              <span className="relative z-10 flex h-7 w-7 items-center justify-center">
                {state === "current" ? (
                  <span
                    aria-hidden
                    className="absolute inset-0 rounded-full bg-spark/30 motion-safe:animate-pulse"
                  />
                ) : null}
                <span
                  className={cn(
                    "relative flex h-7 w-7 items-center justify-center rounded-full border text-[11px]",
                    state === "current" &&
                      "border-spark bg-primary text-primary-foreground shadow-sm ring-4 ring-spark/25",
                    state === "done" &&
                      "border-primary/60 bg-primary/15 text-primary",
                    state === "upcoming" &&
                      "border-dashed border-border bg-muted text-muted-foreground",
                  )}
                >
                  {state === "done" ? (
                    <Check className="h-3.5 w-3.5" aria-hidden />
                  ) : state === "current" ? (
                    <Flame className="h-3.5 w-3.5" aria-hidden />
                  ) : (
                    <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
                  )}
                </span>
              </span>

              <div className="flex flex-col gap-0.5">
                <span
                  className={cn(
                    "font-display text-xs font-semibold tracking-tight",
                    state === "upcoming"
                      ? "text-muted-foreground"
                      : "text-foreground",
                  )}
                >
                  {stage.label}
                </span>
                <span className="hidden text-[11px] leading-tight text-muted-foreground sm:block">
                  {stage.blurb}
                </span>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
