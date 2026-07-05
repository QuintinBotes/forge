import { describe, expect, it } from "vitest";

import type { SpecOverview } from "@/lib/api/types";

import {
  coveragePercent,
  formatCoverage,
  gateSummary,
  isApprovable,
  stageIndex,
  stageState,
  traceSealed,
} from "./spec-meta";

describe("stageIndex / stageState", () => {
  it("orders the lifecycle and defaults unknown status to draft", () => {
    expect(stageIndex("draft")).toBe(0);
    expect(stageIndex("validated")).toBe(4);
    expect(stageIndex(undefined)).toBe(0);
  });

  it("classifies nodes relative to the current stage", () => {
    // current = approved (index 2)
    expect(stageState(0, "approved")).toBe("done");
    expect(stageState(2, "approved")).toBe("current");
    expect(stageState(4, "approved")).toBe("upcoming");
  });
});

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
