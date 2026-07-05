import {
  BadgeCheck,
  Plug,
  ShieldAlert,
  ShieldCheck,
  ShieldQuestion,
  ShieldX,
  Sparkles,
  Users,
  type LucideIcon,
} from "lucide-react";

import type {
  ArtifactKind,
  InstallStatus,
  TrustLevel,
  VerificationStatus,
} from "@/lib/api/types";
import { cn } from "@/lib/utils";

import {
  installStatusBadge,
  shortHash,
  trustBadge,
  verificationBadge,
} from "./marketplace-meta";

const TRUST_ICON: Record<TrustLevel, LucideIcon> = {
  official: BadgeCheck,
  trusted: ShieldCheck,
  community: Users,
  unverified: ShieldAlert,
};

const VERIFICATION_ICON: Record<VerificationStatus, LucideIcon> = {
  verified: ShieldCheck,
  unsigned: ShieldQuestion,
  untrusted_registry: ShieldAlert,
  signature_invalid: ShieldX,
  hash_mismatch: ShieldX,
};

const KIND_ICON: Partial<Record<ArtifactKind, LucideIcon>> = {
  mcp_connector: Plug,
  skill_profile: Sparkles,
};

const PILL =
  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium";

/** Registry provenance mark (official / trusted / community / unverified). */
export function TrustBadge({
  level,
  className,
}: {
  level: TrustLevel;
  className?: string;
}) {
  const spec = trustBadge(level);
  const Icon = TRUST_ICON[level];
  return (
    <span
      data-testid={`trust-${level}`}
      className={cn(PILL, spec.className, className)}
    >
      <Icon aria-hidden className="h-3 w-3" />
      {spec.label}
    </span>
  );
}

/** The struck hallmark: the cryptographic verification result. */
export function VerificationBadge({
  status,
  className,
}: {
  status: VerificationStatus;
  className?: string;
}) {
  const spec = verificationBadge(status);
  const Icon = VERIFICATION_ICON[status];
  return (
    <span
      data-testid={`verification-${status}`}
      className={cn(PILL, spec.className, className)}
    >
      <Icon aria-hidden className="h-3 w-3" />
      {spec.label}
    </span>
  );
}

/** Installation lifecycle badge. */
export function StatusBadge({
  status,
  className,
}: {
  status: InstallStatus;
  className?: string;
}) {
  const spec = installStatusBadge(status);
  return (
    <span
      data-testid={`install-status-${status}`}
      className={cn(PILL, spec.className, className)}
    >
      {spec.label}
    </span>
  );
}

/** The kind glyph (a plug for connectors, a spark for skill profiles). */
export function KindGlyph({
  kind,
  className,
}: {
  kind: ArtifactKind;
  className?: string;
}) {
  const Icon = KIND_ICON[kind] ?? Sparkles;
  return <Icon aria-hidden className={cn("h-4 w-4", className)} />;
}

/**
 * The "assay" chip: a content hash abbreviated to a mono glyph, full value on
 * hover. Encodes the supply-chain provenance the way an assayer marks metal.
 */
export function HashChip({
  hash,
  className,
}: {
  hash: string;
  className?: string;
}) {
  return (
    <code
      title={hash}
      data-testid="hash-chip"
      className={cn(
        "inline-flex items-center rounded border border-border bg-muted/60 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground",
        className,
      )}
    >
      {shortHash(hash)}
    </code>
  );
}
