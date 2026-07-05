"use client";

import { ChevronRight, Lock, PackageOpen, ShieldCheck } from "lucide-react";
import { Fragment } from "react";

import type { EnvironmentRead } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { HealthBadge, StateBadge } from "./deployment-badges";
import {
  actorLabel,
  formatRelativeTime,
  shortSha,
  sortEnvironmentsByRank,
} from "./deployment-meta";

export interface PipelineStagesProps {
  environments: EnvironmentRead[];
  selectedDeploymentId: string | null;
  onSelectDeployment: (deploymentId: string) => void;
  /** Promote this stage's live commit to the next stage. */
  onPromoteFrom: (fromEnvName: string) => void;
  now?: number;
}

/**
 * The promotion pipeline: ranked environment stages (dev -> staging -> prod)
 * with what is currently live on each, its gate/health state, and a per-stage
 * "Promote →" that carries the live commit forward to the next stage. Selecting
 * a stage's live deployment focuses it in the gate panel. Scrolls horizontally
 * on narrow viewports so the page body never does.
 */
export function PipelineStages({
  environments,
  selectedDeploymentId,
  onSelectDeployment,
  onPromoteFrom,
  now,
}: PipelineStagesProps) {
  const stages = sortEnvironmentsByRank(environments);

  return (
    <div
      data-testid="pipeline-stages"
      role="list"
      aria-label="Promotion pipeline"
      className="flex items-stretch gap-2 overflow-x-auto pb-1"
    >
      {stages.map((env, index) => {
        const isLast = index === stages.length - 1;
        return (
          <Fragment key={env.id}>
            <Stage
              env={env}
              hasNext={!isLast}
              selected={
                !!env.currently_deployed &&
                env.currently_deployed.id === selectedDeploymentId
              }
              onSelect={onSelectDeployment}
              onPromote={() => onPromoteFrom(env.name)}
              now={now}
            />
            {!isLast ? (
              <div
                aria-hidden
                className="flex shrink-0 items-center self-center text-muted-foreground"
              >
                <ChevronRight className="h-5 w-5" />
              </div>
            ) : null}
          </Fragment>
        );
      })}
    </div>
  );
}

function Stage({
  env,
  hasNext,
  selected,
  onSelect,
  onPromote,
  now,
}: {
  env: EnvironmentRead;
  hasNext: boolean;
  selected: boolean;
  onSelect: (deploymentId: string) => void;
  onPromote: () => void;
  now?: number;
}) {
  const live = env.currently_deployed ?? null;
  return (
    <section
      role="listitem"
      data-testid="pipeline-stage"
      data-env={env.name}
      className={cn(
        "flex w-[16rem] shrink-0 flex-col gap-3 rounded-lg border bg-card p-3.5 transition-colors",
        selected ? "border-primary/60 ring-1 ring-primary/30" : "border-border",
      )}
    >
      <header className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="inline-flex h-5 min-w-5 items-center justify-center rounded border border-border bg-muted px-1 font-mono text-[10px] font-semibold text-muted-foreground">
            {env.rank}
          </span>
          <h3 className="truncate font-display text-sm font-semibold tracking-tight text-foreground">
            {env.name}
          </h3>
        </div>
        <div className="flex items-center gap-1.5 text-muted-foreground">
          {env.requires_approval ? (
            <ShieldCheck
              aria-label="Requires approval"
              className="h-3.5 w-3.5"
            />
          ) : null}
          {env.is_restricted ? (
            <Lock aria-label="Restricted environment" className="h-3.5 w-3.5" />
          ) : null}
        </div>
      </header>

      {live ? (
        <button
          type="button"
          data-testid="stage-live"
          onClick={() => onSelect(live.id)}
          aria-pressed={selected}
          className={cn(
            "flex flex-col gap-2 rounded-md border border-border bg-background/60 p-2.5 text-left transition-colors",
            "hover:border-primary/40 hover:bg-accent/60",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          )}
        >
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono text-xs font-medium text-foreground">
              {shortSha(live.commit_sha)}
            </span>
            <HealthBadge status={live.health_status} />
          </div>
          <StateBadge state={live.state} />
          <p className="truncate text-[11px] text-muted-foreground">
            {actorLabel(live.initiated_by)} · {formatRelativeTime(live.requested_at, now)}
          </p>
        </button>
      ) : (
        <div
          data-testid="stage-empty"
          className="flex flex-1 flex-col items-center justify-center gap-1.5 rounded-md border border-dashed border-border px-2 py-4 text-center"
        >
          <PackageOpen className="h-5 w-5 text-muted-foreground" aria-hidden />
          <p className="text-[11px] text-muted-foreground">Nothing deployed yet</p>
        </div>
      )}

      {hasNext ? (
        <button
          type="button"
          data-testid="stage-promote"
          disabled={!live}
          onClick={onPromote}
          className={cn(
            "inline-flex h-8 items-center justify-center gap-1.5 rounded-md border border-border px-2.5 text-xs font-medium text-foreground transition-colors",
            "hover:bg-accent hover:text-accent-foreground",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            "disabled:pointer-events-none disabled:opacity-50",
          )}
        >
          Promote
          <ChevronRight className="h-3.5 w-3.5" aria-hidden />
        </button>
      ) : null}
    </section>
  );
}
