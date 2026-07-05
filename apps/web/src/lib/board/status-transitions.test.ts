import { describe, expect, it } from "vitest";

import {
  backwardTarget,
  canTransition,
  forwardTarget,
  legalTargets,
} from "./status";

describe("canTransition", () => {
  it("treats a same-status move as an idempotent no-op", () => {
    expect(canTransition("in_progress", "in_progress")).toBe(true);
  });

  it("allows legal edges and rejects illegal ones", () => {
    expect(canTransition("backlog", "ready")).toBe(true);
    expect(canTransition("in_progress", "done")).toBe(true);
    // Terminal states may only be reopened.
    expect(canTransition("done", "in_progress")).toBe(true);
    expect(canTransition("done", "cancelled")).toBe(false);
    expect(canTransition("backlog", "done")).toBe(false);
  });
});

describe("legalTargets", () => {
  it("excludes the source and lists targets in column order", () => {
    expect(legalTargets("backlog")).toEqual([
      "ready",
      "ready_for_agent",
      "blocked",
      "cancelled",
    ]);
    expect(legalTargets("done")).toEqual(["in_progress"]);
  });
});

describe("forwardTarget / backwardTarget", () => {
  it("advances to the nearest legal column on the right", () => {
    expect(forwardTarget("backlog")).toBe("ready");
    // in_progress cannot legally reach ready_for_agent to its left; nearest legal
    // column to the left is ready.
    expect(backwardTarget("in_progress")).toBe("ready");
  });

  it("returns null when there is no legal neighbour in that direction", () => {
    // done's only legal target (in_progress) sits to its left, so forward is null.
    expect(forwardTarget("done")).toBeNull();
    expect(backwardTarget("backlog")).toBeNull();
  });
});
