import { describe, expect, it } from "vitest";

// next.config.mjs is plain JS; the TS 6 toolchain now resolves it without a
// directive (the prior @ts-expect-error became unused → TS2578).
import { securityHeaders as configHeaders } from "../../next.config.mjs";
import {
  buildContentSecurityPolicy,
  CONTENT_SECURITY_POLICY,
  SECURITY_HEADERS,
} from "./security-headers";

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

/**
 * The policy shipped with no `script-src` at all, so scripts fell back to
 * `default-src 'self'` — which forbids inline script. Next.js bootstraps every
 * document with one (`<script id="_R_">self.__next_r=…`), so the browser
 * blocked it, the client threw `InvariantError: Expected a request ID to be
 * defined for the document`, and hydration never ran: the nav rendered, every
 * effect was dead, and each data view sat on a skeleton forever.
 */
describe("CSP script policy", () => {
  it("states a script-src rather than inheriting default-src", () => {
    expect(CONTENT_SECURITY_POLICY).toContain("script-src");
  });

  it("carries the nonce Next stamps onto its inline bootstrap", () => {
    const csp = buildContentSecurityPolicy({ nonce: "abc123" });
    expect(csp).toContain("'nonce-abc123'");
    expect(csp).toContain("'strict-dynamic'");
  });

  it("never opens scripts up with unsafe-inline", () => {
    const csp = buildContentSecurityPolicy({ nonce: "abc123", isDev: true });
    const scriptSrc = csp.split("; ").find((d) => d.startsWith("script-src")) ?? "";
    expect(scriptSrc).not.toContain("'unsafe-inline'");
  });

  it("keeps unsafe-eval and the HMR socket out of production", () => {
    const prod = buildContentSecurityPolicy({ nonce: "abc123", isDev: false });
    expect(prod).not.toContain("'unsafe-eval'");
    expect(prod).not.toContain("ws:");

    // Turbopack evaluates modules, and hot reload needs its socket.
    const dev = buildContentSecurityPolicy({ nonce: "abc123", isDev: true });
    expect(dev).toContain("'unsafe-eval'");
    expect(dev).toContain("ws:");
  });

  it("allows a cross-origin API when the dev server is reached directly", () => {
    const csp = buildContentSecurityPolicy({
      connectSrc: ["http://localhost:8080"],
    });
    expect(csp).toContain("connect-src 'self' http://localhost:8080");
  });

  it("keeps the rest of the policy locked down", () => {
    const csp = buildContentSecurityPolicy({ nonce: "abc123", isDev: true });
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("base-uri 'self'");
  });
});
