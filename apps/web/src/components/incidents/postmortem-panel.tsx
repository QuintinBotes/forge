import { CheckSquare, FileText, ListChecks } from "lucide-react";
import { Fragment, type ReactNode } from "react";

import type { PostmortemView } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { humanize } from "./incident-meta";

// --- Minimal, safe markdown rendering (headings / bullets / bold) --------- //
// Renders through React text nodes (never dangerouslySetInnerHTML) so the
// rendered postmortem cannot inject markup.

function renderInline(text: string, keyBase: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return (
        <strong key={`${keyBase}-${i}`} className="font-semibold text-foreground">
          {part.slice(2, -2)}
        </strong>
      );
    }
    return <Fragment key={`${keyBase}-${i}`}>{part}</Fragment>;
  });
}

/** Render a small markdown subset (h1-3, bullet lists, paragraphs). */
export function renderMarkdown(md: string): ReactNode {
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let list: string[] = [];

  const flushList = () => {
    if (list.length === 0) return;
    const items = list;
    blocks.push(
      <ul key={`ul-${blocks.length}`} className="ml-4 list-disc space-y-1">
        {items.map((item, i) => (
          <li key={i}>{renderInline(item, `li-${blocks.length}-${i}`)}</li>
        ))}
      </ul>,
    );
    list = [];
  };

  lines.forEach((raw, idx) => {
    const line = raw.trimEnd();
    if (/^#{1,6}\s/.test(line)) {
      flushList();
      const level = (line.match(/^#+/)?.[0].length ?? 1) as number;
      const text = line.replace(/^#+\s/, "");
      const cls =
        level <= 1
          ? "font-display text-base font-semibold text-foreground"
          : level === 2
            ? "font-display text-sm font-semibold text-foreground"
            : "text-sm font-semibold text-foreground";
      blocks.push(
        <p key={`h-${idx}`} className={cn("mt-3 first:mt-0", cls)}>
          {renderInline(text, `h-${idx}`)}
        </p>,
      );
    } else if (/^[-*]\s/.test(line)) {
      list.push(line.replace(/^[-*]\s/, ""));
    } else if (line.trim() === "") {
      flushList();
    } else {
      flushList();
      blocks.push(
        <p key={`p-${idx}`} className="text-sm leading-relaxed text-muted-foreground">
          {renderInline(line, `p-${idx}`)}
        </p>,
      );
    }
  });
  flushList();
  return <div className="space-y-2">{blocks}</div>;
}

// --- Panel ---------------------------------------------------------------- //

export interface PostmortemPanelProps {
  postmortem: PostmortemView | null | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
  /** Shown when publishing is available (postmortem generated, not yet published). */
  canPublish?: boolean;
  onPublish?: () => void;
  publishing?: boolean;
}

/**
 * The postmortem view — root cause, the rendered writeup and the extracted
 * action items (each a board task key). Empty until the incident reaches a
 * resolved/postmortem state, so the panel guides responders there.
 */
export function PostmortemPanel({
  postmortem,
  isLoading,
  isError,
  onRetry,
  canPublish,
  onPublish,
  publishing,
}: PostmortemPanelProps) {
  if (isLoading) {
    return <PostmortemSkeleton />;
  }
  if (isError) {
    return (
      <div
        role="alert"
        className="flex flex-col items-start gap-2 rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground"
      >
        Couldn&apos;t load the postmortem.
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
  if (!postmortem) {
    return (
      <div
        data-testid="postmortem-empty"
        className="flex flex-col items-center gap-2 rounded-md border border-dashed border-border p-8 text-center"
      >
        <FileText className="h-7 w-7 text-muted-foreground" />
        <p className="text-sm font-medium text-foreground">No postmortem yet</p>
        <p className="max-w-sm text-xs text-muted-foreground">
          A postmortem — root cause, timeline recap and action items — is
          generated once the incident is resolved.
        </p>
      </div>
    );
  }

  const actionItems = postmortem.action_item_task_keys;

  return (
    <div data-testid="postmortem" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] font-medium capitalize text-muted-foreground">
          {humanize(postmortem.status)}
        </span>
        {canPublish && onPublish ? (
          <button
            type="button"
            onClick={onPublish}
            disabled={publishing}
            className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-3 text-xs font-medium text-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
          >
            <CheckSquare className="h-3.5 w-3.5" />
            {publishing ? "Publishing…" : "Publish postmortem"}
          </button>
        ) : null}
      </div>

      {postmortem.root_cause ? (
        <div className="rounded-md border border-border bg-card p-3">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Root cause
          </p>
          <p className="mt-1 text-sm text-foreground">{postmortem.root_cause}</p>
        </div>
      ) : null}

      <section aria-label="Postmortem writeup" className="rounded-md border border-border bg-card p-4">
        {renderMarkdown(postmortem.content_md)}
      </section>

      <section aria-label="Action items">
        <div className="flex items-center gap-2">
          <ListChecks className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-semibold text-foreground">
            Action items
            <span className="ml-1.5 font-normal text-muted-foreground">
              ({actionItems.length})
            </span>
          </h3>
        </div>
        {actionItems.length === 0 ? (
          <p
            data-testid="action-items-empty"
            className="mt-2 text-xs text-muted-foreground"
          >
            No follow-up tasks were filed from this incident.
          </p>
        ) : (
          <ul className="mt-2 flex flex-wrap gap-2">
            {actionItems.map((key) => (
              <li
                key={key}
                data-testid="action-item"
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-muted px-2 py-1 font-mono text-xs text-foreground"
              >
                <CheckSquare className="h-3 w-3 text-muted-foreground" />
                {key}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function PostmortemSkeleton() {
  return (
    <div data-testid="postmortem-skeleton" aria-busy="true" className="flex flex-col gap-3">
      <div className="h-4 w-24 animate-pulse rounded bg-muted" />
      <div className="h-20 w-full animate-pulse rounded bg-muted/60" />
      <div className="h-3 w-1/2 animate-pulse rounded bg-muted" />
    </div>
  );
}
