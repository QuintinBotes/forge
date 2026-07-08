import { AoSettingsView } from "@/components/ao-settings/ao-settings-view";

/**
 * Adaptive Orchestration "Models & effort" settings (`ao-settings-ui`): per-role
 * model + effort selectors, the tier -> model map editor, complexity thresholds,
 * the auto-route toggle, and a live routing-preview panel. Backed by the typed
 * `/ao/role-config`, `/ao/settings` and `/ao/routing-preview` routers.
 */
export default function AoSettingsPage() {
  return <AoSettingsView />;
}
