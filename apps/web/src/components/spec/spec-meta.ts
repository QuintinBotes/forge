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
  ValidationReport,
} from "@/lib/api/types";

export type StageState = "done" | "current" | "upcoming";

export const STATUS_LABELS: Record<SpecStatus, string> = {
  draft: "Draft",
  clarifying: "Clarifying",
  changes_requested: "Changes requested",
  rejected: "Rejected",
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
    case "changes_requested":
      return "border-warning/40 bg-warning/10 text-warning";
    case "rejected":
      return "border-danger/40 bg-danger/10 text-danger";
    case "draft":
    default:
      return "border-border bg-muted text-muted-foreground";
  }
}

/**
 * The statuses at (or before) the human approval gate. Rejected /
 * changes-requested specs stay reviewable — the decision can be revised
 * (mirroring the engine's `REVIEWABLE_STATUSES` gate).
 */
export function isApprovable(status: SpecStatus | undefined): boolean {
  return (
    status === "draft" ||
    status === "clarifying" ||
    status === "changes_requested" ||
    status === "rejected"
  );
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

// --------------------------------------------------------------------------- //
// Plain-language lifecycle stepper (ss-lifecycle)                             //
//                                                                              //
// The SDD lifecycle wired inline as five everyday verbs, each backed by one   //
// `/spec` engine action: Describe<-Clarify, Refine<-Plan, Approve<-Approve,   //
// Build<-Generate tasks, Verify<-Validate. `SpecStatus` alone can't place a   //
// spec on this rail (the engine never sets an "implementing"/"planned"       //
// status — `plan`/`tasks` just populate `plan_ref`/`tasks_ref`), so           //
// completion is read straight off the manifest fields each action produces.  //
// --------------------------------------------------------------------------- //

export interface PlainStepMeta {
  id: string;
  label: string;
  blurb: string;
  /** The `/spec` engine action this step's inline button runs. */
  actionLabel: string;
}

export const PLAIN_LIFECYCLE_STEPS: readonly PlainStepMeta[] = [
  { id: "describe", label: "Describe", blurb: "Requirements captured", actionLabel: "Clarify" },
  { id: "refine", label: "Refine", blurb: "Questions & plan resolved", actionLabel: "Plan" },
  { id: "approve", label: "Approve", blurb: "Human gate passed", actionLabel: "Approve" },
  { id: "build", label: "Build", blurb: "Tasks generated", actionLabel: "Generate tasks" },
  { id: "verify", label: "Verify", blurb: "Traceability sealed", actionLabel: "Validate" },
];

/** The manifest fields the stepper needs to place a spec on the rail. */
export interface PlainStepInput {
  status?: SpecStatus;
  plan_ref?: string | null;
  tasks_ref?: string | null;
  validation?: ValidationReport | null;
}

function statusAtLeast(status: SpecStatus | undefined, floor: SpecStatus): boolean {
  if (!status) return false;
  return SPEC_STATUSES.indexOf(status) >= SPEC_STATUSES.indexOf(floor);
}

/** Whether each of the five plain steps' underlying action has run. */
export function plainStepCompletion(spec: PlainStepInput): boolean[] {
  const describeDone = statusAtLeast(spec.status, "clarifying");
  const refineDone = Boolean(spec.plan_ref);
  const approveDone = statusAtLeast(spec.status, "approved");
  const buildDone = Boolean(spec.tasks_ref);
  const verifyDone =
    spec.status === "validated" || spec.status === "closed" || spec.validation?.passed === true;
  return [describeDone, refineDone, approveDone, buildDone, verifyDone];
}

/** The first not-yet-complete step, or the last step once everything is done. */
export function plainCurrentStep(completion: boolean[]): number {
  const index = completion.findIndex((done) => !done);
  return index === -1 ? completion.length - 1 : index;
}

/** Where a plain-language node sits relative to the stepper's current step. */
export function plainStepState(
  index: number,
  completion: boolean[],
  current: number,
): StageState {
  if (completion[index]) return "done";
  if (index === current) return "current";
  return "upcoming";
}
