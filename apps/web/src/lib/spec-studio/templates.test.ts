import { describe, expect, it } from "vitest";

import type { SpecManifest } from "@/lib/api/types";

import { SPEC_TEMPLATES, applySpecTemplate, specTemplate } from "./templates";

describe("SPEC_TEMPLATES", () => {
  it("exposes exactly the feature/bugfix/spike starter templates", () => {
    expect(SPEC_TEMPLATES.map((t) => t.id)).toEqual(["feature", "bugfix", "spike"]);
  });

  it("each template seeds at least one requirement and one linked acceptance criterion", () => {
    for (const template of SPEC_TEMPLATES) {
      expect(template.requirements.length).toBeGreaterThan(0);
      expect(template.acceptanceCriteria.length).toBeGreaterThan(0);
      for (const ac of template.acceptanceCriteria) {
        expect(ac.req_refs?.length ?? 0).toBeGreaterThan(0);
      }
    }
  });
});

describe("specTemplate", () => {
  it("looks a template up by id", () => {
    expect(specTemplate("bugfix").label).toBe("Bugfix");
  });

  it("throws on an unknown id", () => {
    // @ts-expect-error deliberate bad id for the runtime guard
    expect(() => specTemplate("nope")).toThrow(/Unknown spec template/);
  });
});

describe("applySpecTemplate", () => {
  const blank: SpecManifest = { id: "", name: "" };

  it("seeds requirements, acceptance criteria and constraints from the template", () => {
    const seeded = applySpecTemplate("feature", blank);
    expect(seeded.requirements).toEqual(specTemplate("feature").requirements);
    expect(seeded.acceptance_criteria).toEqual(specTemplate("feature").acceptanceCriteria);
    expect(seeded.constraints).toEqual([]);
  });

  it("seeds bugfix constraints", () => {
    const seeded = applySpecTemplate("bugfix", blank);
    expect(seeded.constraints).toEqual(specTemplate("bugfix").constraints);
  });

  it("never clobbers requirements the author already drafted", () => {
    const drafted: SpecManifest = {
      id: "",
      name: "My spec",
      requirements: [{ id: "R1", text: "Already written" }],
    };
    const seeded = applySpecTemplate("feature", drafted);
    expect(seeded.requirements).toEqual(drafted.requirements);
    // Untouched fields still get seeded.
    expect(seeded.acceptance_criteria).toEqual(specTemplate("feature").acceptanceCriteria);
  });

  it("preserves the name and any other existing draft fields", () => {
    const drafted: SpecManifest = { id: "", name: "My spec", execution_mode: "single_agent" };
    const seeded = applySpecTemplate("spike", drafted);
    expect(seeded.name).toBe("My spec");
    expect(seeded.execution_mode).toBe("single_agent");
  });

  it("returns fresh arrays, not the template's own arrays (no shared mutation)", () => {
    const seeded = applySpecTemplate("feature", blank);
    seeded.requirements!.push({ id: "R2", text: "mutated" });
    expect(specTemplate("feature").requirements).toHaveLength(1);
  });
});
