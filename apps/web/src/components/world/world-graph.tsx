"use client";

/**
 * The entity neighbourhood graph: an SVG node-link diagram with the focal
 * entity at the centre and its entity neighbours on a deterministic radial
 * layout (see `graph-layout.ts`). Edges are coloured by predicate from the
 * `--chart-1..6` ramp, labelled with the fact's phrase, and arrowed in the
 * direction the statement reads. Neighbour nodes are buttons — click, or
 * Enter/Space when focused, refocuses the graph on them — and the plain
 * "Connections" list below is the text alternative to the diagram.
 */

import { useId, useMemo, type KeyboardEvent } from "react";

import { cn } from "@/lib/utils";

import {
  GRAPH_HEIGHT,
  GRAPH_WIDTH,
  predicateColor,
  predicateColors,
  radialCenter,
  radialLayout,
  type GraphPoint,
} from "./graph-layout";

export interface GraphNeighbour {
  id: string;
  label: string;
}

export interface GraphEdge {
  /** Unique per rendered edge (the fact id). */
  id: string;
  neighbourId: string;
  /** `outgoing`: focal is the subject. `incoming`: focal is the object. */
  direction: "outgoing" | "incoming";
  /** Edge label: the fact's phrase, falling back to its predicate key. */
  label: string;
  predicateKey: string | null;
}

export interface WorldGraphProps {
  focal: GraphNeighbour;
  neighbours: GraphNeighbour[];
  edges: GraphEdge[];
  /** Neighbours hidden by the caller's cap, shown as "+N more". */
  hiddenNeighbours?: number;
  onFocusEntity: (entityId: string) => void;
}

const FOCAL_RADIUS = 30;
const NEIGHBOUR_RADIUS = 26;

function truncate(label: string, max: number): string {
  return label.length > max ? `${label.slice(0, max - 1)}…` : label;
}

/** Move `point` toward `toward` by `distance` (keeps arrows off the nodes). */
function trimToward(
  point: GraphPoint,
  toward: GraphPoint,
  distance: number,
): GraphPoint {
  const dx = toward.x - point.x;
  const dy = toward.y - point.y;
  const length = Math.hypot(dx, dy);
  if (length === 0 || distance >= length) {
    return point;
  }
  return {
    x: point.x + (dx / length) * distance,
    y: point.y + (dy / length) * distance,
  };
}

interface DrawnEdge {
  edge: GraphEdge;
  path: string;
  labelPoint: GraphPoint;
  color: string;
}

export function WorldGraph({
  focal,
  neighbours,
  edges,
  hiddenNeighbours = 0,
  onFocusEntity,
}: WorldGraphProps) {
  const markerPrefix = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const center = radialCenter();
  const positions = useMemo(
    () => radialLayout(neighbours.length),
    [neighbours.length],
  );
  const positionById = useMemo(() => {
    const byId = new Map<string, GraphPoint>();
    neighbours.forEach((neighbour, index) => {
      byId.set(neighbour.id, positions[index]);
    });
    return byId;
  }, [neighbours, positions]);
  const labelById = useMemo(() => {
    const byId = new Map<string, string>();
    neighbours.forEach((neighbour) => byId.set(neighbour.id, neighbour.label));
    return byId;
  }, [neighbours]);
  const colors = useMemo(
    () => predicateColors(edges.map((edge) => edge.predicateKey)),
    [edges],
  );

  const drawnEdges = useMemo<DrawnEdge[]>(() => {
    // Offset parallel edges (same neighbour + direction) so their curves
    // and labels do not sit exactly on top of each other.
    const groups = new Map<string, GraphEdge[]>();
    for (const edge of edges) {
      const key = `${edge.neighbourId}:${edge.direction}`;
      const group = groups.get(key) ?? [];
      group.push(edge);
      groups.set(key, group);
    }
    const drawn: DrawnEdge[] = [];
    for (const group of groups.values()) {
      group.forEach((edge, groupIndex) => {
        const neighbourPoint = positionById.get(edge.neighbourId);
        if (!neighbourPoint) {
          return;
        }
        const source = edge.direction === "outgoing" ? center : neighbourPoint;
        const target = edge.direction === "outgoing" ? neighbourPoint : center;
        const start = trimToward(
          source,
          target,
          (edge.direction === "outgoing" ? FOCAL_RADIUS : NEIGHBOUR_RADIUS) + 2,
        );
        const end = trimToward(
          target,
          source,
          (edge.direction === "outgoing" ? NEIGHBOUR_RADIUS : FOCAL_RADIUS) +
            10,
        );
        const mid = {
          x: (start.x + end.x) / 2,
          y: (start.y + end.y) / 2,
        };
        const dx = end.x - start.x;
        const dy = end.y - start.y;
        const length = Math.hypot(dx, dy) || 1;
        const offset = (groupIndex - (group.length - 1) / 2) * 22;
        const control = {
          x: mid.x + (-dy / length) * offset,
          y: mid.y + (dx / length) * offset,
        };
        drawn.push({
          edge,
          path: `M ${start.x} ${start.y} Q ${control.x} ${control.y} ${end.x} ${end.y}`,
          labelPoint: {
            x: 0.25 * start.x + 0.5 * control.x + 0.25 * end.x,
            y: 0.25 * start.y + 0.5 * control.y + 0.25 * end.y,
          },
          color: predicateColor(edge.predicateKey, colors),
        });
      });
    }
    return drawn;
  }, [edges, positionById, center, colors]);

  const markerColors = useMemo(
    () => [...new Set(drawnEdges.map((drawn) => drawn.color))],
    [drawnEdges],
  );
  const markerIdFor = (color: string) =>
    `${markerPrefix}-arrow-${markerColors.indexOf(color)}`;

  const onNeighbourKeyDown = (
    event: KeyboardEvent<SVGGElement>,
    entityId: string,
  ) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onFocusEntity(entityId);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <svg
        viewBox={`0 0 ${GRAPH_WIDTH} ${GRAPH_HEIGHT}`}
        // Never drawn larger than its viewBox, so a wide column cannot blow it up.
        className="max-h-[480px] w-full rounded-lg border border-border bg-card"
        role="group"
        aria-label={`Graph of ${focal.label} and its neighbours`}
      >
        <defs>
          {markerColors.map((color, index) => (
            <marker
              key={color}
              id={`${markerPrefix}-arrow-${index}`}
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill={color} />
            </marker>
          ))}
        </defs>

        {drawnEdges.map(({ edge, path, labelPoint, color }) => (
          <g key={edge.id}>
            <path
              d={path}
              fill="none"
              stroke={color}
              strokeWidth={1.5}
              markerEnd={`url(#${markerIdFor(color)})`}
            />
            <text
              x={labelPoint.x}
              y={labelPoint.y}
              textAnchor="middle"
              fontSize={11}
              fill={color}
              stroke="hsl(var(--card))"
              strokeWidth={3}
              paintOrder="stroke"
            >
              {truncate(edge.label, 28)}
            </text>
          </g>
        ))}

        {neighbours.map((neighbour, index) => {
          const point = positions[index];
          return (
            <g
              key={neighbour.id}
              role="button"
              tabIndex={0}
              aria-label={`Focus ${neighbour.label}`}
              className={cn(
                "cursor-pointer focus:outline-none",
                "[&:focus>circle]:stroke-[hsl(var(--ring))]",
                "[&:focus>circle]:[stroke-width:3]",
              )}
              onClick={() => onFocusEntity(neighbour.id)}
              onKeyDown={(event) => onNeighbourKeyDown(event, neighbour.id)}
            >
              <title>{neighbour.label}</title>
              <circle
                cx={point.x}
                cy={point.y}
                r={NEIGHBOUR_RADIUS}
                fill="hsl(var(--card))"
                stroke="hsl(var(--border))"
                strokeWidth={1.5}
              />
              <text
                x={point.x}
                // Outside the node, away from the centre, so no edge crosses it.
                y={
                  point.y < center.y
                    ? point.y - NEIGHBOUR_RADIUS - 8
                    : point.y + NEIGHBOUR_RADIUS + 16
                }
                textAnchor="middle"
                fontSize={12}
                fill="hsl(var(--foreground))"
              >
                {truncate(neighbour.label, 20)}
              </text>
            </g>
          );
        })}

        <g aria-label={`Focused entity: ${focal.label}`}>
          <title>{focal.label}</title>
          <circle
            cx={center.x}
            cy={center.y}
            r={FOCAL_RADIUS}
            fill="hsl(var(--primary))"
            stroke="hsl(var(--primary))"
            strokeWidth={1.5}
          />
          <text
            x={center.x}
            y={center.y + FOCAL_RADIUS + 16}
            textAnchor="middle"
            fontSize={12}
            fontWeight={600}
            fill="hsl(var(--foreground))"
          >
            {truncate(focal.label, 24)}
          </text>
        </g>

        {neighbours.length === 0 ? (
          <text
            x={center.x}
            y={center.y + FOCAL_RADIUS + 36}
            textAnchor="middle"
            fontSize={12}
            fill="hsl(var(--muted-foreground))"
          >
            No entity connections at this time.
          </text>
        ) : null}
      </svg>

      {hiddenNeighbours > 0 ? (
        <p className="text-sm text-muted-foreground">
          Showing the first {neighbours.length} neighbours (+
          {hiddenNeighbours} more)
        </p>
      ) : null}

      {colors.size > 0 ? (
        <ul
          aria-label="Predicate colours"
          className="flex flex-wrap gap-x-4 gap-y-1"
        >
          {[...colors.entries()].map(([predicate, color]) => (
            <li
              key={predicate}
              className="flex items-center gap-1.5 text-xs text-muted-foreground"
            >
              <span
                aria-hidden
                className="inline-block h-2 w-4 rounded-sm"
                style={{ backgroundColor: color }}
              />
              {predicate}
            </li>
          ))}
        </ul>
      ) : null}

      <div>
        <h3 className="text-sm font-semibold text-foreground">Connections</h3>
        {edges.length === 0 ? (
          <p className="mt-1 text-sm text-muted-foreground">
            No connections at this time.
          </p>
        ) : (
          <ul className="mt-1 flex flex-col gap-1">
            {edges.map((edge) => {
              const neighbourLabel =
                labelById.get(edge.neighbourId) ?? edge.neighbourId;
              const text =
                edge.direction === "outgoing"
                  ? `${focal.label} — ${edge.label} → ${neighbourLabel}`
                  : `${neighbourLabel} — ${edge.label} → ${focal.label}`;
              return (
                <li key={edge.id}>
                  <button
                    type="button"
                    onClick={() => onFocusEntity(edge.neighbourId)}
                    aria-label={`${text}. Focus ${neighbourLabel}`}
                    className="rounded-sm text-left text-sm text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {text}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
