import { AuditView } from "@/components/audit/audit-view";

/**
 * Audit log (F39) — the queryable window onto the immutable, hash-chained,
 * secret-redacted audit trail. A filter toolbar (actor / action / resource /
 * outcome / severity / time) drives a cursor-paginated table; selecting a row
 * opens the detail drawer with the before→after change and the chain-integrity
 * fields. Read-only: entries are export- and verify-only. Backed by the typed
 * `/audit` router.
 */
export default function AuditPage() {
  return <AuditView />;
}
