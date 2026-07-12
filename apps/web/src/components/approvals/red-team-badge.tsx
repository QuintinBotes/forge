"use client";

import { ShieldAlert, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { useRedTeamVerdict } from "@/lib/api/approvals";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import type { RedTeamRecordOut } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { humanizeKey } from "./format";

export interface RedTeamBadgeProps {
  /**
   * The gate's linked workflow run (`ApprovalContext.run_trace_ref.workflow_run_id`)
   * — the same id the Red-Team Gate scans before this human gate opens.
   * Absent -> nothing renders.
   */
  workflowRunId?: string | null;
  client?: ForgeApiClient;
}

function scalar(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/**
 * The Red-Team Gate verdict badge (Red-Team Gate, slice redteam-surface).
 *
 * Before a spec/PR reaches this human gate, a distinct adversary (a
 * heterogeneous model, sandboxed) attacks the candidate diff and either
 * BLOCKS it — an *executed* failing test or a structured spec-violation — or
 * the change SURVIVES. Renders nothing while the run id is unknown or no scan
 * has landed yet: that is a normal state (parked / not yet reached), not an
 * error, so the review panel stays quiet rather than showing a placeholder.
 */
export function RedTeamBadge({ workflowRunId, client = apiClient }: RedTeamBadgeProps) {
  const [expanded, setExpanded] = useState(false);
  const query = useRedTeamVerdict(workflowRunId, client);
  const latest = query.data?.latest;

  if (!workflowRunId || !latest) {
    return null;
  }

  const blocked = latest.verdict === "blocked";
  const Icon = blocked ? ShieldAlert : ShieldCheck;

  return (
    <div data-testid="red-team-badge" data-verdict={latest.verdict} className="flex flex-col gap-2">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
        className={cn(
          "inline-flex w-fit items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          blocked
            ? "border-danger/40 bg-danger/10 text-danger"
            : "border-success/40 bg-success/10 text-success",
        )}
      >
        <Icon aria-hidden className="h-3.5 w-3.5" />
        {blocked ? "Blocked by red-team adversary" : "Survived adversarial review"}
      </button>

      {expanded ? <RedTeamEvidence record={latest} /> : null}
    </div>
  );
}

function RedTeamEvidence({ record }: { record: RedTeamRecordOut }) {
  const entries = Object.entries(record.evidence ?? {});
  return (
    <div
      data-testid="red-team-evidence"
      className="flex flex-col gap-1.5 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs"
    >
      <div className="flex items-center justify-between gap-2 text-muted-foreground">
        <span>{humanizeKey(record.kind)}</span>
        {record.adversary_model ? (
          <span className="truncate font-mono text-[11px]">
            adversary: {record.adversary_model}
          </span>
        ) : null}
      </div>
      {entries.length > 0 ? (
        <dl className="flex flex-col gap-1">
          {entries.map(([key, value]) => (
            <div key={key} className="flex items-start justify-between gap-3">
              <dt className="shrink-0 text-muted-foreground">{humanizeKey(key)}</dt>
              <dd className="min-w-0 truncate text-right font-mono text-foreground">
                {scalar(value)}
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="text-muted-foreground">No evidence recorded.</p>
      )}
    </div>
  );
}
