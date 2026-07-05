import { ShieldAlert, ShieldCheck, Wrench } from "lucide-react";

import type { RemediationPlanView } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { BlastRadiusBadge } from "./incident-badges";
import { humanize, stepStatusClass } from "./incident-meta";

export interface RemediationPanelProps {
  plan: RemediationPlanView | null | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

/**
 * The remediation runbook panel — the proposed, ordered steps with each step's
 * declared blast radius, validated against the incident's blast-radius policy.
 * A step outside the policy is flagged `blocked` and the plan surfaces a guard
 * banner so no over-broad change reaches approval unnoticed.
 */
export function RemediationPanel({
  plan,
  isLoading,
  isError,
  onRetry,
}: RemediationPanelProps) {
  if (isLoading) {
    return <PlanSkeleton />;
  }
  if (isError) {
    return (
      <div
        role="alert"
        className="flex flex-col items-start gap-2 rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground"
      >
        Couldn&apos;t load the remediation plan.
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="rounded-md border border-border px-2 py-1 text-xs font-medium text-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Retry
          </button>
        ) : null}
      </div>
    );
  }
  if (!plan) {
    return (
      <div
        data-testid="remediation-empty"
        className="flex flex-col items-center gap-2 rounded-md border border-dashed border-border p-8 text-center"
      >
        <Wrench className="h-7 w-7 text-muted-foreground" />
        <p className="text-sm font-medium text-foreground">
          No remediation proposed yet
        </p>
        <p className="max-w-sm text-xs text-muted-foreground">
          Once responders (or an agent) propose a runbook, its ordered steps and
          per-step blast radius appear here for review before approval.
        </p>
      </div>
    );
  }

  const blocked = plan.offending_step_ids.length > 0;

  return (
    <div data-testid="remediation-plan" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-foreground">
          Attempt {plan.attempt}
        </span>
        <span className="text-xs text-muted-foreground">·</span>
        <span className="rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] font-medium capitalize text-muted-foreground">
          {humanize(plan.status)}
        </span>
        <span className="text-xs text-muted-foreground">·</span>
        <BlastRadiusBadge radius={plan.max_blast_radius} />
      </div>

      <div
        className={cn(
          "flex items-start gap-2 rounded-md border px-3 py-2 text-xs",
          blocked
            ? "border-danger/40 bg-danger/10 text-danger"
            : "border-success/40 bg-success/10 text-success",
        )}
      >
        {blocked ? (
          <ShieldAlert aria-hidden className="mt-0.5 h-4 w-4 shrink-0" />
        ) : (
          <ShieldCheck aria-hidden className="mt-0.5 h-4 w-4 shrink-0" />
        )}
        <span>
          {blocked
            ? `${plan.offending_step_ids.length} step${plan.offending_step_ids.length === 1 ? "" : "s"} exceed the incident's blast-radius policy — approval is blocked until scoped down.`
            : "All steps are within the incident's blast-radius policy."}
        </span>
      </div>

      <ol className="flex flex-col gap-2">
        {plan.steps.map((step) => (
          <li
            key={step.id}
            data-testid="remediation-step"
            data-blocked={step.blocked || undefined}
            className={cn(
              "rounded-md border p-3",
              step.blocked ? "border-danger/40 bg-danger/5" : "border-border bg-card",
            )}
          >
            <div className="flex items-start gap-3">
              <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border bg-muted text-xs font-medium text-muted-foreground">
                {step.order}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-sm font-medium text-foreground">{step.title}</p>
                  <div className="flex items-center gap-1.5">
                    <span
                      className={cn(
                        "rounded-full border px-2 py-0.5 text-[10px] font-medium capitalize",
                        stepStatusClass(step.status),
                      )}
                    >
                      {step.status}
                    </span>
                    <BlastRadiusBadge radius={step.blast_radius} />
                  </div>
                </div>
                <p className="mt-1 font-mono text-xs text-muted-foreground">
                  {step.action}
                </p>
                {step.rationale ? (
                  <p className="mt-1 text-xs text-muted-foreground">
                    {step.rationale}
                  </p>
                ) : null}
                {step.blocked ? (
                  <p className="mt-1 text-[11px] font-medium text-danger">
                    Outside blast-radius policy
                  </p>
                ) : null}
              </div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

function PlanSkeleton() {
  return (
    <div data-testid="remediation-skeleton" aria-busy="true" className="flex flex-col gap-3">
      <div className="h-4 w-40 animate-pulse rounded bg-muted" />
      {[0, 1].map((i) => (
        <div key={i} className="rounded-md border border-border p-3">
          <div className="h-3 w-2/3 animate-pulse rounded bg-muted" />
          <div className="mt-2 h-2.5 w-1/2 animate-pulse rounded bg-muted/60" />
        </div>
      ))}
    </div>
  );
}
