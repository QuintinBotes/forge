/**
 * Pure presentation helpers for the marketplace surface: labels, the trust /
 * verification "hallmark" badge descriptors (token-based class strings only —
 * never hardcoded colour), the install-safety predicates, and the client-side
 * catalog filter. Kept JSX-free so it is unit-testable in isolation.
 */

import type {
  ArtifactKind,
  InstallStatus,
  Listing,
  TrustLevel,
  VerificationStatus,
} from "@/lib/api/types";

export const KIND_LABELS: Record<ArtifactKind, string> = {
  mcp_connector: "MCP connector",
  skill_profile: "Skill profile",
  workflow_template: "Workflow template",
  policy_template: "Policy template",
};

export function kindLabel(kind: ArtifactKind | string): string {
  return KIND_LABELS[kind as ArtifactKind] ?? kind;
}

/** A tinted pill: label + a token-only className (bg/border/text via /opacity). */
export interface BadgeSpec {
  label: string;
  className: string;
}

/** Registry provenance — the foundry mark. */
export function trustBadge(level: TrustLevel): BadgeSpec {
  switch (level) {
    case "official":
      return {
        label: "Official",
        className: "border-success/30 bg-success/10 text-success",
      };
    case "trusted":
      return {
        label: "Trusted",
        className: "border-primary/30 bg-primary/10 text-primary",
      };
    case "community":
      return {
        label: "Community",
        className: "border-border bg-muted text-muted-foreground",
      };
    case "unverified":
    default:
      return {
        label: "Unverified",
        className: "border-warning/40 bg-warning/10 text-warning",
      };
  }
}

/** The cryptographic assay result — the struck hallmark. */
export function verificationBadge(status: VerificationStatus): BadgeSpec {
  switch (status) {
    case "verified":
      return {
        label: "Verified signature",
        className: "border-success/30 bg-success/10 text-success",
      };
    case "unsigned":
      return {
        label: "Unsigned",
        className: "border-warning/40 bg-warning/10 text-warning",
      };
    case "untrusted_registry":
      return {
        label: "Untrusted registry",
        className: "border-warning/40 bg-warning/10 text-warning",
      };
    case "signature_invalid":
      return {
        label: "Signature invalid",
        className: "border-danger/40 bg-danger/10 text-danger",
      };
    case "hash_mismatch":
    default:
      return {
        label: "Hash mismatch",
        className: "border-danger/40 bg-danger/10 text-danger",
      };
  }
}

/** Installation lifecycle badge. */
export function installStatusBadge(status: InstallStatus): BadgeSpec {
  switch (status) {
    case "installed":
      return {
        label: "Installed",
        className: "border-success/30 bg-success/10 text-success",
      };
    case "update_available":
      return {
        label: "Update available",
        className: "border-primary/30 bg-primary/10 text-primary",
      };
    case "failed":
      return {
        label: "Failed",
        className: "border-danger/40 bg-danger/10 text-danger",
      };
    case "pending":
      return {
        label: "Pending",
        className: "border-warning/40 bg-warning/10 text-warning",
      };
    case "uninstalled":
    default:
      return {
        label: "Uninstalled",
        className: "border-border bg-muted text-muted-foreground",
      };
  }
}

/** Hard-block statuses — install is refused outright. */
export const HARD_BLOCK_STATUSES: readonly VerificationStatus[] = [
  "signature_invalid",
  "hash_mismatch",
];

/** Soft-gated statuses — install requires an explicit admin acknowledgement. */
export const ACK_REQUIRED_STATUSES: readonly VerificationStatus[] = [
  "unsigned",
  "untrusted_registry",
];

export function isBlocked(status: VerificationStatus): boolean {
  return HARD_BLOCK_STATUSES.includes(status);
}

export function needsAcknowledgement(status: VerificationStatus): boolean {
  return ACK_REQUIRED_STATUSES.includes(status);
}

/**
 * Abbreviate a `sha256:<64 hex>` content hash to `sha256:abcd1234…` for the
 * mono "assay" chip. The full hash rides along as a `title` at the call site.
 */
export function shortHash(hash: string, hex = 8): string {
  if (!hash) return "";
  const [algo, digest] = hash.includes(":") ? hash.split(":") : ["", hash];
  if (!digest) return hash;
  const head = digest.slice(0, hex);
  return algo ? `${algo}:${head}…` : `${head}…`;
}

/** A stable, locale-independent date string for provenance rows (UTC). */
export function formatDate(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toISOString().slice(0, 10);
}

/**
 * Client-side catalog filter for the instant (sub-100ms) search feel: matches
 * `q` against name, summary, slug, tags and registry. Empty query → unchanged.
 */
export function filterListings(listings: Listing[], q: string): Listing[] {
  const needle = q.trim().toLowerCase();
  if (!needle) return listings;
  return listings.filter((l) => {
    const haystack = [
      l.name,
      l.summary,
      l.slug,
      l.registry_slug,
      ...(l.tags ?? []),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(needle);
  });
}
