/**
 * Sprint presentation helpers — pure, framework-free, and unit-tested.
 *
 * State labels + token-only badge classes, point/percent/date formatters, and
 * the "which sprint is in focus by default" rule (the running sprint, else the
 * most recent). No hardcoded colour — every class resolves a Forge token.
 */

import type { Sprint, SprintState } from "@/lib/api/types";

export const SPRINT_STATE_LABELS: Record<SprintState, string> = {
  planned: "Planned",
  active: "Active",
  completed: "Completed",
  cancelled: "Cancelled",
};

/** Token-only badge classes per state (ember reserved for the running sprint). */
export function sprintStateBadgeClass(state: SprintState): string {
  switch (state) {
    case "active":
      return "border-primary/30 bg-primary/10 text-primary";
    case "completed":
      return "border-success/30 bg-success/10 text-success";
    case "cancelled":
      return "border-danger/30 bg-danger/10 text-danger";
    default:
      return "border-border bg-muted text-muted-foreground";
  }
}

export type Tone = "success" | "warning" | "danger";

/** Predictability band: on-target, drifting, or off (completed ÷ committed). */
export function predictabilityTone(fraction: number): Tone {
  if (fraction >= 0.9) return "success";
  if (fraction >= 0.7) return "warning";
  return "danger";
}

/** Format a 0..1 ratio as a whole-percent string (clamped, no decimals). */
export function formatPct(fraction: number): string {
  if (!Number.isFinite(fraction)) return "0%";
  return `${Math.round(Math.max(0, fraction) * 100)}%`;
}

/** Story points as a compact integer (rounds; never NaN). */
export function formatPoints(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  return Number.isFinite(v) ? String(Math.round(v)) : "0";
}

/** One-decimal figure for averages/forecasts (e.g. "23.5 pts"). */
export function formatDecimal(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  return Number.isFinite(v) ? v.toFixed(1) : "0.0";
}

/** "Jun 3" style short date from an ISO date/datetime string (UTC-stable). */
export function formatDateShort(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso.length <= 10 ? `${iso}T00:00:00Z` : iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

/** Inclusive day-count of a sprint window, or null when either bound is absent. */
export function sprintLengthDays(
  start: string | null | undefined,
  end: string | null | undefined,
): number | null {
  if (!start || !end) return null;
  const a = new Date(`${start.slice(0, 10)}T00:00:00Z`).getTime();
  const b = new Date(`${end.slice(0, 10)}T00:00:00Z`).getTime();
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.max(1, Math.round((b - a) / 86_400_000) + 1);
}

/** Sort a copy newest-first (by start_date, then name) for the sprint picker. */
export function sortSprintsNewestFirst(sprints: Sprint[]): Sprint[] {
  return [...sprints].sort((a, b) => {
    const ta = a.start_date ? Date.parse(a.start_date) : 0;
    const tb = b.start_date ? Date.parse(b.start_date) : 0;
    if (tb !== ta) return tb - ta;
    return a.name.localeCompare(b.name);
  });
}

/**
 * The sprint to focus by default: the single running sprint if there is one,
 * otherwise the most recently started. Returns its id, or null when empty.
 */
export function pickDefaultSprintId(sprints: Sprint[]): string | null {
  if (sprints.length === 0) return null;
  const active = sprints.find((s) => s.state === "active");
  if (active) return active.id;
  return sortSprintsNewestFirst(sprints)[0]?.id ?? null;
}
