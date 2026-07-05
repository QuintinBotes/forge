/**
 * Board filtering — pure predicates plus preset + saved "views".
 *
 * A {@link BoardView} is a named, serialisable filter (status / priority /
 * assignment). {@link PRESET_VIEWS} are the always-present chips; user-created
 * views are persisted to `localStorage` so a filter survives reloads. Everything
 * here is framework-free and unit-tested; the toolbar composes these with a live
 * text query via {@link filterTasks}.
 */

import type { Priority, TaskDTO, TaskStatus } from "@/lib/api/types";
import { statusOf } from "@/lib/board/status";

/** Assignment axis for a view. `me` / `unassigned` resolve against context. */
export type AssigneeFilter = "any" | "me" | "unassigned";

export interface BoardView {
  id: string;
  label: string;
  statuses?: TaskStatus[];
  priorities?: Priority[];
  assignee?: AssigneeFilter;
  /**
   * Text query captured when the view was saved. Not applied by
   * {@link matchesView} (the live search box owns text matching); selecting a
   * saved view restores this into the box.
   */
  query?: string;
}

export interface FilterContext {
  /** Resolves the `me` assignment axis; absent when the viewer is unknown. */
  currentUserId?: string | null;
}

/** The unfiltered baseline; always first in the bar and never removable. */
export const ALL_VIEW: BoardView = { id: "all", label: "All work" };

/**
 * Built-in views. Deliberately few and workflow-true: the states an engineer
 * actually pivots between on a run — not a decorative catalogue.
 */
export const PRESET_VIEWS: BoardView[] = [
  ALL_VIEW,
  { id: "mine", label: "My tasks", assignee: "me" },
  {
    id: "active",
    label: "Active",
    statuses: ["in_progress", "in_review"],
  },
  { id: "blocked", label: "Blocked", statuses: ["blocked"] },
  { id: "hot", label: "High priority", priorities: ["high", "urgent"] },
  { id: "unassigned", label: "Unassigned", assignee: "unassigned" },
];

/** True when `task` satisfies every axis set on `view`. Empty axes match all. */
export function matchesView(
  task: TaskDTO,
  view: BoardView,
  ctx: FilterContext = {},
): boolean {
  if (view.statuses && view.statuses.length > 0) {
    if (!view.statuses.includes(statusOf(task))) {
      return false;
    }
  }
  if (view.priorities && view.priorities.length > 0) {
    if (!task.priority || !view.priorities.includes(task.priority)) {
      return false;
    }
  }
  switch (view.assignee) {
    case "me":
      if (!ctx.currentUserId || task.assignee_id !== ctx.currentUserId) {
        return false;
      }
      break;
    case "unassigned":
      if (task.assignee_id) {
        return false;
      }
      break;
    default:
      break;
  }
  return true;
}

/** Case-insensitive substring match over a task's title, key and labels. */
export function matchesQuery(task: TaskDTO, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) {
    return true;
  }
  const haystack = [task.title, task.key ?? "", ...(task.labels ?? [])]
    .join(" ")
    .toLowerCase();
  return haystack.includes(q);
}

export interface FilterTasksArgs {
  view: BoardView;
  query: string;
  currentUserId?: string | null;
}

/** Apply an active view *and* the live text query to a task list. */
export function filterTasks(
  tasks: TaskDTO[],
  { view, query, currentUserId }: FilterTasksArgs,
): TaskDTO[] {
  return tasks.filter(
    (task) =>
      matchesView(task, view, { currentUserId }) && matchesQuery(task, query),
  );
}

// --- Saved views (localStorage) ------------------------------------------- //

export const SAVED_VIEWS_KEY = "forge.board.savedViews";

function storage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

/** Load the viewer's saved views; tolerant of absent/corrupt storage. */
export function loadSavedViews(): BoardView[] {
  const store = storage();
  if (!store) {
    return [];
  }
  try {
    const raw = store.getItem(SAVED_VIEWS_KEY);
    if (!raw) {
      return [];
    }
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed.filter(
      (v): v is BoardView =>
        typeof v?.id === "string" && typeof v?.label === "string",
    );
  } catch {
    return [];
  }
}

/** Persist the full list of saved views (no-op when storage is unavailable). */
export function persistSavedViews(views: BoardView[]): void {
  const store = storage();
  if (!store) {
    return;
  }
  try {
    store.setItem(SAVED_VIEWS_KEY, JSON.stringify(views));
  } catch {
    /* ignore quota / privacy-mode failures — filtering still works in-session. */
  }
}
