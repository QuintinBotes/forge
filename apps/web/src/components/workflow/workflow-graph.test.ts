import { describe, expect, it } from "vitest";

import type {
  WorkflowStateNode,
  WorkflowTransitionEdge,
  WorkflowValidationIssue,
} from "@/lib/api/types";

import {
  NODE_H,
  NODE_W,
  edgeTrigger,
  edgesWithErrors,
  errorCount,
  graphBounds,
  nextEdgeId,
  nodeCenter,
  nodeKindMeta,
  nodesWithErrors,
  parseWhenInput,
  transitionPath,
  warningCount,
  whenToText,
} from "./workflow-graph";

function node(id: string, x: number, y: number): WorkflowStateNode {
  return { id, kind: "normal", layout: { x, y } };
}

function edge(over: Partial<WorkflowTransitionEdge> = {}): WorkflowTransitionEdge {
  return {
    id: "e0",
    from_state: "a",
    to_state: "b",
    preconditions: [],
    checks: [],
    ...over,
  };
}

describe("node geometry", () => {
  it("centers a node in the middle of its box", () => {
    expect(nodeCenter(node("a", 0, 0))).toEqual({
      x: NODE_W / 2,
      y: NODE_H / 2,
    });
  });

  it("frames every node with padding in the viewBox bounds", () => {
    const bounds = graphBounds([node("a", 0, 0), node("b", 240, 120)]);
    expect(bounds.minX).toBeLessThan(0);
    expect(bounds.width).toBeGreaterThan(240 + NODE_W);
    expect(bounds.height).toBeGreaterThan(120 + NODE_H);
  });

  it("falls back to a default frame for an empty graph", () => {
    const bounds = graphBounds([]);
    expect(bounds.width).toBeGreaterThan(0);
    expect(bounds.height).toBeGreaterThan(0);
  });
});

describe("transitionPath", () => {
  it("draws a straight-ish curve between two distinct nodes", () => {
    const geo = transitionPath(node("a", 0, 0), node("b", 240, 0));
    expect(geo.selfLoop).toBe(false);
    expect(geo.path.startsWith("M")).toBe(true);
    expect(geo.path).toContain("Q");
  });

  it("draws a loop above the node for a self-transition", () => {
    const geo = transitionPath(node("a", 0, 100), node("a", 0, 100));
    expect(geo.selfLoop).toBe(true);
    // Label sits above the box top edge (y=100).
    expect(geo.labelY).toBeLessThan(100);
  });

  it("separates the two directions of a bidirectional pair", () => {
    const a = node("a", 0, 0);
    const b = node("b", 240, 0);
    const forward = transitionPath(a, b);
    const backward = transitionPath(b, a);
    // The labels land on opposite sides of the connecting line (its y is the
    // shared node-center y), so a→b and b→a don't overlap.
    const midline = nodeCenter(a).y;
    expect(Math.sign(forward.labelY - midline)).not.toBe(
      Math.sign(backward.labelY - midline),
    );
  });
});

describe("edge trigger formatting", () => {
  it("prefers the action, then when, then condition", () => {
    expect(edgeTrigger(edge({ action: "run_checks" }))).toBe("run_checks");
    expect(edgeTrigger(edge({ when: ["ci_green", "reviewed"] }))).toBe(
      "ci_green, reviewed",
    );
    expect(edgeTrigger(edge({ condition: "retry_remaining" }))).toBe(
      "retry_remaining",
    );
    expect(edgeTrigger(edge())).toBe("ε");
  });

  it("round-trips the `when` value through the text input", () => {
    expect(whenToText(["a", "b"])).toBe("a, b");
    expect(whenToText("solo")).toBe("solo");
    expect(whenToText(null)).toBe("");
    expect(parseWhenInput("")).toBeNull();
    expect(parseWhenInput("solo")).toBe("solo");
    expect(parseWhenInput("a, b ,c")).toEqual(["a", "b", "c"]);
  });
});

describe("node kind styling", () => {
  it("gives each kind a distinct accent + legend", () => {
    expect(nodeKindMeta("initial").accent).toBe("text-spark");
    expect(nodeKindMeta("human_gate").accent).toBe("text-warning");
    expect(nodeKindMeta("terminal").label).toBe("Terminal");
    expect(nodeKindMeta("normal").stroke).toBe("stroke-border");
  });
});

describe("issue helpers", () => {
  const issues: WorkflowValidationIssue[] = [
    { code: "dead_end_state", severity: "error", message: "x", node_id: "b" },
    { code: "unknown_event", severity: "error", message: "y", edge_id: "e1" },
    { code: "unreachable_state", severity: "warning", message: "z", node_id: "c" },
  ];

  it("counts errors and warnings", () => {
    expect(errorCount(issues)).toBe(2);
    expect(warningCount(issues)).toBe(1);
  });

  it("indexes error owners by node and edge", () => {
    expect(nodesWithErrors(issues).has("b")).toBe(true);
    expect(nodesWithErrors(issues).has("c")).toBe(false); // warning, not error
    expect(edgesWithErrors(issues).has("e1")).toBe(true);
  });
});

describe("nextEdgeId", () => {
  it("returns the next positional id after the highest existing one", () => {
    expect(nextEdgeId([edge({ id: "e0" }), edge({ id: "e3" })])).toBe("e4");
    expect(nextEdgeId([])).toBe("e0");
  });
});
