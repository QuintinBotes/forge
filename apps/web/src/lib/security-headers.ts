// HARD-09: security response headers for the Next.js app, mirroring the API
// edge (forge_api.security.headers). Factored out so next.config.mjs, the
// middleware, and the unit test consume one source of truth.

export interface SecurityHeader {
  key: string;
  value: string;
}

/**
 * Build the app's Content-Security-Policy.
 *
 * **Scripts need an explicit allowance.** With no `script-src`, scripts fall
 * back to `default-src 'self'`, which forbids *inline* script — and Next.js
 * bootstraps every document with one (`<script id="_R_">self.__next_r=…`).
 * Blocking it leaves `self.__next_r` undefined, the client throws
 * `InvariantError: Expected a request ID to be defined for the document`, and
 * **hydration never runs**: the shell renders, every `useEffect` is dead, and
 * each data view sits on a skeleton forever. That is not a theoretical risk —
 * it is what the shipped policy did, and it made the whole UI look broken.
 *
 * Rather than open the policy with `'unsafe-inline'`, each request carries a
 * fresh `nonce` that Next stamps onto its own scripts, so exactly those run and
 * an injected `<script>` still does not. `'strict-dynamic'` lets Next's
 * bootstrap load the chunk graph it needs without enumerating every chunk.
 *
 * Development additionally needs `'unsafe-eval'` (Turbopack evaluates modules)
 * and a websocket connection for hot reload. Neither is emitted in production.
 */
export function buildContentSecurityPolicy({
  nonce,
  isDev = false,
  connectSrc = [],
}: {
  nonce?: string;
  isDev?: boolean;
  connectSrc?: readonly string[];
} = {}): string {
  const script = ["'self'"];
  if (nonce) {
    script.push(`'nonce-${nonce}'`, "'strict-dynamic'");
  }
  if (isDev) {
    script.push("'unsafe-eval'");
  }

  // The API and realtime socket are same-origin behind the Caddy edge, so
  // 'self' covers the supported deployment. A dev server reached on its own
  // port talks to another origin, hence the configured extras.
  const connect = ["'self'", ...connectSrc];
  if (isDev) {
    connect.push("ws:", "wss:");
  }

  return [
    "default-src 'self'",
    `script-src ${script.join(" ")}`,
    `connect-src ${[...new Set(connect)].join(" ")}`,
    "base-uri 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "img-src 'self' data:",
    "style-src 'self' 'unsafe-inline'",
    "form-action 'self'",
  ].join("; ");
}

/**
 * The static, nonce-less policy.
 *
 * The middleware replaces this per request with a nonce-bearing policy; this
 * value is the floor that applies if the middleware is ever bypassed, and it is
 * what the config-parity test pins.
 */
export const CONTENT_SECURITY_POLICY = buildContentSecurityPolicy();

export const SECURITY_HEADERS: readonly SecurityHeader[] = [
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Content-Security-Policy", value: CONTENT_SECURITY_POLICY },
];
