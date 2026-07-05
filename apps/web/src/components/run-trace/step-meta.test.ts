import { describe, expect, it } from "vitest";

import type { TraceStep } from "@/lib/api/types";

import {
  formatCost,
  formatDuration,
  formatTokens,
  runStatusMeta,
  stepPreview,
  stepTitle,
  stepTone,
  stepUsage,
  traceTotals,
} from "./step-meta";

function step(partial: Partial<TraceStep>): TraceStep {
  return { kind: "message", ...partial };
}

describe("formatDuration", () => {
  it("formats sub-second, second, and minute durations", () => {
    expect(formatDuration(340)).toBe("340ms");
    expect(formatDuration(1200)).toBe("1.2s");
    expect(formatDuration(12000)).toBe("12s");
    expect(formatDuration(65000)).toBe("1m 5s");
  });
  it("guards nullish / non-finite input", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
    expect(formatDuration(Number.NaN)).toBe("—");
  });
});

describe("formatTokens", () => {
  it("keeps small counts and abbreviates thousands", () => {
    expect(formatTokens(812)).toBe("812");
    expect(formatTokens(1500)).toBe("1.5k");
    expect(formatTokens(null)).toBe("—");
  });
});

describe("formatCost", () => {
  it("uses extra precision for sub-cent amounts", () => {
    expect(formatCost(0)).toBe("$0.00");
    expect(formatCost(0.0012)).toBe("$0.0012");
    expect(formatCost(1.234)).toBe("$1.23");
    expect(formatCost(undefined)).toBe("—");
  });
});

describe("stepUsage", () => {
  it("pulls token/cost/model telemetry out of metadata", () => {
    const usage = stepUsage(
      step({
        metadata: {
          input_tokens: 1000,
          output_tokens: 200,
          cost_usd: 0.004,
          model: "claude-sonnet",
        },
      }),
    );
    expect(usage.inputTokens).toBe(1000);
    expect(usage.outputTokens).toBe(200);
    expect(usage.totalTokens).toBe(1200);
    expect(usage.costUsd).toBe(0.004);
    expect(usage.model).toBe("claude-sonnet");
  });
  it("returns nulls when metadata is empty", () => {
    const usage = stepUsage(step({}));
    expect(usage.totalTokens).toBeNull();
    expect(usage.costUsd).toBeNull();
  });
});

describe("traceTotals", () => {
  it("sums step-level tokens and cost", () => {
    const totals = traceTotals([
      step({ metadata: { input_tokens: 500, output_tokens: 100, cost_usd: 0.01 } }),
      step({ metadata: { total_tokens: 400, cost: 0.02 } }),
      step({}),
    ]);
    expect(totals.tokens).toBe(1000);
    expect(totals.costUsd).toBeCloseTo(0.03);
    expect(totals.hasTokens).toBe(true);
    expect(totals.hasCost).toBe(true);
  });
});

describe("stepTitle / stepPreview", () => {
  it("titles a tool call with its tool and target", () => {
    expect(
      stepTitle(
        step({ kind: "tool_call", tool_call: { tool: "fs.read", path: "src/app.ts" } }),
      ),
    ).toBe("fs.read · src/app.ts");
  });
  it("titles a decision with its effect and reason", () => {
    expect(
      stepTitle(
        step({
          kind: "decision",
          decision: { effect: "deny", reason: "path not allowed" },
        }),
      ),
    ).toBe("Denied · path not allowed");
  });
  it("falls back to the first non-empty line for prose steps", () => {
    expect(stepTitle(step({ kind: "output", output: "\nShipped PR #42\nmore" }))).toBe(
      "Shipped PR #42",
    );
  });
  it("previews the thought under a tool call", () => {
    expect(
      stepPreview(
        step({ kind: "tool_call", thought: "read entrypoint", tool_call: { tool: "x" } }),
      ),
    ).toBe("read entrypoint");
  });
});

describe("stepTone / runStatusMeta", () => {
  it("colours decisions by their effect", () => {
    expect(stepTone(step({ kind: "decision", decision: { effect: "allow" } }))).toBe(
      "success",
    );
    expect(stepTone(step({ kind: "decision", decision: { effect: "deny" } }))).toBe(
      "danger",
    );
    expect(stepTone(step({ kind: "error" }))).toBe("danger");
  });
  it("maps run status to a label + tone", () => {
    expect(runStatusMeta("succeeded")).toEqual({ label: "Succeeded", tone: "success" });
    expect(runStatusMeta("failed").tone).toBe("danger");
    expect(runStatusMeta(null).label).toBe("Unknown");
  });
});
