import { describe, expect, it } from "vitest";

// next.config.mjs is plain JS; the TS 6 toolchain now resolves it without a
// directive (the prior @ts-expect-error became unused → TS2578).
import { securityHeaders as configHeaders } from "../../next.config.mjs";
import { CONTENT_SECURITY_POLICY, SECURITY_HEADERS } from "./security-headers";

// HARD-09: the web app must ship the same hardening headers as the API edge.
describe("security headers", () => {
  const byKey = new Map(SECURITY_HEADERS.map((h) => [h.key, h.value]));

  it("sets HSTS with a long max-age and subdomains", () => {
    const hsts = byKey.get("Strict-Transport-Security") ?? "";
    expect(hsts).toContain("max-age=");
    expect(hsts).toContain("includeSubDomains");
  });

  it("denies framing (clickjacking) two ways", () => {
    expect(byKey.get("X-Frame-Options")).toBe("DENY");
    expect(CONTENT_SECURITY_POLICY).toContain("frame-ancestors 'none'");
  });

  it("blocks MIME sniffing and referrer leakage", () => {
    expect(byKey.get("X-Content-Type-Options")).toBe("nosniff");
    expect(byKey.get("Referrer-Policy")).toBe("no-referrer");
  });

  it("has a default-deny-ish CSP", () => {
    expect(byKey.get("Content-Security-Policy")).toBe(CONTENT_SECURITY_POLICY);
    expect(CONTENT_SECURITY_POLICY).toContain("default-src 'self'");
    expect(CONTENT_SECURITY_POLICY).toContain("object-src 'none'");
  });

  it("is wired into next.config (config parity)", () => {
    // The array Next.js actually serves must match the canonical module, so the
    // two never drift.
    expect(configHeaders).toEqual([...SECURITY_HEADERS]);
  });
});
