/**
 * Board status + priority metadata and grouping helpers.
 *
 * The status order mirrors `forge_contracts.enums.TaskStatus` (Phase-0 frozen
 * contract) and drives both the kanban column order and the
 * forward/back ("move") controls on cards.
 */

import type { Priority, TaskDTO, TaskStatus } from "@/lib/api/types";
import { TASK_PRIORITIES, TASK_STATUSES } from "@/lib/api/types";

/** Kanban columns, in workflow order (matches the contract enum order). */
export const STATUS_COLUMNS: readonly TaskStatus[] = TASK_STATUSES;

export const STATUS_LABELS: Record<TaskStatus, string> = {
  backlog: "Backlog",
  ready: "Ready",
  ready_for_agent: "Ready for agent",
  in_progress: "In progress",
  in_review: "In review",
  blocked: "Blocked",
  done: "Done",
  cancelled: "Cancelled",
};

export const PRIORITY_LABELS: Record<Priority, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  urgent: "Urgent",
};

/** Status a task moves to when advanced one step forward, or null at the end. */
export function nextStatus(status: TaskStatus): TaskStatus | null {
  const index = STATUS_COLUMNS.indexOf(status);
  if (index < 0 || index >= STATUS_COLUMNS.length - 1) {
    return null;
  }
  return STATUS_COLUMNS[index + 1];
}

/** Status a task moves to when stepped back, or null at the start. */
export function prevStatus(status: TaskStatus): TaskStatus | null {
  const index = STATUS_COLUMNS.indexOf(status);
  if (index <= 0) {
    return null;
  }
  return STATUS_COLUMNS[index - 1];
}

/** Resolve a task's effective status, defaulting an absent status to backlog. */
export function statusOf(task: TaskDTO): TaskStatus {
  return (task.status as TaskStatus | undefined) ?? "backlog";
}

/** Group tasks into one bucket per column; every column is always present. */
export function groupByStatus(tasks: TaskDTO[]): Record<TaskStatus, TaskDTO[]> {
  const grouped = Object.fromEntries(
    STATUS_COLUMNS.map((status) => [status, [] as TaskDTO[]]),
  ) as Record<TaskStatus, TaskDTO[]>;

  for (const task of tasks) {
    grouped[statusOf(task)].push(task);
  }
  return grouped;
}

export const PRIORITY_ORDER: Record<Priority, number> = {
  urgent: 0,
  high: 1,
  medium: 2,
  low: 3,
};

/** Priorities exported for callers that need the canonical ordering. */
export const PRIORITIES = TASK_PRIORITIES;
