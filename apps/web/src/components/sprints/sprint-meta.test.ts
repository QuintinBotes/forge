import { describe, expect, it } from "vitest";

import type { Sprint } from "@/lib/api/types";

import {
  ALLOCATION_STATUS_LABELS,
  allocationStatusBadgeClass,
  formatDateShort,
  formatDecimal,
  formatPct,
  formatPoints,
  pickDefaultSprintId,
  predictabilityTone,
  sortSprintsNewestFirst,
  SPRINT_STATE_LABELS,
  sprintLengthDays,
  sprintStateBadgeClass,
} from "./sprint-meta";

function sprint(over: Partial<Sprint> & Pick<Sprint, "id">): Sprint {
  return {
    project_id: "p",
    workspace_id: "w",
    name: over.name ?? over.id,
    state: "planned",
    committed_points: 0,
    committed_task_count: 0,
    completed_points: 0,
    added_points: 0,
    removed_points: 0,
    carryover_points: 0,
    remaining_points: 0,
    predictability: 0,
    scope_change_ratio: 0,
    velocity_version: 0,
    ...over,
  };
}

describe("sprint-meta", () => {
  it("labels every sprint state", () => {
    expect(SPRINT_STATE_LABELS.active).toBe("Active");
    expect(SPRINT_STATE_LABELS.planned).toBe("Planned");
    expect(SPRINT_STATE_LABELS.completed).toBe("Completed");
    expect(SPRINT_STATE_LABELS.cancelled).toBe("Cancelled");
  });

  it("badge classes are token-only (no hardcoded colour)", () => {
    for (const state of ["planned", "active", "completed", "cancelled"] as const) {
      const cls = sprintStateBadgeClass(state);
      expect(cls).not.toMatch(/#[0-9a-f]{3,}/i);
      expect(cls).not.toMatch(/rgb\(/i);
    }
    expect(sprintStateBadgeClass("active")).toContain("text-primary");
  });

  it("bands predictability into a tone", () => {
    expect(predictabilityTone(0.95)).toBe("success");
    expect(predictabilityTone(0.75)).toBe("warning");
    expect(predictabilityTone(0.4)).toBe("danger");
  });

  it("formats percents, points and decimals safely", () => {
    expect(formatPct(0.844)).toBe("84%");
    expect(formatPct(Number.NaN)).toBe("0%");
    expect(formatPoints(23.6)).toBe("24");
    expect(formatPoints(null)).toBe("0");
    expect(formatDecimal(26.5)).toBe("26.5");
    expect(formatDecimal(undefined)).toBe("0.0");
  });

  it("formats short dates from date-only and datetime strings", () => {
    expect(formatDateShort("2026-06-03")).toBe("Jun 3");
    expect(formatDateShort("2026-06-03T12:00:00Z")).toBe("Jun 3");
    expect(formatDateShort(null)).toBe("—");
    expect(formatDateShort("not-a-date")).toBe("—");
  });

  it("counts inclusive sprint length days", () => {
    expect(sprintLengthDays("2026-06-01", "2026-06-14")).toBe(14);
    expect(sprintLengthDays(null, "2026-06-14")).toBeNull();
  });

  it("orders sprints newest-first by start date", () => {
    const list = [
      sprint({ id: "a", start_date: "2026-05-01" }),
      sprint({ id: "b", start_date: "2026-06-01" }),
      sprint({ id: "c", start_date: "2026-04-01" }),
    ];
    expect(sortSprintsNewestFirst(list).map((s) => s.id)).toEqual(["b", "a", "c"]);
  });

  it("focuses the active sprint, else the most recent", () => {
    const withActive = [
      sprint({ id: "old", start_date: "2026-06-01" }),
      sprint({ id: "run", start_date: "2026-05-01", state: "active" }),
    ];
    expect(pickDefaultSprintId(withActive)).toBe("run");

    const noActive = [
      sprint({ id: "x", start_date: "2026-05-01" }),
      sprint({ id: "y", start_date: "2026-06-10" }),
    ];
    expect(pickDefaultSprintId(noActive)).toBe("y");

    expect(pickDefaultSprintId([])).toBeNull();
  });

  it("labels and token-classes every allocation status", () => {
    expect(ALLOCATION_STATUS_LABELS.over).toBe("Over-allocated");
    expect(ALLOCATION_STATUS_LABELS.under).toBe("Under-allocated");
    expect(ALLOCATION_STATUS_LABELS.balanced).toBe("Balanced");
    for (const status of ["under", "balanced", "over"] as const) {
      const cls = allocationStatusBadgeClass(status);
      expect(cls).not.toMatch(/#[0-9a-f]{3,}/i);
      expect(cls).not.toMatch(/rgb\(/i);
    }
    expect(allocationStatusBadgeClass("over")).toContain("text-danger");
  });
});
