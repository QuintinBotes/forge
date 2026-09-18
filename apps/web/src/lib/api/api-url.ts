/**
 * Runtime resolution of the Forge REST API base URL for the typed client.
 *
 * Next.js inlines `NEXT_PUBLIC_*` at **build** time, and the `web` image takes no
 * `NEXT_PUBLIC_API_URL` build arg (see `deploy/docker/web.Dockerfile`) — so a
 * compose-deployed instance bakes the static default into the client bundle, and
 * the runtime `environment: NEXT_PUBLIC_API_URL` on the `web` service is too late
 * to reach it. Left unresolved, a browser on `https://forge.example.com` would
 * send REST to the baked-in `http://localhost:8000` (wrong host + mixed content).
 *
 * These helpers instead derive the base from `window.location` at **runtime** so
 * REST targets the same origin the page was served from — which the edge routes
 * to the API under `/api` (Caddy/nginx strip the prefix; see
 * `docs/self-hosting/reverse-proxy.md`). This mirrors
 * `apps/web/src/lib/realtime/ws-url.ts` (board/spec WebSocket same-origin
 * derivation); the local-dev exception below is the same invariant expressed for
 * HTTP — kept self-contained here rather than shared, so the freshly-shipped WS
 * helper is not disturbed.
 *
 * Resolution precedence (see {@link resolveApiBaseUrl}):
 *   1. `NEXT_PUBLIC_API_URL` set (build-time inlined) →
 *      - absolute (`https://api.example.com`) → used **verbatim** (byte-identical
 *        to the legacy behavior for anyone who set an absolute value);
 *      - relative (`/api`) → resolved against the page origin in the browser
 *        (`https://<page-host>/api`); against the static default origin in SSR
 *        (so `new URL(path, base)` never throws on a relative base — see 3).
 *   2. Unset + browser → same-origin `/api` (the edge strips `/api` → API),
 *      EXCEPT the local `next dev` server (loopback:3000), which serves no `/api`
 *      route and keeps the legacy `http://localhost:8000` API origin.
 *   3. Unset + SSR / non-browser → the legacy static default
 *      (`http://localhost:8000`). Any server-side fetch inside the container
 *      legitimately reaches the API this way; nothing changes for it.
 */

/**
 * Static default origin (scheme + host) used in SSR / non-browser contexts and on
 * the local Next dev server. Matches `client.ts`'s legacy `http://localhost:8000`
 * default: in dev the API listens on :8000 while the web dev server runs on :3000.
 */
const LEGACY_API_ORIGIN = "http://localhost:8000";

/**
 * Path prefix the edge routes to the API. Caddy/nginx strip `/api` before
 * forwarding to `api:8000` (see `docs/self-hosting/reverse-proxy.md`), so a
 * same-origin `/api` base reaches the API's un-prefixed routes.
 */
const SAME_ORIGIN_API_PREFIX = "/api";

/** Loopback hosts that identify a local origin (mirrors `ws-url.ts`). */
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

/**
 * Port the local Next web server listens on (`next dev` / standalone `next
 * start`, `deploy/docker/web.Dockerfile`, `.env.dev` `WEB_PORT`). The full
 * compose stack, by contrast, fronts the app with Caddy on :80/:443, where
 * same-origin `/api` IS routed to the API — so only the dev web server is the
 * exception below (mirrors `ws-url.ts`).
 */
const DEV_WEB_PORT = "3000";

/**
 * True when the page is served by the local Next web server (loopback host on
 * :3000), whose same-origin has no `/api` route — so we keep the legacy API
 * origin (`http://localhost:8000`) rather than the dead `http://localhost:3000/api`.
 */
function isLocalDevWebServer(hostname: string, port: string): boolean {
  return LOOPBACK_HOSTS.has(hostname) && port === DEV_WEB_PORT;
}

/** True for an absolute URL bearing a scheme (`http://`, `https://`, …). */
function isAbsoluteUrl(value: string): boolean {
  return /^[a-z][a-z0-9+.-]*:\/\//i.test(value);
}

/** Join a relative base path (e.g. `/api`) onto an absolute origin. */
function joinOrigin(origin: string, path: string): string {
  return `${origin}${path.startsWith("/") ? "" : "/"}${path}`;
}

/**
 * Ensure `baseUrl` is absolute before it is handed to `new URL(path, base)`,
 * which throws on a relative base. A relative base (e.g. `/api`, whether derived
 * or from an explicit `new ForgeApiClient({ baseUrl: "/api" })`) is resolved
 * against the live page origin, or the static default origin in SSR — so requests
 * are always built against an absolute base and never throw.
 */
export function toAbsoluteApiBase(baseUrl: string): string {
  if (isAbsoluteUrl(baseUrl)) {
    return baseUrl;
  }
  const origin =
    typeof window === "undefined" ? LEGACY_API_ORIGIN : window.location.origin;
  return joinOrigin(origin, baseUrl);
}

/**
 * Derive the API base URL when no operator override is set:
 *   - Browser: same-origin `/api` (`https://host/api` / `http://host/api`) —
 *     EXCEPT the local Next dev server, which keeps the legacy dev origin.
 *   - SSR / non-browser: the legacy static default (current behavior).
 */
export function sameOriginApiBaseUrl(): string {
  if (typeof window === "undefined") {
    return LEGACY_API_ORIGIN;
  }
  const { protocol, host, hostname, port } = window.location;
  if (isLocalDevWebServer(hostname, port)) {
    return LEGACY_API_ORIGIN;
  }
  return `${protocol}//${host}${SAME_ORIGIN_API_PREFIX}`;
}

/**
 * Resolve the client-side API base URL. `NEXT_PUBLIC_API_URL` (build-time
 * inlined) wins: an absolute value is used verbatim; a relative value (e.g.
 * `/api`) is resolved against the page origin in the browser (or the static
 * default origin in SSR). When unset, the base is derived same-origin (see
 * {@link sameOriginApiBaseUrl}).
 */
export function resolveApiBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_URL;
  if (configured) {
    return toAbsoluteApiBase(configured);
  }
  return sameOriginApiBaseUrl();
}
