# F16 — Slack Notifications

> Phase: v1 · Spec module(s): Integration Layer (Slack: task status, approval requests, `/forge` slash commands), Review & Approval Layer (approval gates resolvable from Slack), Human Approval System, Security (signature verification, secrets, audit, RBAC), Core Data Model (per-workspace Slack install + identity mapping) · Status target: **Done** = a workspace admin can install the Forge Slack app via OAuth; Forge posts Block Kit notifications for task-status changes, approval requests, and escalations to routed channels and (for linked users) DMs; a reviewer can Approve / Request changes / Reject an approval directly from an interactive Slack message or via `/forge approve|reject`; `/forge status|tasks|approvals|run|link|help` slash commands work; every inbound Slack request is Slack-signature-verified, deduped, and audited; outbound posts are rendered, retried, and rate-limit-aware; the whole surface is exercised against recorded fixtures with **zero live Slack network calls**. Lint + types + `pytest` green on `packages/integration-sdk` (`forge_integrations/slack`), the `apps/api` Slack router, and the `apps/worker` Slack tasks.

---

## 1. Intent — what & why

Forge is human-in-the-loop by design: spec/plan/PR/deploy approvals and low-confidence escalations are first-class workflow states (F07/F08). Engineers live in Slack, not in a dashboard tab. This slice makes Slack a **first-class notification + action surface** so a reviewer never has to discover that a run is blocked — Slack tells them, and they can unblock it without leaving Slack.

The spec lists three V1 Slack capabilities (Integrations → V1 → Slack): **task status**, **approval requests**, and **`/forge` slash commands**. This slice delivers all three plus the two things that make them safe and usable:

1. **Outbound notifications** — render and deliver Block Kit messages for: task status transitions (board, F01), approval requests + escalations + run failures (workflow/approval events, F07/F08). Routed to per-project/workspace channels and DM'd to assignees/reviewers who have linked their Slack identity.
2. **Inbound interactivity** — Approve / Request changes / Reject buttons on an approval message resolve the underlying `ApprovalRequest` via the canonical `ApprovalService.resolve` (owned by `cross-cutting/F36-human-approval-system`; the `pr` gate's merge hook is F08's) after Slack-signature verification and Slack→Forge identity + server-side RBAC checks; the original message is then updated in place to show the decision.
3. **`/forge` slash commands** — `status`, `tasks`, `approvals`, `approve`, `reject`, `run`, `link`, `help`.
4. **Slack app install (OAuth v2)** — per-workspace bot-token install; token stored in the encrypted secrets vault, never in a plain column.
5. **Identity linking** — map a Slack user to a Forge user so privileged actions (approve/run) carry a real, RBAC-checked principal and the spec's no-self-approval rule still holds.

Why a Slack **app with a bot token** (not an incoming webhook): incoming webhooks are write-only to one channel and cannot host interactive buttons or slash commands. The full Events/Interactivity/Slash + Web API surface is required for approvals-from-Slack — the exact pattern Open SWE uses for its Slack integration (see §11). Why fixtures-only in the build: the overnight/build constraint forbids live external calls, so every Slack interaction goes through a `SlackTransport` Protocol with recorded responses and is fully unit/integration-tested offline; live verification is a post-merge task.

This slice is an **event consumer + notification sink**: it does not own task/workflow/approval state. F36 (approvals), F01 (status/SLA), and F07 (escalation effects) drive it through the two transports in §3.3; F16 decides who to notify where, and posts. When F16 is absent those producers degrade to in-app/email (per F07 §5), so F16 has **no hard producer dependency at runtime** — only the frozen `NotificationEvent` contract (§4) and F36's `ApprovalService`/`Principal` for the action path.

---

## 2. User-facing behavior / journeys

**J1 — Install the Slack app (admin).**
Admin opens Settings → Integrations → Slack, clicks "Add to Slack", and is sent to Slack's OAuth v2 authorize URL (workspace-scoped `state`). After approving scopes, Slack redirects to Forge's OAuth callback; Forge exchanges the code for a bot token, stores the token in the vault, creates one `SlackInstallation` for the workspace, and the settings page shows team name, bot user, granted scopes, and a default-channel picker.

**J2 — Route notifications (admin).**
On the Slack settings page the admin maps notification **categories** to channels: `approvals → #forge-approvals`, `task_status → #forge-activity`, `escalations → #forge-incidents`, `runs → #forge-activity` (per-project overrides optional). Channels are picked from `conversations.list`. Each route can be toggled off.

**J3 — Link my Slack identity (member).**
In Slack a user runs `/forge link`. Forge replies (ephemeral) with a one-time deep link to `${FORGE_PUBLIC_URL}/integrations/slack/link?code=...`; when the signed-in Forge user opens it, Forge writes a `SlackIdentityLink` binding their Slack `user_id` ↔ Forge `user_id`. After linking, privileged Slack actions work; before linking they are refused with an ephemeral "link your account first" prompt.

**J4 — Approval request notification + one-click approve (reviewer).**
A run reaches `awaiting_review`; F08's `create_pr_approval` effect calls F36 `ApprovalService.create(gate_type=pr, …)`, which emits `approval.requested`. F16 posts a Block Kit message to `#forge-approvals` (and DMs linked reviewers) with: task title, repo, acceptance-criteria summary, CI status, confidence, a deep link to the full Review page (F36's `/approvals/{id}` shell), and three buttons — **Approve**, **Request changes**, **Reject**. A linked reviewer clicks **Approve**; Forge verifies the signature, resolves the Slack user → `forge_approval.Principal`, calls F36 `ApprovalService.resolve(approve, principal, …)` (which runs `ApprovalAuthorizer` for RBAC + no-self-approval and the F08 merge hook), and **updates the original message** to "✅ Approved by @alice — merged" (or, when `outcome.completed is False`, "⛔ Approved but not merged: CI is failing" with `outcome.blocking_reasons`). An ephemeral confirmation is sent to the clicker.

**J5 — Task status updates (passive).**
As a task moves `executing → verifying → pr_opened → merged` (or `needs_human_input`, `failed`), F16 posts/updates a compact status message in the `task_status` channel and DMs the assignee if linked. Status messages are **coalesced**: the same task reuses one message (`chat.update`) rather than spamming a new line per transition.

**J6 — Slash command read (any linked or unlinked user).**
`/forge status TASK-123` → ephemeral card with workflow state, assignee, latest run + CI. `/forge tasks mine` → list of the caller's tasks. `/forge approvals` → pending approvals the caller can act on with inline Approve/Reject buttons. Read commands work without linking; action commands (`approve`, `reject`, `run`) require a link.

**J7 — Escalation / failure (passive).**
When the FSM enters `needs_human_input` (confidence < 0.72, retry budget exhausted, or policy conflict), F07 dispatches the `pause_and_notify` / `escalate_to_admin` effect by name; the body F16 registered posts to the `escalations` channel with the reason and a deep link, DM'ing the targeted human (assignee for pause, admins for escalate). A run that reaches `failed` surfaces the same way via its `status_changed` transition.

**J8 — Uninstall (admin or Slack-side).**
Admin clicks "Disconnect" (or Slack sends `app_uninstalled` / `tokens_revoked`); Forge marks the `SlackInstallation` inactive, revokes the stored token reference, stops all posting, and retains audit history. Identity links are retained but dormant.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

All new tables; one Alembic migration `packages/db/migrations/versions/<rev>_slack_notifications.py`. Models under `packages/db/forge_db/models/`. The Slack **bot token and signing secret are NOT stored in these tables** — the bot token lives in the encrypted secrets vault keyed by `bot_token_ref`; the signing secret + client id/secret come from settings/env (see §8).

**`slack_installations`** (`forge_db/models/slack_installation.py`)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK → `workspaces.id` | tenant scope; **UNIQUE** (one Slack workspace per Forge workspace in V1); indexed |
| `slack_team_id` | TEXT | Slack team/workspace id; **UNIQUE** |
| `slack_team_name` | TEXT | |
| `app_id` | TEXT | Slack app id |
| `bot_user_id` | TEXT | the bot's Slack user id (used to ignore self-events) |
| `bot_token_ref` | TEXT | opaque key into the secrets vault; token bytes never stored here |
| `scopes` | TEXT[] | granted OAuth scopes |
| `default_channel_id` | TEXT | nullable; fallback channel |
| `installed_by_user_id` | UUID FK → `users.id` | |
| `active` | BOOLEAN default true | false on uninstall/revoke |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

**`slack_identity_links`** (`forge_db/models/slack_identity_link.py`)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK → `workspaces.id` | indexed |
| `slack_team_id` | TEXT | |
| `slack_user_id` | TEXT | |
| `forge_user_id` | UUID FK → `users.id` | |
| `verified_at` | TIMESTAMPTZ | set when the link deep-link is confirmed |
| `created_at` | TIMESTAMPTZ | |

Constraints: `UNIQUE(slack_team_id, slack_user_id)` (a Slack user maps to ≤1 Forge user) and `UNIQUE(workspace_id, forge_user_id)` (a Forge user has ≤1 Slack identity per workspace).

**`slack_channel_routes`** (`forge_db/models/slack_channel_route.py`)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK → `workspaces.id` | indexed |
| `project_id` | UUID FK → `projects.id` | nullable; NULL = workspace default |
| `category` | ENUM `slack_notification_category` | `approvals` \| `task_status` \| `escalations` \| `runs` |
| `channel_id` | TEXT | Slack channel id |
| `enabled` | BOOLEAN default true | |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

Constraint: `UNIQUE(workspace_id, project_id, category)` (NULLs distinct → one workspace default + per-project overrides). Resolution order at send time: project-specific enabled route → workspace default route → installation `default_channel_id` → drop (logged).

**`slack_message_refs`** (`forge_db/models/slack_message_ref.py`) — message coalescing + send idempotency

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK → `workspaces.id` | indexed |
| `dedup_key` | TEXT | e.g. `approval:{approval_id}` or `task_status:{task_id}`; **UNIQUE with `workspace_id`** |
| `kind` | ENUM `slack_message_kind` | `approval` \| `task_status` \| `escalation` \| `run` |
| `subject_type` | TEXT | `approval_request` \| `task` \| `workflow_run` |
| `subject_id` | UUID | |
| `channel_id` | TEXT | |
| `message_ts` | TEXT | Slack `ts`; lets us `chat.update` |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

`UNIQUE(workspace_id, dedup_key)` makes notification delivery idempotent: a duplicate event finds the existing ref and updates rather than reposts.

**Inbound replay dedup** (Slack retries deliveries) is handled in Redis, not a table: key `slack:seen:{sha256(team_id|event_id|action_ts)}` with 10-minute TTL (Slack retries within ~minutes). The first writer wins; later retries short-circuit to a 200. Full audit of every inbound request goes to the immutable audit log (`cross-cutting/F39-audit-log`), not this dedup cache.

### 3.2 Backend (FastAPI routes + services/packages)

**Router:** `apps/api/forge_api/routers/slack.py` (registered under `/integrations/slack`). The three Slack-callback endpoints are **not bearer-authenticated** — they are authenticated by **Slack request signature** (`X-Slack-Signature` + `X-Slack-Request-Timestamp` over the raw body, §8). The admin/management endpoints are bearer + RBAC.

| Method & path | Purpose | Auth |
|---|---|---|
| `POST /integrations/slack/events` | Slack Events API. Handles `url_verification` (echo `challenge`), `app_uninstalled`, `tokens_revoked`. Verifies signature; dedupes by `event_id`; enqueues async handling; returns `200` fast (≤3s). | Slack signature |
| `POST /integrations/slack/interactivity` | Interactive components (button clicks on approval/status messages). Verifies signature; parses payload; enqueues `handle_slack_interaction`; returns `200` empty immediately and follows up via `response_url` + `chat.update`. | Slack signature |
| `POST /integrations/slack/commands` | Slash command intake (`application/x-www-form-urlencoded`). Verifies signature; parses `SlackCommand`; for fast reads may answer inline, otherwise returns an ephemeral "Working…" ack and delivers the result via `response_url`. | Slack signature |
| `GET /integrations/slack/install-url` | Returns Slack OAuth v2 authorize URL + workspace-scoped `state`. | bearer (admin) |
| `GET /integrations/slack/oauth-callback` | OAuth redirect handler (`code`, `state`). Exchanges code, stores bot token in vault, upserts `SlackInstallation`. | bearer (admin) |
| `GET /integrations/slack/link` | Identity-link confirmation landing (`code`). Binds the signed-in Forge user to the Slack user from the one-time code. | bearer |
| `GET /integrations/slack/installation` | Current install + routes + link status for the workspace. | bearer |
| `PUT /integrations/slack/routes` | Upsert `slack_channel_routes` (category→channel). | bearer (admin) |
| `GET /integrations/slack/channels` | Proxy `conversations.list` for the channel picker. | bearer (admin) |
| `DELETE /integrations/slack/installation` | Soft-disconnect (mark inactive, revoke token ref). | bearer (admin) |

**The 3-second rule** (Slack hard requirement): all three signature-authenticated POSTs do only verify → dedupe → parse → enqueue → return; the real work (DB reads, F08 calls, Slack Web API posts) happens in Celery and is delivered back through `response_url` (valid 30 min, ≤5 uses) or `chat.update`. This is asserted in tests by timing/structure (the handler never awaits a Slack Web API or F08 call).

**Package:** `packages/integration-sdk/forge_integrations/slack/`

| File | Responsibility |
|---|---|
| `signing.py` | `verify_slack_signature(...)` — HMAC-SHA256 over `v0:{ts}:{raw_body}`, constant-time, ±300s timestamp window. |
| `oauth.py` | `SlackOAuth` — `install_url(state, scopes)`, `exchange_code(code)` (`oauth.v2.access`). |
| `transport.py` | `SlackTransport` Protocol; `HttpxSlackTransport` (real); `FixtureSlackTransport` (replays recorded responses keyed by Web API method). |
| `client.py` | `SlackWebClient` implementing the `SlackClient` Protocol (`chat.postMessage`, `chat.update`, `chat.postEphemeral`, `views.open`, `users.info`, `conversations.list`, `respond` via `response_url`). Honors Slack `ok:false`/`429 Retry-After`. |
| `commands.py` | `parse_slash_command(form)`, `SlashCommandRouter` (register/dispatch subcommands). |
| `interactivity.py` | `parse_interaction(payload)`, `interaction_to_intent(...)` → typed action intents. |
| `events.py` | `parse_event_callback(payload)` → typed Events API events. |
| `blocks.py` | Block Kit builders: `render_approval_blocks`, `render_resolved_approval_blocks`, `render_task_status_blocks`, `render_escalation_blocks`, `render_command_card`. |
| `models.py` | thin local DTOs; canonical contracts live in `packages/contracts` (§4). |
| `errors.py` | `SlackSignatureError`, `SlackOAuthError`, `SlackApiError`, `SlackRateLimitError`, `IdentityNotLinkedError`. |

`forge_integrations/github/*` is untouched. `apps/mcp-gateway` is untouched. Email notifications are a separate V1 feature (out of scope, §12).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

**Celery tasks:** `apps/worker/forge_worker/tasks/slack.py` (queue `notifications`).

- `dispatch_notification(event: dict)` — the outbound entrypoint (a Celery task on the `notifications` queue). Consumes a `NotificationEvent` (§4), resolves the target channel via `slack_channel_routes` and target DM users via `slack_identity_links`, renders the right Block Kit blocks, and upserts via `SlackMessageRef`: if a ref exists for `dedup_key`, `chat.update`; else `chat.postMessage` and persist the returned `ts`. Idempotent on `dedup_key`.
- `handle_slack_interaction(payload: dict)` — processes a button click. Re-verifies dedup, parses the action, resolves Slack→Forge identity to a `forge_approval.Principal`, calls F36 `ApprovalService.resolve(ApprovalDecisionRequest(...), principal, workspace_id=...)` for approve/reject/request_changes (authorization — RBAC + no-self-approval — is enforced inside F36's `ApprovalAuthorizer`), then `chat.update`s the source message via `render_resolved_approval_blocks` and posts an ephemeral confirmation (or `outcome.blocking_reasons`) to the clicker. On `IdentityNotLinkedError` or F36 `AuthorizationError` it posts only an ephemeral nudge and makes **no** state change.
- `handle_slash_command(command: dict)` — executes the dispatched subcommand and delivers the result to `response_url`.
- `handle_slack_event(event: dict)` — processes Events API callbacks (`app_uninstalled`/`tokens_revoked` → mark installation inactive).

**How F16 is triggered (two concrete transports, no new bus):**

1. **`notifications` Celery queue.** Producers enqueue a `NotificationEvent` (§4) that `dispatch_notification` consumes. F36's `ApprovalService` synchronous emit on create/resolve fans `approval.requested`/`approval.resolved` onto the queue (F36 §3.3: emit reuses the default Celery app); F01's `board.scan_sla_breaches` beat task hands `sla_breached` off onto the same queue (F01 §3.3). A thin adapter `forge_worker/notifications/subscriber.py` normalizes each producer payload → `NotificationEvent` (category + dedup_key + targets per the §4 mapping) and enqueues `dispatch_notification`.
2. **F07 `EffectRegistry` effect bodies.** F16 registers `pause_and_notify` and `escalate_to_admin` (F07 §3.3 / §5) in `forge_worker/effects/slack_effects.py`; the FSM dispatches them **by name** when a run enters `needs_human_input`. Each builds a `NotificationEvent(category=escalations)` and calls `dispatch_notification`. Approval *creation* for `escalate_to_admin`, where an admin gate is required, is F36's `ApprovalService.create` — F16 only notifies, never creates gates.

`/internal/activity-events` is F01's **ingest** endpoint (other slices push *into* it); F16 does not read from it. The producer → `NotificationCategory` + `dedup_key` mapping is frozen in §4. No LangGraph code in this slice.

**Outbound resilience:** Slack Web API calls go through `SlackWebClient`, which retries on `429` honoring `Retry-After` (Celery `autoretry_for=(SlackRateLimitError,)`, `max_retries=5`, jittered backoff) and treats `ok:false` token/permission errors as terminal (logged + audited, no infinite retry). A `token_revoked`/`account_inactive` response marks the installation inactive.

### 3.4 Frontend / UI (Next.js routes/components, if any)

Settings surface under `apps/web`:

- Route `apps/web/app/(settings)/integrations/slack/page.tsx` — server component showing install status, team name, scopes, and the routes editor.
- Components in `apps/web/components/integrations/slack/`: `SlackConnectButton.tsx` ("Add to Slack" → install-url → redirect), `SlackRoutesTable.tsx` (category → channel select, per-project override rows, enable toggles; channels from `GET /channels`), `SlackLinkStatus.tsx` (shows whether the current user's Slack identity is linked).
- Route `apps/web/app/(settings)/integrations/slack/callback/page.tsx` — thin OAuth landing that posts `code`+`state` to `GET /oauth-callback`, then redirects back to the settings page.
- Route `apps/web/app/(settings)/integrations/slack/link/page.tsx` — identity-link confirmation page (reads `?code=`, calls `GET /link`, shows success/expired).

Approval/task activity itself is surfaced in the **task timeline** (owned by F01) and the **Review page** (owned by F08); this slice only adds the Slack connection/routing/linking screens.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

- `deploy/docker-compose.yml` / `.env.example`: add `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_SIGNING_SECRET`, `SLACK_APP_ID`, `SLACK_DEFAULT_SCOPES` (e.g. `chat:write,commands,users:read,channels:read,im:write`), and reuse `FORGE_PUBLIC_URL` (for OAuth redirect, slash-command deep links, and notification deep links).
- `deploy/caddy/Caddyfile`: the three Slack callback paths (`/integrations/slack/events`, `/interactivity`, `/commands`) must be reverse-proxied to `api` **without body buffering/transformation** (signature is over the exact raw bytes); add a comment asserting no `request_body` rewrite. Caddy passes bodies untouched by default.
- Worker: route the Slack tasks to the existing `notifications` Celery queue (shared with email); no new compose service.
- Helm (V2): N/A — Helm chart is V2.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

**Notification event contract** (`packages/contracts/forge_contracts/notifications.py`) — the producer-agnostic event F16 consumes. F07/F08/F01 emit these (or F16's subscriber derives them from producer events per the mapping table below).

```python
# packages/contracts/forge_contracts/notifications.py
from __future__ import annotations
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field

class NotificationCategory(str, Enum):
    APPROVALS = "approvals"
    TASK_STATUS = "task_status"
    ESCALATIONS = "escalations"
    RUNS = "runs"

class NotificationEvent(BaseModel):
    id: str                              # globally unique; inbound/outbound idempotency
    workspace_id: str
    project_id: str | None = None
    category: NotificationCategory
    kind: str                            # "approval.requested" | "task.status_changed" | ...
    dedup_key: str                       # "approval:{approval_id}" | "task_status:{task_id}" | ...
    subject_type: str                    # "approval_request" | "task" | "workflow_run"
    subject_id: str
    title: str
    summary: str
    deep_link: str                       # ${FORGE_PUBLIC_URL}/...
    actor_user_id: str | None = None     # who caused it (excluded from DM targets)
    target_user_ids: list[str] = Field(default_factory=list)  # Forge user ids to DM if linked
    data: dict = Field(default_factory=dict)  # category-specific (ci_status, confidence, ac_summary, from/to status)
    occurred_at: datetime
```

**Producer → NotificationEvent mapping (frozen).** Two transports feed `dispatch_notification`: the `notifications` Celery queue (producers enqueue) and F07's `EffectRegistry` (F16-registered effect bodies). See §3.3.

| Producer (real mechanism) | `category` | `kind` | `dedup_key` | DM targets |
|---|---|---|---|---|
| F36 `ApprovalService` emit on create (pr gate) | `approvals` | `approval.requested` | `approval:{approval_id}` | reviewers + assignee |
| F36 `ApprovalService` emit on resolve | `approvals` | `approval.resolved` | `approval:{approval_id}` | requester |
| F07 `escalate_to_admin` effect body (F16-registered) | `escalations` | `workflow.escalate_to_admin` | `escalation:{run_id}` | admins, assignee |
| F07 `pause_and_notify` effect body (F16-registered) | `escalations` | `workflow.pause_and_notify` | `escalation:{run_id}` | assignee |
| F01 `board.scan_sla_breaches` handoff (`notifications` queue) | `escalations` | `task.sla_breached` | `sla:{task_id}` | assignee |
| F01/F07 `status_changed` activity event (incl. `failed`) | `task_status` | `task.status_changed` | `task_status:{task_id}` | assignee |
| F08 PR opened (`pr_event` / `status_changed=pr_opened`) | `runs` | `pr.opened` | `run:{run_id}` | assignee |

**Slack DTOs + Protocols** (`packages/contracts/forge_contracts/slack.py`):

```python
from __future__ import annotations
from typing import Any, Literal, Protocol, runtime_checkable
from pydantic import BaseModel, Field

# ---------- inbound ----------
class SlackCommand(BaseModel):
    team_id: str
    channel_id: str
    user_id: str                         # Slack user id
    command: str                         # "/forge"
    text: str                            # raw args after the command
    response_url: str
    trigger_id: str

class SlackInteraction(BaseModel):
    type: str                            # "block_actions" | "view_submission" | ...
    team_id: str
    user_id: str                         # Slack user id who clicked
    action_id: str                       # "approval.approve" | "approval.reject" | "approval.request_changes"
    value: str                           # carries the approval_id
    response_url: str | None = None
    trigger_id: str | None = None
    channel_id: str | None = None
    message_ts: str | None = None

class SlackEventCallback(BaseModel):
    team_id: str
    event_id: str
    type: str                            # "app_uninstalled" | "tokens_revoked" | ...
    event: dict = Field(default_factory=dict)

# ---------- outbound ----------
class SlackMessage(BaseModel):
    channel: str
    text: str                            # fallback/notification text (accessibility + push)
    blocks: list[dict] = Field(default_factory=list)
    thread_ts: str | None = None

class PostedMessage(BaseModel):
    channel: str
    ts: str

class SlackCommandResponse(BaseModel):
    response_type: Literal["ephemeral", "in_channel"] = "ephemeral"
    text: str
    blocks: list[dict] = Field(default_factory=list)

class SlackUser(BaseModel):
    id: str
    name: str
    real_name: str | None = None
    email: str | None = None             # only if users:read.email granted

class SlackChannel(BaseModel):
    id: str
    name: str
    is_private: bool = False

class SlackOAuthResult(BaseModel):
    team_id: str
    team_name: str
    app_id: str
    bot_user_id: str
    bot_token: str                       # passed straight to the vault; never persisted to a column
    scopes: list[str]

class SlackApiResponse(BaseModel):
    ok: bool
    status_code: int
    body: dict = Field(default_factory=dict)
    retry_after: int | None = None       # from 429 Retry-After

# ---------- Protocols ----------
@runtime_checkable
class SlackTransport(Protocol):
    async def call(self, method: str, *, token: str | None = None,
                   json: dict | None = None,
                   form: dict | None = None) -> SlackApiResponse: ...

@runtime_checkable
class SlackClient(Protocol):
    async def post_message(self, msg: SlackMessage, *, token: str) -> PostedMessage: ...
    async def update_message(self, *, token: str, channel: str, ts: str,
                             text: str, blocks: list[dict]) -> PostedMessage: ...
    async def post_ephemeral(self, *, token: str, channel: str, user: str,
                             text: str, blocks: list[dict] | None = None) -> None: ...
    async def open_view(self, *, token: str, trigger_id: str, view: dict) -> None: ...
    async def get_user_info(self, *, token: str, slack_user_id: str) -> SlackUser: ...
    async def list_conversations(self, *, token: str) -> list[SlackChannel]: ...
    async def respond(self, response_url: str, response: SlackCommandResponse) -> None: ...
```

**Signing + OAuth (free functions / classes in `forge_integrations.slack`):**

```python
def verify_slack_signature(
    signing_secret: str, timestamp: str | None, raw_body: bytes,
    signature: str | None, *, max_skew_s: int = 300,
    now: Callable[[], int] = ...,
) -> bool:
    """basestring = f'v0:{timestamp}:{raw_body.decode()}'; HMAC-SHA256 hex compared
    constant-time to 'v0=<hex>'. False if header/timestamp missing or |now-ts| > max_skew_s."""

def parse_slash_command(form: Mapping[str, str]) -> SlackCommand: ...
def parse_interaction(payload: dict) -> SlackInteraction: ...
def parse_event_callback(payload: dict) -> SlackEventCallback: ...

class SlackOAuth:
    def __init__(self, client_id: str, client_secret: str,
                 transport: SlackTransport, redirect_uri: str) -> None: ...
    def install_url(self, state: str, scopes: list[str]) -> str: ...
    async def exchange_code(self, code: str) -> SlackOAuthResult: ...
```

**Slash-command dispatcher:**

```python
CommandHandler = Callable[[SlackCommand, "CommandContext"], Awaitable[SlackCommandResponse]]

class SlashCommandRouter:
    def register(self, name: str, handler: CommandHandler) -> None: ...
    async def dispatch(self, cmd: SlackCommand, ctx: "CommandContext") -> SlackCommandResponse: ...
    # unknown subcommand -> help; "" -> help
```

**`/forge` subcommand grammar (frozen):**

| Invocation | Action | Requires identity link |
|---|---|---|
| `/forge help` | usage card | no |
| `/forge link` | reply with one-time link deep-link | no |
| `/forge status <TASK-ID>` | task + workflow + latest run/CI | no |
| `/forge tasks [mine] [project:<KEY>] [status:<s>]` | list tasks (ephemeral) | no |
| `/forge approvals` | pending approvals the caller may act on, with buttons | no (read); buttons require link |
| `/forge approve <APPROVAL-ID> [note...]` | resolve approval = approve | **yes** |
| `/forge reject <APPROVAL-ID> [note...]` | resolve approval = reject | **yes** |
| `/forge run <TASK-ID>` | start a run for a `task_ready` task | **yes** |

**Approval surface consumed (frozen by `cross-cutting/F36-human-approval-system` §4, not redefined here):**

```python
# packages/approval-sdk/forge_approval/service.py  (F36 — the ONE canonical approval service)
class ApprovalService:
    async def resolve(self, approval_id: UUID, decision: ApprovalDecisionRequest,
                      actor: Principal, *, workspace_id: UUID) -> ApprovalResolution: ...
    async def get(self, approval_id: UUID, *, workspace_id: UUID) -> ApprovalRequest: ...
    async def list(self, *, workspace_id: UUID, actor: Principal,
                   status: GateStatus | None = None, gate_type: GateType | None = None,
                   project_id: UUID | None = None, mine: bool = False) -> list[ApprovalSummary]: ...

# forge_approval.models (F36) — the Slack action path constructs the request + principal and reads the resolution:
#   ApprovalDecisionRequest(decision: ApprovalAction, note: str | None)
#       ApprovalAction ∈ {"approve","reject","request_changes","escalate"}
#   Principal(kind="user", id=<forge_user_id: UUID>, role: Role, workspace_id: UUID)
#   ApprovalResolution(approval_id: UUID, status: GateStatus, outcome: ResolutionOutcome)
#   ResolutionOutcome(completed: bool, blocking_reasons: list[str], follow_up_state: str | None, details: dict)
# For the pr gate, F08's merge result folds into outcome.details / outcome.blocking_reasons; outcome.completed == False
# means "approved but not merged yet" (e.g. CI still red) — Slack renders blocking_reasons; no merge/state is faked.
```

**Identity resolution helper (this slice) — returns F36's `Principal`, not a Slack-local type:**

```python
from forge_approval.models import Principal, Role   # F36

class SlackIdentityResolver:
    def __init__(self, repo: "SlackRepository") -> None: ...
    async def resolve(self, *, slack_team_id: str, slack_user_id: str) -> Principal:
        """Raises IdentityNotLinkedError if there is no *verified* SlackIdentityLink for
        (slack_team_id, slack_user_id). Returns a forge_approval.Principal
        (kind='user', id=<forge_user_id>, role=<workspace role>, workspace_id=<ws>) — the SAME
        principal type F36's ApprovalService.resolve(...) authorizes, so a Slack reviewer carries
        no privilege the web reviewer doesn't and the RBAC / no-self-approval checks are identical."""
```

---

## 5. Dependencies — features/slices that must exist first

**Hard prerequisites (must exist before this slice builds):**
- `cross-cutting/C01-monorepo-and-api-foundations` — `uv` workspaces, `apps/api` FastAPI skeleton with the `/integrations/slack` router stub pre-registered, `apps/worker` Celery app + Redis broker, `packages/db` baseline (`forge_db`) + Alembic env, `packages/contracts` (`forge_contracts`) module, `deploy/docker-compose.yml` + `.env.example`. (F16 declares the `notifications` Celery queue and routes its tasks there, §3.5.)
- `cross-cutting/F37-auth-secrets-byok` (REQUIRED) — `workspaces`/`users` tables; the `Principal` (`workspace_id`, `user_id`, `role ∈ {admin, member, viewer, agent-runner}`) and `require_role(min_role)` RBAC dependency (`forge_api/deps/auth.py::get_principal`); the internal/service-token path the worker uses for `/internal/activity-events`; the **AES-256-GCM envelope-encrypted secrets vault** (per-workspace key isolation) that stores the Slack bot token under `bot_token_ref`; and the canonical `SecretRedactor`. (Sibling slices also call this `C02-auth-and-rbac` / `F15-auth-secrets-rbac`.)
- `cross-cutting/F39-audit-log` (REQUIRED) — the canonical `AuditEvent` DTO + `AuditSink` Protocol (in `packages/contracts`) and the `SqlAuditWriter` invoked for every accepted inbound Slack request (command/interaction/event) and every outbound Slack Web API call (method, payload hash, result status, latency, redacted); plus the reusable `attach_immutability_trigger(table)` helper.

**Hard for the *action* path (Approve from Slack), soft for *notifications*:**
- `cross-cutting/F36-human-approval-system` — owns the **one canonical** approval primitive in `packages/approval-sdk` (`forge_approval`): `ApprovalService` (`create`/`resolve`/`list`/`get`), `ApprovalRequest`, `ApprovalDecisionRequest`, `ApprovalResolution`/`ResolutionOutcome`, `Principal`, and the `ApprovalAuthorizer` (server-side RBAC + no-self-approval). Slack buttons / `/forge approve` call the **same** `ApprovalService.resolve(...)`; F36 emits `approval.requested`/`approval.resolved` (the events F16 maps to the `approvals` category). F36 explicitly names F16 as a downstream consumer (F36 §5).
- `v1/F08-plan-execute-verify-pr-approval` — owns the **`pr` gate's `GateResolutionHook`** (the merge gate: review-approved AND CI-green AND spec-validated → `GitHubIntegration.merge_pr`), surfaced to Slack through F36's `ApprovalResolution.outcome` (`completed` + `blocking_reasons` + `details`). Notifications can ship before F08 lands (task-status routing only); the interactive-approve→merge path requires both F36 and F08.

**Soft (producers — need NOT exist first; F16 consumes frozen contracts and has nothing to post until they emit):**
- `v1/F01-project-board` — owns the `activity_events` table (`status_changed`, `sla_breached`) and the `POST /internal/activity-events` ingest; F01's `board.scan_sla_breaches` beat task hands SLA breaches to F16 on the `notifications` queue (F01 §3.3). Deep-link target (task timeline); provides `projects`/`tasks` for `/forge status|tasks`.
- `v1/F07-feature-workflow-fsm` — dispatches the `pause_and_notify` and `escalate_to_admin` effects **by name** into its worker-side `EffectRegistry`; F16 registers those effect bodies (F07 §3.3 / §5) and F07 degrades to in-app/email when F16 lags. F07 also drives the `status_changed` transitions consumed for `task_status`.
- `v1/F03-github-app` — supplies CI status shown in approval/status cards (read from F08's `pull_request` row; F16 does not call GitHub).

---

## 6. Acceptance criteria (numbered, testable)

1. **Signature verification.** All three Slack callback endpoints return `401` when `X-Slack-Signature`/`X-Slack-Request-Timestamp` are missing, malformed, computed over a tampered body, or when the timestamp is outside ±300s; they return `200` only when the HMAC over `v0:{ts}:{raw_body}` matches; comparison is constant-time (`hmac.compare_digest`). The handler reads the **raw body** before any form/JSON parsing.
2. **Events url_verification.** A signed `url_verification` POST returns `200` whose body echoes the exact `challenge` value and enqueues no work.
3. **3-second discipline.** The command/interactivity/events handlers verify+enqueue+return without awaiting any Slack Web API call or `ApprovalService` call (asserted by spying that no transport/F08 call occurs during the request handler; the work happens in the enqueued task).
4. **Inbound dedup.** Two deliveries with the same `event_id` (events) or identical interaction signature within the TTL each return `200` but enqueue the downstream task exactly once (Redis replay cache).
5. **OAuth install.** `GET /oauth-callback` with a valid `code`+`state` calls `oauth.v2.access` once, stores the bot token in the vault under `bot_token_ref` (never in a column), and upserts exactly one `SlackInstallation` per workspace with `slack_team_id`, `bot_user_id`, `scopes`, `active=true`; re-running is idempotent (no duplicate rows; uniqueness on `workspace_id` and `slack_team_id`).
6. **Identity link.** `/forge link` returns an ephemeral one-time deep link; opening it as a signed-in Forge user creates a verified `SlackIdentityLink` (`slack_user_id ↔ forge_user_id`); the link code is single-use and expires (≤15 min); duplicate links are prevented by the unique constraints.
7. **Channel routing resolution.** `dispatch_notification` resolves the channel in the order project-route → workspace-route → installation default → drop-and-log; a disabled route is skipped; the resolved channel is asserted per category.
8. **Approval notification render.** On `approval.requested`, a Block Kit message is posted to the `approvals` channel containing the task title, deep link, CI status, confidence, and exactly three buttons with `action_id`s `approval.approve` / `approval.request_changes` / `approval.reject`, each carrying the `approval_id` in `value`; the message `ts` is persisted to `slack_message_refs` keyed by `approval:{approval_id}`.
9. **Notification idempotency / coalescing.** A second event with the same `dedup_key` calls `chat.update` on the stored `ts` (not `chat.postMessage`); task-status transitions for one task reuse a single message.
10. **Interactive approve → resolve → update.** Clicking **Approve** as a linked reviewer with `member`/`admin` role: verifies signature, resolves Slack→Forge identity to a `forge_approval.Principal`, calls F36 `ApprovalService.resolve(ApprovalDecisionRequest(decision=approve), principal, workspace_id=…)` exactly once, then `chat.update`s the original message to a resolved state (no buttons) and posts an ephemeral confirmation. When `ApprovalResolution.outcome.completed is False` (e.g. CI still red), the update/ephemeral renders `outcome.blocking_reasons`, the gate is still recorded `approved`, and **no** merge or workflow state is faked — matching F36/F08 semantics.
11. **No self-approval / RBAC from Slack (server-side).** A click / `/forge approve` by an unlinked user yields an ephemeral "link your account first" prompt and **no** state change (`IdentityNotLinkedError`, raised *before* any `ApprovalService` call). A linked `viewer` or `agent-runner`-role user, or a user attempting to resolve a gate for a run they themselves requested (when `forbid_self_approval`), gets an ephemeral "not permitted" and no state change — because F36's `ApprovalAuthorizer.check(...)` raises `AuthorizationError`, which F16 re-renders as an ephemeral. F16 never re-implements or bypasses the policy; Slack is just another client of the same server-side gate (the spec's "human approval required before merge — always").
12. **Slash read commands.** `/forge status <TASK-ID>` returns an ephemeral card with workflow state + assignee + latest run/CI; `/forge tasks mine` returns the caller's tasks; `/forge approvals` lists actionable pending approvals with buttons; unknown/empty subcommand returns the help card. Results are delivered via `response_url` after the fast ack.
13. **Slash action commands.** `/forge approve <ID>` and `/forge reject <ID>` by a linked, permitted user resolve the approval (same path as buttons); `/forge run <TASK-ID>` starts a run only when the task is `task_ready` and the user is permitted, else returns an explanatory ephemeral message.
14. **Rate-limit handling.** A `429` from Slack with `Retry-After: N` causes exactly one scheduled retry after ≥N seconds (no tight loop); a `token_revoked`/`account_inactive` response marks the `SlackInstallation` inactive and stops further posting.
15. **Uninstall.** `app_uninstalled`/`tokens_revoked` events (and `DELETE /installation`) set `active=false` and prevent subsequent posting; identity links are retained but dormant.
16. **Secret redaction.** The bot token, signing secret, client secret, and OAuth `code` never appear in any audit entry, structured log, or API response (asserted by serializing an installation and capturing logs across a full install → notify → approve flow).
17. **Audit completeness.** Every accepted inbound Slack request and every outbound Slack Web API call produces exactly one audit entry with the method/command/action, `payload_hash`, result status, and latency.
18. **Offline guarantee.** The full suite passes with `HttpxSlackTransport` unimported at runtime; all Slack interactions go through `FixtureSlackTransport`; CI asserts no sockets are opened during the Slack integration-sdk tests (`pytest-socket`).

### 3.x note
All §3 sub-sections are present; none are N/A.

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; implement to green. Roots: `packages/integration-sdk/tests/slack/`, `apps/api/tests/integration/`, `apps/worker/tests/`, `apps/web/__tests__/`.

**Unit — `signing.py`**
- `test_verify_signature_valid/invalid/missing/tampered` — parametrized; tampered body flips one byte.
- `test_verify_signature_timestamp_skew` — ts older/newer than ±300s → False (injected `now`).
- `test_verify_signature_constant_time` — asserts use of `hmac.compare_digest` (implementation check, not timing).

**Unit — `oauth.py`**
- `test_install_url_contains_scopes_and_state`.
- `test_exchange_code_returns_oauth_result` — `FixtureSlackTransport` replays `oauth.v2.access`; asserts `team_id`, `bot_user_id`, `bot_token`, `scopes`.
- `test_exchange_code_error_raises` — `ok:false` → `SlackOAuthError`.

**Unit — `commands.py`**
- `test_parse_slash_command_fields` — form → `SlackCommand`.
- `test_router_dispatch_subcommands` — parametrized over the §4 grammar table → correct handler / help fallback.
- `test_action_commands_require_link` — `approve`/`reject`/`run` with unlinked user → ephemeral nudge, handler not invoked.

**Unit — `interactivity.py` / `events.py`**
- `test_parse_interaction_button` — `block_actions` payload → `SlackInteraction` with `action_id` + `value`.
- `test_parse_event_callback_uninstall` — `app_uninstalled` parsed; `url_verification` returns challenge path.

**Unit — `blocks.py`**
- `test_render_approval_blocks_has_three_buttons` — asserts the three `action_id`s + `approval_id` in `value` + deep link + fallback `text`; snapshot.
- `test_render_resolved_approval_blocks_no_buttons` — resolved render drops buttons, shows decider + outcome.
- `test_render_task_status_blocks`, `test_render_escalation_blocks`, `test_render_command_card`.
- `test_no_secrets_in_any_rendered_block`.

**Unit — `client.py` (FixtureSlackTransport)**
- `test_post_message_returns_ts`, `test_update_message`, `test_post_ephemeral`, `test_list_conversations_maps_channels`, `test_get_user_info`.
- `test_429_raises_rate_limit_with_retry_after`.
- `test_ok_false_token_revoked_raises_terminal`.

**Integration — `apps/api` (Postgres testcontainer + ASGI client)**
- `test_events_url_verification_echoes_challenge` (AC#2).
- `test_command_endpoint_signed_acks_fast_and_enqueues` (AC#1, #3) — Celery eager; asserts ephemeral ack + task enqueued.
- `test_command_endpoint_unsigned_401`, `test_interactivity_unsigned_401` (AC#1).
- `test_inbound_dedup_event_enqueues_once` (AC#4).
- `test_oauth_callback_creates_installation_idempotent` (AC#5) — token written to a fake vault, not a column.
- `test_link_flow_creates_identity_link_single_use` (AC#6).
- `test_routes_upsert_and_resolution_order` (AC#7).
- `test_rbac_only_admin_can_set_routes_or_disconnect`.

**Integration — `apps/worker`**
- `test_dispatch_approval_posts_and_persists_ref` (AC#8) — first event posts; ref stored.
- `test_dispatch_same_dedup_key_updates_message` (AC#9) — second event → `chat.update`.
- `test_escalate_to_admin_effect_posts_escalation` / `test_pause_and_notify_effect_posts` — the F07-registered effect bodies build a `NotificationEvent(category=escalations)` and post to the escalations channel + DM admins/assignee (no gate is created by F16).
- `test_handle_interaction_approve_calls_resolve_and_updates` (AC#10) — fake F36 `ApprovalService.resolve` returns `ApprovalResolution(status=approved, outcome.completed=True)`; resolve called exactly once with a `forge_approval.Principal`; message updated (no buttons), ephemeral sent.
- `test_handle_interaction_blocked_shows_reasons` (AC#10) — resolve returns `outcome.completed=False` with `blocking_reasons`; message/ephemeral render the reasons; no merge/state faked.
- `test_unlinked_user_gets_nudge_no_resolve` (AC#11) — `IdentityNotLinkedError`; `ApprovalService.resolve` never called.
- `test_authorizer_denial_posts_ephemeral_no_state_change` (AC#11) — parametrized over `viewer` / `agent-runner` / self-approval; F36 `ApprovalAuthorizer` raises `AuthorizationError`; ephemeral "not permitted"; no state change.
- `test_slash_status_renders_card`, `test_slash_approve_resolves` (AC#12, #13).
- `test_429_schedules_single_retry` (AC#14) — assert countdown ≥ Retry-After.
- `test_token_revoked_marks_installation_inactive` (AC#14, #15).
- `test_uninstall_event_marks_inactive_and_stops_posting` (AC#15).

**Frontend — `apps/web/__tests__/slack-settings.test.tsx`**
- `test_connect_button_redirects_to_install_url`.
- `test_routes_table_saves_category_channel`.
- `test_link_status_reflects_linked_state`.

**Key fixtures**
- `tests/fixtures/slack/api/*.json`: `oauth.v2.access.json`, `chat.postMessage.json`, `chat.update.json`, `users.info.json`, `conversations.list.json`, `error.token_revoked.json`, `error.ratelimited.json` (+ `Retry-After`).
- `tests/fixtures/slack/inbound/*`: `slash_command.urlencoded`, `interaction.block_actions.approve.json`, `interaction.block_actions.reject.json`, `event.url_verification.json`, `event.app_uninstalled.json`.
- `FixtureSlackTransport(records: dict[str, SlackApiResponse], call_log: list)` — keyed by Web API method; unexpected method fails loudly.
- `sign_slack_request(body: bytes, secret: str, ts: int) -> dict` helper — produces `X-Slack-Signature` + `X-Slack-Request-Timestamp` headers.
- `fake_approval_service` — F36 `ApprovalService` double; records `resolve(...)` calls + the `forge_approval.Principal` passed; settable `ApprovalResolution` (`outcome.completed` True/False + `blocking_reasons`); paired `fake_authorizer` that can raise `AuthorizationError`.
- `fake_vault` — in-memory secrets store asserting the token is never returned in API/log output.
- `clock` fixture — injectable `now()` for signature-skew + link-expiry tests.
- `seeded_installation` / `seeded_identity_link` factories.

---

## 8. Security & policy considerations

- **Request authenticity (raw body):** every inbound Slack request is verified with the Slack signing secret over `v0:{ts}:{raw_body}` using `hmac.compare_digest`; the timestamp window is ±300s to defeat replay; the endpoint reads `Request.body()` bytes **before** any form/JSON parse (FastAPI re-parsing would change bytes and break the HMAC). Caddy must not buffer/transform these paths. A per-installation rate limit and a max body size (e.g. 1 MB) are enforced.
- **Secret storage & redaction:** the Slack **bot token** lives only in the encrypted secrets vault (referenced by `bot_token_ref`); the **signing secret / client id / client secret** come from env/vault, never a DB column. A redaction filter strips `token`, `Authorization`, `signing_secret`, `client_secret`, `code`, and `xoxb-*`-shaped values from all logs/traces/audit. OAuth `code` and `state` are single-use and short-lived.
- **Identity binding before privilege:** no Slack action mutates Forge state without a **verified** `SlackIdentityLink` resolving the Slack user to a Forge user; the resolved `forge_approval.Principal` (with its workspace role) is what F36's `ApprovalService`/`ApprovalAuthorizer` authorizes. This preserves RBAC (`viewer`/`agent-runner` cannot approve) and the spec's **no-self-approval** rule (a user cannot resolve a gate for a run they requested, and the agent that produced a run can never resolve it) — Slack is just another client of the same server-side gate, never a bypass.
- **Least-privilege scopes:** request only `chat:write`, `commands`, `users:read`, `channels:read` (and `im:write` for DMs); do not request message-history or admin scopes. The bot token is bound to one team (`slack_team_id`) and used only for that installation's workspace (analogous to the RFC 8707 token-binding discipline the spec mandates for MCP).
- **Action authorization is server-side:** button `value`/`action_id` are treated as **untrusted input** — the approval id is re-validated against the workspace, the approval's current status, and the resolver's permission; a forged/expired button cannot resolve an approval it shouldn't.
- **Audit immutability:** every accepted inbound request (command/interaction/event) and every outbound Web API call is appended to the immutable audit log (`cross-cutting/F39-audit-log`, via its `AuditSink`/`SqlAuditWriter`) with a redacted payload hash, actor (resolved Forge user or `slack:{user_id}` when unlinked), result, and latency.
- **No outward calls in build/CI:** all Slack interactions run against `FixtureSlackTransport`; CI asserts no real sockets (`pytest-socket`). Live verification is a post-merge task.
- **Tenant isolation:** all Slack rows are workspace-scoped; an interaction/command for an inactive or non-existent installation is rejected (no cross-workspace leakage); deep links resolve only within the caller's workspace.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** — spans a package (signing + OAuth + transport + client + commands + interactivity + events + Block Kit), three signature-authenticated API endpoints plus OAuth/link/routes admin endpoints, Celery tasks + a notifications-queue subscriber + two F07-registered effect bodies, a settings UI with routing + linking, fixtures, and a migration with four tables.

Key risks:
- **3-second ack window** — any synchronous Web API/F08 call in the request path causes Slack timeouts and user-visible failures. *Mitigation:* strict verify→enqueue→return handlers; all real work async via `response_url`/`chat.update`; structural test (AC#3).
- **Raw-body signature integrity** — trivially broken if any middleware/proxy reparses or re-encodes the body. *Mitigation:* read `Request.body()` first, Caddyfile passthrough assertion, tampered-body + skew tests.
- **Identity-spoofing / privilege bypass** — Slack `user_id` and button `value` are attacker-influenceable. *Mitigation:* signature verification + mandatory verified identity link + server-side re-validation through F08's gate (no client-trusted authorization).
- **Slack rate limits (Tier-based, 429)** — bursts of status updates can throttle. *Mitigation:* message coalescing via `slack_message_refs` (`chat.update` not repost), `Retry-After`-honoring single retry, dedicated `notifications` queue.
- **Producer-event drift** — F36/F07/F08/F01 event shapes are not all final. *Mitigation:* depend only on the frozen `NotificationEvent` + mapping table in §4 and on F36's `ApprovalService`/`Principal` contract; the subscriber + the two effect bodies are the single adapters; degrade gracefully when a producer is absent.
- **Fixture drift from the live Slack API** — fixtures are recorded, not live. *Mitigation:* capture real payloads into the fixtures dir; flag a post-merge live-verification task (out of overnight scope).

---

## 10. Key files / paths (exact)

```
packages/contracts/forge_contracts/notifications.py            # NotificationEvent + NotificationCategory + mapping
packages/contracts/forge_contracts/slack.py                    # Slack DTOs + SlackTransport/SlackClient Protocols

packages/db/forge_db/models/slack_installation.py              # new
packages/db/forge_db/models/slack_identity_link.py             # new
packages/db/forge_db/models/slack_channel_route.py             # new
packages/db/forge_db/models/slack_message_ref.py               # new
packages/db/migrations/versions/<rev>_slack_notifications.py   # new migration (4 tables + 2 enums)

packages/integration-sdk/forge_integrations/slack/__init__.py
packages/integration-sdk/forge_integrations/slack/signing.py
packages/integration-sdk/forge_integrations/slack/oauth.py
packages/integration-sdk/forge_integrations/slack/transport.py
packages/integration-sdk/forge_integrations/slack/client.py
packages/integration-sdk/forge_integrations/slack/commands.py
packages/integration-sdk/forge_integrations/slack/interactivity.py
packages/integration-sdk/forge_integrations/slack/events.py
packages/integration-sdk/forge_integrations/slack/blocks.py
packages/integration-sdk/forge_integrations/slack/models.py
packages/integration-sdk/forge_integrations/slack/errors.py
packages/integration-sdk/tests/slack/test_signing.py
packages/integration-sdk/tests/slack/test_oauth.py
packages/integration-sdk/tests/slack/test_commands.py
packages/integration-sdk/tests/slack/test_interactivity.py
packages/integration-sdk/tests/slack/test_blocks.py
packages/integration-sdk/tests/slack/test_client.py
packages/integration-sdk/tests/slack/fixtures/slack/api/*.json
packages/integration-sdk/tests/slack/fixtures/slack/inbound/*

apps/api/forge_api/routers/slack.py                            # real handlers (Phase-0 stub)
apps/api/forge_api/services/slack_service.py                   # install/route/link orchestration
apps/api/forge_api/settings.py                                 # add SLACK_* settings
apps/api/tests/integration/test_slack_endpoints.py
apps/api/tests/integration/test_slack_oauth_link.py

apps/worker/forge_worker/tasks/slack.py                        # dispatch_notification, handle_slack_interaction, handle_slash_command, handle_slack_event
apps/worker/forge_worker/notifications/subscriber.py           # producer payload (F36 emit / F01 SLA handoff) -> NotificationEvent -> dispatch_notification (notifications queue)
apps/worker/forge_worker/effects/slack_effects.py              # pause_and_notify / escalate_to_admin effect bodies registered into F07's EffectRegistry
apps/worker/tests/test_slack_tasks.py

apps/web/app/(settings)/integrations/slack/page.tsx
apps/web/app/(settings)/integrations/slack/callback/page.tsx
apps/web/app/(settings)/integrations/slack/link/page.tsx
apps/web/components/integrations/slack/SlackConnectButton.tsx
apps/web/components/integrations/slack/SlackRoutesTable.tsx
apps/web/components/integrations/slack/SlackLinkStatus.tsx
apps/web/__tests__/slack-settings.test.tsx

deploy/docker-compose.yml                                      # SLACK_* env; notifications queue
deploy/caddy/Caddyfile                                         # raw-body passthrough note for slack callback paths
.env.example                                                   # SLACK_CLIENT_ID/SECRET/SIGNING_SECRET/APP_ID/DEFAULT_SCOPES
```

---

## 11. Research references (relevant links from the spec/research report)

- **V1 Slack scope** (task status, approval requests, `/forge` slash commands) and **roadmap item** "Slack notifications for approvals and task status": `docs/FORGE_SPEC.md` §Integrations → V1 (table) and §Phased Roadmap → Phase 1. **Phase reconciliation:** the high-level capability summary table (`docs/FORGE_SPEC.md` §Product Scope, "Integration Layer" row) groups Slack with the V2 bidirectional PM integrations (Jira/Linear/…); the more specific **V1 Integrations table** and the **Phase 1 roadmap checklist** both place Slack *notifications + approvals + `/forge` commands* in V1 — that is the authoritative scope this slice (v1) implements. Deeper Slack capabilities (digests, two-way chat, Enterprise Grid) remain V2/future (§12).
- **Slack adapter lives in `integration-sdk`**: `docs/FORGE_SPEC.md` §Monorepo Structure (`packages/integration-sdk/ # GitHub, Slack, Jira, Linear, Asana adapters`); F03 confirms `forge_integrations/slack/*` is owned by this slice.
- **Human Approval System** (gate types; "Approval UI Must Show" — the data the Slack approval card summarizes and deep-links to): `docs/FORGE_SPEC.md` §Human Approval System; implemented by `cross-cutting/F36-human-approval-system` (the canonical `ApprovalService` this slice calls), with the `pr` merge gate in `v1/F08-plan-execute-verify-pr-approval`.
- **Open SWE — Slack + Linear integration and PR-opening workflows** (the reference for chat-surface approvals): https://www.langchain.com/blog/open-swe-an-open-source-framework-for-internal-coding-agents · https://github.com/langchain-ai/open-swe · research report §"Symphony and Open SWE: What to Borrow".
- **Symphony — task-as-control-plane** (notifications/actions surface the issue-tracker control plane into chat): https://openai.com/index/open-source-codex-orchestration-symphony/ · research report §Symphony.
- **Security** (secrets encrypted at rest + per-workspace isolation, audit every action, least-privilege token binding, secret redaction): `docs/FORGE_SPEC.md` §Security; MCP token-binding RFC 8707 / read-only-by-default discipline applied by analogy to the Slack bot token.
- **Workflow escalation policy** (`pause_and_notify`, `escalate_to_admin`, confidence threshold 0.72) that triggers escalation notifications: `docs/FORGE_SPEC.md` §Workflow Engine → escalation_policy; surfaced via the `pause_and_notify`/`escalate_to_admin` effect bodies this slice registers into F07's `EffectRegistry` (`v1/F07-feature-workflow-fsm` §3.3 / §5).
- **Docker Compose production practices** (env-driven secrets, healthchecks, named volumes): https://distr.sh/blog/running-docker-in-production/ (`docs/FORGE_SPEC.md` §Self-Hosting).
- External canonical references for the live-verification phase (not in spec, needed to implement against the real API): Slack request-signing — https://api.slack.com/authentication/verifying-requests-from-slack · OAuth v2 — https://api.slack.com/authentication/oauth-v2 · Interactivity & slash commands — https://api.slack.com/interactivity/slash-commands , https://api.slack.com/interactivity/handling · Block Kit — https://api.slack.com/block-kit · Web API rate limits — https://api.slack.com/docs/rate-limits.

---

## 12. Out of scope / future

- **Email notifications & digests** (`Email: Approval requests, @mentions, digest`) — a separate V1 integration feature; F16 covers Slack only. The shared `NotificationEvent` contract (§4) is designed so an email dispatcher can subscribe to the same events.
- **OAuth user sign-in via Slack** — auth/login is owned by the Auth feature; this slice is Slack **app** install + bot auth + per-user identity *linking*, not human login.
- **Spec / plan / deploy approval gates from Slack** — V1 ships the **PR** approval gate end-to-end (F08); other `gate_type`s reuse the same interaction path and land with their owning slices.
- **Scheduled / digest notifications and on-call routing** (daily summaries, quiet hours, escalation schedules) — future; V1 is event-driven only.
- **Per-user notification preferences UI** (mute categories, choose DM vs channel per user) — V1 has workspace/project channel routes + assignee/reviewer DMs; granular per-user prefs are a fast-follow.
- **Threaded conversation / two-way chat with the agent in Slack** (replies driving the run) — future; V1 is notifications + buttons + slash commands.
- **Multiple Slack workspaces per Forge workspace / Slack Enterprise Grid org-install** — V1 assumes one Slack team per Forge workspace.
- **Modal (`views.open`) rich approval dialogs** — V1 uses message buttons + ephemeral responses + deep links; full modal review flows are future (the `open_view` client method is included to enable this later).
- **Jira/Linear/Asana/Monday/GitLab/Datadog/Sentry/PagerDuty/Grafana notifications** — V2.
