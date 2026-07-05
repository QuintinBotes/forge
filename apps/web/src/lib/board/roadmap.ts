/**
 * Roadmap / timeline model — pure derivation from the board's real data.
 *
 * Rather than fabricate per-task start/end dates the DTOs don't carry, the
 * roadmap uses the data that *is* time-anchored: sprints form the horizontal
 * time skeleton (ordered by start date), milestones pin to the sprint window
 * that contains their due date, and epics become the swimlanes. Each cell is the
 * set of tasks in that (epic × sprint). Everything here is framework-free.
 */

import type {
  EpicDTO,
  MilestoneDTO,
  SprintDTO,
  TaskDTO,
} from "@/lib/api/types";

/** Sentinel column/lane ids for work that isn't scheduled / grouped. */
export const NO_SPRINT_ID = "__no_sprint__";
export const NO_EPIC_ID = "__no_epic__";

export interface RoadmapColumn {
  id: string;
  label: string;
  startsAt: string | null;
  endsAt: string | null;
  /** True for the synthetic trailing "No sprint" column. */
  unscheduled: boolean;
}

export interface RoadmapLane {
  id: string;
  label: string;
}

export interface RoadmapMilestone {
  id: string;
  label: string;
  dueAt: string | null;
  /** Column this milestone pins to, or NO_SPRINT_ID when it fits no window. */
  columnId: string;
}

export interface Roadmap {
  columns: RoadmapColumn[];
  lanes: RoadmapLane[];
  milestones: RoadmapMilestone[];
  /** `cell[laneId][columnId]` → tasks. Always present for every lane×column. */
  cell: Record<string, Record<string, TaskDTO[]>>;
  /** Total tasks placed — 0 means render the empty state. */
  total: number;
}

function time(value: string | null | undefined): number {
  if (!value) {
    return Number.POSITIVE_INFINITY;
  }
  const t = Date.parse(value);
  return Number.isNaN(t) ? Number.POSITIVE_INFINITY : t;
}

/** Which sprint window (if any) contains `dueAt`; else the unscheduled column. */
function columnForDate(
  dueAt: string | null | undefined,
  sprints: SprintDTO[],
): string {
  if (!dueAt) {
    return NO_SPRINT_ID;
  }
  const due = Date.parse(dueAt);
  if (Number.isNaN(due)) {
    return NO_SPRINT_ID;
  }
  for (const sprint of sprints) {
    if (!sprint.id) {
      continue;
    }
    const start = sprint.starts_at ? Date.parse(sprint.starts_at) : Number.NEGATIVE_INFINITY;
    const end = sprint.ends_at ? Date.parse(sprint.ends_at) : Number.POSITIVE_INFINITY;
    if (due >= start && due <= end) {
      return sprint.id;
    }
  }
  return NO_SPRINT_ID;
}

export interface BuildRoadmapArgs {
  tasks: TaskDTO[];
  epics: EpicDTO[];
  sprints: SprintDTO[];
  milestones: MilestoneDTO[];
}

/** Assemble the full roadmap grid from the board entities. */
export function buildRoadmap({
  tasks,
  epics,
  sprints,
  milestones,
}: BuildRoadmapArgs): Roadmap {
  const orderedSprints = [...sprints]
    .filter((s) => Boolean(s.id))
    .sort((a, b) => time(a.starts_at) - time(b.starts_at));

  const columns: RoadmapColumn[] = orderedSprints.map((sprint) => ({
    id: sprint.id as string,
    label: sprint.name,
    startsAt: sprint.starts_at ?? null,
    endsAt: sprint.ends_at ?? null,
    unscheduled: false,
  }));
  columns.push({
    id: NO_SPRINT_ID,
    label: "No sprint",
    startsAt: null,
    endsAt: null,
    unscheduled: true,
  });

  const hasUnepiced = tasks.some((t) => !t.epic_id);
  const lanes: RoadmapLane[] = [...epics]
    .filter((e) => Boolean(e.id))
    .sort((a, b) => a.title.localeCompare(b.title))
    .map((epic) => ({ id: epic.id as string, label: epic.title }));
  if (hasUnepiced || lanes.length === 0) {
    lanes.push({ id: NO_EPIC_ID, label: "No epic" });
  }

  const laneIds = new Set(lanes.map((l) => l.id));
  const columnIds = new Set(columns.map((c) => c.id));

  const cell: Record<string, Record<string, TaskDTO[]>> = {};
  for (const lane of lanes) {
    cell[lane.id] = {};
    for (const column of columns) {
      cell[lane.id][column.id] = [];
    }
  }

  let total = 0;
  for (const task of tasks) {
    const laneId = task.epic_id && laneIds.has(task.epic_id) ? task.epic_id : NO_EPIC_ID;
    const columnId =
      task.sprint_id && columnIds.has(task.sprint_id) ? task.sprint_id : NO_SPRINT_ID;
    if (!cell[laneId]) {
      continue;
    }
    cell[laneId][columnId].push(task);
    total += 1;
  }

  const roadmapMilestones: RoadmapMilestone[] = [...milestones]
    .filter((m) => Boolean(m.id))
    .sort((a, b) => time(a.due_at) - time(b.due_at))
    .map((m) => ({
      id: m.id as string,
      label: m.name,
      dueAt: m.due_at ?? null,
      columnId: columnForDate(m.due_at, orderedSprints),
    }));

  return { columns, lanes, milestones: roadmapMilestones, cell, total };
}
