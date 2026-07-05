/**
 * Presentation metadata for the F31 deployment-gates surface (pure + testable).
 *
 * Maps the deployment domain vocabulary — FSM state, gate-check name + status,
 * health — onto Forge design-token classes, labels and icons, plus the small
 * decision helpers the view leans on (which actions a state allows, the default
 * selection, the next promotion target). No JSX here so the maps stay trivially
 * unit-testable; icons are carried as component refs.
 */

import {
  ArrowRightLeft,
  CheckCircle2,
  CircleDashed,
  CircleDot,
  Clock,
  FileCheck2,
  GitCommitHorizontal,
  Hourglass,
  Loader,
  MinusCircle,
  ShieldCheck,
  ShieldX,
  Snowflake,
  Stethoscope,
  Undo2,
  XCircle,
  type LucideIcon,
} from "lucide-react";

import {
  TERMINAL_DEPLOYMENT_STATES,
  type DeploymentRead,
  type DeploymentState,
  type EnvironmentRead,
  type GateCheckName,
  type GateCheckStatus,
  type HealthStatus,
} from "@/lib/api/types";

// --- Tones ---------------------------------------------------------------- //

export type Tone = "success" | "warning" | "danger" | "muted" | "info";

/** Pill/badge classes for a tone (border + tinted surface + on-tint text). */
export function toneBadgeClass(tone: Tone): string {
  switch (tone) {
    case "success":
      return "border-success/40 bg-success/10 text-success";
    case "warning":
      return "border-warning/40 bg-warning/10 text-warning";
    case "danger":
      return "border-danger/40 bg-danger/10 text-danger";
    case "info":
      return "border-primary/40 bg-primary/10 text-primary";
    case "muted":
    default:
      return "border-border bg-muted text-muted-foreground";
  }
}

/** A solid status dot for a tone (rows / connectors). */
export function toneDotClass(tone: Tone): string {
  switch (tone) {
    case "success":
      return "bg-success";
    case "warning":
      return "bg-warning";
    case "danger":
      return "bg-danger";
    case "info":
      return "bg-primary";
    case "muted":
    default:
      return "bg-muted-foreground/50";
  }
}

// --- Deployment state ----------------------------------------------------- //

export interface StateMeta {
  label: string;
  tone: Tone;
  icon: LucideIcon;
  /** Higher = more operator attention (list ranking). */
  attention: number;
  /** True while the FSM is mid-flight (spin the icon). */
  active: boolean;
}

const STATE_META: Record<DeploymentState, StateMeta> = {
  requested: { label: "Requested", tone: "muted", icon: GitCommitHorizontal, attention: 3, active: false },
  gate_evaluating: { label: "Evaluating gate", tone: "warning", icon: Hourglass, attention: 4, active: true },
  awaiting_approval: { label: "Awaiting approval", tone: "warning", icon: Clock, attention: 6, active: false },
  approved: { label: "Approved", tone: "info", icon: ShieldCheck, attention: 5, active: false },
  deploying: { label: "Deploying", tone: "info", icon: Loader, attention: 5, active: true },
  verifying: { label: "Verifying", tone: "info", icon: Stethoscope, attention: 5, active: true },
  succeeded: { label: "Succeeded", tone: "success", icon: CheckCircle2, attention: 1, active: false },
  failed: { label: "Failed", tone: "danger", icon: XCircle, attention: 2, active: false },
  gate_rejected: { label: "Gate rejected", tone: "danger", icon: ShieldX, attention: 2, active: false },
  rolling_back: { label: "Rolling back", tone: "warning", icon: Undo2, attention: 5, active: true },
  rolled_back: { label: "Rolled back", tone: "muted", icon: Undo2, attention: 1, active: false },
  cancelled: { label: "Cancelled", tone: "muted", icon: MinusCircle, attention: 0, active: false },
};

export function stateMeta(state: DeploymentState): StateMeta {
  return STATE_META[state] ?? STATE_META.requested;
}

const TERMINAL = new Set<DeploymentState>(TERMINAL_DEPLOYMENT_STATES);

export function isTerminalState(state: DeploymentState): boolean {
  return TERMINAL.has(state);
}

// --- Action availability (mirrors the deployments router's FSM guards) ---- //

/** A gated deployment awaiting a human decision can be approved/rejected. */
export function canDecide(state: DeploymentState): boolean {
  return state === "awaiting_approval";
}

/** Any non-terminal deployment can be cancelled. */
export function canCancel(state: DeploymentState): boolean {
  return !isTerminalState(state);
}

/** Only a live (succeeded) deployment can be rolled back. */
export function canRollback(state: DeploymentState): boolean {
  return state === "succeeded";
}

// --- Health --------------------------------------------------------------- //

export interface HealthMeta {
  label: string;
  tone: Tone;
  icon: LucideIcon;
}

const HEALTH_META: Record<HealthStatus, HealthMeta> = {
  passing: { label: "Healthy", tone: "success", icon: CheckCircle2 },
  failing: { label: "Failing", tone: "danger", icon: XCircle },
  unknown: { label: "Unknown", tone: "muted", icon: CircleDashed },
};

export function healthMeta(status: HealthStatus | null | undefined): HealthMeta {
  return HEALTH_META[status ?? "unknown"] ?? HEALTH_META.unknown;
}

// --- Gate checks ---------------------------------------------------------- //

export interface CheckNameMeta {
  label: string;
  description: string;
  icon: LucideIcon;
}

const CHECK_NAME_META: Record<GateCheckName, CheckNameMeta> = {
  policy_allows: {
    label: "Policy",
    description: "Deployment policy permits this environment for the actor.",
    icon: ShieldCheck,
  },
  predecessor_succeeded: {
    label: "Predecessor",
    description: "The previous stage has a successful deployment of this commit.",
    icon: ArrowRightLeft,
  },
  ci_green: {
    label: "CI green",
    description: "Continuous-integration checks are passing for the commit.",
    icon: CheckCircle2,
  },
  spec_validated: {
    label: "Spec validated",
    description: "The spec gate has validated the change set.",
    icon: FileCheck2,
  },
  security_clean: {
    label: "Security",
    description: "No blocking security findings for the commit.",
    icon: ShieldCheck,
  },
  not_frozen: {
    label: "Not frozen",
    description: "No release-freeze window is currently active.",
    icon: Snowflake,
  },
};

export function checkNameMeta(name: GateCheckName): CheckNameMeta {
  return (
    CHECK_NAME_META[name] ?? {
      label: String(name),
      description: "",
      icon: CircleDot,
    }
  );
}

export interface CheckStatusMeta {
  label: string;
  tone: Tone;
  icon: LucideIcon;
}

const CHECK_STATUS_META: Record<GateCheckStatus, CheckStatusMeta> = {
  passed: { label: "Passed", tone: "success", icon: CheckCircle2 },
  failed: { label: "Failed", tone: "danger", icon: XCircle },
  pending: { label: "Pending", tone: "warning", icon: Clock },
  skipped: { label: "Skipped", tone: "muted", icon: MinusCircle },
};

export function checkStatusMeta(status: GateCheckStatus): CheckStatusMeta {
  return CHECK_STATUS_META[status] ?? CHECK_STATUS_META.pending;
}

// --- Environment / pipeline helpers --------------------------------------- //

/** Stages in promotion order (dev -> staging -> prod), lowest rank first. */
export function sortEnvironmentsByRank(
  environments: readonly EnvironmentRead[],
): EnvironmentRead[] {
  return [...environments].sort((a, b) => a.rank - b.rank);
}

/**
 * The next stage after `fromEnvName` in rank order — the default promotion
 * target. Falls back to the first stage when the source is unknown/last.
 */
export function nextEnvironmentName(
  environments: readonly EnvironmentRead[],
  fromEnvName?: string | null,
): string | null {
  const ranked = sortEnvironmentsByRank(environments);
  if (ranked.length === 0) return null;
  if (!fromEnvName) return ranked[0].name;
  const idx = ranked.findIndex((e) => e.name === fromEnvName);
  if (idx < 0) return ranked[0].name;
  return ranked[idx + 1]?.name ?? null;
}

// --- Deployment list helpers ---------------------------------------------- //

/**
 * The deployment the operator most likely wants selected: the one most in need
 * of attention (awaiting approval first), breaking ties by most-recent request.
 */
export function pickDefaultDeploymentId(
  deployments: readonly DeploymentRead[],
): string | null {
  if (deployments.length === 0) return null;
  const ranked = sortDeploymentsForQueue(deployments);
  return ranked[0]?.id ?? null;
}

/** Attention-ranked, then most-recent-first (stable list ordering). */
export function sortDeploymentsForQueue(
  deployments: readonly DeploymentRead[],
): DeploymentRead[] {
  return [...deployments].sort((a, b) => {
    const weight = stateMeta(b.state).attention - stateMeta(a.state).attention;
    if (weight !== 0) return weight;
    return (b.requested_at ?? "").localeCompare(a.requested_at ?? "");
  });
}

// --- Formatting ----------------------------------------------------------- //

/** A short, mono-friendly commit ref (first 7 chars). */
export function shortSha(sha: string | null | undefined): string {
  if (!sha) return "—";
  return sha.length > 7 ? sha.slice(0, 7) : sha;
}

/** Strip a `user:`/`agent:` actor prefix for a cleaner display label. */
export function actorLabel(actor: string | null | undefined): string {
  if (!actor) return "—";
  const idx = actor.indexOf(":");
  return idx >= 0 ? actor.slice(idx + 1) : actor;
}

/** Compact relative time (e.g. "just now", "5m ago", "3h ago", "2d ago"). */
export function formatRelativeTime(
  iso: string | null | undefined,
  now: number = Date.now(),
): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const diffMs = now - then;
  const sec = Math.round(diffMs / 1000);
  if (sec < 45) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day < 7) return `${day}d ago`;
  const wk = Math.round(day / 7);
  if (wk < 5) return `${wk}w ago`;
  return new Date(then).toISOString().slice(0, 10);
}
