import { describe, expect, it } from "vitest";

import {
  CHART_COLORS,
  predicateColor,
  predicateColors,
  radialCenter,
  radialLayout,
} from "./graph-layout";

describe("radialLayout", () => {
  it("is deterministic: the same count yields the same points", () => {
    expect(radialLayout(7)).toEqual(radialLayout(7));
    expect(radialLayout(24)).toEqual(radialLayout(24));
  });

  it.each([1, 2, 7, 24])("gives distinct positions for n = %i", (count) => {
    const points = radialLayout(count);
    expect(points).toHaveLength(count);
    const distinct = new Set(points.map((point) => `${point.x},${point.y}`));
    expect(distinct.size).toBe(count);
  });

  it("returns no points for zero neighbours", () => {
    expect(radialLayout(0)).toEqual([]);
  });

  it("places every neighbour on the same circle around the centre", () => {
    const center = radialCenter();
    const distances = radialLayout(7).map((point) =>
      Math.hypot(point.x - center.x, point.y - center.y),
    );
    for (const distance of distances) {
      expect(distance).toBeGreaterThan(0);
      expect(distance).toBeCloseTo(distances[0]);
    }
  });

  it("places a single neighbour at the top", () => {
    const center = radialCenter();
    const [point] = radialLayout(1);
    expect(point.x).toBeCloseTo(center.x);
    expect(point.y).toBeLessThan(center.y);
  });
});

describe("predicateColors", () => {
  it("assigns chart colours to sorted predicate keys in fixed order", () => {
    const colors = predicateColors(["worked_on", "born_in", "born_in", null]);
    expect(colors.get("born_in")).toBe(CHART_COLORS[0]);
    expect(colors.get("worked_on")).toBe(CHART_COLORS[1]);
    expect(colors.size).toBe(2);
  });

  it("falls back to the muted token for a missing predicate", () => {
    const colors = predicateColors(["born_in"]);
    expect(predicateColor(null, colors)).toBe("hsl(var(--muted-foreground))");
    expect(predicateColor("born_in", colors)).toBe(CHART_COLORS[0]);
  });
});
