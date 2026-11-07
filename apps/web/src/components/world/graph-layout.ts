/**
 * Deterministic radial layout for the entity neighbourhood graph.
 *
 * Pure and dependency-free: the focal entity sits at the centre and its
 * neighbours are placed evenly on a circle around it, starting at the top
 * (-90°) and walking clockwise. The same neighbour count always yields the
 * same positions, so the graph never jumps between renders and needs no
 * physics simulation. Coordinates are in the SVG viewBox space the caller
 * chooses (640 × 480 by default).
 */

export interface GraphPoint {
  x: number;
  y: number;
}

export interface RadialLayoutOptions {
  /** ViewBox width the layout is computed in. */
  width?: number;
  /** ViewBox height the layout is computed in. */
  height?: number;
  /** Space kept between the circle and the viewBox edge (node + label room). */
  margin?: number;
}

export const GRAPH_WIDTH = 640;
export const GRAPH_HEIGHT = 480;

/** The centre of the layout — where the focal entity is drawn. */
export function radialCenter(options: RadialLayoutOptions = {}): GraphPoint {
  const width = options.width ?? GRAPH_WIDTH;
  const height = options.height ?? GRAPH_HEIGHT;
  return { x: width / 2, y: height / 2 };
}

/**
 * Positions for `count` neighbours around the centre, in input order.
 *
 * Every neighbour gets a distinct position: angles are `2π / count` apart,
 * so for any count ≥ 1 no two points coincide. A single neighbour sits at
 * the top. `count` ≤ 0 yields no points.
 */
export function radialLayout(
  count: number,
  options: RadialLayoutOptions = {},
): GraphPoint[] {
  if (count <= 0) {
    return [];
  }
  const width = options.width ?? GRAPH_WIDTH;
  const height = options.height ?? GRAPH_HEIGHT;
  const margin = options.margin ?? 72;
  const center = radialCenter({ width, height });
  const radius = Math.max(Math.min(width, height) / 2 - margin, 0);
  const points: GraphPoint[] = [];
  for (let index = 0; index < count; index += 1) {
    const angle = -Math.PI / 2 + (index * 2 * Math.PI) / count;
    points.push({
      x: center.x + radius * Math.cos(angle),
      y: center.y + radius * Math.sin(angle),
    });
  }
  return points;
}

/** The categorical chart ramp, in its fixed order (theme tokens only). */
export const CHART_COLORS = [
  "hsl(var(--chart-1))",
  "hsl(var(--chart-2))",
  "hsl(var(--chart-3))",
  "hsl(var(--chart-4))",
  "hsl(var(--chart-5))",
  "hsl(var(--chart-6))",
] as const;

/**
 * Map predicate keys to chart colours in a fixed order: the distinct keys
 * are sorted, then assigned `--chart-1..6` in turn (cycling only when a
 * neighbourhood has more than six predicates). Sorting keeps the mapping
 * stable regardless of the order facts arrive in. Facts without a
 * predicate key fall back to the muted foreground token.
 */
export function predicateColors(
  predicateKeys: Iterable<string | null>,
): Map<string, string> {
  const distinct = [
    ...new Set(
      [...predicateKeys].filter(
        (key): key is string => typeof key === "string" && key !== "",
      ),
    ),
  ].sort();
  const colors = new Map<string, string>();
  distinct.forEach((key, index) => {
    colors.set(key, CHART_COLORS[index % CHART_COLORS.length]);
  });
  return colors;
}

/** The edge colour for one predicate key (muted when there is none). */
export function predicateColor(
  predicateKey: string | null,
  colors: Map<string, string>,
): string {
  if (predicateKey && colors.has(predicateKey)) {
    return colors.get(predicateKey) as string;
  }
  return "hsl(var(--muted-foreground))";
}
