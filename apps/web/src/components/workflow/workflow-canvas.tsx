"use client";

import { useId, type KeyboardEvent } from "react";

import type { WorkflowGraph } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  NODE_H,
  NODE_W,
  edgeTrigger,
  edgesWithErrors,
  graphBounds,
  nodeKindMeta,
  nodesWithErrors,
  transitionPath,
} from "./workflow-graph";
import type { Selection } from "./selection";

export interface WorkflowCanvasProps {
  graph: WorkflowGraph;
  selection: Selection | null;
  errorNodeIds: Set<string>;
  errorEdgeIds: Set<string>;
  onSelectNode: (id: string) => void;
  onSelectEdge: (id: string) => void;
  onClearSelection: () => void;
}

/**
 * The signature surface: a hand-rolled SVG state-machine canvas on a forge
 * blueprint grid. States are boxes tinted by kind (initial = spark, human gate =
 * warning, terminal = steel); transitions are curved, arrow-headed paths labelled
 * with their trigger. Selection ember-highlights the node/edge; a validation
 * error danger-rings it. Every node + edge is a focusable button so the whole
 * graph is reachable from the keyboard.
 */
export function WorkflowCanvas({
  graph,
  selection,
  errorNodeIds,
  errorEdgeIds,
  onSelectNode,
  onSelectEdge,
  onClearSelection,
}: WorkflowCanvasProps) {
  const gridId = useId().replace(/:/g, "");
  const bounds = graphBounds(graph.nodes);
  const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));

  function activate(
    event: KeyboardEvent,
    run: () => void,
  ): void {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      run();
    }
  }

  if (graph.nodes.length === 0) {
    return (
      <div
        data-testid="canvas-empty"
        className="flex h-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/40 p-10 text-center"
      >
        <p className="text-sm font-medium text-foreground">No states yet</p>
        <p className="max-w-xs text-xs text-muted-foreground">
          Add your first state to start mapping the workflow, then connect states
          with transitions.
        </p>
      </div>
    );
  }

  return (
    <div className="h-full w-full overflow-auto rounded-lg border border-border bg-card/40">
      <svg
        data-testid="workflow-canvas"
        role="group"
        aria-label="Workflow state graph"
        viewBox={`${bounds.minX} ${bounds.minY} ${bounds.width} ${bounds.height}`}
        className="h-full w-full min-h-[24rem] min-w-[36rem]"
        preserveAspectRatio="xMidYMid meet"
        onClick={(event) => {
          if (event.target === event.currentTarget) onClearSelection();
        }}
      >
        <defs>
          <pattern
            id={`grid-${gridId}`}
            width={24}
            height={24}
            patternUnits="userSpaceOnUse"
          >
            <path
              d="M 24 0 L 0 0 0 24"
              fill="none"
              className="stroke-border/50"
              strokeWidth={1}
            />
          </pattern>
          <marker
            id={`arrow-${gridId}`}
            markerWidth={9}
            markerHeight={9}
            refX={7.5}
            refY={3}
            orient="auto"
            markerUnits="userSpaceOnUse"
          >
            <path d="M0,0 L8,3 L0,6 Z" className="fill-muted-foreground" />
          </marker>
          <marker
            id={`arrow-active-${gridId}`}
            markerWidth={9}
            markerHeight={9}
            refX={7.5}
            refY={3}
            orient="auto"
            markerUnits="userSpaceOnUse"
          >
            <path d="M0,0 L8,3 L0,6 Z" className="fill-primary" />
          </marker>
          <marker
            id={`arrow-error-${gridId}`}
            markerWidth={9}
            markerHeight={9}
            refX={7.5}
            refY={3}
            orient="auto"
            markerUnits="userSpaceOnUse"
          >
            <path d="M0,0 L8,3 L0,6 Z" className="fill-danger" />
          </marker>
        </defs>

        <rect
          x={bounds.minX}
          y={bounds.minY}
          width={bounds.width}
          height={bounds.height}
          fill={`url(#grid-${gridId})`}
        />

        {/* Edges (under the nodes). */}
        <g>
          {graph.edges.map((edge) => {
            const source = nodeById.get(edge.from_state);
            const target = nodeById.get(edge.to_state);
            if (!source || !target) return null;
            const geo = transitionPath(source, target);
            const isSelected =
              selection?.kind === "edge" && selection.id === edge.id;
            const hasError = errorEdgeIds.has(edge.id);
            const marker = isSelected
              ? `arrow-active-${gridId}`
              : hasError
                ? `arrow-error-${gridId}`
                : `arrow-${gridId}`;
            const label = edgeTrigger(edge);
            const labelWidth = Math.max(18, label.length * 6.6 + 12);
            return (
              <g
                key={edge.id}
                data-testid={`wf-edge-${edge.id}`}
                role="button"
                tabIndex={0}
                aria-label={`Transition ${edge.from_state} to ${edge.to_state} on ${label}`}
                aria-pressed={isSelected}
                className="cursor-pointer outline-none"
                onClick={() => onSelectEdge(edge.id)}
                onKeyDown={(event) => activate(event, () => onSelectEdge(edge.id))}
              >
                {/* Fat invisible hit target. */}
                <path
                  d={geo.path}
                  fill="none"
                  stroke="transparent"
                  strokeWidth={16}
                />
                <path
                  d={geo.path}
                  fill="none"
                  markerEnd={`url(#${marker})`}
                  className={cn(
                    "transition-colors",
                    isSelected
                      ? "stroke-primary"
                      : hasError
                        ? "stroke-danger"
                        : "stroke-muted-foreground/70",
                  )}
                  strokeWidth={isSelected ? 2.5 : 1.75}
                />
                <g transform={`translate(${geo.labelX} ${geo.labelY})`}>
                  <rect
                    x={-labelWidth / 2}
                    y={-10}
                    width={labelWidth}
                    height={20}
                    rx={5}
                    className={cn(
                      "fill-card",
                      isSelected
                        ? "stroke-primary/60"
                        : hasError
                          ? "stroke-danger/60"
                          : "stroke-border",
                    )}
                    strokeWidth={1}
                  />
                  <text
                    x={0}
                    y={0}
                    textAnchor="middle"
                    dominantBaseline="central"
                    className={cn(
                      "font-mono",
                      hasError ? "fill-danger" : "fill-muted-foreground",
                    )}
                    fontSize={11}
                  >
                    {label}
                  </text>
                </g>
              </g>
            );
          })}
        </g>

        {/* Nodes (over the edges). */}
        <g>
          {graph.nodes.map((node) => {
            const kind = nodeKindMeta(node.kind);
            const isSelected =
              selection?.kind === "node" && selection.id === node.id;
            const hasError = errorNodeIds.has(node.id);
            return (
              <g
                key={node.id}
                data-testid={`wf-node-${node.id}`}
                role="button"
                tabIndex={0}
                aria-label={`State ${node.id} (${kind.label})`}
                aria-pressed={isSelected}
                transform={`translate(${node.layout.x} ${node.layout.y})`}
                className="cursor-pointer outline-none [&:focus-visible_.wf-box]:stroke-spark [&:focus-visible_.wf-box]:stroke-2"
                onClick={() => onSelectNode(node.id)}
                onKeyDown={(event) => activate(event, () => onSelectNode(node.id))}
              >
                {isSelected ? (
                  <rect
                    x={-4}
                    y={-4}
                    width={NODE_W + 8}
                    height={NODE_H + 8}
                    rx={13}
                    fill="none"
                    className="stroke-spark/70"
                    strokeWidth={1.5}
                  />
                ) : null}
                <rect
                  className={cn(
                    "wf-box fill-card transition-colors",
                    isSelected
                      ? "stroke-primary"
                      : hasError
                        ? "stroke-danger"
                        : kind.stroke,
                  )}
                  width={NODE_W}
                  height={NODE_H}
                  rx={11}
                  strokeWidth={isSelected || hasError ? 2.25 : 1.5}
                />
                <rect
                  x={1.5}
                  y={1.5}
                  width={5}
                  height={NODE_H - 3}
                  rx={2}
                  className={kind.fill}
                />
                <text
                  x={18}
                  y={25}
                  className="fill-foreground font-mono"
                  fontSize={14}
                  fontWeight={600}
                >
                  {node.label ?? node.id}
                </text>
                <text
                  x={18}
                  y={43}
                  className={cn("font-sans uppercase", kind.fill)}
                  fontSize={9.5}
                  letterSpacing={0.6}
                >
                  {kind.label}
                </text>
                {hasError ? (
                  <circle
                    cx={NODE_W - 14}
                    cy={16}
                    r={5}
                    className="fill-danger"
                  />
                ) : null}
              </g>
            );
          })}
        </g>
      </svg>
    </div>
  );
}
