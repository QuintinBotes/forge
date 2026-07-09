"use client";

import { Check, Flame, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ApiError, apiClient, type ForgeApiClient } from "@/lib/api/client";
import {
  useApproveSpec,
  useClarifySpec,
  useGenerateTasks,
  usePlanSpec,
  useValidateSpec,
} from "@/lib/api/spec";
import type { SpecOverview } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  PLAIN_LIFECYCLE_STEPS,
  plainCurrentStep,
  plainStepCompletion,
  plainStepState,
} from "./spec-meta";

export interface LifecycleStepperProps {
  spec: SpecOverview;
  client?: ForgeApiClient;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong";
}

/**
 * The SDD lifecycle wired inline as five everyday verbs — Describe, Refine,
 * Approve, Build, Verify — each one backed by an existing `/spec` engine call
 * (Clarify / Plan / Approve / Generate tasks / Validate). Whichever step is
 * next gets a single button that runs its action right from the dashboard or
 * Spec Studio, no separate page needed. Approve and the safe, single-field
 * Clarify flip are optimistic (the rail advances before the request settles);
 * Plan / Generate tasks / Validate touch richer, gated manifest state, so
 * those wait for the engine's response before the rail moves.
 */
export function LifecycleStepper({ spec, client = apiClient }: LifecycleStepperProps) {
  const clarify = useClarifySpec(client);
  const plan = usePlanSpec(client);
  const approve = useApproveSpec(client);
  const generateTasks = useGenerateTasks(client);
  const validate = useValidateSpec(client);

  const completion = plainStepCompletion(spec);
  const current = plainCurrentStep(completion);
  const allDone = completion.every(Boolean);

  const actions = [
    { mutation: clarify, run: () => clarify.mutate({ specId: spec.id }) },
    { mutation: plan, run: () => plan.mutate({ specId: spec.id }) },
    { mutation: approve, run: () => approve.mutate({ specId: spec.id }) },
    { mutation: generateTasks, run: () => generateTasks.mutate({ specId: spec.id }) },
    { mutation: validate, run: () => validate.mutate({ specId: spec.id }) },
  ] as const;

  const activeStep = PLAIN_LIFECYCLE_STEPS[current];
  const activeAction = actions[current];

  return (
    <div className="flex flex-col gap-3" data-testid="lifecycle-stepper">
      <ol
        aria-label="Spec lifecycle"
        className="flex min-w-[30rem] items-start overflow-x-auto"
      >
        {PLAIN_LIFECYCLE_STEPS.map((step, index) => {
          const state = plainStepState(index, completion, current);
          return (
            <li
              key={step.id}
              data-testid={`plain-stage-${step.id}`}
              data-state={state}
              aria-current={state === "current" ? "step" : undefined}
              className="relative flex flex-1 flex-col items-center gap-2 px-1 text-center"
            >
              {index > 0 ? (
                <span
                  aria-hidden
                  className={cn(
                    "absolute left-[-50%] top-[13px] -z-0 h-0.5 w-full",
                    completion[index - 1]
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
                    state === "done" && "border-primary/60 bg-primary/15 text-primary",
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

              <span
                className={cn(
                  "font-display text-xs font-semibold tracking-tight",
                  state === "upcoming" ? "text-muted-foreground" : "text-foreground",
                )}
              >
                {step.label}
              </span>
              <span className="hidden text-[11px] leading-tight text-muted-foreground sm:block">
                {step.blurb}
              </span>
            </li>
          );
        })}
      </ol>

      {allDone ? (
        <p
          role="status"
          data-testid="stepper-complete"
          className="text-xs text-muted-foreground"
        >
          Lifecycle complete — validated and traceable.
        </p>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            onClick={activeAction.run}
            disabled={activeAction.mutation.isPending}
            data-testid={`stepper-run-${activeStep.id}`}
          >
            {activeAction.mutation.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            ) : (
              <Flame className="h-3.5 w-3.5" aria-hidden />
            )}
            {activeAction.mutation.isPending ? "Working…" : activeStep.actionLabel}
          </Button>
          {activeAction.mutation.isError ? (
            <span role="status" data-testid="stepper-error" className="text-xs text-danger">
              {errorMessage(activeAction.mutation.error)}
            </span>
          ) : null}
        </div>
      )}
    </div>
  );
}
