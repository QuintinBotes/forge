import { describe, expect, it } from "vitest";

import {
  TOUR_STEPS,
  TOUR_STEP_COUNT,
  clampStepIndex,
  isLastStep,
} from "./tour-steps";

describe("tour-steps", () => {
  it("opens with an orientation step then walks the four loop stages in order", () => {
    expect(TOUR_STEPS[0].id).toBe("welcome");
    expect(TOUR_STEPS.slice(1).map((s) => s.id)).toEqual([
      "spec",
      "run",
      "review",
      "merge",
    ]);
    expect(TOUR_STEP_COUNT).toBe(TOUR_STEPS.length);
  });

  it("gives every loop stage a real deep link and a progress key", () => {
    for (const step of TOUR_STEPS.slice(1)) {
      expect(step.href).toMatch(/^\//);
      expect(step.cta).toBeTruthy();
      expect(step.progressKey).toBeTruthy();
    }
  });

  it("clamps indices into range", () => {
    expect(clampStepIndex(-5)).toBe(0);
    expect(clampStepIndex(999)).toBe(TOUR_STEPS.length - 1);
    expect(clampStepIndex(2.9)).toBe(2);
    expect(clampStepIndex(Number.NaN)).toBe(0);
  });

  it("recognises the final step", () => {
    expect(isLastStep(0)).toBe(false);
    expect(isLastStep(TOUR_STEPS.length - 1)).toBe(true);
  });
});
