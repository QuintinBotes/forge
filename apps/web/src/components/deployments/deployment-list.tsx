"use client";

import { ArrowUpRight } from "lucide-react";

import type { DeploymentRead } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { StateBadge } from "./deployment-badges";
import { actorLabel, formatRelativeTime, shortSha } from "./deployment-meta";

export interface DeploymentListProps {
  deployments: DeploymentRead[];
  selectedId: string | null;
  onSelect: (deployment: DeploymentRead) => void;
  now?: number;
}

/**
 * The recent-deployments queue: attention-ranked rows (awaiting-approval first)
 * that focus a deployment in the gate panel. Purely presentational — ordering is
 * decided by the view so the list stays trivially testable.
 */
export function DeploymentList({
  deployments,
  selectedId,
  onSelect,
  now,
}: DeploymentListProps) {
  return (
    <ul data-testid="deployment-list" className="flex flex-col gap-1">
      {deployments.map((d) => {
        const selected = d.id === selectedId;
        return (
          <li key={d.id}>
            <button
              type="button"
              data-testid="deployment-row"
              data-selected={selected}
              aria-pressed={selected}
              onClick={() => onSelect(d)}
              className={cn(
                "flex w-full flex-col gap-1.5 rounded-md border px-3 py-2.5 text-left transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                selected
                  ? "border-primary/50 bg-accent/60"
                  : "border-transparent hover:border-border hover:bg-accent/40",
              )}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="flex min-w-0 items-center gap-1.5 text-sm font-medium text-foreground">
                  <span className="truncate">{d.environment_name}</span>
                  {d.kind === "rollback" ? (
                    <span className="shrink-0 rounded bg-muted px-1 text-[10px] font-medium uppercase text-muted-foreground">
                      rollback
                    </span>
                  ) : null}
                </span>
                <span className="shrink-0 font-mono text-xs text-muted-foreground">
                  {shortSha(d.commit_sha)}
                </span>
              </div>
              <div className="flex items-center justify-between gap-2">
                <StateBadge state={d.state} />
                <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
                  {d.from_environment_name ? (
                    <span className="inline-flex items-center gap-0.5">
                      {d.from_environment_name}
                      <ArrowUpRight className="h-3 w-3" aria-hidden />
                    </span>
                  ) : null}
                  {formatRelativeTime(d.requested_at, now)}
                </span>
              </div>
              <p className="truncate text-[11px] text-muted-foreground">
                by {actorLabel(d.initiated_by)}
              </p>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
