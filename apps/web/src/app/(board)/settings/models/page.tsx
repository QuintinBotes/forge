import { AoSettingsView } from "@/components/ao-settings/ao-settings-view";
import { SelfEvalPanel } from "@/components/self-eval/self-eval-panel";

/**
 * Adaptive Orchestration "Models & effort" settings (`ao-settings-ui`): per-role
 * model + effort selectors, the tier -> model map editor, complexity thresholds,
 * the auto-route toggle, and a live routing-preview panel. Backed by the typed
 * `/ao/role-config`, `/ao/settings` and `/ao/routing-preview` routers.
 *
 * Below it, the Self-Eval Gate panel (`/ao/self-eval/*`): the private suite,
 * frozen baseline, gate posture for pending config changes, and the run
 * trigger — the gate guards exactly the config edited on this page.
 */
export default function AoSettingsPage() {
  return (
    <div className="flex flex-col gap-6">
      <AoSettingsView />
      <SelfEvalPanel />
    </div>
  );
}
