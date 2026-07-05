import { WorkflowEditor } from "@/components/workflow/workflow-editor";

/**
 * Workflow visual editor (F28) — the governed, versioned authoring surface over
 * the workflow DSL. A definitions rail, a hand-rolled SVG state-machine canvas,
 * and an inspector + validation panel over the typed `/workflow/editor` router.
 * Editing is optimistic; Save draft re-validates, Publish (the single ember
 * action) promotes a revision once it has zero errors.
 */
export default function WorkflowEditorPage() {
  return <WorkflowEditor />;
}
