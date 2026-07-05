import { ApprovalInbox } from "@/components/approvals/approval-inbox";

/**
 * Approval inbox — the human-in-the-loop review queue. A risk-ranked list of
 * pending gates (spec / plan / PR / deploy / incident / policy) beside the
 * nine "must-show" review items and the approve / reject / request-changes /
 * escalate decision bar. Backed by the typed F36 `/approvals` API with
 * optimistic decisions and full keyboard control (`j/k`, `a/x/r/e`).
 */
export default function ApprovalsPage() {
  return <ApprovalInbox />;
}
