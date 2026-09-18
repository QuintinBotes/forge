"use client";

import { BadgeCheck, BadgeX, FileQuestion } from "lucide-react";
import { useState } from "react";

import { useApprovalAttestation } from "@/lib/api/approvals";
import { apiClient, type ForgeApiClient } from "@/lib/api/client";
import type { AttestationOut } from "@/lib/api/types";
import { cn } from "@/lib/utils";

export interface AttestationPanelProps {
  /**
   * The approval gate whose attestation to show (`ApprovalSummary.id`).
   * Absent -> nothing renders.
   */
  approvalId?: string | null;
  client?: ForgeApiClient;
}

/**
 * The Attested Changeset panel (Attested Changesets, Task 19).
 *
 * Approving a `pr` gate that carries a workflow run mints a DSSE/Ed25519-signed
 * provenance record over the changeset. This panel shows exactly what the
 * server can vouch for — three honest states, no fake ones:
 *
 * - **verified** — a signed record exists and its signature verifies against
 *   the deployment's key (computed server-side by the same path the
 *   `forge-verify` CLI uses);
 * - **verification-failed** — a signed record exists but its signature does
 *   NOT verify (wrong or rotated key, or a tampered record);
 * - **absent** — the server confirmed no attestation exists (normal while the
 *   gate is still pending: records are minted on approval).
 *
 * While loading — or when the fetch itself fails — nothing renders: a failed
 * request is not proof of absence, so the panel stays quiet rather than
 * claiming a state it cannot back.
 */
export function AttestationPanel({ approvalId, client = apiClient }: AttestationPanelProps) {
  const [expanded, setExpanded] = useState(false);
  const query = useApprovalAttestation(approvalId, client);

  if (!approvalId || query.isPending || query.isError) {
    return null;
  }

  const attestation = query.data ?? null;

  if (attestation === null) {
    return (
      <div
        data-testid="attestation-panel"
        data-state="absent"
        className="inline-flex w-fit items-center gap-1.5 rounded-full border border-border bg-muted/40 px-2.5 py-1 text-xs font-medium text-muted-foreground"
      >
        <FileQuestion aria-hidden className="h-3.5 w-3.5" />
        Not attested — no signed changeset record for this gate yet
      </div>
    );
  }

  const failed = !attestation.verified;
  const Icon = failed ? BadgeX : BadgeCheck;

  return (
    <div
      data-testid="attestation-panel"
      data-state={failed ? "verification-failed" : "verified"}
      className="flex flex-col gap-2"
    >
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
        className={cn(
          "inline-flex w-fit items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          failed
            ? "border-danger/40 bg-danger/10 text-danger"
            : "border-success/40 bg-success/10 text-success",
        )}
      >
        <Icon aria-hidden className="h-3.5 w-3.5" />
        {failed
          ? "Attested changeset — signature failed verification"
          : "Attested changeset — signature verified"}
      </button>

      {expanded ? <AttestationDetails attestation={attestation} /> : null}
    </div>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 truncate text-right font-mono text-foreground">{value}</dd>
    </div>
  );
}

function AttestationDetails({ attestation }: { attestation: AttestationOut }) {
  const { provenance } = attestation;
  const spec =
    provenance.spec_key && provenance.spec_version
      ? `${provenance.spec_key} v${provenance.spec_version}`
      : null;
  return (
    <div
      data-testid="attestation-details"
      className="flex flex-col gap-1.5 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs"
    >
      <dl className="flex flex-col gap-1">
        <DetailRow label="Changeset" value={attestation.changeset_hash} />
        <DetailRow label="Signing key" value={attestation.keyid} />
        {provenance.pr_numbers.length > 0 ? (
          <DetailRow label="PRs" value={provenance.pr_numbers.join(", ")} />
        ) : null}
        {spec ? <DetailRow label="Spec" value={spec} /> : null}
        {provenance.audit_seq != null ? (
          <DetailRow label="Audit seq" value={String(provenance.audit_seq)} />
        ) : null}
        <DetailRow
          label="Attested at"
          value={new Date(attestation.created_at).toLocaleString()}
        />
      </dl>
    </div>
  );
}
