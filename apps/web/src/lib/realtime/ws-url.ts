/**
 * Runtime resolution of the Forge realtime WebSocket URL(s).
 *
 * Next.js inlines `NEXT_PUBLIC_*` at **build** time, and nothing plumbs
 * `NEXT_PUBLIC_WS_URL` (the `web` image takes no such build arg — see
 * `deploy/docker/web.Dockerfile`). A deployed instance behind Caddy/nginx would
 * therefore fall back to the hardcoded `ws://localhost:8000/ws`, which is dead in
 * a browser hitting `https://forge.example.com`. These helpers instead derive the
 * socket URL from `window.location` at **runtime**, so realtime targets the same
 * origin the page was served from — which the edge routes to the API (see
 * `docs/self-hosting/reverse-proxy.md`). An explicit `NEXT_PUBLIC_WS_URL` set at
 * build time still wins verbatim.
 *
 * This follows `apps/web/src/lib/api/client.ts`'s precedence (operator env wins,
 * else a safe default) but goes one step further: `client.ts` does no
 * `window.location` derivation — it leans on a relative `NEXT_PUBLIC_API_URL=/api`
 * being present at build time. The WS endpoint has no such build-time value, so
 * it must be derived from the live origin instead.
 */

/**
 * Static default origin (scheme + host) used in SSR / non-browser contexts and
 * on the local Next dev server (see below). Matches the legacy hardcoded default
 * and `client.ts`'s `http://localhost:8000` fallback: in dev the API listens on
 * :8000 while the web dev server runs on :3000.
 */
const LEGACY_WS_ORIGIN = "ws://localhost:8000";

/** Loopback hosts that identify a local origin. */
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

/**
 * Port the local Next web server listens on (`next dev` / standalone `next
 * start`, `deploy/docker/web*.Dockerfile`, `.env.dev` `WEB_PORT`). The full
 * compose stack, by contrast, fronts the app with Caddy on :80/:443, where
 * same-origin `/ws` IS routed to the API — so only the dev web server is the
 * exception below.
 */
const DEV_WEB_PORT = "3000";

/**
 * True when the page is served by the local Next web server (loopback host on
 * :3000), whose same-origin has no `/ws` route — so we keep the legacy API
 * origin (`ws://localhost:8000`) rather than pointing the socket at the dead
 * `ws://localhost:3000/ws`.
 */
function isLocalDevWebServer(hostname: string, port: string): boolean {
  return LOOPBACK_HOSTS.has(hostname) && port === DEV_WEB_PORT;
}

/**
 * Derive the WebSocket URL for a root `path` (e.g. `/ws`, `/ws/spec`) when no
 * operator override is set:
 *   - Browser: same-origin — `wss://` on an https page, `ws://` otherwise —
 *     EXCEPT on the local Next dev server, which keeps the legacy dev origin.
 *   - SSR / non-browser: the legacy static default (current behavior).
 */
export function sameOriginWsUrl(path: string): string {
  if (typeof window === "undefined") {
    return `${LEGACY_WS_ORIGIN}${path}`;
  }
  const { protocol, host, hostname, port } = window.location;
  if (isLocalDevWebServer(hostname, port)) {
    return `${LEGACY_WS_ORIGIN}${path}`;
  }
  const scheme = protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${host}${path}`;
}

/**
 * Board-push socket URL (`/ws`). `NEXT_PUBLIC_WS_URL` (build-time inlined) wins
 * verbatim — byte-identical to the legacy behavior for anyone who set it.
 */
export function resolveBoardWsUrl(): string {
  return process.env.NEXT_PUBLIC_WS_URL ?? sameOriginWsUrl("/ws");
}

/**
 * Spec co-editing base URL (`/ws/spec`; the room = spec id is appended by the
 * provider). Preserves the hook's existing env precedence exactly:
 * `NEXT_PUBLIC_SPEC_WS_URL` → `NEXT_PUBLIC_WS_URL` with `/ws`→`/ws/spec` →
 * same-origin derivation.
 */
export function resolveSpecWsBaseUrl(): string {
  return (
    process.env.NEXT_PUBLIC_SPEC_WS_URL ??
    process.env.NEXT_PUBLIC_WS_URL?.replace(/\/ws$/, "/ws/spec") ??
    sameOriginWsUrl("/ws/spec")
  );
}
