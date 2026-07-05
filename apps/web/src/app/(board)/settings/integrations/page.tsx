import { PmIntegrationsView } from "@/components/pm/pm-integrations-view";

/**
 * PM integrations settings (F18) — the workspace's external project-management
 * control plane. A rail of connected Jira / Linear adapters beside the selected
 * connection's connect details, live sync health, status-map editor and conflict
 * inbox (or the connect form when adding one). Backed by the typed
 * `/integrations/pm/connections` router; ember is reserved for one primary
 * action per view (Connect / Save mapping).
 */
export default function PmIntegrationsPage() {
  return <PmIntegrationsView />;
}
