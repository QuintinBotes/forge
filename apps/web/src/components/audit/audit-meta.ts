/**
 * Presentation metadata + secret redaction for the audit viewer (pure +
 * unit-testable; no JSX). Maps the F39 vocabulary — actions, actor types,
 * outcomes and severities — onto Forge design-token classes, labels and icon
 * refs, and provides the defensive client-side redaction that guarantees no
 * credential material renders even if a producer ever slipped one through.
 */

import {
  Activity,
  Bot,
  CircleCheck,
  CircleDot,
  CircleX,
  Ban,
  GitBranch,
  KeyRound,
  LogIn,
  Plug,
  Puzzle,
  ScrollText,
  Server,
  ShieldCheck,
  ShieldX,
  Terminal,
  User,
  UserCog,
  type LucideIcon,
} from "lucide-react";

import type { AuditActorType } from "@/lib/api/types";

// --- Secret redaction ----------------------------------------------------- //

/** The token rendered in place of any redacted secret value. */
export const REDACTED_PLACEHOLDER = "••• redacted";

/** Key names whose values are always treated as secret. */
const SECRET_KEY_RE =
  /(pass(word|wd)?|secret|token|api[_-]?key|apikey|auth(orization)?|credential|private[_-]?key|access[_-]?key|client[_-]?secret|refresh[_-]?token|bearer|cookie|session[_-]?(id|token)?|otp|passphrase|salt|signature|encryption[_-]?key)/i;

/** Value prefixes that unambiguously denote a credential. */
const SECRET_VALUE_RE =
  /^(bearer\s+|basic\s+|sk-|rk-|ghp_|gho_|ghs_|github_pat_|xox[baprs]-|akia|asia|-----begin)/i;

/** True when the key name marks its value as secret. */
export function isSecretKey(key: string): boolean {
  return SECRET_KEY_RE.test(key);
}

/** True when a string value looks like a credential blob (JWT / API token). */
export function looksLikeSecretValue(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed) return false;
  if (SECRET_VALUE_RE.test(trimmed)) return true;
  // A long, spaceless, path-free high-entropy token (jwt / base64url / hex).
  return /^[A-Za-z0-9._=+-]{40,}$/.test(trimmed);
}

/**
 * Deep-redact a JSON value: any secret-named key or credential-looking string
 * leaf becomes {@link REDACTED_PLACEHOLDER}. Structure is otherwise preserved so
 * the drawer can still show the shape of the change.
 */
export function redactJson(value: unknown, key?: string): unknown {
  if (key !== undefined && isSecretKey(key)) {
    return REDACTED_PLACEHOLDER;
  }
  if (typeof value === "string") {
    return looksLikeSecretValue(value) ? REDACTED_PLACEHOLDER : value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => redactJson(item));
  }
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = redactJson(v, k);
    }
    return out;
  }
  return value;
}

/** Pretty, redacted JSON for a payload (stable key order via the walker). */
export function redactedJsonString(value: unknown): string {
  return JSON.stringify(redactJson(value), null, 2);
}

// --- Outcome -------------------------------------------------------------- //

export interface OutcomeMeta {
  label: string;
  badgeClass: string;
  icon: LucideIcon;
}

const OUTCOME_META: Record<string, OutcomeMeta> = {
  success: {
    label: "Success",
    badgeClass: "border-success/40 bg-success/10 text-success",
    icon: CircleCheck,
  },
  denied: {
    label: "Denied",
    badgeClass: "border-warning/40 bg-warning/10 text-warning",
    icon: Ban,
  },
  error: {
    label: "Error",
    badgeClass: "border-danger/40 bg-danger/10 text-danger",
    icon: CircleX,
  },
  blocked: {
    label: "Blocked",
    badgeClass: "border-danger/40 bg-danger/10 text-danger",
    icon: ShieldX,
  },
};

export function outcomeMeta(result: string): OutcomeMeta {
  return (
    OUTCOME_META[result] ?? {
      label: humanize(result),
      badgeClass: "border-border bg-muted text-muted-foreground",
      icon: CircleDot,
    }
  );
}

// --- Severity ------------------------------------------------------------- //

export interface SeverityMeta {
  label: string;
  badgeClass: string;
  dotClass: string;
  /** Higher = more urgent (used for the row accent). */
  weight: number;
}

const SEVERITY_META: Record<string, SeverityMeta> = {
  info: {
    label: "Info",
    badgeClass: "border-border bg-muted text-muted-foreground",
    dotClass: "bg-muted-foreground/50",
    weight: 1,
  },
  notice: {
    label: "Notice",
    badgeClass: "border-primary/30 bg-primary/5 text-primary",
    dotClass: "bg-primary/60",
    weight: 2,
  },
  warning: {
    label: "Warning",
    badgeClass: "border-warning/40 bg-warning/10 text-warning",
    dotClass: "bg-warning",
    weight: 3,
  },
  critical: {
    label: "Critical",
    badgeClass: "border-transparent bg-danger text-danger-foreground",
    dotClass: "bg-danger",
    weight: 4,
  },
};

export function severityMeta(severity: string): SeverityMeta {
  return SEVERITY_META[severity] ?? SEVERITY_META.info;
}

// --- Actor ---------------------------------------------------------------- //

export interface ActorTypeMeta {
  label: string;
  icon: LucideIcon;
}

const ACTOR_TYPE_META: Record<AuditActorType, ActorTypeMeta> = {
  user: { label: "User", icon: User },
  agent_runner: { label: "Agent", icon: Bot },
  system: { label: "System", icon: Server },
  integration: { label: "Integration", icon: Puzzle },
  api_key: { label: "API key", icon: KeyRound },
};

export function actorTypeMeta(actorType: string): ActorTypeMeta {
  return (
    ACTOR_TYPE_META[actorType as AuditActorType] ?? {
      label: humanize(actorType),
      icon: User,
    }
  );
}

/**
 * A short, readable actor label. Prefers the durable `actor_label` snapshot;
 * otherwise renders "<Type> <short-id>", falling back to the type alone.
 */
export function actorDisplay(entry: {
  actor_type: string;
  actor_label?: string | null;
  actor_id?: string | null;
}): string {
  if (entry.actor_label) {
    return entry.actor_label;
  }
  const { label } = actorTypeMeta(entry.actor_type);
  if (entry.actor_id) {
    return `${label} ${shortId(entry.actor_id)}`;
  }
  return label;
}

// --- Action --------------------------------------------------------------- //

/** Icon for an action, chosen by its dotted namespace prefix. */
export function actionIcon(action: string): LucideIcon {
  const prefix = action.split(".")[0];
  switch (prefix) {
    case "agent":
      return Bot;
    case "tool":
      return Terminal;
    case "mcp":
      return Plug;
    case "policy":
      return action.endsWith("denied") ? ShieldX : ShieldCheck;
    case "approval":
      return CircleCheck;
    case "workflow":
      return GitBranch;
    case "apikey":
    case "secret":
    case "connection":
      return KeyRound;
    case "rbac":
      return UserCog;
    case "auth":
      return LogIn;
    case "audit":
      return ScrollText;
    default:
      return Activity;
  }
}

/** Human label for a dotted action ("policy.tool_denied" → "Tool denied"). */
export function actionLabel(action: string): string {
  const parts = action.split(".");
  const tail = parts[parts.length - 1] ?? action;
  return humanize(tail);
}

/** The namespace segment of a dotted action ("mcp.tool_call" → "mcp"). */
export function actionNamespace(action: string): string {
  return action.includes(".") ? action.split(".")[0] : "core";
}

export interface ActionMeta {
  icon: LucideIcon;
  label: string;
  namespace: string;
}

/**
 * Bundle the icon/label/namespace for an action. Consumers read `.icon` as a
 * property (rather than binding a component from a bare call) so the icon
 * renders as `<Icon />` without tripping the static-components lint.
 */
export function actionMeta(action: string): ActionMeta {
  return {
    icon: actionIcon(action),
    label: actionLabel(action),
    namespace: actionNamespace(action),
  };
}

// --- Formatting ----------------------------------------------------------- //

/** First 8 chars of an id, for compact display. */
export function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id;
}

/** Title-case a snake/kebab/dotted token. */
export function humanize(token: string): string {
  const spaced = token.replace(/[._-]+/g, " ").trim();
  if (!spaced) return token;
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Compact "time ago" for an ISO timestamp (e.g. "3h ago", "just now"). */
export function relativeTime(
  iso: string | null | undefined,
  now: number = Date.now(),
): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const seconds = Math.round((now - then) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.round(months / 12)}y ago`;
}

/** Absolute, locale-stable timestamp for titles/detail ("2026-07-05 11:00:03 UTC"). */
export function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`
  );
}

// --- Time-range presets --------------------------------------------------- //

export interface TimePreset {
  value: string;
  label: string;
  /** Milliseconds back from "now"; null = all time. */
  ms: number | null;
}

export const TIME_PRESETS: readonly TimePreset[] = [
  { value: "all", label: "All time", ms: null },
  { value: "1h", label: "Last hour", ms: 60 * 60 * 1000 },
  { value: "24h", label: "Last 24 hours", ms: 24 * 60 * 60 * 1000 },
  { value: "7d", label: "Last 7 days", ms: 7 * 24 * 60 * 60 * 1000 },
  { value: "30d", label: "Last 30 days", ms: 30 * 24 * 60 * 60 * 1000 },
] as const;

/** Resolve a preset value to an ISO lower bound (or undefined for "all"). */
export function presetToFrom(
  value: string,
  now: number = Date.now(),
): string | undefined {
  const preset = TIME_PRESETS.find((p) => p.value === value);
  if (!preset || preset.ms === null) return undefined;
  return new Date(now - preset.ms).toISOString();
}
