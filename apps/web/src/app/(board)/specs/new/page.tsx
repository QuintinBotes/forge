import { NewSpecPage } from "@/components/spec-studio/new-spec-page";

/**
 * `/specs/new` — the guided spec-creation entry point (pick an epic, draft
 * the goal/requirements/acceptance criteria via the Guided-mode form).
 */
export default function NewSpecRoute() {
  return <NewSpecPage />;
}
