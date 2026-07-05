/**
 * Pure graph helpers for the workflow visual editor (F28) — node geometry, edge
 * path math, trigger formatting, node-kind styling, and issue lookups. Kept free
 * of React so the SVG canvas is a thin renderer and the tricky bits (border
 * intersection, self-loops, `when` round-trip) are unit-tested in isolation.
 */

import type {
  WorkflowGraph,
  WorkflowNodeKind,
  WorkflowStateNode,
  WorkflowTransitionEdge,
  WorkflowValidationIssue,
} from "@/lib/api/types";

/** Rendered node box size (SVG user units). */
export const NODE_W = 180;
export const NODE_H = 60;
const PAD = 64;

export interface Point {
  x: number;
  y: number;
}

export function nodeCenter(node: WorkflowStateNode): Point {
  return { x: node.layout.x + NODE_W / 2, y: node.layout.y + NODE_H / 2 };
}

/** Point where the ray from `center` in direction `(dx,dy)` meets the box border. */
function borderPoint(center: Point, dx: number, dy: number): Point {
  const hw = NODE_W / 2;
  const hh = NODE_H / 2;
  const adx = Math.abs(dx) || 1e-6;
  const ady = Math.abs(dy) || 1e-6;
  const scale = Math.min(hw / adx, hh / ady);
  return { x: center.x + dx * scale, y: center.y + dy * scale };
}

export interface EdgeGeometry {
  path: string;
  labelX: number;
  labelY: number;
  selfLoop: boolean;
}

/**
 * SVG path from `source` to `target`. Straight transitions curve gently so a
 * pair (a→b and b→a) separates; a self-transition draws a loop above the node.
 */
export function transitionPath(
  source: WorkflowStateNode,
  target: WorkflowStateNode,
): EdgeGeometry {
  if (source.id === target.id) {
    const c = nodeCenter(source);
    const top = source.layout.y;
    const path = [
      `M ${c.x - 24} ${top}`,
      `C ${c.x - 52} ${top - 58} ${c.x + 52} ${top - 58} ${c.x + 24} ${top}`,
    ].join(" ");
    return { path, labelX: c.x, labelY: top - 44, selfLoop: true };
  }

  const s = nodeCenter(source);
  const t = nodeCenter(target);
  const dx = t.x - s.x;
  const dy = t.y - s.y;
  const start = borderPoint(s, dx, dy);
  const end = borderPoint(t, -dx, -dy);

  const mx = (start.x + end.x) / 2;
  const my = (start.y + end.y) / 2;
  const len = Math.hypot(end.x - start.x, end.y - start.y) || 1;
  const nx = -(end.y - start.y) / len;
  const ny = (end.x - start.x) / len;
  const curve = 20;
  const cx = mx + nx * curve;
  const cy = my + ny * curve;

  return {
    path: `M ${start.x} ${start.y} Q ${cx} ${cy} ${end.x} ${end.y}`,
    labelX: cx,
    labelY: cy,
    selfLoop: false,
  };
}

/** Canvas extent (with padding) so the SVG `viewBox` frames every node + loop. */
export function graphBounds(nodes: WorkflowStateNode[]): {
  width: number;
  height: number;
  minX: number;
  minY: number;
} {
  if (nodes.length === 0) {
    return { width: 480, height: 320, minX: 0, minY: 0 };
  }
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of nodes) {
    minX = Math.min(minX, node.layout.x);
    minY = Math.min(minY, node.layout.y);
    maxX = Math.max(maxX, node.layout.x + NODE_W);
    maxY = Math.max(maxY, node.layout.y + NODE_H);
  }
  // Extra headroom on top for self-loops.
  return {
    minX: minX - PAD,
    minY: minY - PAD,
    width: maxX - minX + PAD * 2,
    height: maxY - minY + PAD * 2,
  };
}

/** The human-readable trigger for an edge: action → when → condition → ε. */
export function edgeTrigger(edge: WorkflowTransitionEdge): string {
  if (edge.action) return edge.action;
  const when = whenToText(edge.when);
  if (when) return when;
  if (edge.condition) return edge.condition;
  return "ε";
}

/** Render a `when` value (string | list | null) as an editable comma string. */
export function whenToText(when: string | string[] | null | undefined): string {
  if (when == null) return "";
  return Array.isArray(when) ? when.join(", ") : when;
}

/** Parse the comma-separated `when` input back into string | list | null. */
export function parseWhenInput(text: string): string | string[] | null {
  const parts = text
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
  if (parts.length === 0) return null;
  if (parts.length === 1) return parts[0];
  return parts;
}

export interface NodeKindMeta {
  label: string;
  /** SVG stroke utility for the node border. */
  stroke: string;
  /** SVG fill utility for the accent bar + kind text. */
  fill: string;
  /** Text/badge accent utility (HTML). */
  accent: string;
  /** Small legend dot background (HTML). */
  dot: string;
}

const NODE_KIND_META: Record<WorkflowNodeKind, NodeKindMeta> = {
  initial: {
    label: "Initial",
    stroke: "stroke-spark",
    fill: "fill-spark",
    accent: "text-spark",
    dot: "bg-spark",
  },
  human_gate: {
    label: "Human gate",
    stroke: "stroke-warning",
    fill: "fill-warning",
    accent: "text-warning",
    dot: "bg-warning",
  },
  terminal: {
    label: "Terminal",
    stroke: "stroke-muted-foreground",
    fill: "fill-muted-foreground",
    accent: "text-muted-foreground",
    dot: "bg-muted-foreground",
  },
  normal: {
    label: "State",
    stroke: "stroke-border",
    fill: "fill-primary",
    accent: "text-foreground",
    dot: "bg-primary",
  },
};

export function nodeKindMeta(kind: WorkflowNodeKind): NodeKindMeta {
  return NODE_KIND_META[kind] ?? NODE_KIND_META.normal;
}

export function errorCount(issues: WorkflowValidationIssue[]): number {
  return issues.filter((issue) => issue.severity === "error").length;
}

export function warningCount(issues: WorkflowValidationIssue[]): number {
  return issues.filter((issue) => issue.severity === "warning").length;
}

/** Node ids that carry at least one error (for danger-ringing the box). */
export function nodesWithErrors(
  issues: WorkflowValidationIssue[],
): Set<string> {
  const ids = new Set<string>();
  for (const issue of issues) {
    if (issue.severity === "error" && issue.node_id) ids.add(issue.node_id);
  }
  return ids;
}

/** Edge ids that carry at least one error. */
export function edgesWithErrors(
  issues: WorkflowValidationIssue[],
): Set<string> {
  const ids = new Set<string>();
  for (const issue of issues) {
    if (issue.severity === "error" && issue.edge_id) ids.add(issue.edge_id);
  }
  return ids;
}

export function issuesForNode(
  issues: WorkflowValidationIssue[],
  nodeId: string,
): WorkflowValidationIssue[] {
  return issues.filter((issue) => issue.node_id === nodeId);
}

export function issuesForEdge(
  issues: WorkflowValidationIssue[],
  edgeId: string,
): WorkflowValidationIssue[] {
  return issues.filter((issue) => issue.edge_id === edgeId);
}

/** The next positional edge id (`e0`, `e1`, …) not already used. */
export function nextEdgeId(edges: WorkflowTransitionEdge[]): string {
  let max = -1;
  for (const edge of edges) {
    const match = /^e(\d+)$/.exec(edge.id);
    if (match) max = Math.max(max, Number(match[1]));
  }
  return `e${max + 1}`;
}

/** Deterministic dirty check: the working graph vs the loaded baseline. */
export function graphsEqual(a: WorkflowGraph, b: WorkflowGraph): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}
