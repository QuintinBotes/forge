"use client";

import { AlertTriangle, Check, User } from "lucide-react";
import { useState, type ReactNode } from "react";

import type {
  IncidentDetailView,
  IncidentEventView,
  PostmortemView,
  RemediationPlanView,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { BlastRadiusBadge, LifecycleBadge, SeverityBadge } from "./incident-badges";
import { IncidentTimeline } from "./incident-timeline";
import {
  actorLabel,
  eventMeta,
  relativeTime,
  type EventIntent,
} from "./incident-meta";
import { PostmortemPanel } from "./postmortem-panel";
import { RemediationPanel } from "./remediation-panel";

type TabKey = "timeline" | "remediation" | "postmortem";

const INTENT_CLASS: Record<EventIntent, string> = {
  advance:
    "border-border text-foreground hover:bg-accent hover:text-accent-foreground",
  approve:
    "border-success/40 text-success hover:bg-success/10",
  danger: "border-danger/40 text-danger hover:bg-danger/10",
};

interface QueryProps<T> {
  data: T;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

export interface IncidentDetailProps {
  detail: IncidentDetailView | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
  timeline: QueryProps<IncidentEventView[] | undefined>;
  remediation: QueryProps<RemediationPlanView | null | undefined>;
  postmortem: QueryProps<PostmortemView | null | undefined> & {
    canPublish?: boolean;
    onPublish?: () => void;
    publishing?: boolean;
  };
  actions: {
    allowedEvents: string[];
    onEvent: (event: string) => void;
    pending: boolean;
    error: string | null;
  };
}

/**
 * The incident detail pane: identity + lifecycle/severity/blast-radius badges,
 * the FSM action bar (allowed events), and the timeline / remediation /
 * postmortem tabs. Presentational — all data + handlers are passed in.
 */
export function IncidentDetail({
  detail,
  isLoading,
  isError,
  onRetry,
  timeline,
  remediation,
  postmortem,
  actions,
}: IncidentDetailProps) {
  const [tab, setTab] = useState<TabKey>("timeline");
  // Reset to the timeline whenever a different incident is selected — done as a
  // render-time state adjustment (React's recommended alternative to a reset
  // effect) so the tab is correct on the very first render of the new incident.
  const [seenId, setSeenId] = useState<string | undefined>(detail?.id);
  if (detail?.id !== seenId) {
    setSeenId(detail?.id);
    setTab("timeline");
  }

  if (isLoading || !detail) {
    if (isError) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-2 p-10 text-center">
          <AlertTriangle className="h-7 w-7 text-danger" />
          <p className="text-sm text-muted-foreground">
            Couldn&apos;t load this incident.
          </p>
          {onRetry ? (
            <button
              type="button"
              onClick={onRetry}
              className="rounded-md border border-border px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Retry
            </button>
          ) : null}
        </div>
      );
    }
    return <DetailSkeleton />;
  }

  const stepCount = remediation.data?.steps.length ?? detail.remediation_plan?.steps.length ?? 0;
  const hasPostmortem = Boolean(postmortem.data);

  return (
    <div data-testid="incident-detail" className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <div className="flex flex-col gap-3 border-b border-border px-6 py-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="font-mono text-xs text-muted-foreground">{detail.key}</p>
            <h2 className="mt-0.5 truncate font-display text-lg font-semibold leading-tight">
              {detail.title}
            </h2>
          </div>
          <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
            <SeverityBadge severity={detail.severity} />
            <LifecycleBadge state={detail.lifecycle_state} />
            <BlastRadiusBadge radius={detail.blast_radius} />
          </div>
        </div>

        {detail.description ? (
          <p className="text-sm text-muted-foreground">{detail.description}</p>
        ) : null}

        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>
            Source <span className="text-foreground">{detail.source}</span>
          </span>
          <span className="inline-flex items-center gap-1">
            <User className="h-3 w-3" />
            {detail.commander_id
              ? `Commander ${actorLabel(`user:${detail.commander_id}`)}`
              : "No commander assigned"}
          </span>
          <span>Declared {relativeTime(detail.created_at)}</span>
          {detail.acknowledged_at ? (
            <span>Ack&apos;d {relativeTime(detail.acknowledged_at)}</span>
          ) : null}
          {detail.resolved_at ? (
            <span className="text-success">
              Resolved {relativeTime(detail.resolved_at)}
            </span>
          ) : null}
        </div>

        {/* FSM action bar */}
        <ActionBar
          allowedEvents={actions.allowedEvents}
          onEvent={actions.onEvent}
          pending={actions.pending}
          error={actions.error}
        />
      </div>

      {/* Tabs */}
      <div role="tablist" aria-label="Incident detail" className="flex gap-1 border-b border-border px-4">
        <TabButton active={tab === "timeline"} onClick={() => setTab("timeline")}>
          Timeline
          {detail.event_count > 0 ? <TabCount>{detail.event_count}</TabCount> : null}
        </TabButton>
        <TabButton active={tab === "remediation"} onClick={() => setTab("remediation")}>
          Remediation
          {stepCount > 0 ? <TabCount>{stepCount}</TabCount> : null}
        </TabButton>
        <TabButton active={tab === "postmortem"} onClick={() => setTab("postmortem")}>
          Postmortem
          {hasPostmortem ? (
            <span
              aria-hidden
              className="ml-1.5 h-1.5 w-1.5 rounded-full bg-success"
            />
          ) : null}
        </TabButton>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-6" role="tabpanel">
        {tab === "timeline" ? (
          <IncidentTimeline
            events={timeline.data}
            isLoading={timeline.isLoading}
            isError={timeline.isError}
            onRetry={timeline.onRetry}
          />
        ) : null}
        {tab === "remediation" ? (
          <RemediationPanel
            plan={remediation.data}
            isLoading={remediation.isLoading}
            isError={remediation.isError}
            onRetry={remediation.onRetry}
          />
        ) : null}
        {tab === "postmortem" ? (
          <PostmortemPanel
            postmortem={postmortem.data}
            isLoading={postmortem.isLoading}
            isError={postmortem.isError}
            onRetry={postmortem.onRetry}
            canPublish={postmortem.canPublish}
            onPublish={postmortem.onPublish}
            publishing={postmortem.publishing}
          />
        ) : null}
      </div>
    </div>
  );
}

// --- Action bar ----------------------------------------------------------- //

function ActionBar({
  allowedEvents,
  onEvent,
  pending,
  error,
}: {
  allowedEvents: string[];
  onEvent: (event: string) => void;
  pending: boolean;
  error: string | null;
}) {
  if (allowedEvents.length === 0 && !error) {
    return (
      <p className="text-xs text-muted-foreground">
        No further actions — this incident has reached a terminal state.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <div data-testid="action-bar" className="flex flex-wrap items-center gap-2">
        {allowedEvents.map((event) => {
          const meta = eventMeta(event);
          return (
            <button
              key={event}
              type="button"
              data-event={event}
              disabled={pending}
              onClick={() => onEvent(event)}
              className={cn(
                "inline-flex h-8 items-center gap-1.5 rounded-md border px-3 text-xs font-medium transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                "disabled:pointer-events-none disabled:opacity-50",
                INTENT_CLASS[meta.intent],
              )}
            >
              {meta.intent === "approve" ? <Check className="h-3.5 w-3.5" /> : null}
              {meta.label}
            </button>
          );
        })}
      </div>
      {error ? (
        <p role="alert" className="text-xs text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}

// --- Tabs primitives ------------------------------------------------------ //

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "-mb-px inline-flex items-center border-b-2 px-3 py-2.5 text-sm font-medium transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        active
          ? "border-primary text-foreground"
          : "border-transparent text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function TabCount({ children }: { children: ReactNode }) {
  return (
    <span className="ml-1.5 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
      {children}
    </span>
  );
}

function DetailSkeleton() {
  return (
    <div data-testid="detail-skeleton" aria-busy="true" className="flex flex-col gap-4 p-6">
      <div className="h-3 w-16 animate-pulse rounded bg-muted" />
      <div className="h-5 w-2/3 animate-pulse rounded bg-muted" />
      <div className="flex gap-2">
        <div className="h-5 w-16 animate-pulse rounded-full bg-muted" />
        <div className="h-5 w-24 animate-pulse rounded-full bg-muted" />
      </div>
      <div className="h-24 w-full animate-pulse rounded bg-muted/60" />
    </div>
  );
}
