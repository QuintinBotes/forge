import { cn } from "@/lib/utils";

import { blastMeta, lifecycleMeta, severityMeta } from "./incident-meta";

const PILL =
  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium";

/** Severity pill (critical / high / medium / low). */
export function SeverityBadge({
  severity,
  className,
}: {
  severity: string;
  className?: string;
}) {
  const meta = severityMeta(severity);
  return (
    <span
      data-testid="severity-badge"
      className={cn(PILL, "uppercase tracking-wide", meta.badgeClass, className)}
    >
      {meta.label}
    </span>
  );
}

/** Lifecycle-state pill with its phase icon (the incident's FSM position). */
export function LifecycleBadge({
  state,
  className,
}: {
  state: string;
  className?: string;
}) {
  const meta = lifecycleMeta(state);
  const Icon = meta.icon;
  return (
    <span
      data-testid="lifecycle-badge"
      className={cn(PILL, meta.badgeClass, className)}
    >
      <Icon aria-hidden className="h-3 w-3" />
      {meta.label}
    </span>
  );
}

/** Blast-radius pill (low / medium / high, or "unknown"). */
export function BlastRadiusBadge({
  radius,
  className,
}: {
  radius: string | null | undefined;
  className?: string;
}) {
  const meta = blastMeta(radius);
  return (
    <span
      data-testid="blast-radius-badge"
      title={meta.label}
      className={cn(PILL, "uppercase tracking-wide", meta.badgeClass, className)}
    >
      {meta.label}
    </span>
  );
}
