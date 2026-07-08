import { describe, expect, it } from "vitest";

import type { SpecManifest } from "@/lib/api/types";

import {
  addAcceptanceCriterion,
  addAdr,
  addRequirement,
  composeGivenWhenThen,
  computeChecklist,
  computeCoverage,
  computeNudges,
  nextSequentialId,
  parseGivenWhenThen,
} from "./guided-helpers";

describe("nextSequentialId", () => {
  it("returns R1 for an empty list", () => {
    expect(nextSequentialId("R", [])).toBe("R1");
  });

  it("returns one past the highest existing number", () => {
    expect(nextSequentialId("R", ["R1", "R2", "R5"])).toBe("R6");
  });

  it("ignores ids that don't match the prefix pattern", () => {
    expect(nextSequentialId("AC", ["R1", "AC3", "custom-id"])).toBe("AC4");
  });
});

describe("Given/When/Then round trip", () => {
  it("composes then parses back to the same parts", () => {
    const parts = { given: "a user", when: "they sign in", then: "they land on the board" };
    const text = composeGivenWhenThen(parts);
    expect(text).toBe("Given a user When they sign in Then they land on the board");
    expect(parseGivenWhenThen(text)).toEqual(parts);
  });

  it("treats unstructured text as the Then clause", () => {
    expect(parseGivenWhenThen("just some prose")).toEqual({
      given: "",
      when: "",
      then: "just some prose",
    });
  });

  it("omits empty parts when composing", () => {
    expect(composeGivenWhenThen({ given: "", when: "", then: "it works" })).toBe("Then it works");
  });
});

describe("computeNudges", () => {
  const base: SpecManifest = { id: "s1", name: "Passwordless auth" };

  it("nudges an empty goal and missing requirements", () => {
    const nudges = computeNudges({ ...base, name: "" });
    expect(nudges.map((n) => n.id)).toEqual(expect.arrayContaining(["no-goal", "no-requirements"]));
  });

  it("nudges a requirement with no linked acceptance criterion", () => {
    const nudges = computeNudges({
      ...base,
      requirements: [{ id: "R1", text: "Sign in" }],
      acceptance_criteria: [],
    });
    expect(nudges.some((n) => n.id === "uncovered-R1")).toBe(true);
  });

  it("has no coverage nudges once every requirement is linked", () => {
    const nudges = computeNudges({
      ...base,
      requirements: [{ id: "R1", text: "Sign in" }],
      acceptance_criteria: [{ id: "AC1", text: "Given...", req_refs: ["R1"] }],
    });
    expect(nudges.some((n) => n.id.startsWith("uncovered-"))).toBe(false);
    expect(nudges.some((n) => n.id === "unlinked-AC1")).toBe(false);
  });

  it("nudges an unresolved open question", () => {
    const nudges = computeNudges({
      ...base,
      open_questions: [{ id: "Q1", text: "Which provider?" }],
    });
    expect(nudges.some((n) => n.id === "open-questions")).toBe(true);
  });
});

describe("computeCoverage", () => {
  it("is 0/0 with no requirements", () => {
    expect(computeCoverage({ id: "s1", name: "x" })).toEqual({ satisfied: 0, total: 0, pct: 0 });
  });

  it("computes the satisfied fraction", () => {
    const coverage = computeCoverage({
      id: "s1",
      name: "x",
      requirements: [
        { id: "R1", text: "a" },
        { id: "R2", text: "b" },
      ],
      acceptance_criteria: [{ id: "AC1", text: "t", req_refs: ["R1"] }],
    });
    expect(coverage).toEqual({ satisfied: 1, total: 2, pct: 50 });
  });
});

describe("computeChecklist", () => {
  it("is all incomplete for an empty manifest", () => {
    const items = computeChecklist({ id: "s1", name: "" });
    expect(items.every((i) => !i.done)).toBe(true);
  });

  it("is all complete for a fully covered manifest", () => {
    const items = computeChecklist({
      id: "s1",
      name: "Passwordless auth",
      requirements: [{ id: "R1", text: "a" }],
      acceptance_criteria: [{ id: "AC1", text: "t", req_refs: ["R1"] }],
    });
    expect(items.every((i) => i.done)).toBe(true);
  });
});

describe("add* helpers", () => {
  it("addRequirement appends the next sequential requirement", () => {
    expect(addRequirement([{ id: "R1", text: "a" }])).toEqual([
      { id: "R1", text: "a" },
      { id: "R2", text: "" },
    ]);
  });

  it("addAcceptanceCriterion appends the next sequential AC with empty req_refs", () => {
    expect(addAcceptanceCriterion([])).toEqual([{ id: "AC1", text: "", req_refs: [] }]);
  });

  it("addAdr appends the next sequential ADR", () => {
    expect(addAdr([])).toEqual([{ id: "ADR1", title: "", status: "proposed" }]);
  });
});
