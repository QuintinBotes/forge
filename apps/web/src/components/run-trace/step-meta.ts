/**
 * Pure presentation helpers for the run-trace viewer (fully unit-testable).
 *
 * These map a {@link TraceStep} to its icon / accent, derive a compact title +
 * preview line, and extract token/cost telemetry from the step's free-form
 * `metadata` bag (the backend records usage there per step). Everything here is
 * side-effect-free so the timeline component stays thin.
 */

import {
  AlertTriangle,
  ArrowRightLeft,
  Eye,
  FileOutput,
  GitBranch,
  MessageSquare,
  Target,
  Wrench,
  type LucideIcon,
} from "lucide-react";

import type {
  RunStatus,
  RunTrace,
  StepKind,
  TraceStep,
} from "@/lib/api/types";

// --- kind + tone metadata ------------------------------------------------- //

export type Tone = "muted" | "info" | "success" | "warning" | "danger" | "ember";

export const STEP_KIND_META: Record<StepKind, { label: string; Icon: LucideIcon }> =
  {
    plan: { label: "Plan", Icon: Target },
    tool_call: { label: "Tool call", Icon: Wrench },
    observation: { label: "Observation", Icon: Eye },
    decision: { label: "Decision", Icon: GitBranch },
    message: { label: "Message", Icon: MessageSquare },
    output: { label: "Output", Icon: FileOutput },
    error: { label: "Error", Icon: AlertTriangle },
    handoff: { label: "Handoff", Icon: ArrowRightLeft },
  };

/** Node/badge classes per tone — all Forge tokens, never raw colour. */
export const TONE_NODE_CLASS: Record<Tone, string> = {
  muted: "border-border bg-muted text-muted-foreground",
  info: "border-border bg-card text-foreground",
  success: "border-success/40 bg-success/10 text-success",
  warning: "border-warning/40 bg-warning/10 text-warning",
  danger: "border-danger/40 bg-danger/10 text-danger",
  ember: "border-primary/40 bg-primary/10 text-primary",
};

/** Kind-only tone (ignores decision effect) — used by the composition legend. */
export const KIND_TONE: Record<StepKind, Tone> = {
  plan: "info",
  tool_call: "info",
  observation: "muted",
  decision: "info",
  message: "muted",
  output: "success",
  error: "danger",
  handoff: "warning",
};

/** The accent tone for a step, driven by kind and (for decisions) effect. */
export function stepTone(step: TraceStep): Tone {
  switch (step.kind) {
    case "error":
      return "danger";
    case "handoff":
      return "warning";
    case "output":
      return "success";
    case "decision": {
      const effect = step.decision?.effect;
      if (effect === "allow") return "success";
      if (effect === "deny") return "danger";
      if (effect === "requires_approval") return "warning";
      return "info";
    }
    case "observation":
    case "message":
      return "muted";
    default:
      return "info";
  }
}

const RUN_STATUS_META: Record<RunStatus, { label: string; tone: Tone }> = {
  pending: { label: "Pending", tone: "muted" },
  running: { label: "Running", tone: "ember" },
  succeeded: { label: "Succeeded", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  escalated: { label: "Escalated", tone: "warning" },
  cancelled: { label: "Cancelled", tone: "muted" },
};

export function runStatusMeta(
  status: RunStatus | null | undefined,
): { label: string; tone: Tone } {
  return status ? RUN_STATUS_META[status] : { label: "Unknown", tone: "muted" };
}

/** Badge classes for a tone (border + faint fill + text). */
export const TONE_BADGE_CLASS: Record<Tone, string> = TONE_NODE_CLASS;

// --- number/string formatting --------------------------------------------- //

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

export function formatTokens(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (Math.abs(n) < 1000) return String(Math.round(n));
  return `${(n / 1000).toFixed(1)}k`;
}

export function formatCost(usd: number | null | undefined): string {
  if (usd == null || !Number.isFinite(usd)) return "—";
  if (usd === 0) return "$0.00";
  if (Math.abs(usd) < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

// --- metadata extraction (token/cost per step) ---------------------------- //

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export interface StepUsage {
  inputTokens: number | null;
  outputTokens: number | null;
  totalTokens: number | null;
  costUsd: number | null;
  model: string | null;
}

/** Pull token/cost/model telemetry out of a step's `metadata` bag. */
export function stepUsage(step: TraceStep): StepUsage {
  const meta = step.metadata ?? {};
  const inputTokens = num(meta.input_tokens) ?? num(meta.prompt_tokens);
  const outputTokens = num(meta.output_tokens) ?? num(meta.completion_tokens);
  const explicitTotal =
    num(meta.total_tokens) ?? num(meta.tokens) ?? num(meta.token_count);
  const summed =
    inputTokens != null || outputTokens != null
      ? (inputTokens ?? 0) + (outputTokens ?? 0)
      : null;
  const model = typeof meta.model === "string" ? meta.model : null;
  return {
    inputTokens,
    outputTokens,
    totalTokens: explicitTotal ?? summed,
    costUsd: num(meta.cost_usd) ?? num(meta.cost),
    model,
  };
}

export interface TraceTotals {
  tokens: number;
  costUsd: number;
  hasTokens: boolean;
  hasCost: boolean;
}

/** Roll step-level telemetry up into run-level totals for the header. */
export function traceTotals(steps: TraceStep[]): TraceTotals {
  let tokens = 0;
  let costUsd = 0;
  let hasTokens = false;
  let hasCost = false;
  for (const step of steps) {
    const usage = stepUsage(step);
    if (usage.totalTokens != null) {
      tokens += usage.totalTokens;
      hasTokens = true;
    }
    if (usage.costUsd != null) {
      costUsd += usage.costUsd;
      hasCost = true;
    }
  }
  return { tokens, costUsd, hasTokens, hasCost };
}

// --- title + preview ------------------------------------------------------ //

function firstLine(text: string | null | undefined): string | null {
  if (!text) return null;
  const line = text.split("\n").find((l) => l.trim().length > 0);
  return line ? line.trim() : null;
}

/** A short, human title for the collapsed step row. */
export function stepTitle(step: TraceStep): string {
  switch (step.kind) {
    case "tool_call": {
      const call = step.tool_call;
      if (call) {
        const target = call.action ?? call.path ?? call.resource;
        return target ? `${call.tool} · ${target}` : call.tool;
      }
      return firstLine(step.thought) ?? "Tool call";
    }
    case "decision": {
      const d = step.decision;
      if (d) {
        const label =
          d.effect === "allow"
            ? "Allowed"
            : d.effect === "deny"
              ? "Denied"
              : "Approval required";
        return d.reason ? `${label} · ${d.reason}` : label;
      }
      return "Decision";
    }
    default:
      return (
        firstLine(step.output) ??
        firstLine(step.thought) ??
        firstLine(step.observation) ??
        STEP_KIND_META[step.kind].label
      );
  }
}

/** A secondary preview line shown under the title when collapsed. */
export function stepPreview(step: TraceStep): string | null {
  const candidate =
    step.kind === "tool_call" || step.kind === "decision"
      ? (firstLine(step.thought) ?? firstLine(step.observation))
      : firstLine(step.observation);
  return candidate ?? null;
}
