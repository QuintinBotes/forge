import { cn } from "@/lib/utils";

import { outcomeMeta, severityMeta } from "./audit-meta";

const PILL =
  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium";

/** Outcome pill (success / denied / error / blocked). */
export function OutcomeBadge({
  result,
  className,
}: {
  result: string;
  className?: string;
}) {
  const meta = outcomeMeta(result);
  const Icon = meta.icon;
  return (
    <span
      data-testid="outcome-badge"
      className={cn(PILL, meta.badgeClass, className)}
    >
      <Icon aria-hidden className="h-3 w-3" />
      {meta.label}
    </span>
  );
}

/** Severity pill (info / notice / warning / critical). */
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
