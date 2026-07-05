"use client";

import { Lock, Plus, Workflow } from "lucide-react";

import type { WorkflowDefinitionSummary } from "@/lib/api/types";
import { cn } from "@/lib/utils";

export interface DefinitionRailProps {
  definitions: WorkflowDefinitionSummary[];
  selectedName: string | null;
  isLoading: boolean;
  isError: boolean;
  onSelect: (name: string) => void;
  onNew: () => void;
}

/** Left rail: every workflow definition, badged by origin + draft/published. */
export function DefinitionRail({
  definitions,
  selectedName,
  isLoading,
  isError,
  onSelect,
  onNew,
}: DefinitionRailProps) {
  return (
    <div className="flex min-h-0 flex-col gap-2">
      <div className="flex items-center justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Workflows
        </h2>
        <button
          type="button"
          data-testid="new-workflow"
          onClick={onNew}
          className="inline-flex h-7 items-center gap-1 rounded-md border border-border px-2 text-xs font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Plus className="h-3.5 w-3.5" aria-hidden />
          New
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-border bg-card/40 p-1.5">
        {isLoading ? (
          <RailSkeleton />
        ) : definitions.length === 0 ? (
          <EmptyRail isError={isError} onNew={onNew} />
        ) : (
          <ul role="list" className="flex flex-col gap-1">
            {definitions.map((def) => {
              const active = def.name === selectedName;
              return (
                <li key={def.name}>
                  <button
                    type="button"
                    data-testid={`definition-${def.name}`}
                    aria-current={active ? "true" : undefined}
                    onClick={() => onSelect(def.name)}
                    className={cn(
                      "flex w-full flex-col gap-1 rounded-md border px-2.5 py-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      active
                        ? "border-primary/40 bg-accent"
                        : "border-transparent hover:bg-accent/60",
                    )}
                  >
                    <div className="flex items-center gap-1.5">
                      <Workflow
                        className={cn(
                          "h-3.5 w-3.5 shrink-0",
                          active ? "text-primary" : "text-muted-foreground",
                        )}
                        aria-hidden
                      />
                      <span className="truncate text-sm font-medium text-foreground">
                        {def.title}
                      </span>
                    </div>
                    <span className="truncate font-mono text-[11px] text-muted-foreground">
                      {def.name}
                    </span>
                    <div className="flex flex-wrap items-center gap-1">
                      <OriginBadge origin={def.origin} />
                      {def.has_draft ? (
                        <Badge className="border-warning/40 bg-warning/10 text-warning">
                          Draft
                        </Badge>
                      ) : null}
                      {def.published_revision != null &&
                      def.published_revision > 0 ? (
                        <Badge className="border-border bg-muted text-muted-foreground">
                          v{def.published_revision}
                        </Badge>
                      ) : null}
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

function OriginBadge({
  origin,
}: {
  origin: WorkflowDefinitionSummary["origin"];
}) {
  if (origin === "bundled") {
    return (
      <Badge className="border-border bg-muted text-muted-foreground">
        <Lock className="h-2.5 w-2.5" aria-hidden />
        Bundled
      </Badge>
    );
  }
  if (origin === "bundled_fork") {
    return (
      <Badge className="border-border bg-muted text-muted-foreground">Fork</Badge>
    );
  }
  return (
    <Badge className="border-primary/30 bg-primary/10 text-primary">Custom</Badge>
  );
}

function Badge({
  className,
  children,
}: {
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium",
        className,
      )}
    >
      {children}
    </span>
  );
}

function EmptyRail({
  isError,
  onNew,
}: {
  isError: boolean;
  onNew: () => void;
}) {
  return (
    <div className="flex flex-col items-center gap-2 px-3 py-8 text-center">
      <Workflow className="h-6 w-6 text-muted-foreground" aria-hidden />
      <p className="text-xs text-muted-foreground">
        {isError
          ? "Couldn't load workflows. Retry shortly."
          : "No workflows yet."}
      </p>
      {!isError ? (
        <button
          type="button"
          onClick={onNew}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-3 text-xs font-medium text-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Plus className="h-3.5 w-3.5" aria-hidden />
          New workflow
        </button>
      ) : null}
    </div>
  );
}

function RailSkeleton() {
  return (
    <div
      data-testid="rail-skeleton"
      aria-busy="true"
      className="flex flex-col gap-1"
    >
      {Array.from({ length: 5 }, (_, i) => (
        <div key={i} className="flex flex-col gap-1.5 rounded-md px-2.5 py-2">
          <div className="h-3.5 w-2/3 animate-pulse rounded bg-muted" />
          <div className="h-2.5 w-1/2 animate-pulse rounded bg-muted/60" />
        </div>
      ))}
    </div>
  );
}
