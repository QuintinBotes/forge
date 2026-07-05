import {
  CircleDot,
  FileText,
  GitCommitVertical,
  Wrench,
  type LucideIcon,
} from "lucide-react";

import type { IncidentEventView } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { actorLabel, humanize, relativeTime } from "./incident-meta";

const KIND_ICON: Record<string, LucideIcon> = {
  state_change: GitCommitVertical,
  remediation_proposed: Wrench,
  note: FileText,
};

function eventIcon(kind: string): LucideIcon {
  return KIND_ICON[kind] ?? CircleDot;
}

export interface IncidentTimelineProps {
  events: IncidentEventView[] | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

/**
 * The incident's ordered event log — the response narrative. Each entry is one
 * FSM transition, remediation proposal or note, newest last so the current
 * lifecycle state sits at the foot of the rail.
 */
export function IncidentTimeline({
  events,
  isLoading,
  isError,
  onRetry,
}: IncidentTimelineProps) {
  if (isLoading) {
    return <TimelineSkeleton />;
  }
  if (isError) {
    return (
      <div
        role="alert"
        className="flex flex-col items-start gap-2 rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground"
      >
        Couldn&apos;t load the timeline.
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
  if (!events || events.length === 0) {
    return (
      <p
        data-testid="timeline-empty"
        className="rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground"
      >
        No events yet — activity will appear here as the incident is driven.
      </p>
    );
  }

  return (
    <ol data-testid="incident-timeline" className="flex flex-col">
      {events.map((event, i) => {
        const Icon = eventIcon(event.kind);
        const last = i === events.length - 1;
        return (
          <li key={event.id} className="flex gap-3">
            <div className="flex flex-col items-center">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-border bg-card text-muted-foreground">
                <Icon aria-hidden className="h-3.5 w-3.5" />
              </span>
              {!last ? <span className="w-px flex-1 bg-border" /> : null}
            </div>
            <div className={cn("min-w-0 pb-5", last && "pb-0")}>
              <p className="text-sm text-foreground">{event.summary}</p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                <span className="font-medium">{humanize(event.kind)}</span>
                {" · "}
                {actorLabel(event.actor)}
                {" · "}
                {relativeTime(event.created_at)}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function TimelineSkeleton() {
  return (
    <div data-testid="timeline-skeleton" aria-busy="true" className="flex flex-col gap-4">
      {[0, 1, 2].map((i) => (
        <div key={i} className="flex gap-3">
          <span className="h-7 w-7 shrink-0 animate-pulse rounded-full bg-muted" />
          <div className="flex flex-1 flex-col gap-1.5 pt-1">
            <div className="h-3 w-2/3 animate-pulse rounded bg-muted" />
            <div className="h-2.5 w-1/3 animate-pulse rounded bg-muted/60" />
          </div>
        </div>
      ))}
    </div>
  );
}
