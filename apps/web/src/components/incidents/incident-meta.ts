/**
 * Presentation metadata for the incident surface (pure + unit-testable).
 *
 * Maps the F17 domain vocabulary — severity, lifecycle state, blast radius and
 * FSM events — onto Forge design-token classes, labels and icons. No JSX here so
 * the maps stay trivially testable; the icon is carried as a component ref.
 */

import {
  Activity,
  CheckCircle2,
  CircleDashed,
  CircleDot,
  ClipboardList,
  PauseCircle,
  Search,
  Siren,
  Wrench,
  XCircle,
  type LucideIcon,
} from "lucide-react";

import type { BlastRadius, IncidentSeverity } from "@/lib/api/types";

// --- Severity ------------------------------------------------------------- //

export interface SeverityMeta {
  label: string;
  /** Small status dot (queue rows). */
  dotClass: string;
  /**
   * Left-edge severity stripe (queue rows). Written as a literal Tailwind
   * class per severity — Tailwind's JIT scanner only generates CSS for
   * class names it finds as literal substrings in source, so this must
   * NOT be derived at runtime (e.g. via `dotClass.replace("bg-", "border-l-")`)
   * or the stripe silently renders uncoloured.
   */
  stripeClass: string;
  /** Pill badge classes (header / rows). */
  badgeClass: string;
  /** Higher = more severe (queue ranking). */
  weight: number;
}

const SEVERITY_META: Record<IncidentSeverity, SeverityMeta> = {
  critical: {
    label: "Critical",
    dotClass: "bg-danger",
    stripeClass: "border-l-danger",
    badgeClass: "border-transparent bg-danger text-danger-foreground",
    weight: 4,
  },
  high: {
    label: "High",
    dotClass: "bg-danger/70",
    stripeClass: "border-l-danger/70",
    badgeClass: "border-danger/40 bg-danger/10 text-danger",
    weight: 3,
  },
  medium: {
    label: "Medium",
    dotClass: "bg-warning",
    stripeClass: "border-l-warning",
    badgeClass: "border-warning/40 bg-warning/10 text-warning",
    weight: 2,
  },
  low: {
    label: "Low",
    dotClass: "bg-muted-foreground/50",
    stripeClass: "border-l-muted-foreground/50",
    badgeClass: "border-border bg-muted text-muted-foreground",
    weight: 1,
  },
};

export function severityMeta(severity: string): SeverityMeta {
  return SEVERITY_META[severity as IncidentSeverity] ?? SEVERITY_META.low;
}

// --- Lifecycle state ------------------------------------------------------ //

type Tone =
  | "open"
  | "active"
  | "action"
  | "resolved"
  | "paused"
  | "danger"
  | "muted";

const TONE_BADGE: Record<Tone, string> = {
  open: "border-danger/40 bg-danger/10 text-danger",
  active: "border-warning/40 bg-warning/10 text-warning",
  action: "border-primary/40 bg-primary/10 text-primary",
  resolved: "border-success/40 bg-success/10 text-success",
  paused: "border-warning/40 bg-warning/10 text-warning",
  danger: "border-danger/40 bg-danger/10 text-danger",
  muted: "border-border bg-muted text-muted-foreground",
};

export interface LifecycleMeta {
  label: string;
  tone: Tone;
  badgeClass: string;
  icon: LucideIcon;
}

const LIFECYCLE: Record<string, { label: string; tone: Tone; icon: LucideIcon }> = {
  alert_received: { label: "Alert received", tone: "open", icon: Siren },
  incident_created: { label: "Declared", tone: "open", icon: Siren },
  context_gathering: { label: "Gathering context", tone: "active", icon: Search },
  impact_assessed: { label: "Impact assessed", tone: "active", icon: Activity },
  remediation_proposed: {
    label: "Remediation proposed",
    tone: "action",
    icon: Wrench,
  },
  awaiting_approval: { label: "Awaiting approval", tone: "action", icon: Wrench },
  executing_runbook: { label: "Executing runbook", tone: "active", icon: Activity },
  monitoring: { label: "Monitoring", tone: "active", icon: Activity },
  resolved: { label: "Resolved", tone: "resolved", icon: CheckCircle2 },
  postmortem_created: { label: "Postmortem", tone: "resolved", icon: ClipboardList },
  closed: { label: "Closed", tone: "muted", icon: CircleDot },
  needs_human_input: {
    label: "Needs human input",
    tone: "paused",
    icon: PauseCircle,
  },
  failed: { label: "Failed", tone: "danger", icon: XCircle },
  cancelled: { label: "Cancelled", tone: "muted", icon: CircleDot },
};

export function lifecycleMeta(state: string): LifecycleMeta {
  const entry = LIFECYCLE[state] ?? {
    label: humanize(state),
    tone: "muted" as const,
    icon: CircleDashed,
  };
  return { ...entry, badgeClass: TONE_BADGE[entry.tone] };
}

/** True for the terminal-resolved states (used to nudge the postmortem tab). */
export function isResolvedState(state: string): boolean {
  return (
    state === "resolved" ||
    state === "postmortem_created" ||
    state === "closed"
  );
}

// --- Blast radius --------------------------------------------------------- //

const BLAST_META: Record<BlastRadius, { label: string; badgeClass: string }> = {
  high: {
    label: "High blast radius",
    badgeClass: "border-danger/40 bg-danger/10 text-danger",
  },
  medium: {
    label: "Medium blast radius",
    badgeClass: "border-warning/40 bg-warning/10 text-warning",
  },
  low: {
    label: "Low blast radius",
    badgeClass: "border-success/40 bg-success/10 text-success",
  },
};

export function blastMeta(radius: string | null | undefined): {
  label: string;
  badgeClass: string;
} {
  if (!radius) {
    return { label: "Blast radius unknown", badgeClass: TONE_BADGE.muted };
  }
  return (
    BLAST_META[radius as BlastRadius] ?? {
      label: `${humanize(radius)} blast radius`,
      badgeClass: TONE_BADGE.muted,
    }
  );
}

// --- Remediation step status --------------------------------------------- //

export function stepStatusClass(status: string): string {
  switch (status) {
    case "succeeded":
      return "border-success/40 bg-success/10 text-success";
    case "approved":
    case "running":
      return "border-primary/40 bg-primary/10 text-primary";
    case "failed":
      return "border-danger/40 bg-danger/10 text-danger";
    case "skipped":
      return "border-border bg-muted text-muted-foreground";
    default:
      return "border-border bg-muted text-muted-foreground";
  }
}

// --- FSM events (advance actions) ---------------------------------------- //

export type EventIntent = "advance" | "approve" | "danger";

export interface EventMeta {
  label: string;
  intent: EventIntent;
}

const EVENT_META: Record<string, EventMeta> = {
  alert_ingested: { label: "Ingest alert", intent: "advance" },
  incident_acknowledged: { label: "Acknowledge", intent: "advance" },
  context_gathered: { label: "Mark context gathered", intent: "advance" },
  impact_assessed: { label: "Record impact", intent: "advance" },
  remediation_proposed: { label: "Mark remediation proposed", intent: "advance" },
  remediation_approved: { label: "Approve remediation", intent: "approve" },
  remediation_rejected: { label: "Reject remediation", intent: "danger" },
  remediation_blast_radius_exceeded: {
    label: "Flag blast-radius exceeded",
    intent: "danger",
  },
  runbook_completed: { label: "Mark runbook complete", intent: "advance" },
  runbook_step_failed: { label: "Report step failure", intent: "danger" },
  recovery_confirmed: { label: "Confirm recovery", intent: "approve" },
  recovery_failed: { label: "Report recovery failed", intent: "danger" },
  postmortem_requested: { label: "Request postmortem", intent: "advance" },
  close: { label: "Close incident", intent: "advance" },
  resume: { label: "Resume", intent: "advance" },
  cancel: { label: "Cancel incident", intent: "danger" },
  fail: { label: "Mark failed", intent: "danger" },
};

export function eventMeta(event: string): EventMeta {
  return EVENT_META[event] ?? { label: humanize(event), intent: "advance" };
}

// --- Formatting ----------------------------------------------------------- //

/** Compact "time ago" for an ISO timestamp (e.g. "3h ago", "just now"). */
export function relativeTime(
  iso: string | null | undefined,
  now: number = Date.now(),
): string {
  if (!iso) {
    return "—";
  }
  const then = Date.parse(iso);
  if (Number.isNaN(then)) {
    return "—";
  }
  const seconds = Math.round((now - then) / 1000);
  if (seconds < 45) {
    return "just now";
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  const hours = Math.round(minutes / 60);
  if (hours < 24) {
    return `${hours}h ago`;
  }
  const days = Math.round(hours / 24);
  if (days < 30) {
    return `${days}d ago`;
  }
  const months = Math.round(days / 30);
  if (months < 12) {
    return `${months}mo ago`;
  }
  return `${Math.round(months / 12)}y ago`;
}

/** Turn a `kind:uuid` actor ref into a short, readable label. */
export function actorLabel(actor: string | null | undefined): string {
  if (!actor || actor === "system") {
    return "System";
  }
  const [kind, id] = actor.split(":");
  if (!id) {
    return actor;
  }
  const short = id.length > 8 ? id.slice(0, 8) : id;
  const nicer = kind.charAt(0).toUpperCase() + kind.slice(1);
  return `${nicer} ${short}`;
}

/** Title-case a snake/kebab token ("runbook_completed" → "Runbook completed"). */
export function humanize(token: string): string {
  const spaced = token.replace(/[_-]+/g, " ").trim();
  if (!spaced) {
    return token;
  }
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
