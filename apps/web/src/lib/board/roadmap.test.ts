import { describe, expect, it } from "vitest";

import type { EpicDTO, MilestoneDTO, SprintDTO, TaskDTO } from "@/lib/api/types";
import { NO_EPIC_ID, NO_SPRINT_ID, buildRoadmap } from "./roadmap";

const epics: EpicDTO[] = [
  { id: "e1", title: "Auth" },
  { id: "e2", title: "Billing" },
];

const sprints: SprintDTO[] = [
  { id: "s2", name: "Sprint 2", starts_at: "2026-02-01", ends_at: "2026-02-14" },
  { id: "s1", name: "Sprint 1", starts_at: "2026-01-01", ends_at: "2026-01-14" },
];

const milestones: MilestoneDTO[] = [
  { id: "m1", name: "Beta", due_at: "2026-01-10" },
  { id: "m2", name: "GA", due_at: "2026-09-01" },
];

const tasks: TaskDTO[] = [
  { id: "t1", title: "Login", epic_id: "e1", sprint_id: "s1" },
  { id: "t2", title: "Invoices", epic_id: "e2", sprint_id: "s2" },
  { id: "t3", title: "Loose end", epic_id: "e1" },
  { id: "t4", title: "Orphan" },
];

describe("buildRoadmap", () => {
  it("orders sprint columns by start date and appends a No-sprint column", () => {
    const roadmap = buildRoadmap({ tasks, epics, sprints, milestones });
    expect(roadmap.columns.map((c) => c.id)).toEqual(["s1", "s2", NO_SPRINT_ID]);
    expect(roadmap.columns.at(-1)?.unscheduled).toBe(true);
  });

  it("builds epic lanes plus a No-epic lane when unepiced work exists", () => {
    const roadmap = buildRoadmap({ tasks, epics, sprints, milestones });
    expect(roadmap.lanes.map((l) => l.id)).toEqual(["e1", "e2", NO_EPIC_ID]);
  });

  it("places tasks in the right epic × sprint cells", () => {
    const roadmap = buildRoadmap({ tasks, epics, sprints, milestones });
    expect(roadmap.cell.e1.s1.map((t) => t.id)).toEqual(["t1"]);
    expect(roadmap.cell.e1[NO_SPRINT_ID].map((t) => t.id)).toEqual(["t3"]);
    expect(roadmap.cell[NO_EPIC_ID][NO_SPRINT_ID].map((t) => t.id)).toEqual(["t4"]);
    expect(roadmap.total).toBe(4);
  });

  it("pins milestones to the sprint window that contains their due date", () => {
    const roadmap = buildRoadmap({ tasks, epics, sprints, milestones });
    const beta = roadmap.milestones.find((m) => m.id === "m1");
    const ga = roadmap.milestones.find((m) => m.id === "m2");
    expect(beta?.columnId).toBe("s1");
    // GA falls outside every sprint window → unscheduled column.
    expect(ga?.columnId).toBe(NO_SPRINT_ID);
  });

  it("reports zero total when there are no tasks", () => {
    expect(buildRoadmap({ tasks: [], epics, sprints, milestones }).total).toBe(0);
  });
});
