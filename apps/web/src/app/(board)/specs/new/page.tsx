import { Suspense } from "react";

import { NewSpecPage } from "@/components/spec-studio/new-spec-page";

/**
 * `/specs/new` — the guided spec-creation entry point (pick an epic, draft
 * the goal/requirements/acceptance criteria via the Guided-mode form).
 *
 * Wrapped in `Suspense`: `NewSpecPage` reads `?epicId=` via `useSearchParams`
 * (the board-epic "Create spec" entry point preselects the epic), which Next
 * requires a Suspense boundary around for static export.
 */
export default function NewSpecRoute() {
  return (
    <Suspense fallback={null}>
      <NewSpecPage />
    </Suspense>
  );
}
