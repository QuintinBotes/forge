"use client";

import {
  Ban,
  Check,
  GitCommitHorizontal,
  ShieldAlert,
  ShieldCheck,
  Undo2,
  X,
} from "lucide-react";
import { useId, useState } from "react";

import type {
  DeploymentDecision,
  DeploymentDetail,
  GateCheckResult,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { CheckStatusIcon, HealthBadge, StateBadge, TonePill } from "./deployment-badges";
import {
  actorLabel,
  canCancel,
  canDecide,
  canRollback,
  checkNameMeta,
  formatRelativeTime,
  shortSha,
} from "./deployment-meta";

export interface GatePanelActions {
  onDecision: (decision: DeploymentDecision, note?: string) => void;
  onCancel: () => void;
  onRollback: () => void;
  pending: boolean;
  error: string | null;
}

export interface GatePanelProps {
  detail?: DeploymentDetail;
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
  actions: GatePanelActions;
  now?: number;
}

/**
 * The gate detail for the focused deployment: the gate verdict (can it proceed,
 * does it need a human), the per-check breakdown, the transition history, and
 * the decision action bar (approve / reject / request changes / cancel / roll
 * back) enabled per the deployment's FSM state. This is where a reviewer clears
 * a gate — the approve/reject controls carry the reviewer's semantic weight, not
 * the screen's precious ember (reserved for Promote).
 */
export function GatePanel({
  detail,
  isLoading,
  isError,
  onRetry,
  actions,
  now,
}: GatePanelProps) {
  const noteId = useId();
  const [note, setNote] = useState("");

  if (isLoading && !detail) {
    return <GateSkeleton />;
  }
  if (isError && !detail) {
    return (
      <PanelState
        testId="gate-error"
        icon={<ShieldAlert className="h-8 w-8 text-muted-foreground" aria-hidden />}
        title="Gate unavailable"
        body="The deployment's gate could not be loaded."
        action={
          <button
            type="button"
            onClick={onRetry}
            className="inline-flex h-8 items-center rounded-md border border-border px-3 text-xs font-medium text-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Retry
          </button>
        }
      />
    );
  }
  if (!detail) {
    return (
      <PanelState
        testId="gate-none"
        icon={<ShieldCheck className="h-8 w-8 text-muted-foreground" aria-hidden />}
        title="No deployment selected"
        body="Pick a deployment from the pipeline or the list to review its gate."
      />
    );
  }

  const gate = detail.gate ?? null;
  const checks = detail.checks?.length ? detail.checks : (gate?.checks ?? []);
  const showDecision = canDecide(detail.state);

  return (
    <div data-testid="gate-panel" className="flex min-h-0 flex-1 flex-col">
      {/* Header */}
      <header className="flex flex-col gap-3 border-b border-border p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <h2 className="truncate font-display text-base font-semibold tracking-tight text-foreground">
              {detail.from_environment_name
                ? `${detail.from_environment_name} → ${detail.environment_name}`
                : detail.environment_name}
            </h2>
            <StateBadge state={detail.state} />
          </div>
          <HealthBadge status={detail.health_status} />
        </div>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs sm:grid-cols-4">
          <Meta label="Commit">
            <span className="inline-flex items-center gap-1 font-mono text-foreground">
              <GitCommitHorizontal className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
              {shortSha(detail.commit_sha)}
            </span>
          </Meta>
          <Meta label="Repo">
            <span className="truncate font-mono text-foreground">{detail.repo_id}</span>
          </Meta>
          <Meta label="Requested by">
            <span className="truncate text-foreground">{actorLabel(detail.initiated_by)}</span>
          </Meta>
          <Meta label="When">
            <span className="text-foreground">{formatRelativeTime(detail.requested_at, now)}</span>
          </Meta>
        </dl>
      </header>

      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4">
        {/* Gate verdict */}
        {gate ? <GateVerdict gate={gate} /> : null}

        {/* Checks */}
        <section className="flex flex-col gap-2">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Gate checks
          </h3>
          {checks.length > 0 ? (
            <ul data-testid="gate-checks" className="flex flex-col divide-y divide-border rounded-md border border-border">
              {checks.map((c) => (
                <CheckRow key={c.name} check={c} />
              ))}
            </ul>
          ) : (
            <p className="rounded-md border border-dashed border-border px-3 py-4 text-center text-xs text-muted-foreground">
              No gate checks were recorded for this deployment.
            </p>
          )}
        </section>

        {/* Transitions */}
        {detail.transitions.length > 0 ? (
          <section className="flex flex-col gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              History
            </h3>
            <ol data-testid="gate-transitions" className="flex flex-col gap-1.5">
              {detail.transitions.map((t) => (
                <li
                  key={t.sequence}
                  className="flex items-center gap-2 text-[11px] text-muted-foreground"
                >
                  <span className="font-mono text-foreground">{t.to_state}</span>
                  <span className="text-muted-foreground/70">·</span>
                  <span>{t.event}</span>
                  <span className="text-muted-foreground/70">·</span>
                  <span>{actorLabel(t.actor)}</span>
                  {t.created_at ? (
                    <span className="ml-auto">{formatRelativeTime(t.created_at, now)}</span>
                  ) : null}
                </li>
              ))}
            </ol>
          </section>
        ) : null}
      </div>

      {/* Action bar */}
      <footer className="flex flex-col gap-3 border-t border-border p-4">
        {showDecision ? (
          <div className="flex flex-col gap-1.5">
            <label htmlFor={noteId} className="text-xs font-medium text-muted-foreground">
              Decision note <span className="font-normal">(optional)</span>
            </label>
            <textarea
              id={noteId}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              rows={2}
              placeholder="Context for approving, rejecting or requesting changes…"
              className="w-full resize-none rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
        ) : null}

        {actions.error ? (
          <p role="alert" className="text-xs text-danger">
            {actions.error}
          </p>
        ) : null}

        <div className="flex flex-wrap items-center gap-2">
          {showDecision ? (
            <>
              <ActionButton
                testId="approve-action"
                tone="success"
                disabled={actions.pending}
                onClick={() => actions.onDecision("approve", note.trim() || undefined)}
                icon={<Check className="h-4 w-4" aria-hidden />}
                label="Approve"
              />
              <ActionButton
                testId="reject-action"
                tone="danger"
                disabled={actions.pending}
                onClick={() => actions.onDecision("reject", note.trim() || undefined)}
                icon={<X className="h-4 w-4" aria-hidden />}
                label="Reject"
              />
              <ActionButton
                testId="changes-action"
                tone="outline"
                disabled={actions.pending}
                onClick={() =>
                  actions.onDecision("changes_requested", note.trim() || undefined)
                }
                label="Request changes"
              />
            </>
          ) : null}

          {canRollback(detail.state) ? (
            <ActionButton
              testId="rollback-action"
              tone="danger-outline"
              disabled={actions.pending}
              onClick={actions.onRollback}
              icon={<Undo2 className="h-4 w-4" aria-hidden />}
              label="Roll back"
            />
          ) : null}

          {canCancel(detail.state) ? (
            <ActionButton
              testId="cancel-action"
              tone="outline"
              disabled={actions.pending}
              onClick={actions.onCancel}
              icon={<Ban className="h-4 w-4" aria-hidden />}
              label="Cancel"
              className="ml-auto"
            />
          ) : null}

          {!showDecision && !canRollback(detail.state) && !canCancel(detail.state) ? (
            <p className="text-xs text-muted-foreground">
              This deployment has reached a terminal state — no actions remain.
            </p>
          ) : null}
        </div>
      </footer>
    </div>
  );
}

// --- Gate verdict banner -------------------------------------------------- //

function GateVerdict({
  gate,
}: {
  gate: NonNullable<DeploymentDetail["gate"]>;
}) {
  const blocked = gate.blocking_reasons.length > 0;
  const tone = blocked ? "danger" : gate.requires_human_approval ? "warning" : "success";
  const Icon = blocked ? ShieldAlert : gate.requires_human_approval ? ShieldAlert : ShieldCheck;
  const headline = blocked
    ? "Gate blocked"
    : gate.requires_human_approval
      ? "Awaiting human approval"
      : "Gate clear";
  return (
    <section
      data-testid="gate-verdict"
      className={cn(
        "flex flex-col gap-2 rounded-md border p-3",
        blocked
          ? "border-danger/40 bg-danger/5"
          : gate.requires_human_approval
            ? "border-warning/40 bg-warning/5"
            : "border-success/40 bg-success/5",
      )}
    >
      <div className="flex items-center gap-2">
        <Icon
          aria-hidden
          className={cn(
            "h-4 w-4",
            blocked ? "text-danger" : gate.requires_human_approval ? "text-warning" : "text-success",
          )}
        />
        <span className="text-sm font-semibold text-foreground">{headline}</span>
        <TonePill tone={tone} className="ml-auto">
          {gate.can_proceed ? "can proceed" : "blocked"}
        </TonePill>
      </div>
      {blocked ? (
        <ul className="flex flex-col gap-1 pl-6 text-xs text-muted-foreground">
          {gate.blocking_reasons.map((reason) => (
            <li key={reason} className="list-disc">
              {reason}
            </li>
          ))}
        </ul>
      ) : (
        <p className="pl-6 text-xs text-muted-foreground">
          {gate.requires_human_approval
            ? "All automated checks pass; a reviewer must approve this promotion."
            : "All required checks pass and no approval is required."}
        </p>
      )}
    </section>
  );
}

function CheckRow({ check }: { check: GateCheckResult }) {
  const nameMeta = checkNameMeta(check.name);
  return (
    <li
      data-testid="gate-check-row"
      data-check={check.name}
      className="flex items-start gap-2.5 px-3 py-2.5"
    >
      <CheckStatusIcon status={check.status} className="mt-0.5 shrink-0" />
      <div className="flex min-w-0 flex-col">
        <span className="text-sm font-medium text-foreground">{nameMeta.label}</span>
        <span className="text-xs text-muted-foreground">
          {check.detail || nameMeta.description}
        </span>
      </div>
    </li>
  );
}

// --- Small building blocks ------------------------------------------------ //

function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="min-w-0 truncate">{children}</dd>
    </div>
  );
}

type ActionTone = "success" | "danger" | "danger-outline" | "outline";

function ActionButton({
  testId,
  tone,
  label,
  icon,
  onClick,
  disabled,
  className,
}: {
  testId: string;
  tone: ActionTone;
  label: string;
  icon?: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  className?: string;
}) {
  const toneClass =
    tone === "success"
      ? "bg-success text-success-foreground hover:bg-success/90"
      : tone === "danger"
        ? "bg-danger text-danger-foreground hover:bg-danger/90"
        : tone === "danger-outline"
          ? "border border-danger/40 text-danger hover:bg-danger/10"
          : "border border-border text-foreground hover:bg-accent hover:text-accent-foreground";
  return (
    <button
      type="button"
      data-testid={testId}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "inline-flex h-9 items-center gap-1.5 rounded-md px-3 text-sm font-medium transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        "disabled:pointer-events-none disabled:opacity-50",
        toneClass,
        className,
      )}
    >
      {icon}
      {label}
    </button>
  );
}

function PanelState({
  testId,
  icon,
  title,
  body,
  action,
}: {
  testId: string;
  icon: React.ReactNode;
  title: string;
  body: string;
  action?: React.ReactNode;
}) {
  return (
    <div
      data-testid={testId}
      className="flex h-full flex-col items-center justify-center gap-2 p-10 text-center"
    >
      {icon}
      <p className="text-sm font-medium text-foreground">{title}</p>
      <p className="max-w-sm text-xs text-muted-foreground">{body}</p>
      {action}
    </div>
  );
}

function GateSkeleton() {
  return (
    <div data-testid="gate-skeleton" aria-busy="true" className="flex flex-col gap-4 p-4">
      <div className="h-6 w-1/2 animate-pulse rounded bg-muted" />
      <div className="grid grid-cols-4 gap-3">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="h-8 animate-pulse rounded bg-muted/60" />
        ))}
      </div>
      <div className="h-16 animate-pulse rounded-md bg-muted/50" />
      <div className="h-40 animate-pulse rounded-md bg-muted/40" />
    </div>
  );
}
