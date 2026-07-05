"use client";

import {
  BookOpen,
  CheckCircle2,
  FileDiff,
  GitBranch,
  Target,
  ShieldQuestion,
  XCircle,
  MinusCircle,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";

import type {
  ApprovalContext,
  ApprovalSummary,
  RiskFlag,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { riskBadgeClass, riskLabel } from "./approval-meta";
import { humanizeKey } from "./format";

// --- small `unknown` narrowing helpers ------------------------------------ //

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}
function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}
function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function scalar(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

export interface ReviewPanelProps {
  summary: ApprovalSummary;
  context: ApprovalContext | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

/**
 * The nine "must-show" review items (spec: Approval UI Must Show). Sections map
 * to fixed positions 1–9; item 9 (available actions) is realised by the
 * decision bar below the panel. Gate-agnostic `dict` payloads render
 * defensively so any provider's shape is legible.
 */
export function ReviewPanel({
  summary,
  context,
  isLoading,
  isError,
  onRetry,
}: ReviewPanelProps) {
  if (isLoading) {
    return <ReviewSkeleton />;
  }
  if (isError || !context) {
    return (
      <div
        role="alert"
        data-testid="review-error"
        className="m-4 rounded-lg border border-dashed border-danger/40 bg-danger/5 p-6 text-center"
      >
        <p className="text-sm font-medium text-foreground">
          Couldn&apos;t load the review context.
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          The gate exists but its context provider is unavailable right now.
        </p>
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="mt-3 inline-flex items-center rounded-md border border-border px-3 py-1.5 text-xs font-medium hover:bg-accent"
          >
            Retry
          </button>
        ) : null}
      </div>
    );
  }

  const goal = context.goal?.trim() || summary.title;
  const requirements = asArray(context.requirements);
  const diff = asRecord(context.diff);
  const verification = asRecord(context.verification);
  const traceability = context.traceability ?? [];
  const knowledge = context.knowledge_refs ?? [];
  const confidence = asRecord(context.confidence);
  const risks = context.risk_flags ?? [];
  const runTrace = asRecord(context.run_trace_ref);

  return (
    <div
      data-testid="review-panel"
      className="flex flex-col gap-5 px-6 py-5"
    >
      {/* 1 — Goal & requirements (always shown) */}
      <Section n={1} title="Goal & requirements" icon={Target}>
        <p className="text-sm leading-relaxed text-foreground">
          {goal || "No goal recorded for this gate."}
        </p>
        {requirements.length > 0 ? (
          <ul className="mt-3 flex flex-col gap-1.5">
            {requirements.map((req, i) => (
              <RequirementRow key={i} value={req} />
            ))}
          </ul>
        ) : null}
      </Section>

      {/* 2 — Diff preview */}
      {diff ? (
        <Section n={2} title="Diff preview" icon={FileDiff}>
          <DiffPreview diff={diff} />
        </Section>
      ) : null}

      {/* 3 — Verification results */}
      {verification ? (
        <Section n={3} title="Verification results" icon={CheckCircle2}>
          <VerificationGrid verification={verification} />
        </Section>
      ) : null}

      {/* 4 — Spec traceability */}
      {traceability.length > 0 ? (
        <Section n={4} title="Spec traceability" icon={GitBranch}>
          <ul className="flex flex-col divide-y divide-border overflow-hidden rounded-md border border-border">
            {traceability.map((row, i) => (
              <TraceabilityRow key={i} value={row} />
            ))}
          </ul>
        </Section>
      ) : null}

      {/* 5 — Knowledge provenance */}
      {knowledge.length > 0 ? (
        <Section n={5} title="Knowledge provenance" icon={BookOpen}>
          <ul className="flex flex-col gap-2">
            {knowledge.map((row, i) => (
              <KnowledgeRow key={i} value={row} />
            ))}
          </ul>
        </Section>
      ) : null}

      {/* 6 — Confidence */}
      {confidence ? (
        <Section n={6} title="Confidence" icon={ShieldQuestion}>
          <ConfidenceMeter confidence={confidence} />
        </Section>
      ) : null}

      {/* 7 — Risks flagged (always shown) */}
      <Section n={7} title="Risks flagged" icon={XCircle}>
        {risks.length > 0 ? (
          <ul className="flex flex-col gap-2" data-testid="risk-flags">
            {risks.map((flag, i) => (
              <RiskRow key={i} flag={flag} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">No risks flagged.</p>
        )}
      </Section>

      {/* 8 — Run trace */}
      {runTrace ? (
        <Section n={8} title="Run trace" icon={GitBranch}>
          <RunTrace runTrace={runTrace} />
        </Section>
      ) : null}
    </div>
  );
}

// --- Section shell -------------------------------------------------------- //

interface SectionProps {
  n: number;
  title: string;
  icon: LucideIcon;
  children: ReactNode;
}

function Section({ n, title, icon: Icon, children }: SectionProps) {
  return (
    <section
      aria-label={title}
      data-testid={`review-section-${n}`}
      className="scroll-mt-4"
    >
      <div className="mb-2 flex items-center gap-2">
        <span className="flex h-5 w-5 items-center justify-center rounded-full border border-border font-mono text-[11px] text-muted-foreground">
          {n}
        </span>
        <Icon aria-hidden className="h-4 w-4 text-muted-foreground" />
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {title}
        </h3>
      </div>
      <div className="pl-7">{children}</div>
    </section>
  );
}

// --- 1. Requirements ------------------------------------------------------ //

function RequirementRow({ value }: { value: unknown }) {
  const rec = asRecord(value);
  const text =
    scalar(value) ??
    asString(rec?.text) ??
    asString(rec?.title) ??
    asString(rec?.requirement) ??
    asString(rec?.id) ??
    JSON.stringify(value);
  const ref = asString(rec?.ref) ?? asString(rec?.spec_ref) ?? asString(rec?.id);
  return (
    <li className="flex items-start gap-2 text-sm text-foreground">
      <span aria-hidden className="mt-2 h-1 w-1 shrink-0 rounded-full bg-primary" />
      <span className="flex-1">{text}</span>
      {ref && ref !== text ? (
        <span className="shrink-0 font-mono text-xs text-muted-foreground">{ref}</span>
      ) : null}
    </li>
  );
}

// --- 2. Diff -------------------------------------------------------------- //

function DiffPreview({ diff }: { diff: Record<string, unknown> }) {
  const files = asArray(diff.files);
  const summaryText = asString(diff.summary);
  const filesChanged = asNumber(diff.files_changed) ?? (files.length || null);
  const additions = asNumber(diff.additions);
  const deletions = asNumber(diff.deletions);

  if (files.length === 0) {
    // No file list — surface the scalar summary keys the provider did give us.
    return <KeyValueGrid record={diff} />;
  }

  return (
    <div className="overflow-hidden rounded-md border border-border">
      <div className="flex items-center justify-between gap-3 border-b border-border bg-muted/40 px-3 py-1.5 text-xs text-muted-foreground">
        <span>
          {filesChanged} file{filesChanged === 1 ? "" : "s"} changed
        </span>
        <span className="flex items-center gap-2 font-mono">
          {additions !== null ? (
            <span className="text-success">+{additions}</span>
          ) : null}
          {deletions !== null ? (
            <span className="text-danger">-{deletions}</span>
          ) : null}
        </span>
      </div>
      <ul className="divide-y divide-border" data-testid="diff-files">
        {files.map((file, i) => (
          <DiffFileRow key={i} value={file} />
        ))}
      </ul>
      {summaryText ? (
        <p className="border-t border-border px-3 py-2 text-xs text-muted-foreground">
          {summaryText}
        </p>
      ) : null}
    </div>
  );
}

function DiffFileRow({ value }: { value: unknown }) {
  const rec = asRecord(value);
  const path = asString(rec?.path) ?? scalar(value) ?? "unknown file";
  const add = asNumber(rec?.additions);
  const del = asNumber(rec?.deletions);
  const status = asString(rec?.status);
  return (
    <li className="flex items-center justify-between gap-3 px-3 py-2 text-xs">
      <span className="flex min-w-0 items-center gap-2">
        {status ? (
          <span className="shrink-0 rounded border border-border px-1 py-0.5 font-mono text-[10px] uppercase text-muted-foreground">
            {status.slice(0, 3)}
          </span>
        ) : null}
        <span className="truncate font-mono text-foreground">{path}</span>
      </span>
      <span className="flex shrink-0 items-center gap-2 font-mono">
        {add !== null ? <span className="text-success">+{add}</span> : null}
        {del !== null ? <span className="text-danger">-{del}</span> : null}
      </span>
    </li>
  );
}

// --- 3. Verification ------------------------------------------------------ //

type CheckTone = "pass" | "fail" | "neutral";

function checkTone(value: unknown): CheckTone {
  if (typeof value === "boolean") return value ? "pass" : "fail";
  const rec = asRecord(value);
  const status = (
    asString(rec?.status) ??
    asString(rec?.result) ??
    asString(value)
  )?.toLowerCase();
  const passedFlag = rec?.passed;
  if (typeof passedFlag === "boolean") return passedFlag ? "pass" : "fail";
  if (status) {
    if (["pass", "passed", "success", "ok", "green", "clean"].includes(status))
      return "pass";
    if (["fail", "failed", "error", "red", "broken"].includes(status))
      return "fail";
  }
  return "neutral";
}

function checkDetail(value: unknown): string | null {
  const rec = asRecord(value);
  if (!rec) {
    return typeof value === "boolean" ? null : scalar(value);
  }
  const cov = asNumber(rec.coverage) ?? asNumber(rec.percent);
  if (cov !== null) return `${Math.round(cov <= 1 ? cov * 100 : cov)}%`;
  const passed = asNumber(rec.passed);
  const total = asNumber(rec.total);
  if (passed !== null && total !== null) return `${passed}/${total}`;
  return asString(rec.status) ?? asString(rec.result);
}

const TONE_ICON: Record<CheckTone, LucideIcon> = {
  pass: CheckCircle2,
  fail: XCircle,
  neutral: MinusCircle,
};
const TONE_CLASS: Record<CheckTone, string> = {
  pass: "text-success",
  fail: "text-danger",
  neutral: "text-muted-foreground",
};

function VerificationGrid({
  verification,
}: {
  verification: Record<string, unknown>;
}) {
  const entries = Object.entries(verification);
  if (entries.length === 0) {
    return <p className="text-sm text-muted-foreground">No checks reported.</p>;
  }
  return (
    <div
      className="grid grid-cols-2 gap-2 sm:grid-cols-3"
      data-testid="verification-grid"
    >
      {entries.map(([key, value]) => {
        const tone = checkTone(value);
        const Icon = TONE_ICON[tone];
        const detail = checkDetail(value);
        return (
          <div
            key={key}
            className="flex items-center gap-2 rounded-md border border-border px-2.5 py-2"
          >
            <Icon aria-hidden className={cn("h-4 w-4 shrink-0", TONE_CLASS[tone])} />
            <span className="flex min-w-0 flex-col">
              <span className="truncate text-xs font-medium text-foreground">
                {humanizeKey(key)}
              </span>
              {detail ? (
                <span className="truncate text-[11px] text-muted-foreground">
                  {detail}
                </span>
              ) : null}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// --- 4. Traceability ------------------------------------------------------ //

function TraceabilityRow({ value }: { value: unknown }) {
  const rec = asRecord(value) ?? {};
  const requirement =
    asString(rec.requirement) ??
    asString(rec.text) ??
    asString(rec.title) ??
    scalar(value) ??
    "Requirement";
  const ref = asString(rec.spec_ref) ?? asString(rec.ref) ?? asString(rec.id);
  const covered =
    typeof rec.covered === "boolean"
      ? rec.covered
      : asString(rec.status)?.toLowerCase() === "covered"
        ? true
        : asString(rec.status)?.toLowerCase() === "missing"
          ? false
          : null;
  return (
    <li className="flex items-center justify-between gap-3 bg-card px-3 py-2 text-sm">
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-foreground">{requirement}</span>
        {ref ? (
          <span className="font-mono text-xs text-muted-foreground">{ref}</span>
        ) : null}
      </span>
      {covered !== null ? (
        <span
          className={cn(
            "shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium",
            covered
              ? "border-success/40 bg-success/10 text-success"
              : "border-danger/40 bg-danger/10 text-danger",
          )}
        >
          {covered ? "Covered" : "Missing"}
        </span>
      ) : null}
    </li>
  );
}

// --- 5. Knowledge provenance ---------------------------------------------- //

function KnowledgeRow({ value }: { value: unknown }) {
  const rec = asRecord(value) ?? {};
  const title =
    asString(rec.title) ??
    asString(rec.source) ??
    asString(rec.path) ??
    scalar(value) ??
    "Reference";
  const path = asString(rec.path);
  const score = asNumber(rec.score);
  return (
    <li className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2">
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-sm text-foreground">{title}</span>
        {path && path !== title ? (
          <span className="truncate font-mono text-xs text-muted-foreground">
            {path}
          </span>
        ) : null}
      </span>
      {score !== null ? (
        <span className="shrink-0 font-mono text-xs text-muted-foreground">
          {(score <= 1 ? score : score / 100).toFixed(2)}
        </span>
      ) : null}
    </li>
  );
}

// --- 6. Confidence -------------------------------------------------------- //

function ConfidenceMeter({
  confidence,
}: {
  confidence: Record<string, unknown>;
}) {
  const raw = asNumber(confidence.score);
  const pct =
    raw === null ? null : Math.round(Math.max(0, Math.min(1, raw <= 1 ? raw : raw / 100)) * 100);
  const rationale = asString(confidence.rationale);
  const tone =
    pct === null
      ? "bg-muted-foreground"
      : pct >= 75
        ? "bg-success"
        : pct >= 50
          ? "bg-warning"
          : "bg-danger";
  return (
    <div className="flex flex-col gap-2" data-testid="confidence">
      {pct !== null ? (
        <div className="flex items-center gap-3">
          <div
            role="meter"
            aria-valuenow={pct}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="Confidence score"
            className="h-2 flex-1 overflow-hidden rounded-full bg-muted"
          >
            <div className={cn("h-full rounded-full", tone)} style={{ width: `${pct}%` }} />
          </div>
          <span className="w-10 shrink-0 text-right font-mono text-sm text-foreground">
            {pct}%
          </span>
        </div>
      ) : null}
      {rationale ? (
        <p className="text-sm leading-relaxed text-muted-foreground">{rationale}</p>
      ) : null}
    </div>
  );
}

// --- 7. Risks ------------------------------------------------------------- //

function RiskRow({ flag }: { flag: RiskFlag }) {
  const severity = flag.severity ?? "info";
  return (
    <li
      className={cn(
        "flex items-start gap-3 rounded-md border px-3 py-2",
        riskBadgeClass(severity),
      )}
    >
      <span className="mt-0.5 shrink-0 rounded-full border border-current/30 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide">
        {riskLabel(severity)}
      </span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="text-sm text-foreground">{flag.message}</span>
        <span className="text-xs text-muted-foreground">
          {flag.category}
          {flag.source ? ` · ${flag.source}` : ""}
        </span>
      </span>
    </li>
  );
}

// --- 8. Run trace --------------------------------------------------------- //

function RunTrace({ runTrace }: { runTrace: Record<string, unknown> }) {
  const entries = Object.entries(runTrace).filter(([, v]) => scalar(v) !== null);
  if (entries.length === 0) {
    return <p className="text-sm text-muted-foreground">No run linked.</p>;
  }
  return (
    <dl className="flex flex-col gap-1.5" data-testid="run-trace">
      {entries.map(([key, value]) => (
        <div key={key} className="flex items-center justify-between gap-3">
          <dt className="text-xs text-muted-foreground">{humanizeKey(key)}</dt>
          <dd className="truncate font-mono text-xs text-foreground">
            {scalar(value)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

// --- generic dict fallback ------------------------------------------------ //

function KeyValueGrid({ record }: { record: Record<string, unknown> }) {
  const entries = Object.entries(record).filter(([, v]) => scalar(v) !== null);
  if (entries.length === 0) {
    return <p className="text-sm text-muted-foreground">No details.</p>;
  }
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5">
      {entries.map(([key, value]) => (
        <div key={key} className="flex items-center justify-between gap-2">
          <dt className="text-xs text-muted-foreground">{humanizeKey(key)}</dt>
          <dd className="font-mono text-xs text-foreground">{scalar(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

// --- loading skeleton ----------------------------------------------------- //

function ReviewSkeleton() {
  return (
    <div
      data-testid="review-skeleton"
      aria-busy="true"
      aria-label="Loading review context"
      className="flex flex-col gap-5 px-6 py-5"
    >
      {[0, 1, 2].map((i) => (
        <div key={i} className="flex flex-col gap-2">
          <div className="h-3 w-32 animate-pulse rounded bg-muted" />
          <div className="h-16 w-full animate-pulse rounded-md bg-muted/60" />
        </div>
      ))}
    </div>
  );
}
