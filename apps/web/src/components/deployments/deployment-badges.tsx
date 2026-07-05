import type { ReactNode } from "react";

import type {
  DeploymentState,
  GateCheckStatus,
  HealthStatus,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  checkStatusMeta,
  healthMeta,
  stateMeta,
  toneBadgeClass,
  type Tone,
} from "./deployment-meta";

const PILL =
  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium";

/** A generic tone pill (border + tinted surface + on-tint text). */
export function TonePill({
  tone,
  className,
  children,
}: {
  tone: Tone;
  className?: string;
  children: ReactNode;
}) {
  return <span className={cn(PILL, toneBadgeClass(tone), className)}>{children}</span>;
}

/** The deployment's FSM-state pill, with its phase icon (spins while mid-flight). */
export function StateBadge({
  state,
  className,
}: {
  state: DeploymentState;
  className?: string;
}) {
  const meta = stateMeta(state);
  const Icon = meta.icon;
  return (
    <span
      data-testid="state-badge"
      data-state={state}
      className={cn(PILL, toneBadgeClass(meta.tone), className)}
    >
      <Icon
        aria-hidden
        className={cn("h-3 w-3", meta.active && "animate-spin [animation-duration:1.6s]")}
      />
      {meta.label}
    </span>
  );
}

/** The health-status pill for a live deployment (healthy / failing / unknown). */
export function HealthBadge({
  status,
  className,
}: {
  status: HealthStatus | null | undefined;
  className?: string;
}) {
  const meta = healthMeta(status);
  const Icon = meta.icon;
  return (
    <span
      data-testid="health-badge"
      title={`Health: ${meta.label}`}
      className={cn(PILL, toneBadgeClass(meta.tone), className)}
    >
      <Icon aria-hidden className="h-3 w-3" />
      {meta.label}
    </span>
  );
}

/** A gate-check status glyph (passed / failed / pending / skipped). */
export function CheckStatusIcon({
  status,
  className,
}: {
  status: GateCheckStatus;
  className?: string;
}) {
  const meta = checkStatusMeta(status);
  const Icon = meta.icon;
  const tint =
    meta.tone === "success"
      ? "text-success"
      : meta.tone === "danger"
        ? "text-danger"
        : meta.tone === "warning"
          ? "text-warning"
          : "text-muted-foreground";
  return (
    <Icon
      aria-hidden
      data-testid="check-status-icon"
      data-status={status}
      className={cn("h-4 w-4", tint, className)}
    />
  );
}
