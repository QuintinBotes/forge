# Security Findings — automated-scan triage (HARD-09)

This is the honest triage of the automated security audit run for HARD-09.
Each finding is either **FIXED** (with a regression test) or **ACCEPTED** (with
a reason and, where it gates CI, a dated entry in
[`security/waivers.yaml`](security/waivers.yaml)).

Scanners run: `bandit` (SAST), `pip-audit` (Python CVE), `semgrep` with custom
Forge rules (`.semgrep/forge.yml`), `gitleaks` (secret scan over full history),
and `cyclonedx-py` (SBOM). Baseline date: **2026-07-04**. Reproduce with
`make security` (or the individual commands in §7 of the slice doc).

---

## Summary

| Scanner | Scanned | High/Critical | Medium | Low | Action |
|---|---|---|---|---|---|
| pip-audit | full `uv.lock` (frozen export) | 0 | 0 | 0 | clean |
| bandit | `packages` + `apps` (tests excluded) | 1 → **fixed** | 5 | (B101 skipped) | 1 fixed, rest triaged |
| semgrep (custom) | first-party tree | 0 | 0 | 0 | clean |
| semgrep (`p/python`) | first-party tree | 0 blocking | — | — | clean |
| gitleaks | 49 commits, full history | 0 real | — | 14 fixtures/docs/dev-defaults | allowlisted |
| CycloneDX SBOM | full environment | — | — | — | 189 components committed |

No real secret appears in source, tests, fixtures, logs, `uv.lock`, or
`pnpm-lock.yaml`.

---

## bandit

### FIXED

- **B324 (HIGH) — weak SHA-1 hash**, `packages/knowledge-core/forge_knowledge/embeddings.py:62`.
  The deterministic embedding uses SHA-1 to bucket tokens (feature hashing), not
  for security. **Fixed** by passing `usedforsecurity=False`, which both states
  the intent and clears the finding. This is behaviour-preserving (same bucket
  assignment) and covered by the existing `test_embeddings.py` determinism tests.

### ACCEPTED (triaged, not security issues)

- **B108 (MEDIUM) — "/tmp" literal**, `forge_agent/sandbox/container.py:84`
  (waiver W-001). The string is a tmpfs **mount target** inside a container
  spec, not host temp-file usage. No host path is created or raced.
- **B608 (MEDIUM) ×2 — SQL string construction**, migrations `0019` and `0022`
  (waivers W-002, W-003). Both interpolate a module-level table-name constant in
  one-shot Alembic DDL/data migrations; no runtime/user input can reach them.
- **B310 (MEDIUM) — urlopen scheme audit**,
  `forge_marketplace/registry_client.py:110` (waiver W-004). The registry
  client already resolves and validates every host against its own SSRF
  blocklist + operator allowlist before fetching; the residual redirect-to-
  internal concern is tracked as punch-list item **PT-07**.
- **B101 (LOW) — assert_used**, 26 sites in first-party source (waiver W-006,
  globally skipped in `[tool.bandit]`). Forge's production images run CPython
  **without** `-O`, so asserts are not stripped; the flagged asserts are
  internal state-machine invariants, not auth/authz checks. Test-tree asserts
  are excluded from the scan entirely.
- **B105/B106 (LOW) — "hardcoded password" false positives**. Every hit is an
  enum member or constant named `*_token` / `*_secret` (e.g.
  `APIKeyKind.INTEGRATION_TOKEN`, a webhook header name, an OAuth `token_url`),
  or a metrics label — none is a credential value. Left as low-noise; not
  waived because they are LOW and the gate only blocks on high/critical.
- **B404/B603/B607 (LOW) — subprocess usage**. The agent sandbox and git
  runners shell out with **argv lists** (never `shell=True`; the custom semgrep
  rule `forge-no-subprocess-shell-true` enforces this). Accepted as the intended
  design; no shell injection surface.
- **B311 (LOW) — non-crypto `random`**. Used for jitter/backoff and test data,
  never for tokens or keys (those use `secrets` / `os.urandom` via
  `forge_api.auth.crypto`). Accepted.

---

## pip-audit

Clean: **0 known vulnerabilities** across the frozen `uv.lock` export
(`uv export --frozen --format requirements-txt --no-emit-workspace | pip-audit`).
The dependency set was re-locked to latest during the Python 3.14 migration, so
there is no accepted-CVE waiver at baseline. A newly disclosed CVE will fail the
`security` CI job until fixed or waived (dated) in `security/waivers.yaml`.

---

## semgrep (custom Forge rules)

`.semgrep/forge.yml` encodes eight Forge invariants as SAST so they cannot
regress: no `subprocess(shell=True)`, no `verify=False`, no unsafe `yaml.load`,
no `MCPConnection(allow_write=True)` literal, no `eval`/`exec`, no
literal-keyed cipher, no logging of secret-named values. The rules fire on the
positive fixtures in `tests/security/fixtures/bad/` and on **zero** lines of the
first-party tree (`test_semgrep_rules_fire_on_bad_and_not_on_good`). Clean at
baseline.

---

## gitleaks

14 matches over the full 49-commit history, **all triaged as non-secrets** and
allowlisted narrowly in [`.gitleaks.toml`](.gitleaks.toml):

- 11 are **deliberately-fake fixture secrets** that exist to exercise the
  redaction layer (e.g. `test_obs_redaction.py`, `test_mcp_redaction.py`,
  `forge_mcp/testing.py`) — redaction tests are worthless without a realistic
  fake secret to redact.
- 2 are **spec/prose** in `docs/implementation-slices/**` that pattern-match the
  generic-api-key rule but contain no real value.
- 1 is `deploy/.env.dev:22` — a documented **DEV-ONLY** `FORGE_SECRET_KEY` whose
  file header states it must never be used in production; the production
  compose/helm paths require an operator-provided secret, and the API refuses to
  boot in a non-development environment without one
  (`check_secret_key_required_prod`). Waiver **W-005**.

Each allowlist entry is a **named file or a specific fixture path**, never a
blanket directory wildcard, and a gitleaks rule additionally fails the build if
`deploy/secrets/`, `.env*` (bar the reviewed examples), `*.pem`, or `*.key` are
ever staged.

---

## SBOM

A CycloneDX SBOM (`docs/security/evidence/sbom.cdx.json`, 189 components) is
generated from the resolved environment and committed to the evidence pack. It
lists every first-party workspace package alongside every third-party
dependency (`test_committed_sbom_lists_workspace_packages`).

---

## Regression coverage

Every fixed finding above and every enforcement control is pinned by
`tests/security/` (`uv run pytest -m security`). The SHA-1 fix is covered by the
knowledge determinism tests; the SSRF/rate-limit/body-limit/docs-lockdown/
principal-default fixes each have a dedicated matrix row; the semgrep rules and
waiver-expiry logic have positive+negative fixtures. A regression re-introduces
a red test or a red scanner, not a silent hole.
