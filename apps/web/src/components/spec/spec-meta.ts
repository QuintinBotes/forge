/**
 * Pure presentation helpers for the spec-validation dashboard.
 *
 * Kept framework-free (no JSX) so the lifecycle ordering, gate roll-ups and
 * token-class maps are unit-testable in isolation. Every colour is a Forge
 * design-token utility class (never a raw hex/rgb).
 */

import { SPEC_STATUSES } from "@/lib/api/types";
import type {
  RequirementTrace,
  SpecOverview,
  SpecStatus,
} from "@/lib/api/types";

export interface StageMeta {
  status: SpecStatus;
  label: string;
  /** One-line description of what reaching this stage means. */
  blurb: string;
}

/** The SDD lifecycle in order — the spine of the forge heat rail. */
export const LIFECYCLE_STAGES: readonly StageMeta[] = [
  { status: "draft", label: "Draft", blurb: "Requirements captured" },
  { status: "clarifying", label: "Clarifying", blurb: "Questions resolved" },
  { status: "approved", label: "Approved", blurb: "Human gate passed" },
  { status: "implementing", label: "Implementing", blurb: "Tasks in flight" },
  { status: "validated", label: "Validated", blurb: "Traceability sealed" },
  { status: "closed", label: "Closed", blurb: "Shipped & archived" },
];

/** Zero-based position of a status in the lifecycle (defaults to draft). */
export function stageIndex(status: SpecStatus | undefined): number {
  if (!status) return 0;
  const index = SPEC_STATUSES.indexOf(status);
  return index < 0 ? 0 : index;
}

export type StageState = "done" | "current" | "upcoming";

/** Where a lifecycle node sits relative to the spec's current stage. */
export function stageState(
  nodeIndex: number,
  status: SpecStatus | undefined,
): StageState {
  const current = stageIndex(status);
  if (nodeIndex < current) return "done";
  if (nodeIndex === current) return "current";
  return "upcoming";
}

export const STATUS_LABELS: Record<SpecStatus, string> = {
  draft: "Draft",
  clarifying: "Clarifying",
  approved: "Approved",
  implementing: "Implementing",
  validated: "Validated",
  closed: "Closed",
};

/** Token-class pill styling for a spec status (semantic, theme-aware). */
export function statusBadgeClass(status: SpecStatus | undefined): string {
  switch (status) {
    case "validated":
    case "closed":
      return "border-success/40 bg-success/10 text-success";
    case "approved":
    case "implementing":
      return "border-primary/40 bg-primary/10 text-primary";
    case "clarifying":
      return "border-warning/40 bg-warning/10 text-warning";
    case "draft":
    default:
      return "border-border bg-muted text-muted-foreground";
  }
}

/** The two statuses at (or before) the human approval gate. */
export function isApprovable(status: SpecStatus | undefined): boolean {
  return status === "draft" || status === "clarifying";
}

export interface GateSummary {
  /** Overall validation verdict; `null` when the spec has no report yet. */
  passed: boolean | null;
  /** Coverage as a 0–100 percentage, or `null` when unknown. */
  coverage: number | null;
  checksPassed: number;
  checksTotal: number;
  reqsSatisfied: number;
  reqsTotal: number;
  /** Unresolved open questions still blocking clarification. */
  openQuestions: number;
  hasValidation: boolean;
}

/** Normalise a coverage float (0–1 fraction or 0–100 percent) to a percent. */
export function coveragePercent(
  coverage: number | null | undefined,
): number | null {
  if (coverage == null || Number.isNaN(coverage)) return null;
  const pct = coverage <= 1 ? coverage * 100 : coverage;
  return Math.max(0, Math.min(100, Math.round(pct)));
}

export function formatCoverage(
  coverage: number | null | undefined,
): string {
  const pct = coveragePercent(coverage);
  return pct == null ? "—" : `${pct}%`;
}

/** Roll a spec's manifest + validation report up into the gate tiles' inputs. */
export function gateSummary(spec: SpecOverview): GateSummary {
  const report = spec.validation ?? null;
  const traces = report?.traceability ?? [];
  const checks = report?.checks ?? [];
  const reqsTotal = traces.length || spec.requirements?.length || 0;
  const reqsSatisfied = traces.filter((t) => t.satisfied).length;
  const openQuestions = (spec.open_questions ?? []).filter(
    (q) => !q.resolution,
  ).length;
  return {
    passed: report ? Boolean(report.passed) : null,
    coverage: coveragePercent(report?.coverage),
    checksPassed: checks.filter((c) => c.passed).length,
    checksTotal: checks.length,
    reqsSatisfied,
    reqsTotal,
    openQuestions,
    hasValidation: report != null,
  };
}

/** A satisfied requirement is only truly "sealed" when it also has tests. */
export function traceSealed(trace: RequirementTrace): boolean {
  return Boolean(trace.satisfied) && (trace.test_refs?.length ?? 0) > 0;
}
