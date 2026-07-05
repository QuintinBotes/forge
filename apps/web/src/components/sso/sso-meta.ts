/**
 * Pure derivations for the SSO / SCIM settings screen (no React, no I/O) so the
 * view stays declarative and the tricky bits — token lifecycle, domain
 * validation, SP-URL derivation, federation state — are unit-tested in
 * isolation. Everything here is a total function of its inputs.
 */

import type { ScimTokenInfo, SsoConfig } from "@/lib/api/types";

// --- Federation trust state (drives the signature header) ----------------- //

export type FederationState = "established" | "paused" | "unlinked";

/**
 * The state of the IdP <-> Forge trust link: `unlinked` when nothing is
 * configured, `established` when a config exists and SSO is on, `paused` when a
 * config exists but SSO is switched off.
 */
export function federationState(config: SsoConfig | null): FederationState {
  if (!config) return "unlinked";
  return config.enabled ? "established" : "paused";
}

// --- SCIM token lifecycle -------------------------------------------------- //

export type ScimTokenStatus = "active" | "revoked" | "expired";

/** Classify a SCIM token: revoked beats expired beats active. */
export function scimTokenStatus(
  token: Pick<ScimTokenInfo, "revoked_at" | "expires_at">,
  now: Date = new Date(),
): ScimTokenStatus {
  if (token.revoked_at) return "revoked";
  if (token.expires_at && new Date(token.expires_at).getTime() <= now.getTime()) {
    return "expired";
  }
  return "active";
}

/** How many tokens can currently authenticate (active = not revoked/expired). */
export function activeTokenCount(
  tokens: ScimTokenInfo[],
  now: Date = new Date(),
): number {
  return tokens.filter((t) => scimTokenStatus(t, now) === "active").length;
}

// --- Domains --------------------------------------------------------------- //

const DOMAIN_RE =
  /^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$/;

/** Lower-case + trim a domain (and strip a leading `@` if pasted from an email). */
export function normalizeDomain(input: string): string {
  return input.trim().toLowerCase().replace(/^@/, "");
}

/** A syntactically valid registrable domain (e.g. `acme.com`, `id.acme.io`). */
export function isValidDomain(input: string): boolean {
  return DOMAIN_RE.test(normalizeDomain(input));
}

// --- NameID formats -------------------------------------------------------- //

export const NAMEID_FORMAT_EMAIL =
  "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress";

export const NAMEID_FORMATS: ReadonlyArray<{ value: string; label: string }> = [
  { value: NAMEID_FORMAT_EMAIL, label: "Email address" },
  {
    value: "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
    label: "Persistent",
  },
  {
    value: "urn:oasis:names:tc:SAML:2.0:nameid-format:transient",
    label: "Transient",
  },
  {
    value: "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
    label: "Unspecified",
  },
];

/** Friendly label for a NameID format URN (falls back to the trailing token). */
export function nameIdFormatLabel(format: string): string {
  const known = NAMEID_FORMATS.find((f) => f.value === format);
  if (known) return known.label;
  const tail = format.split(":").pop() ?? format;
  return tail.replace(/nameid-format-?/i, "") || format;
}

// --- URL / host derivation ------------------------------------------------- //

/**
 * A human-readable host for the IdP node label. Entity IDs are usually URLs
 * (`https://idp.acme.com/saml`) but may be bare URNs; fall back to the raw value
 * trimmed of a scheme.
 */
export function hostLabel(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return "";
  try {
    return new URL(trimmed).host || trimmed;
  } catch {
    return trimmed.replace(/^[a-z]+:\/\//i, "").split("/")[0];
  }
}

/**
 * The workspace's SCIM 2.0 base URL — what the admin points their IdP's
 * provisioning at. Derived from the SP ACS URL (`{public}/auth/saml/{slug}/acs`)
 * when a config exists, else from the API base.
 */
export function scimBaseUrl(
  config: SsoConfig | null,
  fallbackBase: string,
): string {
  const public_ = config
    ? config.sp_acs_url.split("/auth/saml/")[0]
    : fallbackBase.replace(/\/+$/, "");
  return `${public_.replace(/\/+$/, "")}/scim/v2`;
}

/** Number of PEM certificate blocks present in the IdP cert text. */
export function countCerts(certText: string): number {
  const matches = certText.match(/-----BEGIN CERTIFICATE-----/g);
  return matches ? matches.length : 0;
}

// --- Formatting ------------------------------------------------------------ //

/** Absolute, locale-stable timestamp (UTC) for token metadata. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    hour12: false,
  });
}

/** Coarse relative age ("just now", "3h ago", "5d ago") for "last used". */
export function formatRelative(
  iso: string | null | undefined,
  now: Date = new Date(),
): string {
  if (!iso) return "Never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "Never";
  const secs = Math.max(0, Math.round((now.getTime() - then) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.round(months / 12)}y ago`;
}
