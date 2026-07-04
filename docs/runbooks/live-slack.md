# Runbook — Live Slack integration (HARD-06)

This runbook wires a **real** Slack app against a disposable test workspace and
runs the creds-gated integration lane (`pytest -m live_slack`). It closes the
Slack half of release blocker #1 (the SPEC's **G-SLACK** gate).

> The hermetic default suite (`uv run pytest -q`) needs **none** of this — it is
> network-free and every `live_slack` test skips cleanly when the `SLACK_*`
> credentials are absent. This runbook is only for the operator who wants to
> prove the surface against slack.com and a real signed inbound request.

---

## 0. What gets proved

| AC | Assertion | Lane |
|----|-----------|------|
| AC1 | `notify` posts a real `chat.postMessage`; `ok:true` + non-empty `ts` | `test_live_notify_posts_to_test_channel` |
| AC2 | `notify_approval` posts the Block Kit message with Approve/Reject buttons | `test_live_notify_approval_block_kit` |
| AC11 | A signature computed with the REAL signing secret verifies end-to-end | `test_live_signed_request_verifies` |

The offline ACs (3–10, 12) run anywhere and are green in the default suite:
signature verify/reject, the 501/401/200 inbound routes, the Approve/Reject
round-trip into the `ApprovalStore`, the no-op audit path, the retry/backoff
path, secret redaction, `chat.update`, and frozen-contract conformance.

## 1. Create + install the Slack app

1. Create a Slack app (<https://api.slack.com/apps> → Create New App → From
   scratch) in a **disposable** test workspace.
2. **OAuth & Permissions** → Bot Token Scopes: `chat:write` (post/update
   messages) and `commands` (slash command). Install to the workspace; copy the
   **Bot User OAuth Token** (`xoxb-…`).
3. **Basic Information** → App Credentials → copy the **Signing Secret** (this is
   what Slack signs every inbound request with; it is the only inbound trust
   boundary).
4. Invite the bot to a test channel (`/invite @your-app`) and note the channel
   id (`C…`) or name (`#…`).
5. (Inbound, optional) **Slash Commands** → create `/forge` with the Request URL
   `https://<your-api-host>/integration/slack/commands`. **Interactivity &
   Shortcuts** → turn on, Request URL
   `https://<your-api-host>/integration/slack/interactions`. Slack "Verify"s each
   URL by POSTing a signed request — the route must return 200 to a valid
   signature and 401 to a forged one.

## 2. Configure env (never committed)

```bash
cp .env.integration.example .env.integration    # then fill in the SLACK_* block
```

| Env var | Used by | Meaning |
|---|---|---|
| `SLACK_BOT_TOKEN` | live SDK lane | `xoxb-…` bot token (scopes `chat:write`, `commands`). |
| `SLACK_SIGNING_SECRET` | live SDK lane (AC11) | App signing secret for inbound v0 verification. |
| `SLACK_TEST_CHANNEL` | live SDK lane | Disposable channel the live post lane targets. |
| `FORGE_SLACK_TOKEN` | apps/api Settings | Same bot token, FORGE_-prefixed for the running service. |
| `FORGE_SLACK_SIGNING_SECRET` | apps/api Settings | Same signing secret; **unset ⇒ inbound routes 501**. |
| `FORGE_SLACK_DEFAULT_CHANNEL` | apps/api Settings | Default approval channel (e.g. `#approvals`). |

The SDK live tests read the **native** Slack names; the API service reads the
**FORGE_-prefixed** forms (its Settings carry the `FORGE_` prefix). Set both when
running the app against a real workspace. `.env`/`.env.*` (except `*.example`)
are gitignored (verified).

## 3. Run the live lane

```bash
set -a && source .env.integration && set +a
uv run pytest -m live_slack -q
```

Expected evidence to capture for G-SLACK:

- a real message id (`ts`) printed for the `chat.postMessage` post (AC1);
- a Block Kit approval message with Approve/Reject/Request-changes buttons (AC2);
- the real-secret signature verifying, and a tampered body rejected (AC11).

Each live post is cleaned up best-effort via `chat.delete`.

## 4. Prove the inbound routes against the running API (optional)

With `FORGE_SLACK_SIGNING_SECRET` set and the API deployed, point the Slack app's
slash-command + interactivity Request URLs (step 1.5) at the API and click
**Verify** in the Slack UI, then run `/forge help` and an Approve button:

- `POST /integration/slack/commands` → verifies the v0 signature and returns an
  ephemeral Block Kit help response within Slack's 3-second budget.
- `POST /integration/slack/interactions` → verifies the signature, resolves the
  embedded `approve:{id}` / `reject:{id}` value, and records the decision through
  the same audited `ApprovalStore` the web Approval UI uses (a Slack decision is
  visible in the web UI and vice-versa).

Fail-closed behaviour: unset signing secret ⇒ **501**; forged/stale/missing
signature ⇒ **401**, with no state change.

## 5. Security notes

- The signature (`X-Slack-Signature` v0, HMAC-SHA256 over `v0:{ts}:{body}`) is the
  **only** inbound trust boundary; the routes read the raw body before parsing and
  compare in constant time. A request older than 300 s (replay) is rejected.
- No bot token, signing secret, `Authorization` header, or signature value is ever
  logged, audited, or committed — everything passes the shared `redact_*` filter,
  and `SlackNotifier.__repr__` is secret-safe.
- Outbound retries are bounded (`FORGE_SLACK_MAX_RETRIES`, cap) and honour
  `Retry-After` / `5xx` backoff; a delivery failure degrades gracefully (the gate
  still lives in the API + web UI).

## 6. CI

The `.github/workflows/ci.yml` `slack-integration` job runs this lane **only**
where the `SLACK_BOT_TOKEN` secret is present (protected context). Forks /
secret-less PRs skip the job entirely; the default `python` job stays
network-free (`live_slack` skips without creds).

## 7. PARKED — live verification

This sandbox has **no** `SLACK_*` credentials and no outbound network to
slack.com, so AC1/AC2/AC11 cannot run here (by design). Run, once a real Slack
test workspace + creds exist:

```bash
set -a && source .env.integration && set +a
uv run pytest -m live_slack -q
```

The one-time Slack-app provisioning (create app, scopes `chat:write`+`commands`,
install to a test workspace, point the slash-command + interactivity Request URLs
at the deployed API) is an **operator** step, not an agent step. The hermetic
offline ACs run anywhere and are green in the default suite.
