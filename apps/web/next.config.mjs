/** @type {import('next').NextConfig} */

// HARD-09: security response headers, mirroring the API edge
// (forge_api.security.headers) and src/lib/security-headers.ts. Kept in JS here
// because next.config.mjs is loaded by Node (cannot import the .ts module); a
// Vitest parity test (src/lib/security-headers.test.ts) asserts the two agree.
const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "base-uri 'self'",
  "frame-ancestors 'none'",
  "object-src 'none'",
  "img-src 'self' data:",
  "style-src 'self' 'unsafe-inline'",
  "form-action 'self'",
].join("; ");

export const securityHeaders = [
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Content-Security-Policy", value: CONTENT_SECURITY_POLICY },
];

const nextConfig = {
  reactStrictMode: true,
  // HARD-07: emit a self-contained production server (.next/standalone) so the
  // runtime container ships only the traced server + static assets, not the
  // whole pnpm workspace (smaller image, smaller attack surface / SBOM).
  output: "standalone",
  // Note: Next.js 16 removed the built-in ESLint integration (`next lint` and the
  // `eslint` config key). Linting now runs via `pnpm lint` (eslint.config.mjs).
  //
  // `NEXT_PUBLIC_API_URL` is intentionally NOT declared in an `env:` block. Next
  // already inlines any `process.env.NEXT_PUBLIC_*` reference at build time from
  // the build environment (a build arg wins; unset → `undefined`), exactly like
  // `NEXT_PUBLIC_WS_URL`. Forcing a `?? "http://localhost:8000"` default here (as
  // this block used to) would inline that literal into every bundle — a truthy,
  // absolute value — so the client could never tell "unset" from "localhost", and
  // the same-origin fallback in src/lib/api/api-url.ts would be dead code. Leaving
  // it unset lets an un-configured build derive a same-origin `/api` base at
  // runtime in the browser (see api-url.ts + docs/self-hosting/reverse-proxy.md).
  // HARD-09: apply the hardening headers to every route.
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
