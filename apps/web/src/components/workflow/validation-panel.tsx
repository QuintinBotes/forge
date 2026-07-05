"use client";

import { AlertTriangle, CheckCircle2, OctagonAlert } from "lucide-react";

import type {
  WorkflowValidationIssue,
  WorkflowValidationState,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import type { Selection } from "./selection";
import { errorCount, warningCount } from "./workflow-graph";

/** One validation issue as a compact, optionally-clickable row. */
export function IssueRow({
  issue,
  onSelect,
}: {
  issue: WorkflowValidationIssue;
  onSelect?: (selection: Selection) => void;
}) {
  const isError = issue.severity === "error";
  const Icon = isError ? OctagonAlert : AlertTriangle;
  const target: Selection | null = issue.edge_id
    ? { kind: "edge", id: issue.edge_id }
    : issue.node_id
      ? { kind: "node", id: issue.node_id }
      : null;
  const clickable = Boolean(onSelect && target);

  const body = (
    <>
      <Icon
        className={cn(
          "mt-0.5 h-4 w-4 shrink-0",
          isError ? "text-danger" : "text-warning",
        )}
        aria-hidden
      />
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="text-xs leading-snug text-foreground">
          {issue.message}
        </span>
        <span className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
          {issue.code}
          {issue.node_id ? ` · ${issue.node_id}` : ""}
        </span>
      </span>
    </>
  );

  if (clickable) {
    return (
      <li>
        <button
          type="button"
          data-testid="issue-row"
          onClick={() => target && onSelect?.(target)}
          className="flex w-full items-start gap-2 rounded-md border border-border bg-card px-2.5 py-2 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {body}
        </button>
      </li>
    );
  }

  return (
    <li
      data-testid="issue-row"
      className="flex items-start gap-2 rounded-md border border-border bg-card px-2.5 py-2"
    >
      {body}
    </li>
  );
}

export interface ValidationPanelProps {
  issues: WorkflowValidationIssue[];
  validationState: WorkflowValidationState;
  onSelectIssue?: (selection: Selection) => void;
}

/** Validation summary + the full issue list (errors first, then warnings). */
export function ValidationPanel({
  issues,
  validationState,
  onSelectIssue,
}: ValidationPanelProps) {
  const errors = errorCount(issues);
  const warnings = warningCount(issues);
  const ordered = [...issues].sort((a, b) => {
    if (a.severity === b.severity) return 0;
    return a.severity === "error" ? -1 : 1;
  });

  return (
    <div
      data-testid="validation-panel"
      className="flex min-h-0 flex-col gap-2"
    >
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Validation
        </h3>
        <div className="flex items-center gap-1.5">
          <span
            data-testid="error-count"
            className={cn(
              "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium",
              errors > 0
                ? "bg-danger/10 text-danger"
                : "bg-muted text-muted-foreground",
            )}
          >
            {errors} errors
          </span>
          <span
            data-testid="warning-count"
            className={cn(
              "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium",
              warnings > 0
                ? "bg-warning/10 text-warning"
                : "bg-muted text-muted-foreground",
            )}
          >
            {warnings} warnings
          </span>
        </div>
      </div>

      {issues.length === 0 ? (
        validationState === "valid" ? (
          <div
            data-testid="validation-clean"
            className="flex items-center gap-2 rounded-md border border-success/30 bg-success/5 px-3 py-2 text-xs text-success"
          >
            <CheckCircle2 className="h-4 w-4" aria-hidden />
            No issues. This workflow is ready to publish.
          </div>
        ) : (
          <p
            data-testid="validation-unchecked"
            className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground"
          >
            Save the draft or run Validate to check this workflow.
          </p>
        )
      ) : (
        <ul className="flex max-h-60 flex-col gap-1.5 overflow-y-auto pr-0.5">
          {ordered.map((issue, i) => (
            <IssueRow key={i} issue={issue} onSelect={onSelectIssue} />
          ))}
        </ul>
      )}
    </div>
  );
}
