import { DeploymentsView } from "@/components/deployments/deployments-view";

/**
 * Deployment gates (F31) — the promotion control plane. A ranked environment
 * pipeline (dev → staging → prod) above the recent-deployments list and the
 * focused deployment's gate detail: verdict, per-check breakdown, history, and
 * the approve / reject / cancel / roll-back controls. Backed by the typed
 * `/deployments` + `/projects/{id}/pipeline` routers, keyboard-first (`j/k`
 * move, `p` promotes) with a single ember Promote action.
 */
export default function DeploymentsPage() {
  return <DeploymentsView />;
}
