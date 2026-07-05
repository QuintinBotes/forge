import { WalkthroughView } from "@/components/walkthrough/walkthrough-view";

/**
 * Guided walkthrough — the first-run product tour that ties the whole Forge
 * loop together: create a spec -> run an agent -> review the PR -> merge. The
 * tour spotlights each real, navigable stop; it is dismissible, resumable and
 * restartable from the Help menu or the ⌘K palette. Live progress is read from
 * the typed `/projects/{id}/specs`, `/approvals` and `/deployments` routers.
 */
export default function WalkthroughPage() {
  return <WalkthroughView />;
}
