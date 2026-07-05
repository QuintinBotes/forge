import { SprintsView } from "@/components/sprints/sprints-view";

/**
 * Sprints & velocity (F26) — the project's sprint workspace: a board to move
 * work across the workflow, the committed-vs-completed velocity trend with a
 * rolling-average guide, and the day-by-day burndown against the ideal. Backed
 * by the typed F26 `/projects/{id}/sprints`, `/velocity` and `/sprints/{id}`
 * routes plus the board's optimistic task-status mutation.
 */
export default function SprintsPage() {
  return <SprintsView />;
}
