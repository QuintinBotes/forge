import { RbacAdminView } from "@/components/rbac/rbac-admin-view";

/**
 * Multi-team & RBAC admin (F30) — the workspace's access control plane. Three
 * scope tabs: workspace Members and their roles (admin / member / viewer /
 * agent-runner), Teams and their leads, and per-project visibility + team
 * access. Backed by the typed `/access/grants`, `/teams` and
 * `/projects/{id}/access` routers; ember is reserved for one primary action
 * per tab.
 */
export default function RbacAdminPage() {
  return <RbacAdminView />;
}
