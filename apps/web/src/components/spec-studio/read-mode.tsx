"use client";

import { useCallback, useState, type KeyboardEvent } from "react";

import { actionForKey } from "@/components/approvals/approval-meta";
import { DecisionBar } from "@/components/approvals/decision-bar";
import { ManifestPanel } from "@/components/spec/manifest-panel";
import { STATUS_LABELS, isApprovable, statusBadgeClass } from "@/components/spec/spec-meta";
import type { ApprovalAction, SpecManifest } from "@/lib/api/types";
import { cn } from "@/lib/utils";

/** The three review decisions Read mode exposes (no escalate — that's F36's). */
const REVIEW_ACTIONS: ApprovalAction[] = ["approve", "request_changes", "reject"];

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

export interface ReadModeProps {
  spec: SpecManifest;
  /** Approves the spec at the human gate (`POST /spec/specs/{id}/approve`, real). */
  onApprove: () => void;
  approving?: boolean;
  approveError?: string | null;
  /**
   * Reject / request-changes have no backend endpoint yet — `forge_spec`'s
   * `FileSpecEngine` only exposes `approve_spec` (no `reject_spec` /
   * `request_changes` state transition, and `SpecStatus` has no such values).
   * Read mode still records the decision + note locally (so the keyboard-first
   * review flow is fully usable) and surfaces it through these optional
   * callbacks for a caller to persist once that endpoint exists — parked,
   * tracked separately; not faked as a server round-trip.
   */
  onReject?: (note: string) => void;
  onRequestChanges?: (note: string) => void;
}

/**
 * Read mode — Spec Studio's clean, rendered-prose surface for reviewers (the
 * same sections `spec.md` renders: Goal, Requirements, Acceptance Criteria,
 * Constraints, Open Questions, Decisions) paired with the approval gate:
 * Approve / Reject / Request changes, keyboard-first (`a`/`x`/`r`, the same
 * map the F36 approval inbox uses) via the shared {@link DecisionBar}. The
 * full manifest facts (repos, plan/tasks/validation refs) stay one disclosure
 * away rather than competing with the prose for attention.
 */
export function ReadMode({
  spec,
  onApprove,
  approving = false,
  approveError = null,
  onReject,
  onRequestChanges,
}: ReadModeProps) {
  const [activeNote, setActiveNote] = useState<"reject" | "request_changes" | null>(null);
  const [note, setNote] = useState("");
  const [recorded, setRecorded] = useState<{ action: "reject" | "request_changes"; note: string } | null>(
    null,
  );

  const reviewable = isApprovable(spec.status);

  const submit = useCallback(
    (action: ApprovalAction, reason?: string) => {
      if (action === "approve") {
        onApprove();
        return;
      }
      const decision = action as "reject" | "request_changes";
      const trimmed = (reason ?? "").trim();
      setRecorded({ action: decision, note: trimmed });
      setActiveNote(null);
      setNote("");
      if (decision === "reject") onReject?.(trimmed);
      else onRequestChanges?.(trimmed);
    },
    [onApprove, onReject, onRequestChanges],
  );

  const trigger = useCallback(
    (action: ApprovalAction) => {
      if (!reviewable || approving) return;
      if (action === "reject" || action === "request_changes") {
        setNote("");
        setActiveNote(action);
      } else {
        submit(action);
      }
    },
    [reviewable, approving, submit],
  );

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (isEditableTarget(event.target) || activeNote) return;
    const action = actionForKey(event.key);
    if (action && REVIEW_ACTIONS.includes(action)) {
      event.preventDefault();
      trigger(action);
    }
  };

  const requirements = spec.requirements ?? [];
  const criteria = spec.acceptance_criteria ?? [];
  const constraints = spec.constraints ?? [];
  const openQuestions = spec.open_questions ?? [];
  const decisions = spec.decisions ?? [];

  return (
    <div
      data-testid="read-mode"
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="flex flex-col gap-6 outline-none"
    >
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-display text-lg font-semibold tracking-tight text-foreground">
            {spec.name}
          </h2>
          <p className="text-xs text-muted-foreground">
            spec.md, rendered read-only for review
          </p>
        </div>
        <span
          data-testid="read-status"
          className={cn(
            "rounded-full border px-2.5 py-0.5 text-xs font-medium capitalize",
            statusBadgeClass(spec.status),
          )}
        >
          {STATUS_LABELS[spec.status ?? "draft"]}
        </span>
      </header>

      <article
        data-testid="read-prose"
        className="flex flex-col gap-5 rounded-lg border border-border bg-card/60 p-5"
      >
        <section>
          <h3 className="font-display text-sm font-semibold text-foreground">Goal</h3>
          <p className="mt-1 text-sm leading-relaxed text-foreground/90">{spec.name}</p>
        </section>

        {requirements.length > 0 ? (
          <section>
            <h3 className="font-display text-sm font-semibold text-foreground">Requirements</h3>
            <ul className="mt-1 flex flex-col gap-1.5">
              {requirements.map((r) => (
                <li key={r.id} className="text-sm leading-relaxed text-foreground/90">
                  <span className="font-mono text-xs text-primary">{r.id}</span> {r.text}
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {criteria.length > 0 ? (
          <section>
            <h3 className="font-display text-sm font-semibold text-foreground">
              Acceptance Criteria
            </h3>
            <ul className="mt-1 flex flex-col gap-1.5">
              {criteria.map((c) => {
                const refs = c.req_refs ?? [];
                return (
                  <li key={c.id} className="text-sm leading-relaxed text-foreground/90">
                    <span className="font-mono text-xs text-primary">
                      {c.id}
                      {refs.length > 0 ? ` (${refs.join(", ")})` : ""}:
                    </span>{" "}
                    {c.text}
                  </li>
                );
              })}
            </ul>
          </section>
        ) : null}

        {constraints.length > 0 ? (
          <section>
            <h3 className="font-display text-sm font-semibold text-foreground">Constraints</h3>
            <ul className="mt-1 flex flex-col gap-1">
              {constraints.map((c, i) => (
                <li key={i} className="text-sm leading-relaxed text-foreground/90">
                  {c}
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {openQuestions.length > 0 ? (
          <section>
            <h3 className="font-display text-sm font-semibold text-foreground">
              Open Questions
            </h3>
            <ul className="mt-1 flex flex-col gap-1">
              {openQuestions.map((q) => (
                <li key={q.id} className="text-sm leading-relaxed text-foreground/90">
                  <span className="font-mono text-xs text-primary">{q.id}</span> {q.text}
                  {q.resolution ? (
                    <span className="mt-0.5 block pl-4 text-xs text-success">
                      Resolution: {q.resolution}
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {decisions.length > 0 ? (
          <section>
            <h3 className="font-display text-sm font-semibold text-foreground">Decisions</h3>
            <ul className="mt-1 flex flex-col gap-2">
              {decisions.map((d) => (
                <li key={d.id} className="text-sm leading-relaxed text-foreground/90">
                  <span className="font-mono text-xs text-primary">{d.id}</span> — {d.title}
                  {d.decision ? (
                    <span className="block text-xs text-muted-foreground">{d.decision}</span>
                  ) : null}
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </article>

      <details className="rounded-lg border border-border bg-card/40 p-4" data-testid="read-manifest-facts">
        <summary className="cursor-pointer font-display text-sm font-semibold text-foreground">
          Manifest facts
        </summary>
        <div className="mt-4">
          <ManifestPanel spec={spec} />
        </div>
      </details>

      <div className="rounded-lg border border-border bg-card" data-testid="review-gate">
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <h3 className="font-display text-sm font-semibold text-foreground">Approval gate</h3>
          {!reviewable ? (
            <span className="text-xs text-muted-foreground" data-testid="review-gate-closed">
              Already past the human gate.
            </span>
          ) : null}
        </div>
        {recorded ? (
          <p
            role="status"
            data-testid="review-recorded"
            className="px-4 pt-3 text-xs text-muted-foreground"
          >
            {recorded.action === "reject" ? "Rejected" : "Changes requested"}
            {recorded.note ? ` — "${recorded.note}"` : ""} (recorded locally; not yet
            persisted server-side).
          </p>
        ) : null}
        <DecisionBar
          actions={REVIEW_ACTIONS}
          activeNote={activeNote}
          note={note}
          onNoteChange={setNote}
          pending={approving}
          disabled={!reviewable}
          errorMessage={approveError}
          onTrigger={trigger}
          onConfirm={() => activeNote && submit(activeNote, note)}
          onCancel={() => {
            setActiveNote(null);
            setNote("");
          }}
        />
      </div>
    </div>
  );
}
