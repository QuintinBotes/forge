"use client";

/**
 * The Conflicts tab: functional conflicts, unordered successions and
 * reasoning violations at the current world time. Focusing a conflict's
 * subject jumps back to Explore with that entity centred, so the competing
 * facts can be read in place.
 */

import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Loading, Skeleton } from "@/components/ui/skeleton";
import type { ConflictOut } from "@/lib/api/types";

import { errorDetail, shortId } from "./world-format";

export interface ConflictsPanelProps {
  conflicts?: ConflictOut[];
  isLoading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  /** Best known label for an entity id, when one has been loaded. */
  resolveLabel: (entityId: string) => string;
  onFocusEntity: (entityId: string) => void;
}

export function ConflictsPanel({
  conflicts,
  isLoading = false,
  error,
  onRetry,
  resolveLabel,
  onFocusEntity,
}: ConflictsPanelProps) {
  if (isLoading) {
    return (
      <Loading label="Loading conflicts…">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      </Loading>
    );
  }
  if (error) {
    return (
      <ErrorState
        title="Conflicts didn't load"
        description={errorDetail(error)}
        onRetry={onRetry}
      />
    );
  }
  if (!conflicts || conflicts.length === 0) {
    return (
      <EmptyState
        icon={<AlertTriangle aria-hidden />}
        title="No conflicts"
        description="Nothing in the graph contradicts itself at this world time."
      />
    );
  }
  return (
    <ul className="flex flex-col gap-3">
      {conflicts.map((conflict, index) => {
        const subjectId = conflict.subject_id;
        const subjectLabel = subjectId ? resolveLabel(subjectId) : null;
        return (
          <li
            key={`${conflict.kind}-${conflict.subject_id ?? ""}-${conflict.predicate_key ?? ""}-${index}`}
            className="flex flex-col gap-2 rounded-lg border border-border bg-card px-4 py-3"
          >
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-semibold text-foreground">
                {conflict.kind}
              </span>
              {conflict.predicate_key ? (
                <span className="text-muted-foreground">
                  {conflict.predicate_key}
                </span>
              ) : null}
              <span className="text-muted-foreground">
                {conflict.fact_ids.length}{" "}
                {conflict.fact_ids.length === 1 ? "fact" : "facts"}
              </span>
            </div>
            <p className="text-sm text-muted-foreground">{conflict.detail}</p>
            {subjectId ? (
              <div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => onFocusEntity(subjectId)}
                  aria-label={`Focus subject ${subjectLabel ?? shortId(subjectId)}`}
                >
                  Focus subject: {subjectLabel ?? shortId(subjectId)}
                </Button>
              </div>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
