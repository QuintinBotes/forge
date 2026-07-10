/**
 * Pure, unit-testable helpers for the PM integrations screen: display labels,
 * semantic tones (mapped to design tokens by the view), the sync-health rollup,
 * and the status-map ↔ editor-rows conversion. No React, no styling here.
 */

import type {
  PmConflictPolicy,
  PmConnectionStatus,
  PmProvider,
  PmStatusCategory,
  PmSyncDirection,
  PmSyncState,
} from "@/lib/api/types";

/** Semantic tone keys; the view maps these to token-based classes. */
export type Tone = "success" | "warning" | "danger" | "info" | "muted";

const PROVIDER_LABELS: Record<PmProvider, string> = {
  jira: "Jira",
  linear: "Linear",
  asana: "Asana",
  monday: "monday.com",
  github_projects: "GitHub Projects",
  clickup: "ClickUp",
  trello: "Trello",
  gitlab: "GitLab",
  generic: "Custom (generic)",
};

export function providerLabel(provider: PmProvider): string {
  return PROVIDER_LABELS[provider];
}

const SYNC_DIRECTION_LABELS: Record<PmSyncDirection, string> = {
  bidirectional: "Bidirectional",
  inbound_only: "Inbound only",
  outbound_only: "Outbound only",
};

export function syncDirectionLabel(direction: PmSyncDirection): string {
  return SYNC_DIRECTION_LABELS[direction];
}

const SYNC_DIRECTION_HINTS: Record<PmSyncDirection, string> = {
  bidirectional: "Changes flow both ways between Forge and the provider.",
  inbound_only: "Provider changes update Forge; Forge edits stay local.",
  outbound_only: "Forge changes push out; provider edits are ignored.",
};

export function syncDirectionHint(direction: PmSyncDirection): string {
  return SYNC_DIRECTION_HINTS[direction];
}

const CONFLICT_POLICY_LABELS: Record<PmConflictPolicy, string> = {
  forge_wins: "Forge wins",
  external_wins: "Provider wins",
  newest_wins: "Newest wins",
  manual: "Resolve manually",
};

export function conflictPolicyLabel(policy: PmConflictPolicy): string {
  return CONFLICT_POLICY_LABELS[policy];
}

const CONFLICT_POLICY_HINTS: Record<PmConflictPolicy, string> = {
  forge_wins: "Forge's value overwrites the provider on a clash.",
  external_wins: "The provider's value overwrites Forge on a clash.",
  newest_wins: "The most recently edited side wins automatically.",
  manual: "Clashes pause in the conflict inbox for a human call.",
};

export function conflictPolicyHint(policy: PmConflictPolicy): string {
  return CONFLICT_POLICY_HINTS[policy];
}

const STATUS_CATEGORY_LABELS: Record<PmStatusCategory, string> = {
  backlog: "Backlog",
  unstarted: "Todo",
  started: "In progress",
  completed: "Done",
  canceled: "Canceled",
};

export function statusCategoryLabel(category: PmStatusCategory): string {
  return STATUS_CATEGORY_LABELS[category];
}

interface StatusMeta {
  label: string;
  tone: Tone;
}

const CONNECTION_STATUS_META: Record<PmConnectionStatus, StatusMeta> = {
  connected: { label: "Connected", tone: "success" },
  pending: { label: "Pending", tone: "warning" },
  error: { label: "Error", tone: "danger" },
  disabled: { label: "Disabled", tone: "muted" },
};

export function connectionStatusMeta(status: PmConnectionStatus): StatusMeta {
  return CONNECTION_STATUS_META[status];
}

const SYNC_STATE_META: Record<PmSyncState, StatusMeta> = {
  synced: { label: "Synced", tone: "success" },
  pending_out: { label: "Pending out", tone: "info" },
  pending_in: { label: "Pending in", tone: "info" },
  conflict: { label: "Conflict", tone: "warning" },
  error: { label: "Error", tone: "danger" },
};

export function syncStateMeta(state: PmSyncState): StatusMeta {
  return SYNC_STATE_META[state];
}

/** True once the connection is disabled (its sync is paused). */
export function isConnectionEnabled(status: PmConnectionStatus): boolean {
  return status !== "disabled";
}

export interface LinkRollup {
  total: number;
  synced: number;
  pending: number;
  conflicts: number;
  errors: number;
  /** Fraction (0–1) of links in the healthy `synced` state. */
  healthyFraction: number;
}

/** Roll a `link_counts` map up into the numbers the health strip reports. */
export function summarizeLinks(
  counts: Partial<Record<PmSyncState, number>> | undefined,
): LinkRollup {
  const c = counts ?? {};
  const synced = c.synced ?? 0;
  const pending = (c.pending_out ?? 0) + (c.pending_in ?? 0);
  const conflicts = c.conflict ?? 0;
  const errors = c.error ?? 0;
  const total = synced + pending + conflicts + errors;
  return {
    total,
    synced,
    pending,
    conflicts,
    errors,
    healthyFraction: total === 0 ? 1 : synced / total,
  };
}

export interface StatusMapRow {
  /** The external workflow-state / status name (e.g. "In Review"). */
  external: string;
  /** The normalized Forge category it maps onto. */
  category: string;
}

/** Explode a `status_map` object into editor rows (stable, alphabetical). */
export function statusMapToRows(
  map: Record<string, string> | undefined,
): StatusMapRow[] {
  return Object.entries(map ?? {})
    .map(([external, category]) => ({ external, category }))
    .sort((a, b) => a.external.localeCompare(b.external));
}

/**
 * Collapse editor rows back into a `status_map` object. Rows with a blank
 * external name are dropped; on a duplicate name the last row wins.
 */
export function rowsToStatusMap(rows: StatusMapRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const row of rows) {
    const key = row.external.trim();
    if (key) {
      out[key] = row.category;
    }
  }
  return out;
}

/** Compact, human "time ago" for an ISO timestamp (null → em dash). */
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
