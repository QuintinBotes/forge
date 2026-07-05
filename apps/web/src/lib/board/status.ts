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

/**
 * Default task-status workflow transitions — a verbatim TypeScript mirror of
 * `forge_board.workflow.TASK_STATUS_TRANSITIONS` (the backend's authoritative
 * policy). The Kanban consults it so a drag can only *land* on a column the
 * server would accept, and so the move buttons only ever produce legal edges.
 * Keep in lockstep with the Python table.
 */
export const TASK_STATUS_TRANSITIONS: Record<TaskStatus, readonly TaskStatus[]> = {
  backlog: ["ready", "ready_for_agent", "blocked", "cancelled"],
  ready: ["ready_for_agent", "in_progress", "backlog", "blocked", "cancelled"],
  ready_for_agent: ["in_progress", "ready", "blocked", "cancelled"],
  in_progress: ["in_review", "blocked", "ready", "done", "cancelled"],
  in_review: ["done", "in_progress", "blocked", "cancelled"],
  blocked: ["ready", "ready_for_agent", "in_progress", "backlog", "cancelled"],
  // Terminal states may only be reopened.
  done: ["in_progress"],
  cancelled: ["backlog"],
};

/**
 * True when moving `src` → `dst` is a legal workflow edge. A same-status move is
 * an idempotent no-op (allowed), matching the backend's `can_transition`.
 */
export function canTransition(src: TaskStatus, dst: TaskStatus): boolean {
  if (src === dst) {
    return true;
  }
  return TASK_STATUS_TRANSITIONS[src]?.includes(dst) ?? false;
}

/** Legal *move* targets from `src` (excludes the no-op same-status), in column order. */
export function legalTargets(src: TaskStatus): TaskStatus[] {
  return STATUS_COLUMNS.filter((s) => s !== src && canTransition(src, s));
}

/**
 * Nearest legal target to the *right* of `src` within `columns` (the "advance"
 * button destination), or null when there is none — so the button matches the
 * column status rules rather than blindly stepping to the adjacent column.
 */
export function forwardTarget(
  src: TaskStatus,
  columns: readonly TaskStatus[] = STATUS_COLUMNS,
): TaskStatus | null {
  const from = columns.indexOf(src);
  if (from < 0) {
    return null;
  }
  for (let i = from + 1; i < columns.length; i += 1) {
    if (canTransition(src, columns[i])) {
      return columns[i];
    }
  }
  return null;
}

/** Nearest legal target to the *left* of `src` within `columns`, or null. */
export function backwardTarget(
  src: TaskStatus,
  columns: readonly TaskStatus[] = STATUS_COLUMNS,
): TaskStatus | null {
  const from = columns.indexOf(src);
  if (from < 0) {
    return null;
  }
  for (let i = from - 1; i >= 0; i -= 1) {
    if (canTransition(src, columns[i])) {
      return columns[i];
    }
  }
  return null;
}
