import { describe, expect, it } from "vitest";

import type { SpecManifest } from "@/lib/api/types";

import {
  addAcceptanceCriterion,
  addAdr,
  addRequirement,
  classifyCriterionStyle,
  composeChecklist,
  composeGivenWhenThen,
  computeChecklist,
  computeCoverage,
  computeNudges,
  convertCriterionText,
  nextSequentialId,
  parseChecklist,
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

describe("classifyCriterionStyle", () => {
  it("defaults blank text to gherkin (the editor's default shape)", () => {
    expect(classifyCriterionStyle("")).toBe("gherkin");
    expect(classifyCriterionStyle("   ")).toBe("gherkin");
  });

  it("classifies Given/When/Then prose as gherkin", () => {
    expect(classifyCriterionStyle("Given a user When they sign in Then the board loads")).toBe("gherkin");
    expect(classifyCriterionStyle("Then it works")).toBe("gherkin");
  });

  it("classifies a keyword-free sentence as a plain assertion", () => {
    expect(classifyCriterionStyle("The endpoint returns 200 for a valid token")).toBe("assertion");
  });

  it("classifies check-item lines as a checklist, even when a label says 'when'", () => {
    expect(classifyCriterionStyle("- [ ] Logs an event when it runs\n- [x] Retries on failure")).toBe(
      "checklist",
    );
  });
});

describe("checklist (de)serialisation", () => {
  it("composes then parses back to the same items", () => {
    const items = [
      { label: "Email field validates", checked: false },
      { label: "Password is masked", checked: true },
    ];
    const text = composeChecklist(items);
    expect(text).toBe("- [ ] Email field validates\n- [x] Password is masked");
    expect(parseChecklist(text)).toEqual(items);
  });

  it("renders an empty label without a trailing space", () => {
    expect(composeChecklist([{ label: "", checked: false }])).toBe("- [ ]");
  });
});

describe("convertCriterionText", () => {
  it("wraps prose into a single unchecked item when switching to a checklist", () => {
    expect(convertCriterionText("Then it works", "checklist")).toBe("- [ ] Then it works");
  });

  it("joins checklist labels back into prose when leaving the checklist style", () => {
    const text = "- [ ] first\n- [x] second";
    expect(convertCriterionText(text, "assertion")).toBe("first; second");
    expect(convertCriterionText(text, "gherkin")).toBe("first second");
  });

  it("is a no-op between gherkin and assertion (shared flat prose)", () => {
    expect(convertCriterionText("The system does X", "gherkin")).toBe("The system does X");
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
