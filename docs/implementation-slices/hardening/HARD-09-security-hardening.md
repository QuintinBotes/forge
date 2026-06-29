# HARD-09 — Security Audit (automated) + Threat Model + Pentest Punch-list

> Phase: hardening · Blocker(s): #4 (no real security audit / no pentest) · Status target: **DONE** means SAST (bandit + semgrep), dependency audit (`pip-audit`/`uv` + `osv-scanner` + `pnpm audit`), secret scan (gitleaks), and an SBOM all run as **blocking** CI jobs with **zero unwaived high/critical** findings (each waiver dated + justified in `security/waivers.yaml`); the **enforcement-matrix regression suite** proves, on the wired/live request path, that RBAC is default-deny per role, MCP writes are denied by default, policy is default-deny, secrets are redacted in logs/traces/audit/retrieval, every router rejects unauthenticated calls (401), outbound URLs are SSRF-guarded, request bodies are size-bounded, and a rate limit returns 429; a STRIDE **threat model**, a repo-root **`SECURITY.md`** (coordinated-disclosure policy), an extended **secrets-rotation runbook**, and a scoped **human-pentest punch-list** (severities + remediation owners) are committed as the evidence pack. **No external creds required** — enforcement tests use generated test API keys. The **live-RBAC-on-real-Postgres** rows of the matrix depend on HARD-01; the **crypto/secret-key default** assertions depend on HARD-10. The **human penetration test itself cannot be performed by build agents** and ships as a named, scoped punch-list item — this is blocker #4 closed *partially and honestly*.

---

## 1. Intent — what & why

The ALPHA implements the right security primitives — a deny-by-default RBAC matrix (`forge_api.auth.rbac`), fail-closed MCP write classification (`forge_mcp.security.is_write_tool`), a default-deny policy evaluator (`forge_policy.evaluator`), encrypt-at-rest BYOK vault (`forge_api.auth.vault` + `crypto`), constant-time webhook signature verification (`forge_integrations.webhooks`), a tamper-evident audit hash chain (`forge_api.observability.audit`), and a redaction filter — but **MORNING_REPORT §2.3 / §6 are explicit that no real security audit, no SAST, no dependency CVE scan, no secret scan, and no pentest have ever been run.** `test_security_fixes.py` covers two reproductions (auth wired, CORS lockdown); there is no consolidated, machine-checkable proof that *all* the FORGE_SPEC "Security" controls actually hold on the wired path, and there is no continuous scanning to catch a regression or a newly-disclosed CVE.

HARD-09 closes the *automatable* half of a real security audit and produces the human-pentest hand-off. Concretely it:

1. **Wires continuous automated scanning into CI** — SAST (bandit + semgrep with Forge-specific rules), dependency CVE audit (`pip-audit`/`uv export | pip-audit`, `osv-scanner` over `uv.lock` + `pnpm-lock.yaml`, `pnpm audit` for the web app), secret scanning (gitleaks over history), and an SBOM (CycloneDX) — all **blocking** on high/critical with a dated waiver file for accepted risk.
2. **Threat-models the system** (STRIDE over the real attack surface: auth/RBAC, the BYOK vault, MCP transport, webhook verifiers, the agent git-worktree sandbox, and SSRF via admin-configured embedder/reranker/MCP/OAuth URLs) and **fixes the gaps the model surfaces** — the `Principal.role` ADMIN default (a fail-open smell), OpenAPI docs exposed in prod (`docs_enabled=True`), no request rate limiting, no request body-size bound, and no SSRF guard on outbound HTTP.
3. **Turns the spec's Security table into a single enforcement-matrix regression suite** that asserts each control on the wired path so any future drop is a red test, and emits the matrix as a committed evidence artifact.
4. **Writes the human-facing security docs**: a repo-root `SECURITY.md` (coordinated disclosure, supported versions, contact), an extended `docs/self-hosting/security.md` rotation runbook, a STRIDE `docs/security/threat-model.md`, and a scoped `docs/security/pentest-punch-list.md` ready to hand to a third-party pentester.

This is blocker #4. The honest ceiling (stated in the spec and repeated in §9): a **3rd-party human penetration test and a formal audit sign-off cannot be executed by the build agents** — HARD-09 delivers the automated evidence and the scoped punch-list; the engagement itself remains a named external gap.

## 2. User-facing / operator behavior

HARD-09 is operator- and contributor-facing, not end-user-facing. Observable behavior:

- **Journey A — Contributor runs the audit locally.** A developer runs `make security` and gets a single pass/fail roll-up: bandit, semgrep, gitleaks, `pip-audit`, `osv-scanner`, `pnpm audit`, SBOM generation, and the enforcement-matrix pytest suite, each with a green/red line and a findings count. A new high/critical fails the command with a non-zero exit and a pointer to `security/waivers.yaml` for triage.
- **Journey B — CI blocks a vulnerable change.** A PR that introduces `subprocess.run(..., shell=True)`, a hardcoded token, an `httpx` client with `verify=False`, or pulls in a dependency with a known critical CVE is **blocked** by the `security` CI job. SARIF is uploaded to GitHub code-scanning so the finding annotates the diff inline.
- **Journey C — Operator reads the security posture.** A self-hoster opens `SECURITY.md` (how to report a vuln, supported versions, response SLA) and `docs/self-hosting/security.md` (rotation runbook + the enforcement matrix + the operational checklist), and can see exactly which controls are enforced and how to rotate every credential.
- **Journey D — Maintainer hands off to a pentester.** A maintainer hands `docs/security/threat-model.md` + `docs/security/pentest-punch-list.md` to a contracted pentester: the attack-surface inventory, per-item severity, suggested test cases, and remediation owners are pre-filled; the pentester records findings against the same template.
- **Journey E — A blocked outbound call.** When an admin misconfigures a workspace embedder/reranker/MCP URL to point at `169.254.169.254` (cloud metadata) or a private/loopback host, the request is rejected with a clear `SsrfBlockedError` (HTTP 422 at the API edge) instead of silently exfiltrating to or scanning the internal network. A rate-limited caller gets `429 Too Many Requests` with a `Retry-After` header; an oversized body gets `413 Payload Too Large`.

## 3. Vertical slice

### 3.1 Data model

**No new tables.** HARD-09 audits and enforces around the existing `forge_db` schema and adds **no** migration. It does add three relevant assertions against existing structures, all exercised by the enforcement suite:

- **Audit immutability** — the `AuditLog` hash chain (`apps/api/forge_api/observability/audit.py`: `AuditEntry.seq/prev_hash/entry_hash`, `InMemoryAuditStore.append` is append-only) is asserted tamper-evident; the **Postgres audit-immutability trigger** (rejects UPDATE/DELETE) is asserted on a live DB — this row is **shared with and depends on HARD-01** (the trigger only runs on real pgvector).
- **Vault encrypt-at-rest** — assert `StoredSecret.ciphertext` never equals the plaintext and that `StoredSecret.__repr__` / `SecretInfo` never expose plaintext (the records already guarantee this; the matrix pins it as a regression test). The Postgres-backed `SecretStore` swap-in (the `api_key.encrypted_secret` column) is asserted to round-trip under the real cipher — shared with HARD-01/HARD-10.
- **Per-tenant isolation** — assert vault/knowledge reads filter by `workspace_id` (re-using `test_rbac_tenant_r2/r3` style row-id assertions) so the enforcement matrix has a "tenant-isolation" row backed by a real query.

The enforcement matrix itself is a **committed YAML artifact** (`security/enforcement-matrix.yaml`), not a DB table — it is the source of truth the regression suite iterates.

### 3.2 Backend

Net-new security code lives under a new `apps/api/forge_api/security/` subpackage (extends `forge_api`; no new top-level package) and small DI seams in the leaf clients. **Existing primitives are reused, not duplicated.**

```
apps/api/forge_api/security/
├── __init__.py          # re-exports the public surface
├── ssrf.py              # assert_safe_url / SsrfBlockedError / is_public_host
├── ratelimit.py         # RateLimitMiddleware (ASGI) + TokenBucket
├── bodylimit.py         # BodySizeLimitMiddleware (Content-Length + streamed cap)
└── headers.py           # SecurityHeadersMiddleware (HSTS, X-Content-Type-Options, etc.)
```

Wiring in `apps/api/forge_api/main.py` (`create_app`): mount `BodySizeLimitMiddleware`, `RateLimitMiddleware`, and `SecurityHeadersMiddleware` (order matters — body limit outermost, then rate limit, then headers); gate OpenAPI docs behind `settings.docs_enabled and settings.environment != "production"`.

**Fixes the threat model surfaces (the "fix" half of the blocker):**

1. **`Principal.role` fail-open default** — `apps/api/forge_api/deps.py` declares `role: UserRole = UserRole.ADMIN`. Change the *model default* to the least-privileged role (`UserRole.VIEWER`) so a partially-constructed principal can never silently be admin; the real `get_current_principal` already sets the role from the verified key, so this only hardens the failure mode. (Regression-tested.)
2. **OpenAPI docs in prod** — disable `/docs`, `/redoc`, `/openapi.json` when `environment == "production"` unless `docs_enabled` is explicitly set (info-disclosure reduction).
3. **SSRF guard** — `assert_safe_url(url, *, allow_private=False, allowlist=())` resolves the host, rejects loopback / RFC1918 / link-local / unique-local / `169.254.169.254` / `0.0.0.0` and non-`http(s)` schemes; applied at the API/service layer where an admin sets a workspace embedder/reranker/MCP/OAuth URL, and injected (DI) into the leaf HTTP clients so the pure packages stay decoupled (see §3.3).
4. **Rate limiting** — per-principal (fallback per-IP) token bucket; `429` + `Retry-After`. Default on; tunable/off via env for trusted internal deployments.
5. **Body-size bound** — reject `Content-Length` over `FORGE_MAX_BODY_BYTES` (default 1 MiB) early with `413`, and cap streamed bodies defensively.
6. **Security headers** — `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, a conservative `Content-Security-Policy` for API JSON responses.

**Custom semgrep rules** (`.semgrep/forge.yml`) encode Forge invariants as SAST so they cannot regress: no logging of `SENSITIVE_KEYS`-named values; no `httpx`/`requests` call with `verify=False`; no `subprocess` with `shell=True`; no `MCPConnection`/tool default `allow_write=True`; no `yaml.load` without `SafeLoader`; no `eval`/`exec` on request-derived data; flag any cipher constructed from a literal key.

### 3.3 Worker / agent

- **SSRF guard reaches the leaf clients via DI.** `forge_knowledge.embeddings.OpenAICompatEmbeddingProvider` and `forge_knowledge.reranker.JinaRerankerClient` (and the `forge_mcp.transport` HTTP transport) take an optional `url_validator: Callable[[str], None] = _noop` parameter; the worker/api wire the real `assert_safe_url`. This keeps `forge_knowledge`/`forge_mcp` pure (no import of `forge_api`) while still enforcing SSRF on every outbound call the worker makes (embed/rerank/MCP read). Asserted by an integration test that points a provider base_url at `169.254.169.254` and expects `SsrfBlockedError`.
- **Agent sandbox surface** is inventoried in the threat model (git-worktree escape, command injection via tool args, policy-denied tool dispatch must hard-stop). HARD-09 adds the **policy-default-deny enforcement test on the agent dispatch path** (`forge_agent` tool dispatch → `forge_policy.evaluate` returns DENY for an unlisted action and the agent does not execute it) — this is a security-regression row; the deeper worker/agent error-path coverage is owned by HARD-12.
- **No live model creds needed**: the agent enforcement test uses a fake model client (the live model path is HARD-02); HARD-09 only proves the *policy gate* fires.

### 3.4 Frontend

- **`pnpm audit`** (web dependency CVE scan) is added as a blocking step in the existing web CI job; a high/critical advisory fails the build (waivable via `pnpm` overrides documented in `security/waivers.yaml`).
- **Security headers** for the Next.js app are set in `apps/web/next.config.*` (CSP, HSTS, `X-Content-Type-Options`, `frame-ancestors 'none'`) to mirror the API edge; a Vitest/unit check asserts the headers config is present.
- No new UI surface. (The security posture is documentation + CI, not a screen.)

### 3.5 Infra / deploy / CI

A new **`security` job** in `.github/workflows/ci.yml` (Python 3.13 lane), plus a `pnpm audit` step added to the existing `web` job:

```yaml
  security:
    name: security (sast + deps + secrets + sbom)
    runs-on: ubuntu-latest
    permissions:
      contents: read
      security-events: write   # upload SARIF to code scanning
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }      # full history for gitleaks
      - uses: astral-sh/setup-uv@v5
        with: { enable-cache: true }
      - run: uv python install 3.13
      - run: uv sync --all-packages
      # --- SAST ---
      - name: Bandit (SARIF)
        run: uv run bandit -c pyproject.toml -r packages apps -f sarif -o bandit.sarif
      - name: Semgrep (SARIF)
        run: uv run semgrep --config .semgrep/forge.yml --config p/python --sarif -o semgrep.sarif
      # --- Dependency audit ---
      - name: pip-audit
        run: uv export --frozen --format requirements-txt --no-emit-workspace | uv run pip-audit -r /dev/stdin --strict
      - name: osv-scanner (lockfiles)
        uses: google/osv-scanner-action@v1
        with: { scan-args: "--lockfile=uv.lock --lockfile=apps/web/pnpm-lock.yaml" }
      # --- Secret scan (history) ---
      - name: gitleaks
        uses: gitleaks/gitleaks-action@v2
        env: { GITLEAKS_CONFIG: .gitleaks.toml }
      # --- SBOM ---
      - name: SBOM (CycloneDX)
        run: uv run cyclonedx-py environment -o sbom.cdx.json
      # --- Enforcement matrix regression suite ---
      - name: Enforcement matrix
        run: uv run pytest -m security -q
      - name: Upload SARIF
        if: always()
        uses: github/codeql-action/upload-sarif@v3
        with: { sarif_file: "." }
```

- **Severity gate**: each scanner runs in fail-on-high/critical mode; the roll-up reads `security/waivers.yaml` (dated, justified, owner, expiry) and only un-waived high/critical fail. A waiver past its `expires` date fails closed.
- **`make security`** aggregates the same commands for local parity (single source of truth: a `scripts/security/run.sh`, `shellcheck`-clean — shellcheck wiring is shared with HARD-08).
- **`deploy/secrets/`** is added to `.gitignore` (the spec's creds-handling rule 1) alongside the already-ignored `.env*`/`*.pem`/`*.key`; gitleaks' config allowlists the committed `*.example` files only.
- **Non-blocking-to-blocking ramp**: the `security` job lands non-blocking for one PR to baseline existing findings into `waivers.yaml`, then flips to **required** (branch protection) — recorded in the PR description.

## 4. Public interfaces / contracts (exact signatures, env vars, config keys)

**New module `apps/api/forge_api/security/ssrf.py`:**

```python
class SsrfBlockedError(ValueError):
    """Raised when an outbound URL targets a non-public / disallowed host."""

def is_public_host(host: str) -> bool:
    """True iff host resolves only to global-scope IPs (no loopback/private/
    link-local/ULA/0.0.0.0/169.254.169.254)."""

def assert_safe_url(
    url: str,
    *,
    allow_private: bool = False,
    allowlist: Iterable[str] = (),     # exact hostnames always permitted
    schemes: frozenset[str] = frozenset({"https", "http"}),
) -> str:
    """Validate an outbound URL; return it normalized or raise SsrfBlockedError."""
```

**New module `apps/api/forge_api/security/ratelimit.py`:**

```python
class TokenBucket:
    def __init__(self, *, rate_per_min: int, burst: int) -> None: ...
    def allow(self, key: str, *, now: float | None = None) -> bool: ...

class RateLimitMiddleware:  # ASGI; keys by principal id else client IP
    def __init__(self, app, *, rate_per_min: int = 120, burst: int = 60,
                 enabled: bool = True, exempt_paths: frozenset[str] = frozenset({"/health"})) -> None: ...
```

**New module `apps/api/forge_api/security/bodylimit.py`:** `BodySizeLimitMiddleware(app, *, max_bytes: int = 1_048_576)` → `413` when exceeded.

**Leaf-client DI seam (added param, default no-op — backwards compatible):**

```python
# forge_knowledge/embeddings.py, forge_knowledge/reranker.py, forge_mcp/transport.py
def __init__(self, ..., url_validator: Callable[[str], None] = lambda _u: None) -> None: ...
```

**New env vars / config keys (added to `Settings`, `FORGE_`-prefixed, and to `.env.example`):**

| Key | Default | Meaning |
|---|---|---|
| `FORGE_RATELIMIT_ENABLED` | `true` | enable the rate-limit middleware |
| `FORGE_RATELIMIT_RPM` | `120` | requests/min per principal (or IP) |
| `FORGE_RATELIMIT_BURST` | `60` | token-bucket burst |
| `FORGE_MAX_BODY_BYTES` | `1048576` | max request body (1 MiB) before `413` |
| `FORGE_SSRF_ALLOW_PRIVATE` | `false` | allow private/loopback outbound targets (dev only) |
| `FORGE_OUTBOUND_ALLOWLIST` | `[]` | JSON list of hostnames always permitted outbound |
| `FORGE_DOCS_ENABLED` | `true` | OpenAPI docs; forced off when `environment=production` unless explicitly set |

**Reused (unchanged) public surfaces the matrix pins:** `forge_api.auth.rbac.{Permission, ROLE_PERMISSIONS, can, ensure, PermissionDeniedError}`; `forge_api.routers._rbac.require_permission`; `forge_mcp.security.{is_write_tool, token_binding, redact, redact_text, payload_hash}`; `forge_policy.evaluator.evaluate`; `forge_integrations.webhooks.{verify_github_signature, verify_slack_signature}`; `forge_api.auth.crypto.{default_cipher, FernetCipher}`; `forge_api.observability.audit.AuditLog`.

**New pytest marker:** `security` (added to `[tool.pytest.ini_options].markers` in root `pyproject.toml`): `"security: enforcement-matrix + security regression tests (run offline; no external creds)"`.

**New config artifacts:** `.semgrep/forge.yml` (custom rules), `.gitleaks.toml`, `[tool.bandit]` table in root `pyproject.toml`, `security/enforcement-matrix.yaml` (the matrix), `security/waivers.yaml` (dated/owned/expiring waivers).

## 5. Dependencies (other slices/foundation that must exist first)

- **Foundation (exists):** `forge_api.auth` (rbac/vault/crypto/apikeys), `forge_api.routers._rbac`, `forge_mcp.security`, `forge_policy.evaluator`, `forge_integrations.webhooks`, `forge_api.observability.audit/redaction`, the `Settings` layer, and `.github/workflows/ci.yml` — all present and the targets HARD-09 extends.
- **HARD-10 — Production crypto + OAuth seam (REQUIRED for two matrix rows).** The "crypto default is Fernet" and "`FORGE_SECRET_KEY` required / no silent ephemeral fallback" enforcement-matrix rows assert HARD-10's behavior. If HARD-10 has not landed, those two rows run in `xfail`/skip with a linked reason; the rest of HARD-09 is independent. (HARD-09's tooling will *flag* the ephemeral-key fallback as a finding until HARD-10 fixes it — that is the intended handshake.)
- **HARD-01 — Real Postgres + pgvector (REQUIRED for the live-DB rows).** The audit-immutability-trigger row and the Postgres-backed vault round-trip row need a live pgvector container; absent it they skip (suite stays green) exactly like the existing 3 ALPHA postgres skips.
- **HARD-12 — Whole-workspace typecheck (SOFT).** HARD-09's new `forge_api/security/*` modules must be inside the `make typecheck` green set; HARD-12 fixes the workspace-wide mypy invocation. New modules are written typed (`py.typed` already present) so they don't regress HARD-12.
- **HARD-08 — Build + shellcheck (SOFT, shared).** `scripts/security/run.sh` is linted by the same `shellcheck` CI step HARD-08 introduces.
- **Independent of all creds-bearing slices** (HARD-02/05/06/07): HARD-09 uses generated test API keys and fakes; it does **not** require GitHub/Slack/MCP/model creds. (It *audits* the code those slices light up.)

## 6. Acceptance criteria (numbered, testable)

Marked **[offline]** (no external creds, runs in the hermetic suite/CI) or **[live]** (needs a real PG container or a sibling slice).

1. **[offline]** `make security` and the CI `security` job run bandit, semgrep, gitleaks, `pip-audit`, `osv-scanner`, `pnpm audit`, and SBOM generation; with no unwaived findings the command exits 0; introducing a high/critical (e.g. a planted `shell=True`) makes it exit non-zero.
2. **[offline]** Every scanner is configured fail-on-high/critical; `security/waivers.yaml` only suppresses listed, dated, owned findings, and a waiver past its `expires` date fails the gate closed (tested with a synthetic expired waiver).
3. **[offline]** Custom semgrep rules fire on: a secret-named value passed to a logger; an `httpx`/`requests` call with `verify=False`; `subprocess(..., shell=True)`; a tool/connection literal `allow_write=True`; `yaml.load` without `SafeLoader` (each has a positive + negative fixture).
4. **[offline]** **RBAC enforcement matrix:** `viewer` is denied WRITE/RUN_AGENT (`require_permission` → 403); `agent-runner` is allowed `READ`+`RUN_AGENT` only and denied `WRITE`/`MANAGE_KEYS`/`MANAGE_MEMBERS`; `member` is denied `MANAGE_KEYS`/`MANAGE_MEMBERS`/`ADMIN`; `admin` is allowed all — asserted against the real `ROLE_PERMISSIONS` and through a wired route per role.
5. **[offline]** **Auth-required:** one sampled route per feature router returns `401` with no/invalid credentials (extends `test_security_fixes`), proving no anonymous access anywhere.
6. **[offline]** **MCP write-default-deny:** `is_write_tool` classifies an un-annotated / unrecognized-verb tool (e.g. `merge_pull_request`, `approve`) as a write; a write/tool call is denied on the gateway path unless explicitly enabled; an annotated `read_only=True` tool is permitted.
7. **[offline]** **Policy default-deny:** `evaluate` returns `DENY` for an unlisted action, for a write to a non-allowlisted path, and for an empty tool call; the agent dispatch path does not execute a policy-denied tool.
8. **[offline]** **Secret redaction holds end-to-end:** a payload containing a bearer token / `api_key=...` / JWT / AWS-key is redacted in the audit entry, in a logged trace, and in any MCP query-through snapshot (re-using `forge_mcp.security.redact` + `forge_api.observability.redaction`); no secret substring survives.
9. **[offline]** **SSRF guard:** `assert_safe_url` rejects loopback, RFC1918, link-local `169.254.169.254`, `0.0.0.0`, ULA, and non-http(s) schemes, and accepts a normal public host and any `FORGE_OUTBOUND_ALLOWLIST` host; a worker embedder/reranker/MCP client wired with the validator raises `SsrfBlockedError` for a metadata-IP base_url.
10. **[offline]** **Rate limit + body limit + headers:** exceeding `FORGE_RATELIMIT_RPM` returns `429` with `Retry-After`; a body over `FORGE_MAX_BODY_BYTES` returns `413`; responses carry `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and HSTS; `/health` is exempt from the rate limit.
11. **[offline]** **Docs lockdown:** with `environment=production`, `/docs` / `/openapi.json` return 404 unless `FORGE_DOCS_ENABLED` is explicitly set.
12. **[offline]** **`Principal.role` fails safe:** the model default is `VIEWER` (not `ADMIN`); a default-constructed `Principal` has no write/admin permission.
13. **[offline]** **Vault never leaks plaintext:** `StoredSecret.ciphertext != plaintext`, `repr(StoredSecret)` and `SecretInfo` contain no plaintext, and a wrong-key decrypt raises `InvalidTokenError` (no oracle).
14. **[offline]** **Audit tamper-evidence:** the `AuditLog` hash chain detects a mutated entry; the store exposes no update/delete API.
15. **[live]** **Audit-immutability trigger** rejects UPDATE/DELETE on the audit table on a real pgvector DB (shared with HARD-01); **Postgres-backed vault** round-trips under the real cipher (shared with HARD-01/HARD-10). Skips cleanly when no DB.
16. **[live/dep]** **Crypto defaults:** `default_cipher` returns `FernetCipher`; the app refuses to start in prod mode without `FORGE_SECRET_KEY` (no silent ephemeral fallback). `xfail`/skip with a linked reason until HARD-10 lands.
17. **[offline]** **SBOM** (CycloneDX JSON) is generated and committed to the evidence pack; it lists every workspace package + third-party dep.
18. **[offline]** **Evidence pack + docs complete:** repo-root `SECURITY.md`, `docs/security/threat-model.md` (STRIDE over the named surfaces), `docs/security/pentest-punch-list.md` (attack-surface inventory, per-item severity + remediation owner + suggested test cases), and an extended `docs/self-hosting/security.md` rotation runbook all exist and the punch-list explicitly names the **human pentest** as the residual gap.
19. **[offline]** The whole-suite green gate stays green: `uv run pytest -q` (incl. `-m security`), `uv run ruff check .`, `uv run ruff format --check .`, `make typecheck`, and `cd apps/web && pnpm test` all pass; no real secret appears in source, tests, fixtures, logs, or `uv.lock`/`pnpm-lock.yaml`.

## 7. Test plan (TDD) — unit + integration + how to run

Write tests first. Tests live in a new `tests/security/` tree (top-level, matching the existing `tests/` smoke layout) plus per-package additions; all carry `@pytest.mark.security`.

**Matrix-driven suite — `tests/security/test_enforcement_matrix.py`:** parametrizes over `security/enforcement-matrix.yaml` rows so adding a control = adding a row. Each row names a control id, the assertion fn, and offline/live. Emits `docs/security/evidence/enforcement-matrix.md` as a side artifact (the committed evidence).

Unit (pure, no DB, no network):
- `test_rbac_matrix_per_role` (AC4) — assert `can(role, perm)` for the full cartesian product against `ROLE_PERMISSIONS`; assert `ensure` raises `PermissionDeniedError` for each denied pair.
- `test_is_write_tool_fail_closed` (AC6) — `merge_pull_request`/`approve`/un-annotated → write; `get_*`/`read_only=True` → read.
- `test_policy_default_deny` (AC7) — unlisted action, non-allowlisted write path, empty call → `DENY`.
- `test_ssrf_blocks_private_and_metadata` / `test_ssrf_allows_public_and_allowlist` (AC9) — table of hosts incl. `169.254.169.254`, `127.0.0.1`, `10.0.0.5`, `::1`, `fd00::1`, `0.0.0.0`, `file://…`, a public host, an allowlisted host.
- `test_token_bucket_allows_then_blocks` (AC10) — bucket math at the boundary.
- `test_redaction_strips_bearer_kv_jwt_aws` (AC8) — positive + non-secret-preserving.
- `test_vault_repr_and_info_have_no_plaintext` / `test_wrong_key_raises_invalid_token` (AC13).
- `test_audit_chain_detects_tamper` (AC14).
- `test_principal_default_role_is_viewer` (AC12).
- `test_semgrep_rules_fire_on_fixtures` (AC3) — run `semgrep` over `tests/security/fixtures/{bad,good}/` and assert findings only on `bad/`.
- `test_waiver_expiry_fails_closed` (AC2) — synthetic expired waiver.

Integration (FastAPI `TestClient`, fakes; no external creds):
- `test_unauthenticated_routes_401` (AC5) — sample one route per router in `forge_api.routers`.
- `test_role_gated_route_403_for_viewer` / `test_agent_runner_cannot_write` (AC4).
- `test_rate_limit_returns_429_with_retry_after`, `test_body_over_limit_413`, `test_security_headers_present`, `test_health_exempt_from_ratelimit` (AC10).
- `test_docs_disabled_in_production` (AC11).
- `test_mcp_write_denied_by_default_on_gateway` (AC6) — wired gateway path with a fake transport.
- `test_agent_does_not_execute_policy_denied_tool` (AC7) — fake model client.
- `test_worker_client_ssrf_blocked_for_metadata_url` (AC9) — embedder/reranker wired with `assert_safe_url`.
- `test_redaction_holds_in_audit_and_trace` (AC8).

Integration (gated on a live DB — `@pytest.mark.security and postgres`, skips without `FORGE_TEST_DATABASE_URL`):
- `test_audit_trigger_rejects_update_delete` (AC15) — shared HARD-01.
- `test_pg_vault_roundtrip_under_real_cipher` (AC15/16).

Tooling/CI tests:
- `test_sbom_lists_workspace_packages` (AC17) — generate + parse the CycloneDX JSON.
- `test_evidence_pack_files_exist` (AC18) — assert `SECURITY.md`, threat-model, punch-list, rotation runbook, and the matrix evidence MD are present and non-empty and that the punch-list names the human pentest.

How to run:
```bash
# Full local audit roll-up (SAST + deps + secrets + SBOM + matrix):
make security

# Just the enforcement-matrix + security regression suite (fast, offline):
uv run pytest -m security -q

# Live-DB rows too (needs a pgvector container):
export FORGE_TEST_DATABASE_URL=postgresql+psycopg://forge:forge@localhost:5432/forge_test
uv run pytest -m "security" -q

# Individual scanners:
uv run bandit -c pyproject.toml -r packages apps
uv run semgrep --config .semgrep/forge.yml --config p/python
uv run pip-audit -r <(uv export --frozen --format requirements-txt --no-emit-workspace)
gitleaks detect --config .gitleaks.toml --no-banner
cd apps/web && pnpm audit --audit-level=high
```

## 8. Security & policy considerations

- **This slice is itself a security control** — its main risk is false confidence. Every matrix row asserts behavior on the **wired/live path** (route → dependency → primitive), not just the pure helper, so a control that is implemented but not mounted is still caught (this is exactly the ALPHA "implemented but never exercised externally" trap).
- **No secrets in scanners' output.** gitleaks/bandit/semgrep SARIF and the SBOM are scrubbed of values (gitleaks reports locations + rule ids, not full secrets); CI artifacts never echo `.env.integration`. The `deploy/secrets/` dir and `.env*`/`*.pem`/`*.key` stay gitignored; a gitleaks rule additionally fails the build if any of those are ever staged.
- **Waivers are auditable, not silent.** `security/waivers.yaml` requires id, finding, justification, owner, and `expires`; an expired or missing field fails closed. This prevents a permanent "ignore" from hiding a real CVE.
- **SSRF guard is defense-in-depth, not a substitute for network policy.** The compose `data`/`mcp` network segmentation (FORGE_SPEC + `docs/self-hosting/security.md`) remains the primary control; `assert_safe_url` stops an admin-configured (or member-influenced BYOK) URL from reaching the metadata endpoint or internal hosts even inside the perimeter.
- **Fail-closed everywhere it matters:** MCP write classification (`is_write_tool`), policy (`evaluate`), webhook verification (rejects unsigned/stale), CORS (no wildcard-with-credentials), docs in prod, and the new rate/body limits all default to the safe outcome. The `Principal.role` default is changed from ADMIN to VIEWER for the same reason.
- **Redaction is single-source.** HARD-09 does not fork a new redaction implementation; it asserts the existing `forge_mcp.security.redact`/`redact_text` and `forge_api.observability.redaction` are applied on every sink (logs, traces, audit, retrieval, MCP snapshots) and adds a semgrep rule to forbid logging `SENSITIVE_KEYS`-named values.
- **Rate limiting is per-principal then per-IP** so a single tenant/key cannot exhaust the service; `/health` is exempt so liveness probes are unaffected.
- **The human pentest is explicitly out of agent scope** (see §9) — the punch-list is written *for* a pentester and the release notes carry the honest asterisk verbatim.

## 9. Effort & risk (S/M/L + risks)

**Effort: L.** Tooling + CI wiring is S–M; the enforcement-matrix suite + the net-new middleware/SSRF/leaf-DI fixes are M; the threat model + SECURITY.md + punch-list + rotation runbook are M (writing-heavy). Baselining existing findings into `waivers.yaml` without masking real issues is the fiddly part.

Risks:
- **Scanner false-positive noise** could make the gate annoying and tempt blanket waivers. Mitigation: scope bandit/semgrep configs to the real tree, start non-blocking for one baseline PR, require per-finding (not per-rule) waivers with expiry. (Medium)
- **SSRF guard breaking legitimate self-hosted endpoints.** Self-hosted embedder/reranker/MCP often *are* on private networks. Mitigation: `FORGE_OUTBOUND_ALLOWLIST` + `FORGE_SSRF_ALLOW_PRIVATE` (documented, default-deny) so operators explicitly opt-in their internal hosts; the guard rejects only metadata/loopback by default in `allow_private` mode. (Medium)
- **Rate-limit middleware in a multi-replica deploy** is per-process (in-memory token bucket), so the effective limit scales with replica count. Mitigation: documented as a coarse per-instance guard; a Redis-backed shared limiter is noted as future work (§12). (Low-Med)
- **DI seam churn** in `forge_knowledge`/`forge_mcp` clients must stay backwards-compatible (default no-op validator). Mitigation: optional param with no-op default; covered by existing client tests + new SSRF tests. (Low)
- **Waiver rot** — an accepted CVE never fixed. Mitigation: `expires` fails closed; CI surfaces upcoming expiries. (Low)

**Cannot be done in-sandbox / by agents (named, not hidden):**
- A **3rd-party human penetration test** and a **formal security audit sign-off** — HARD-09 produces the automated evidence, the threat model, and the scoped punch-list; the engagement and the sign-off are external. This is the explicit residual of blocker #4 and is repeated in the BETA/PROD release notes verbatim.
- **SOC2 / compliance attestation** — out of scope for the codebase (noted so a green `security` job is never mistaken for compliance).
- The **SARIF→code-scanning upload** and `osv-scanner`/`gitleaks` GitHub Actions need a **networked CI runner** (registry + advisory DB access); the local `make security` path runs the same scanners offline against cached DBs where possible.

## 10. Key files / paths (exact, in the real monorepo)

New:
- `apps/api/forge_api/security/__init__.py`, `ssrf.py`, `ratelimit.py`, `bodylimit.py`, `headers.py` — net-new edge controls.
- `.semgrep/forge.yml` — custom SAST rules; `.gitleaks.toml` — secret-scan config; `[tool.bandit]` in root `pyproject.toml`.
- `security/enforcement-matrix.yaml` — the control matrix (source of truth); `security/waivers.yaml` — dated/owned/expiring waivers.
- `scripts/security/run.sh` — `make security` roll-up (shellcheck-clean).
- `tests/security/test_enforcement_matrix.py`, `tests/security/test_*` + `tests/security/fixtures/{good,bad}/` — the regression suite.
- `SECURITY.md` (repo root) — coordinated-disclosure policy.
- `docs/security/threat-model.md`, `docs/security/pentest-punch-list.md`, `docs/security/evidence/` (SBOM + matrix MD + scanner summaries).

Edited (extend, do not duplicate):
- `apps/api/forge_api/main.py` — mount middlewares; gate docs by env.
- `apps/api/forge_api/deps.py` — `Principal.role` default → `VIEWER`.
- `apps/api/forge_api/settings.py` — new `FORGE_RATELIMIT_*`, `FORGE_MAX_BODY_BYTES`, `FORGE_SSRF_*`, `FORGE_OUTBOUND_ALLOWLIST` keys.
- `packages/knowledge-core/forge_knowledge/embeddings.py`, `reranker.py`; `packages/mcp-sdk/forge_mcp/transport.py` — optional `url_validator` DI param.
- `.github/workflows/ci.yml` — new `security` job + `pnpm audit` in `web`.
- `pyproject.toml` (root) — `security` pytest marker; `[tool.bandit]`; dev-group tools (`bandit`, `semgrep`, `pip-audit`, `cyclonedx-py`).
- `apps/web/next.config.*` — security headers/CSP.
- `.gitignore` — add `deploy/secrets/`.
- `docs/self-hosting/security.md` — extend rotation runbook + enforcement-matrix summary + SSRF/rate-limit operator knobs.
- `Makefile` — `security` target.

Audited (asserted, mostly unchanged): `apps/api/forge_api/auth/{rbac,vault,crypto,apikeys}.py`, `routers/_rbac.py`, `packages/mcp-sdk/forge_mcp/security.py`, `packages/policy-sdk/forge_policy/evaluator.py`, `packages/integration-sdk/forge_integrations/webhooks.py`, `apps/api/forge_api/observability/{audit,redaction}.py`.

## 11. Research references

- FORGE_SPEC `docs/FORGE_SPEC.md` → "Security" table (RBAC roles, secret redaction, MCP read-only default, encrypted-at-rest BYOK, immutable audit log, per-workspace isolation), "MCP Security Rules", "Production Docker Compose Requirements" (network segmentation).
- MORNING_REPORT `docs/MORNING_REPORT.md` → §1.15 (auth/RBAC + Fernet/OAuth PARKED), §5(6,7) (crypto/secret-key), §6 ("Provider/transport realism… No interaction with a real external system"), §2.3 (security pass in `test_security_fixes.py`).
- SPEC-PRODUCTION-HARDENING `SPEC-PRODUCTION-HARDENING.md` → HARD-09 workstream, gates **G-SEC-AUTOMATED** / **G-SEC-EVIDENCE**, DoD BETA #11 + PROD #20, "Credentials & secrets handling" rules 1–7, the "human pentest cannot be done by agents" ceiling.
- Tooling: bandit (PyCQA), semgrep (`p/python` + custom rules), `pip-audit` (PyPA), `osv-scanner` (Google OSV), gitleaks, CycloneDX (`cyclonedx-py`), `pnpm audit`.
- Standards: OWASP ASVS (verification checklist source for the matrix), OWASP Top 10 (SSRF = A10:2021), STRIDE threat-modeling, RFC 8707 (OAuth resource indicators — already implemented in `forge_mcp.security.token_binding`), GitHub `SECURITY.md` / coordinated-disclosure conventions, cloud metadata SSRF (`169.254.169.254`).

## 12. Out of scope / future

- **The human penetration test and formal audit sign-off** — produced as a punch-list here; the engagement is external (the named residual of blocker #4).
- **SOC2 / ISO 27001 / compliance attestation** — out of scope for the codebase.
- **Distributed (Redis-backed) rate limiting** and a WAF — V1 ships a per-instance token bucket; a shared limiter is future.
- **DAST / fuzzing** (e.g. schemathesis against the OpenAPI surface, ZAP) — future automated dynamic testing beyond the static SAST/dep/secret scanners here.
- **Signed releases / image attestation (cosign, SLSA provenance, signed SBOM)** — pairs with HARD-08's image digest pinning; future.
- **Container image CVE scanning (trivy/grype)** of the built images — depends on HARD-08 actually building the images; future once `docker compose build` runs.
- **Secrets management backends** (Vault/KMS/SOPS) beyond the env + encrypted DB vault — future; HARD-10 owns the crypto/secret-key seam this slice audits.
- **Full RBAC depth** (multi-team, conditional policy) — F29/F30 (V3); HARD-09 only enforces the V1 four-role matrix.
