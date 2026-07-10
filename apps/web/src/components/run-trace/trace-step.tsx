"use client";

import { ChevronRight, Clock, Coins, Hash } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";

import type { TraceStep } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  STEP_KIND_META,
  TONE_BADGE_CLASS,
  TONE_NODE_CLASS,
  formatCost,
  formatDuration,
  formatTokens,
  stepPreview,
  stepTitle,
  stepTone,
  stepUsage,
} from "./step-meta";

export interface TraceStepRowProps {
  step: TraceStep;
  /** 1-based display position on the timeline. */
  position: number;
  isActive: boolean;
  isExpanded: boolean;
  /** True while replay's playhead rests on this step. */
  isPlayhead: boolean;
  onToggle: () => void;
  onFocus: () => void;
}

/** Read the sub-agent role the assembler stamps onto delegated steps. */
function subagentRole(step: TraceStep): string | null {
  const role = step.metadata?.subagent_role;
  return typeof role === "string" ? role : null;
}

/**
 * One step on the run-trace timeline: a kind-coloured node on the spine, a
 * one-line title with inline duration/token/cost telemetry, and an expandable
 * detail panel. The whole header is a button so the row toggles by click or
 * keyboard (Enter/Space) and reports its selection upward.
 */
export function TraceStepRow({
  step,
  position,
  isActive,
  isExpanded,
  isPlayhead,
  onToggle,
  onFocus,
}: TraceStepRowProps) {
  const tone = stepTone(step);
  const meta = STEP_KIND_META[step.kind];
  const Icon = meta.Icon;
  const usage = stepUsage(step);
  const preview = stepPreview(step);
  const role = subagentRole(step);
  const rowRef = useRef<HTMLLIElement>(null);

  // Keep the active step in view as the selection / playhead advances.
  useEffect(() => {
    if (isActive && typeof rowRef.current?.scrollIntoView === "function") {
      rowRef.current.scrollIntoView({ block: "nearest" });
    }
  }, [isActive]);

  return (
    <li
      ref={rowRef}
      data-testid="trace-step"
      data-step-index={position}
      aria-current={isActive ? "step" : undefined}
      className="relative"
    >
      {/* The continuous spine behind the nodes. */}
      <span
        aria-hidden
        className="absolute left-[1.4375rem] top-0 bottom-0 w-px bg-border"
      />
      <button
        type="button"
        onClick={() => {
          onFocus();
          onToggle();
        }}
        aria-expanded={isExpanded}
        className={cn(
          "group relative flex w-full items-start gap-3 rounded-lg px-3 py-3 text-left transition-colors",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          isActive ? "bg-accent/60" : "hover:bg-accent/40",
          isPlayhead && "ring-2 ring-primary/60",
        )}
      >
        {/* Node */}
        <span
          className={cn(
            "relative z-10 mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border",
            TONE_NODE_CLASS[tone],
            isPlayhead && "ring-2 ring-primary/40 motion-reduce:animate-none animate-pulse",
          )}
        >
          <Icon aria-hidden className="h-3 w-3" />
        </span>

        {/* Content */}
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="flex items-center gap-2">
            <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
              {String(position).padStart(2, "0")}
            </span>
            <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              {meta.label}
            </span>
            {role ? (
              <span className="rounded-full border border-border bg-muted px-1.5 py-px text-[10px] font-medium text-muted-foreground">
                {role}
              </span>
            ) : null}
          </span>

          <span className="flex items-start justify-between gap-3">
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium text-foreground">
                {stepTitle(step)}
              </span>
              {preview ? (
                <span className="mt-0.5 block truncate text-xs text-muted-foreground">
                  {preview}
                </span>
              ) : null}
            </span>

            <span className="flex shrink-0 items-center gap-3 pt-0.5 text-[11px] tabular-nums text-muted-foreground">
              {step.duration_ms != null ? (
                <span className="inline-flex items-center gap-1" title="Duration">
                  <Clock aria-hidden className="h-3 w-3" />
                  {formatDuration(step.duration_ms)}
                </span>
              ) : null}
              {usage.totalTokens != null ? (
                <span className="inline-flex items-center gap-1" title="Tokens">
                  <Hash aria-hidden className="h-3 w-3" />
                  {formatTokens(usage.totalTokens)}
                </span>
              ) : null}
              {usage.costUsd != null ? (
                <span className="inline-flex items-center gap-1" title="Cost">
                  <Coins aria-hidden className="h-3 w-3" />
                  {formatCost(usage.costUsd)}
                </span>
              ) : null}
              <ChevronRight
                aria-hidden
                className={cn(
                  "h-4 w-4 transition-transform",
                  isExpanded && "rotate-90",
                )}
              />
            </span>
          </span>
        </span>
      </button>

      {isExpanded ? (
        <div className="ml-12 mr-3 mb-2 rounded-lg border border-border bg-card/60 p-4">
          <StepDetail step={step} />
        </div>
      ) : null}
    </li>
  );
}

// --- expanded detail ------------------------------------------------------ //

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <dt className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="text-sm text-foreground">{children}</dd>
    </div>
  );
}

function CodeBlock({ value }: { value: string }) {
  return (
    <div className="overflow-x-auto rounded-md border border-border bg-muted/50 p-3">
      <pre className="font-mono text-xs leading-relaxed text-foreground">
        {value}
      </pre>
    </div>
  );
}

function Prose({ value }: { value: string }) {
  return (
    <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-foreground">
      {value}
    </p>
  );
}

/** Keys already surfaced elsewhere, so the "Metadata" dump stays signal-only. */
const HANDLED_META_KEYS = new Set([
  "input_tokens",
  "prompt_tokens",
  "output_tokens",
  "completion_tokens",
  "total_tokens",
  "tokens",
  "token_count",
  "cost_usd",
  "cost",
  "model",
  "subagent_role",
]);

function StepDetail({ step }: { step: TraceStep }) {
  const usage = stepUsage(step);
  const hasUsage =
    usage.totalTokens != null || usage.costUsd != null || usage.model != null;

  const extraMeta = Object.fromEntries(
    Object.entries(step.metadata ?? {}).filter(
      ([key]) => !HANDLED_META_KEYS.has(key),
    ),
  );
  const hasExtraMeta = Object.keys(extraMeta).length > 0;

  return (
    <dl data-testid="step-detail" className="grid gap-4">
      {step.thought ? (
        <Field label="Reasoning">
          <Prose value={step.thought} />
        </Field>
      ) : null}

      {step.tool_call ? (
        <Field label="Tool call">
          <div className="grid gap-2">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
              <span className="font-mono text-foreground">
                {step.tool_call.tool}
              </span>
              {step.tool_call.action ? (
                <span className="text-muted-foreground">
                  action: <span className="font-mono">{step.tool_call.action}</span>
                </span>
              ) : null}
              {step.tool_call.path ? (
                <span className="text-muted-foreground">
                  path: <span className="font-mono">{step.tool_call.path}</span>
                </span>
              ) : null}
              {step.tool_call.connection_id ? (
                <span className="text-muted-foreground">
                  via <span className="font-mono">{step.tool_call.connection_id}</span>
                </span>
              ) : null}
            </div>
            {step.tool_call.arguments &&
            Object.keys(step.tool_call.arguments).length > 0 ? (
              <CodeBlock value={JSON.stringify(step.tool_call.arguments, null, 2)} />
            ) : null}
          </div>
        </Field>
      ) : null}

      {step.observation ? (
        <Field label="Observation">
          <Prose value={step.observation} />
        </Field>
      ) : null}

      {step.output ? (
        <Field label="Output">
          <Prose value={step.output} />
        </Field>
      ) : null}

      {step.decision ? (
        <Field label="Policy decision">
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <span
                className={cn(
                  "rounded-full border px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide",
                  TONE_BADGE_CLASS[stepTone(step)],
                )}
              >
                {step.decision.effect.replace(/_/g, " ")}
              </span>
              {step.decision.severity ? (
                <span className="text-xs text-muted-foreground">
                  severity: {step.decision.severity}
                </span>
              ) : null}
              {step.decision.approval_gate ? (
                <span className="text-xs text-muted-foreground">
                  gate: {step.decision.approval_gate}
                </span>
              ) : null}
            </div>
            {step.decision.reason ? <Prose value={step.decision.reason} /> : null}
            {step.decision.matched_rule ? (
              <p className="text-xs text-muted-foreground">
                matched rule:{" "}
                <span className="font-mono">{step.decision.matched_rule}</span>
              </p>
            ) : null}
          </div>
        </Field>
      ) : null}

      {hasUsage ? (
        <Field label="Usage">
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
            {usage.model ? (
              <span className="text-muted-foreground">
                model: <span className="font-mono text-foreground">{usage.model}</span>
              </span>
            ) : null}
            {usage.inputTokens != null ? (
              <span className="text-muted-foreground">
                in: <span className="tabular-nums text-foreground">{formatTokens(usage.inputTokens)}</span>
              </span>
            ) : null}
            {usage.outputTokens != null ? (
              <span className="text-muted-foreground">
                out: <span className="tabular-nums text-foreground">{formatTokens(usage.outputTokens)}</span>
              </span>
            ) : null}
            {usage.totalTokens != null ? (
              <span className="text-muted-foreground">
                total: <span className="tabular-nums text-foreground">{formatTokens(usage.totalTokens)}</span>
              </span>
            ) : null}
            {usage.costUsd != null ? (
              <span className="text-muted-foreground">
                cost: <span className="tabular-nums text-foreground">{formatCost(usage.costUsd)}</span>
              </span>
            ) : null}
          </div>
        </Field>
      ) : null}

      <Field label="Timing">
        <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
          <span>duration: {formatDuration(step.duration_ms)}</span>
          {step.confidence != null ? (
            <span>confidence: {Math.round(step.confidence * 100)}%</span>
          ) : null}
          {step.timestamp ? (
            <span className="font-mono">{step.timestamp}</span>
          ) : null}
        </div>
      </Field>

      {hasExtraMeta ? (
        <Field label="Metadata">
          <CodeBlock value={JSON.stringify(extraMeta, null, 2)} />
        </Field>
      ) : null}
    </dl>
  );
}
