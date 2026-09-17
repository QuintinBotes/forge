# Current task — EVALUATION-FINDINGS-2026-09-17 remediation

**Baseline:** `main` @ `24832c7`. Branch: `fix/eval-findings-2026-09-17`.
**Risk:** medium-high (auth surface, compose defaults, public API contract).

## Goal

Close F1–F7 from `EVALUATION-FINDINGS-2026-09-17.md` so a clean self-host can
start, sign in, and drive work through the UI and the API.

## Approved decisions

| # | Decision |
|---|----------|
| F1 | Seed prints a dev admin API key; UI gains an API-key field. Token in `sessionStorage`, opt-in `localStorage` via "remember on this browser". |
| F2 | Repin MinIO to `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z@sha256:14cea493…` (arm64+amd64+ppc64le, verified). |
| F3 | Forward **all nine** `FORGE_*_BACKEND` selectors, defaulted to `db`, in **both** the dev and the production compose. |
| F4 | Docs + `scripts/dev.sh` lead with the Caddy port (8080); web calls the API through Caddy; Offline indicator names the URL it failed to reach. |
| F5 | `/spec/specs/{id}` accepts **either** the uuid or the human key. No change to `SpecManifest`. |
| F6 | `POST /spec/specs` accepts an optional client-supplied `key`; the key regex relaxes to `<PREFIX>-<n>` so auto-numbering stays per-prefix. |
| F7 | Self-hosting guide documents the shared-origin `localStorage` caveat; token storage defaults to `sessionStorage`. |

## Non-goals

- OIDC / real SSO sign-in (still landing; the API key is the bootstrap path).
- An `external_ref` field on `SpecManifest`.
- Changing the `memory` default in `Settings` (unit tests depend on it; the
  compose files are the composition root that selects `db`).

## Acceptance criteria

1. `deploy/scripts/pin-digests.sh --check` passes; no compose ref resolves 401.
2. Both compose files set all nine `FORGE_*_BACKEND` vars on every Python service.
3. `make seed` prints a usable admin API key when `FORGE_SEED_PRINT_API_KEY=1`,
   and that key authenticates `GET /auth/me`; it survives a restart.
4. Pasting the key into the UI authenticates every board screen.
5. `GET /spec/specs/SPEC-1` and `GET /spec/specs/<uuid5>` both return the spec.
6. `POST /spec/specs` with `key: "MOD-893"` creates a spec resolvable at that key.
7. Docs name `http://localhost:8080` as the entry point.

## Commands

```
uv run pytest apps/api/tests packages/spec-engine deploy/tests -q
uv run ruff check . && uv run ruff format --check .
uv run mypy -p forge_api -p forge_spec
pnpm --filter web test
deploy/scripts/pin-digests.sh --check
```

---

## Discovered while verifying (not in the evaluation)

Driving the real UI to check F1 surfaced three further blockers. The first two
are why every list "stayed a skeleton" and the New-task button was "inert" —
the evaluation attributed those to the missing credential, but they are
independent and would have persisted with a valid key.

| # | Severity | Finding |
|---|----------|---------|
| F8 | blocker | The CSP declares no `script-src`, so `default-src 'self'` blocks Next's inline `self.__next_r` bootstrap and **hydration never runs**. Fixed with a per-request nonce (`src/middleware.ts`) rather than `'unsafe-inline'`. |
| F9 | blocker | `ForgeApiClient` stored `globalThis.fetch` unbound and called it as a method, so a browser rejected **every** request with `Illegal invocation`. jsdom does not enforce the receiver, so the suite could not see it. |
| F10 | high | `task.project_id` is a required FK but the create path allows no project, and the seed created no project at all — so with the board on `db` (F3) every task create was a 500. The service now resolves the workspace's default project; the seed creates one; a workspace with none gets a 422, not a 500. |

Also documented: `make dev` does **not** read the root `.env` (it passes
`--env-file deploy/.env.dev`), which is a reliable way to end up with a database
volume whose password no longer matches. Added to getting-started and
troubleshooting.

## Verification performed

- Full Python suite against the source tree: 4169 passed. The 10 failures are
  **identical on `main`** (OTel SDK drift in the ad-hoc venv, plus SLA/sandbox
  tests) — diffed, no regressions.
- Web: 790 passed, `tsc --noEmit` clean, eslint at the 12-warning baseline.
- `ruff check` clean; `ruff format` clean on every file touched; `mypy -p
  forge_api -p forge_spec -p forge_board` clean.
- A real stack (isolated compose project) brought up end to end: MinIO pulled
  from quay.io, all eight services healthy, seed printed a key, that key
  authenticated through Caddy, survived an API restart, and drove the UI
  through connect → board → task list.
