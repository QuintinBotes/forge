// HARD-09: security response headers for the Next.js app, mirroring the API
// edge (forge_api.security.headers). Factored out so both next.config.mjs and
// the unit test consume one source of truth.

export interface SecurityHeader {
  key: string;
  value: string;
}

// A conservative Content-Security-Policy for the app. `frame-ancestors 'none'`
// is the modern clickjacking control (pairs with X-Frame-Options: DENY for
// older agents). Kept intentionally strict; widen deliberately per feature.
export const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "base-uri 'self'",
  "frame-ancestors 'none'",
  "object-src 'none'",
  "img-src 'self' data:",
  "style-src 'self' 'unsafe-inline'",
  "form-action 'self'",
].join("; ");

export const SECURITY_HEADERS: readonly SecurityHeader[] = [
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Content-Security-Policy", value: CONTENT_SECURITY_POLICY },
];
