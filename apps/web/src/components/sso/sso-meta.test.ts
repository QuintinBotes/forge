import { describe, expect, it } from "vitest";

import type { ScimTokenInfo, SsoConfig } from "@/lib/api/types";

import {
  activeTokenCount,
  countCerts,
  federationState,
  formatRelative,
  hostLabel,
  isValidDomain,
  nameIdFormatLabel,
  normalizeDomain,
  scimBaseUrl,
  scimTokenStatus,
} from "./sso-meta";

const NOW = new Date("2026-07-05T12:00:00Z");

function token(over: Partial<ScimTokenInfo> = {}): ScimTokenInfo {
  return {
    id: "t1",
    name: "okta",
    token_prefix: "forge_sc",
    created_at: "2026-07-01T00:00:00Z",
    ...over,
  };
}

function config(over: Partial<SsoConfig> = {}): SsoConfig {
  return {
    id: "c1",
    workspace_id: "w1",
    protocol: "saml",
    enabled: true,
    idp: {
      entity_id: "https://idp.acme.com/saml",
      sso_url: "https://idp.acme.com/sso",
      slo_url: null,
      x509_certs: ["cert"],
      name_id_format: "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
    },
    sp_entity_id: "https://forge.example.com/auth/saml/acme/metadata",
    sp_acs_url: "https://forge.example.com/auth/saml/acme/acs",
    sp_slo_url: "https://forge.example.com/auth/saml/acme/slo",
    sp_metadata_url: "https://forge.example.com/auth/saml/acme/metadata",
    sp_cert_pem: "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----",
    domains: ["acme.com"],
    allow_idp_initiated: false,
    sign_authn_requests: true,
    want_assertions_signed: true,
    attribute_mapping: { email: "" },
    default_role: "member",
    group_role_map: {},
    jit_provisioning: true,
    ...over,
  };
}

describe("federationState", () => {
  it("is unlinked with no config", () => {
    expect(federationState(null)).toBe("unlinked");
  });
  it("is established when configured and enabled", () => {
    expect(federationState(config({ enabled: true }))).toBe("established");
  });
  it("is paused when configured but disabled", () => {
    expect(federationState(config({ enabled: false }))).toBe("paused");
  });
});

describe("scimTokenStatus", () => {
  it("classifies an active token", () => {
    expect(scimTokenStatus(token(), NOW)).toBe("active");
  });
  it("classifies a revoked token (revoked beats everything)", () => {
    expect(
      scimTokenStatus(
        token({ revoked_at: "2026-07-02T00:00:00Z", expires_at: "2030-01-01T00:00:00Z" }),
        NOW,
      ),
    ).toBe("revoked");
  });
  it("classifies an expired token", () => {
    expect(
      scimTokenStatus(token({ expires_at: "2026-07-04T00:00:00Z" }), NOW),
    ).toBe("expired");
  });
  it("treats a future expiry as active", () => {
    expect(
      scimTokenStatus(token({ expires_at: "2026-08-01T00:00:00Z" }), NOW),
    ).toBe("active");
  });
});

describe("activeTokenCount", () => {
  it("counts only usable tokens", () => {
    const tokens = [
      token({ id: "a" }),
      token({ id: "b", revoked_at: "2026-07-02T00:00:00Z" }),
      token({ id: "c", expires_at: "2026-07-04T00:00:00Z" }),
      token({ id: "d", expires_at: "2027-01-01T00:00:00Z" }),
    ];
    expect(activeTokenCount(tokens, NOW)).toBe(2);
  });
});

describe("domain validation", () => {
  it("normalizes case, whitespace and a pasted leading @", () => {
    expect(normalizeDomain("  @Acme.COM ")).toBe("acme.com");
  });
  it("accepts registrable domains and subdomains", () => {
    expect(isValidDomain("acme.com")).toBe(true);
    expect(isValidDomain("id.acme.io")).toBe(true);
  });
  it("rejects malformed input", () => {
    expect(isValidDomain("acme")).toBe(false);
    expect(isValidDomain("acme@com")).toBe(false);
    expect(isValidDomain("")).toBe(false);
  });
});

describe("nameIdFormatLabel", () => {
  it("labels a known format", () => {
    expect(
      nameIdFormatLabel("urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"),
    ).toBe("Email address");
  });
  it("falls back to the trailing token for an unknown urn", () => {
    expect(nameIdFormatLabel("urn:custom:persistent")).toBe("persistent");
  });
});

describe("hostLabel", () => {
  it("extracts the host from a URL entity id", () => {
    expect(hostLabel("https://idp.acme.com/saml/metadata")).toBe("idp.acme.com");
  });
  it("returns a bare urn unchanged", () => {
    expect(hostLabel("acme-idp")).toBe("acme-idp");
  });
});

describe("scimBaseUrl", () => {
  it("derives the SCIM base from the SP ACS URL when configured", () => {
    expect(scimBaseUrl(config(), "http://localhost:8000")).toBe(
      "https://forge.example.com/scim/v2",
    );
  });
  it("falls back to the API base when unconfigured", () => {
    expect(scimBaseUrl(null, "http://localhost:8000/")).toBe(
      "http://localhost:8000/scim/v2",
    );
  });
});

describe("countCerts", () => {
  it("counts PEM blocks", () => {
    expect(countCerts("")).toBe(0);
    expect(
      countCerts(
        "-----BEGIN CERTIFICATE-----\nA\n-----END CERTIFICATE-----\n-----BEGIN CERTIFICATE-----\nB\n-----END CERTIFICATE-----",
      ),
    ).toBe(2);
  });
});

describe("formatRelative", () => {
  it("returns Never for a missing timestamp", () => {
    expect(formatRelative(null, NOW)).toBe("Never");
  });
  it("bins recent activity", () => {
    expect(formatRelative("2026-07-05T11:59:40Z", NOW)).toBe("just now");
    expect(formatRelative("2026-07-05T09:00:00Z", NOW)).toBe("3h ago");
    expect(formatRelative("2026-06-30T12:00:00Z", NOW)).toBe("5d ago");
  });
});
