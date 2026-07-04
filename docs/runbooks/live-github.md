# Runbook — Live GitHub App integration (HARD-01)

This runbook wires the **real** Forge GitHub App against a disposable test
repository and runs the creds-gated integration lane (`pytest -m live_github`).
It closes the GitHub half of release blocker #1 (BETA gate **G-GH**).

> The hermetic default suite (`uv run pytest -q`) needs **none** of this — it is
> network-free and every `live_github` test skips cleanly when the
> `FORGE_GITHUB_*` credentials are absent. This runbook is only for the operator
> who wants to prove the surface against api.github.com.

---

## 0. What gets proved

| AC | Assertion | Lane |
|----|-----------|------|
| AC8 | Installation token mints; `health()` is `healthy:true` with real `latency_ms` | `test_live_token_mint_and_health` |
| AC9 | `create_branch` + `push_files` + `open_pr` succeed; PR `number`/`url` non-null | `test_live_push_open_pr_and_read_reviews` |
| AC10 | `list_reviews` / `list_review_comments` read back (empty is a pass) | same |
| AC12 | Idempotent cleanup — PR closed + branch deleted in teardown | same (`try/finally`) |
| AC13 | Rate-limit surface exercised without erroring | `test_live_rate_limit_handling` |
| AC11 | Webhook HMAC verifies on real delivery bytes; tampered body rejected | `test_real_delivery_signature_verifies` |

## 1. Create + install the GitHub App

1. Create a GitHub App (org → Settings → Developer settings → GitHub Apps → New),
   or use Forge's published App.
2. Permissions (least privilege): **Contents: Read & write**, **Pull requests:
   Read & write**, **Metadata: Read-only**. Subscribe to the **Push**,
   **Check run/suite**, **Status**, and **Workflow run** webhook events.
3. Set a **Webhook secret** (a long random string). Record it.
4. **Generate a private key** → downloads a `.pem`. This file is the only
   long-lived secret; treat it like a password.
5. **Install** the App on a *disposable* test repo (e.g. `your-org/forge-ci-sandbox`)
   and note the **installation id** (the numeric id in the install URL, or via
   `GET /app/installations` with an App JWT).

## 2. Place the secret material (never committed)

```bash
mkdir -p deploy/secrets
cp ~/Downloads/your-app.*.private-key.pem deploy/secrets/github-app.pem
chmod 600 deploy/secrets/github-app.pem
```

`deploy/secrets/` and `*.pem` are both gitignored (verified). The key is read at
call time from this path and used only to sign the in-memory App JWT — it is
never an env var, never logged, never persisted to the DB or an audit row.

## 3. Configure env

```bash
cp .env.integration.example .env.integration    # then fill in the FORGE_GITHUB_* block
```

Required keys (all `FORGE_`-prefixed — the authoritative names; the spec's bare
`GITHUB_*` shorthands map onto these):

| Env var | Meaning |
|---|---|
| `FORGE_GITHUB_APP_ID` | GitHub App id (the JWT `iss`). |
| `FORGE_GITHUB_INSTALLATION_ID` | Installation to mint tokens for. |
| `FORGE_GITHUB_APP_PRIVATE_KEY_PATH` | Path to the `.pem` (default `deploy/secrets/github-app.pem`). |
| `FORGE_GITHUB_WEBHOOK_SECRET` | HMAC secret from step 1.3. |
| `FORGE_GITHUB_TEST_REPO` | `owner/repo` of the disposable test repo. |
| `FORGE_GITHUB_API_URL` | Optional; override for GitHub Enterprise Server. |

## 4. Run the live lane

```bash
set -a && source .env.integration && set +a
uv run pytest -m live_github -q
```

Expected evidence to capture for G-GH:

- token mint + `health()` OK with a real latency;
- a PR number + URL printed for `forge/hardening-smoke-<runid>`;
- the review reads returning (possibly empty) lists;
- the webhook real-delivery assertion passing (or a clear skip if the App has no
  delivery yet — see step 5);
- teardown leaving **no** orphan branch/PR (rerun twice; both green).

## 5. Webhook deliveries

`test_real_delivery_signature_verifies` reads the App's most recent delivery from
`GET /app/hook/deliveries`. If the App has **no** deliveries yet, push any commit
to the test repo (or open/close the smoke PR) to generate one, then re-run.

**Route-shape note (foundation-conforming deviation):** the as-built
`POST /integration/github/webhooks` route (F03) verifies the `X-Hub-Signature-256`
HMAC and then parses Forge's `WebhookEvent` envelope — it is not a raw-GitHub
payload ingress. The live lane therefore proves the **route** fail-closed
behaviour (200 on a valid signature, 401 on a tamper) with a signed envelope, and
proves the **HMAC primitive** on the actual delivered bytes. Raw-payload
ingestion is an F03 concern, out of scope for HARD-01.

## 6. Docker Compose (production wiring)

`deploy/docker-compose.yml` mounts the key **read-only** and passes the
`FORGE_GITHUB_*` env into `api` + `worker`:

```yaml
    environment:
      FORGE_GITHUB_APP_ID: ${FORGE_GITHUB_APP_ID:-}
      FORGE_GITHUB_INSTALLATION_ID: ${FORGE_GITHUB_INSTALLATION_ID:-}
      FORGE_GITHUB_APP_PRIVATE_KEY_PATH: /run/secrets/github-app.pem
      FORGE_GITHUB_WEBHOOK_SECRET: ${FORGE_GITHUB_WEBHOOK_SECRET:-}
    volumes:
      - ./secrets/github-app.pem:/run/secrets/github-app.pem:ro
```

The key is a **file mount**, never an env value. When the vars are unset the
routes fail closed (`501 Not Configured` for writes; webhook stays `401`).

## 7. CI

The `.github/workflows/ci.yml` `integration-github` job runs this lane **only**
where the `FORGE_GITHUB_APP_ID` secret is present (protected context). It writes
the `.pem` at runtime with `add-mask` (no `set -x` around the write) and never
echoes it. Forks / secret-less PRs skip the job entirely; the default `python`
job stays network-free.

## 8. PARKED — live verification

This sandbox has **no** `FORGE_GITHUB_*` credentials and no outbound network to
api.github.com, so AC8–AC13 cannot run here (by design). Run, once real creds
exist:

```bash
set -a && source .env.integration && set +a
uv run pytest -m live_github -q          # SDK + API live lanes
```

The hermetic AC1–AC7 and AC14–AC15 run anywhere and are green in the default
suite.
