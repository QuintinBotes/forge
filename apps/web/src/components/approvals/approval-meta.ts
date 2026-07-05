/**
 * Presentation metadata for the approval inbox: gate/status/risk labels, the
 * keyboard-shortcut map for decisions, and token-only styling helpers. Kept
 * separate from the components so the maps are unit-testable and the JSX stays
 * about layout.
 */

import {
  FileText,
  GitPullRequest,
  ListChecks,
  Rocket,
  ShieldAlert,
  Siren,
  type LucideIcon,
} from "lucide-react";

import type {
  ApprovalAction,
  GateStatus,
  GateType,
  RiskLevel,
} from "@/lib/api/types";

export interface GateMeta {
  label: string;
  icon: LucideIcon;
}

export const GATE_META: Record<GateType, GateMeta> = {
  spec: { label: "Spec", icon: FileText },
  plan: { label: "Plan", icon: ListChecks },
  pr: { label: "Pull request", icon: GitPullRequest },
  deploy: { label: "Deploy", icon: Rocket },
  incident_remediation: { label: "Incident remediation", icon: Siren },
  policy_override: { label: "Policy override", icon: ShieldAlert },
};

export function gateMeta(gate: GateType): GateMeta {
  return GATE_META[gate] ?? { label: gate, icon: FileText };
}

export const STATUS_LABELS: Record<GateStatus, string> = {
  pending: "Pending",
  approved: "Approved",
  rejected: "Rejected",
  changes_requested: "Changes requested",
  expired: "Expired",
};

/** Token-only badge classes per gate status. */
export function statusBadgeClass(status: GateStatus): string {
  switch (status) {
    case "approved":
      return "border-success/40 bg-success/10 text-success";
    case "rejected":
      return "border-danger/40 bg-danger/10 text-danger";
    case "changes_requested":
      return "border-warning/40 bg-warning/10 text-warning";
    case "expired":
      return "border-border bg-muted text-muted-foreground";
    default:
      return "border-spark/40 bg-spark/10 text-foreground";
  }
}

export const RISK_LABELS: Record<RiskLevel, string> = {
  info: "Info",
  warning: "Warning",
  critical: "Critical",
};

export function riskLabel(level: string): string {
  return RISK_LABELS[level as RiskLevel] ?? level;
}

/** Token-only styling for a risk level (badges, left rails, dots). */
export function riskBadgeClass(level: string): string {
  switch (level) {
    case "critical":
      return "border-danger/40 bg-danger/10 text-danger";
    case "warning":
      return "border-warning/40 bg-warning/10 text-warning";
    default:
      return "border-border bg-muted text-muted-foreground";
  }
}

/** Solid dot / rail color per risk level (queue accents). */
export function riskDotClass(level: string): string {
  switch (level) {
    case "critical":
      return "bg-danger";
    case "warning":
      return "bg-warning";
    default:
      return "bg-muted-foreground/50";
  }
}

export type DecisionVariant = "approve" | "reject" | "request_changes" | "escalate";

export interface ActionMeta {
  action: ApprovalAction;
  label: string;
  /** Single-key shortcut (spec: a/r/x/e). */
  shortcut: string;
  /** True when the action must collect a reason before it is sent. */
  requiresNote: boolean;
}

/**
 * The decision keyboard map (spec: "a/r/x/e keyboard shortcuts").
 *   a → approve · x → reject · r → request changes · e → escalate
 */
export const ACTION_META: Record<ApprovalAction, ActionMeta> = {
  approve: { action: "approve", label: "Approve", shortcut: "a", requiresNote: false },
  reject: { action: "reject", label: "Reject", shortcut: "x", requiresNote: true },
  request_changes: {
    action: "request_changes",
    label: "Request changes",
    shortcut: "r",
    requiresNote: true,
  },
  escalate: { action: "escalate", label: "Escalate", shortcut: "e", requiresNote: false },
};

/** Resolve the action bound to a pressed key, or null. */
export function actionForKey(key: string): ApprovalAction | null {
  const lower = key.toLowerCase();
  for (const meta of Object.values(ACTION_META)) {
    if (meta.shortcut === lower) {
      return meta.action;
    }
  }
  return null;
}

/** The canonical display order of decision actions. */
export const ACTION_ORDER: ApprovalAction[] = [
  "approve",
  "request_changes",
  "reject",
  "escalate",
];
