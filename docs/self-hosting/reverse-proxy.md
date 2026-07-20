# Reverse proxy: Caddy (default) vs. nginx (alternative)

Forge's edge — the single TLS-terminating entrypoint in front of the API, web
UI, MCP gateway, and the admin-gated Temporal/Grafana UIs — is
[Caddy](https://caddyserver.com/) by default. This guide explains why, what it
routes, and how to swap in the nginx alternative this repo also ships if you'd
rather run a proxy you already operate.

## Caddy (default)

[`deploy/caddy/Caddyfile`](../../deploy/caddy/Caddyfile) is the config the
`caddy` service in [`deploy/docker-compose.yml`](../../deploy/docker-compose.yml)
mounts. It's the default for one reason: **automatic TLS**. Point `DOMAIN` at
a real hostname with a public A/AAAA record and Caddy obtains and renews a
Let's Encrypt certificate with zero extra config; for local trials
(`DOMAIN=localhost`, the default) it serves over Caddy's internal CA instead.
It also reverse-proxies websockets with no special configuration — Caddy
detects the `Upgrade` header on any route automatically.

Nothing else about this guide changes Caddy's behavior. Use it unless you have
a specific reason not to.

## nginx (alternative)

[`deploy/nginx/forge.conf`](../../deploy/nginx/forge.conf) is a route-for-route
equivalent of the Caddyfile, for operators who'd rather run nginx — an
existing nginx-based load balancer/WAF in front, more familiar config syntax,
or an nginx-specific module you need. It is **not** a generic reverse-proxy
template: every `location` in it traces back to a `handle`/`handle_path` block
in the Caddyfile (cited in comments), using the same service:port upstreams
from `docker-compose.yml`.

### Routes

| Path | Caddyfile directive | Upstream | Prefix stripped? |
|---|---|---|---|
| `/api/v1/integrations/alerts/*/webhook` | `handle` | `api:8000` | No (HMAC'd raw body) |
| `/api/v1/integrations/pm/webhooks/*` | `handle` | `api:8000` | No (HMAC'd raw body) |
| `/public/*` | `handle` | `api:8000` | No |
| `/ws`, `/ws/spec/*` | `handle` | `api:8000` | No |
| `/api/*` | `handle_path` | `api:8000` | Yes (`/api` stripped) |
| `/mcp/*` | `handle_path` | `mcp-gateway:8001` | Yes (`/mcp` stripped) |
| `/_temporal*` | `handle` + `basic_auth` | `temporal-ui:8080` | No |
| `/grafana/*` | `handle` + `basic_auth` | `grafana:3000` | No |
| everything else | `handle` (catch-all) | `web:3000` | No |

**`/ws` and `/ws/spec/{spec_id}`** (the board-push and CRDT spec-co-editing
websocket channels, `forge_api.routers.realtime`) mount at the API app **root**,
not under `/api` — `app.include_router(realtime_router)` with no prefix (see
`apps/api/forge_api/main.py`). The web client builds the socket URL from
`NEXT_PUBLIC_WS_URL` (default `ws://localhost:8000/ws`, see
`apps/web/src/lib/realtime/use-board-realtime.ts`): a same-origin **`/ws`** path
with no `/api` prefix and no `window.location` rewrite. So behind either proxy
you set `NEXT_PUBLIC_WS_URL=wss://<your-domain>/ws` (the same-origin analogue of
`NEXT_PUBLIC_API_URL=/api`), and the edge must route that bare `/ws` to the API.
Both proxies now do: the Caddyfile has explicit `handle /ws` + `handle /ws/spec/*`
blocks and `forge.conf` has `location = /ws` + `location /ws/spec/`, each
proxying to `api:8000`. Without them, `/ws` falls through to the catch-all
`web:3000` route — wrong, since neither socket exists on the frontend — and
realtime is silently dead behind the proxy.

### Websocket and streaming requirements

- **Upgrade headers.** `/ws`, `/ws/spec/*`, `/_temporal`, and `/grafana/` all
  set `proxy_http_version 1.1`, `Upgrade`, and `Connection: upgrade` (via the
  standard `map $http_upgrade $connection_upgrade` recipe) — Caddy does this
  automatically on every route (including Grafana Live's `/grafana/api/live/ws`),
  nginx does not, so each location that might carry a websocket needs it
  spelled out explicitly. The remaining locations (`/public/`, the two webhook
  routes, `/api/`, `/mcp/`, the catch-all) also set `proxy_http_version 1.1`
  for consistency, even though none of them carry an upgradeable connection
  today.
- **Timeouts.** nginx's default `proxy_read_timeout`/`proxy_send_timeout` is
  60s, which would kill an idle websocket or a slow-trickling stream; Caddy
  has no such default. `forge.conf` sets both to `3600s` on the websocket and
  `/api/` locations — matching the `sseReadTimeout` this repo's Kubernetes
  ingress already uses for the same reason (`deploy/helm/forge/values.yaml`).
- **Buffering.** nginx buffers proxied responses (and, separately, request
  bodies) by default; Caddy streams both by default. `forge.conf` sets
  `proxy_buffering off` on `/api/` (it carries the NDJSON audit-export stream,
  `forge_api.routers.audit`) and `proxy_request_buffering off` on the two
  webhook routes, where request-body buffering would be actively harmful:
  alert/PM webhook signatures are HMAC'd over the exact raw body, and a
  buffering proxy that rewrites or re-chunks it breaks verification.
- **DNS.** Caddy resolves each upstream hostname lazily, per request, so
  Compose's non-deterministic service start order never matters. A static
  nginx `upstream {}` block resolves once at startup and fails hard
  (`nginx: [emerg] host not found in upstream`) if that name isn't up yet.
  `forge.conf` works around this with `resolver 127.0.0.11` (Docker's embedded
  DNS, present on every container on a user-defined/Compose network) plus a
  `set $upstream_x http://service:port;` + variable `proxy_pass` on every
  location, which defers resolution to request time.

### Known gaps vs. Caddy

- **Request body size cap.** Caddy's `reverse_proxy` imposes no `client_max_body_size`
  equivalent — request bodies of any size pass through uncapped. Stock nginx
  defaults `client_max_body_size` to 1m, which would silently 413 any >1 MB
  webhook/API payload Caddy would have let through. `forge.conf` sets an
  explicit `client_max_body_size 50m;` at the `server` level instead of
  relying on the 1m default; tune it for your actual max payload.
- **TLS is not automatic.** Caddy's ACME integration has no nginx equivalent
  in this file. Run certbot/acme.sh alongside nginx, or terminate TLS
  upstream of it, and uncomment the `listen 443 ssl; ssl_certificate ...;`
  block in `forge.conf`.
- **`zstd` compression.** The Caddyfile does `encode gzip zstd`; stock nginx
  ships gzip only (no zstd module compiled in), so `forge.conf` only does
  gzip.
- **Removing the `Server` header.** Caddy's `-Server` in its `header` block
  strips the header outright. Stock nginx has no directive to do that (it
  needs the third-party `headers-more` module); `server_tokens off` is the
  closest built-in equivalent — it hides the version but the bare `nginx`
  token stays.
- **Basic-auth credentials aren't environment-driven.** Caddy reads
  `TEMPORAL_UI_BASIC_AUTH`/`GRAFANA_BASIC_AUTH` bcrypt hashes from env vars,
  defaulting to a hash that accepts nothing. nginx's `auth_basic_user_file`
  needs a real htpasswd file on disk; `forge.conf` points at
  `/etc/nginx/secrets/{temporal,grafana}.htpasswd`, which don't exist until
  you create them, so both routes fail closed (401 for everyone) the same way
  Caddy's dummy hash does. Generate one with:

  ```bash
  htpasswd -c /etc/nginx/secrets/grafana.htpasswd admin
  ```

- **Network reachability:** the edge proxy reaches `mcp-gateway:8001` for the
  `/mcp/*` route because `docker-compose.yml`'s `mcp-gateway` service is on the
  `edge` network alongside the `caddy` service (and any drop-in nginx
  replacement, which is also on `edge`). It keeps its `backend` + `mcp`
  membership for the in-cluster api/worker paths. If you swap in your own edge
  network topology, make sure the proxy container and `mcp-gateway` still share
  a network, or `/mcp/*` will 502 (the gateway resolves nowhere from the
  proxy's networks).

### Health checks

The edge proxy itself doesn't need a dedicated health-check route: Docker
Compose's `api`/`mcp-gateway`/`web` healthchecks all probe each container
directly (`/health/ready`, `/health`, `/` respectively — see
[docker-compose.md](docker-compose.md)), bypassing the proxy entirely. The
API's own liveness/readiness endpoints (`/health`, `/healthz`, `/readyz`,
`/health/ready`) are mounted at the API app root, same as `/ws`; reachable
through either proxy only via `/api/health` etc. (under the `/api/*` surface),
not bare `/health` — the Caddyfile doesn't expose the bare paths externally
either, so `forge.conf` doesn't add that.

### Switching to it

```yaml
# deploy/docker-compose.yml — replace the `caddy` service's image/volumes
caddy:
  image: nginx:1.27-alpine
  volumes:
    - ./nginx/forge.conf:/etc/nginx/conf.d/forge.conf:ro
    - ./nginx/secrets:/etc/nginx/secrets:ro   # htpasswd files, see above
    # Remove or override the image's own conf.d/default.conf too — it also
    # `listen`s on 80 and will otherwise shadow forge.conf as the default
    # server for that port.
```

Validate the syntax before rolling it out:

```bash
docker run --rm -v "$PWD/deploy/nginx/forge.conf:/etc/nginx/conf.d/forge.conf:ro" nginx:alpine nginx -t
```

This checks config syntax and routing logic only (see `forge.conf`'s header
comment for exactly what was and wasn't exercised) — it does not stand up the
real Forge services, so it can't catch a runtime issue like a proxy and an
upstream landing on different networks. Confirm end-to-end behavior against a
running stack before trusting either proxy in production.
