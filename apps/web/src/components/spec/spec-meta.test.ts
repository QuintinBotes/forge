import { describe, expect, it } from "vitest";

import type { SpecOverview } from "@/lib/api/types";

import {
  coveragePercent,
  formatCoverage,
  gateSummary,
  isApprovable,
  PLAIN_LIFECYCLE_STEPS,
  plainCurrentStep,
  plainStepCompletion,
  plainStepState,
  traceSealed,
} from "./spec-meta";

describe("coverage helpers", () => {
  it("normalises a 0–1 fraction to a percent", () => {
    expect(coveragePercent(0.87)).toBe(87);
    expect(formatCoverage(0.87)).toBe("87%");
  });

  it("passes through a 0–100 value and clamps the range", () => {
    expect(coveragePercent(92)).toBe(92);
    expect(coveragePercent(150)).toBe(100);
  });

  it("renders an em dash when coverage is unknown", () => {
    expect(formatCoverage(null)).toBe("—");
    expect(coveragePercent(undefined)).toBeNull();
  });
});

describe("isApprovable", () => {
  it("is true only at or before the human gate", () => {
    expect(isApprovable("draft")).toBe(true);
    expect(isApprovable("clarifying")).toBe(true);
    expect(isApprovable("approved")).toBe(false);
    expect(isApprovable("validated")).toBe(false);
  });
});

describe("plain-language lifecycle stepper", () => {
  it("has five steps whose actions match the /spec engine calls", () => {
    expect(PLAIN_LIFECYCLE_STEPS.map((s) => s.label)).toEqual([
      "Describe",
      "Refine",
      "Approve",
      "Build",
      "Verify",
    ]);
    expect(PLAIN_LIFECYCLE_STEPS.map((s) => s.actionLabel)).toEqual([
      "Clarify",
      "Plan",
      "Approve",
      "Generate tasks",
      "Validate",
    ]);
  });

  it("marks nothing done for a fresh draft, current = Describe", () => {
    const completion = plainStepCompletion({ status: "draft" });
    expect(completion).toEqual([false, false, false, false, false]);
    expect(plainCurrentStep(completion)).toBe(0);
  });

  it("marks Describe done once clarified, current = Refine", () => {
    const completion = plainStepCompletion({ status: "clarifying" });
    expect(completion).toEqual([true, false, false, false, false]);
    expect(plainCurrentStep(completion)).toBe(1);
  });

  it("marks Refine done once a plan exists, independent of status", () => {
    const completion = plainStepCompletion({ status: "clarifying", plan_ref: "plan.md" });
    expect(completion).toEqual([true, true, false, false, false]);
    expect(plainCurrentStep(completion)).toBe(2);
  });

  it("marks Approve done once the spec is approved (or beyond)", () => {
    const completion = plainStepCompletion({
      status: "approved",
      plan_ref: "plan.md",
    });
    expect(completion).toEqual([true, true, true, false, false]);
    expect(plainCurrentStep(completion)).toBe(3);
  });

  it("marks Build done once tasks are generated", () => {
    const completion = plainStepCompletion({
      status: "approved",
      plan_ref: "plan.md",
      tasks_ref: "tasks.md",
    });
    expect(completion).toEqual([true, true, true, true, false]);
    expect(plainCurrentStep(completion)).toBe(4);
  });

  it("marks Verify done once validated status or a passing report lands", () => {
    const byStatus = plainStepCompletion({
      status: "validated",
      plan_ref: "plan.md",
      tasks_ref: "tasks.md",
    });
    expect(byStatus).toEqual([true, true, true, true, true]);
    expect(plainCurrentStep(byStatus)).toBe(4);

    const byReport = plainStepCompletion({
      status: "approved",
      plan_ref: "plan.md",
      tasks_ref: "tasks.md",
      validation: { passed: true },
    });
    expect(byReport[4]).toBe(true);
  });

  it("falls back to draft-like state for an unknown status", () => {
    expect(plainStepCompletion({})).toEqual([false, false, false, false, false]);
  });

  it("classifies nodes as done/current/upcoming relative to the current step", () => {
    const completion = [true, false, false, false, false];
    expect(plainStepState(0, completion, 1)).toBe("done");
    expect(plainStepState(1, completion, 1)).toBe("current");
    expect(plainStepState(4, completion, 1)).toBe("upcoming");
  });
});

describe("traceSealed", () => {
  it("requires both satisfaction and at least one test", () => {
    expect(traceSealed({ requirement_id: "R1", satisfied: true, test_refs: ["t1"] })).toBe(true);
    expect(traceSealed({ requirement_id: "R2", satisfied: true, test_refs: [] })).toBe(false);
    expect(traceSealed({ requirement_id: "R3", satisfied: false, test_refs: ["t1"] })).toBe(false);
  });
});

describe("gateSummary", () => {
  it("rolls the manifest + validation report into gate inputs", () => {
    const spec: SpecOverview = {
      id: "s1",
      name: "Auth",
      status: "implementing",
      open_questions: [
        { id: "q1", text: "scope?", resolution: "yes" },
        { id: "q2", text: "limits?" },
      ],
      validation: {
        passed: false,
        coverage: 0.72,
        checks: [
          { name: "lint", passed: true },
          { name: "tests", passed: false },
        ],
        traceability: [
          { requirement_id: "R1", satisfied: true, test_refs: ["t1"] },
          { requirement_id: "R2", satisfied: false },
        ],
      },
    };
    const gate = gateSummary(spec);
    expect(gate).toMatchObject({
      passed: false,
      coverage: 72,
      checksPassed: 1,
      checksTotal: 2,
      reqsSatisfied: 1,
      reqsTotal: 2,
      openQuestions: 1,
      hasValidation: true,
    });
  });

  it("reports no validation when the report is absent", () => {
    const spec: SpecOverview = {
      id: "s2",
      name: "Billing",
      requirements: [
        { id: "R1", text: "a" },
        { id: "R2", text: "b" },
      ],
    };
    const gate = gateSummary(spec);
    expect(gate.passed).toBeNull();
    expect(gate.hasValidation).toBe(false);
    expect(gate.reqsTotal).toBe(2);
    expect(gate.reqsSatisfied).toBe(0);
  });
});
