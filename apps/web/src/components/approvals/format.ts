/** Formatting helpers for the approval inbox (pure + unit-testable). */

/** Compact, human "time ago" for an ISO timestamp (e.g. "3h ago", "just now"). */
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
  if (seconds < 0) {
    return "just now";
  }
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

/** Title-case a snake/kebab key for display ("files_changed" → "Files changed"). */
export function humanizeKey(key: string): string {
  const spaced = key.replace(/[_-]+/g, " ").trim();
  if (!spaced) {
    return key;
  }
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
