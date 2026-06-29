# HARD-12 — Release Engineering, Supply-Chain & the Automated RELEASE_READINESS Gate

> Phase: hardening · Blocker(s): **#4** (no real security audit — this slice delivers the supply-chain half: source SBOM, build provenance/signing, the `SECURITY.md` disclosure policy) and **#6** (maturity gaps — there is no versioning discipline, no CHANGELOG, no release process, and no single machine-checkable "are we releasable?" gate). · Gates realized: this slice does not *own* any single product gate — it builds the **meta-gate** that mechanically checks **every** other gate (G-DB, G-MODEL, G-RAG-REAL, G-GH, G-MCP, G-SLACK, G-BUILD, G-TYPES, G-SEC-AUTOMATED, G-CRYPTO, G-IMG-PINNED, G-PARKED-CLOSED, G-PERF, G-MIGRATE, G-SOAK, G-COVERAGE, G-SEC-EVIDENCE, G-FWD-COMPAT) defined in `SPEC-PRODUCTION-HARDENING.md`, plus it adds the supply-chain evidence (source SBOM + provenance) that feeds **G-SEC-EVIDENCE**. · Status target: **"verified"** has two halves. The **offline half** (single-source-of-truth versioning + a consistency guard test; CHANGELOG generated from the conventional-commit history; `SECURITY.md` / `CONTRIBUTING.md` / `CODE_OF_CONDUCT.md` present and linted; the `forge-release-readiness` engine + its `release/gates.yaml` manifest + unit tests; a source-tree CycloneDX SBOM; `forge-release-readiness --bar beta` runs and renders `RELEASE_READINESS.md`) **lands and is green in the hermetic suite and is part of the whole-suite green gate** — no external credentials required. The **release half** (Sigstore/`cosign` keyless signing, SLSA build-provenance attestation, `gh release create`, wheel publish) runs only on a **networked CI runner using GitHub's native OIDC + `GITHUB_TOKEN`** — it **CANNOT** run in the no-network sandbox and needs **no user-supplied external creds**. Two readiness items are **human-only and stay `MANUAL-PENDING` forever until a signed attestation is filed**: the 3rd-party human pentest and the real multi-week multi-tenant fleet soak — the gate surfaces them honestly and never auto-greens them.

---

## 1. Intent — what & why

The overnight ALPHA produces a tree that *tests* well but has **no release engineering at all**. Concretely, verified against the real repo at report time:

1. **Version is duplicated 30+ ways with no source of truth.** The string `0.1.0` is hand-written into the root `pyproject.toml` `[project].version`, all **16** member `pyproject.toml` files (`packages/*` + `apps/{api,worker,mcp-gateway}`), the root `package.json`, `apps/web/package.json` (`@forge/web`), **10** package `__init__.py` `__version__` constants, and the compose default `FORGE_VERSION:-0.1.0` in `deploy/docker-compose.yml`. Nothing keeps them in sync; a release would silently ship mismatched versions. `git tag` is **empty** — there has never been a tagged release.
2. **There is no CHANGELOG and no release notes.** The commit history is *already* largely conventional-commit shaped (`feat(F17): …`, `build(web): …`, `docs: …`, `feat(H5): …`) so a changelog is derivable — but nothing derives it, nothing enforces the format going forward, and a couple of early commits (`checkpoint: phase 1 partial build`) do not conform.
3. **There is no release workflow.** `.github/workflows/ci.yml` runs lint/type/test/compose-config on every push, but there is no tag-triggered pipeline that cuts a version, builds + signs artifacts, generates provenance, and publishes a GitHub Release. Release would be a manual, unauditable act.
4. **There is no supply-chain evidence for the *source tree*.** HARD-07 (container build + pin) generates a CycloneDX SBOM **per built image** and a digest `build-manifest.json` — that is the *runtime* dependency surface. But there is no SBOM for the **source dependency closure** (`uv.lock` + `pnpm-lock.yaml`), no **build provenance** (who/what/which-commit built an artifact), and **no signing** (HARD-07 explicitly deferred image signing / SLSA provenance to "future"). Blocker #4's security audit needs all three.
5. **There is no project governance surface.** No `SECURITY.md` (vulnerability-disclosure policy + supported-versions table), no `CONTRIBUTING.md`, no `CODE_OF_CONDUCT.md`. For an OSS platform a serious team is meant to "adopt, self-host, audit, and trust," these are table stakes and are part of the security posture (a clear private-disclosure channel is the #1 ask of any auditor).
6. **Most importantly: there is no single answer to "is Forge releasable?"** `SPEC-PRODUCTION-HARDENING.md` defines three named bars (ALPHA/BETA/PRODUCTION) and 18 lettered gates / 22 DoD items, each "met only when **every** gate under it is green from real command output (evidence captured), not asserted." Today that judgement is a human reading prose. This slice turns it into **one command** that runs/inspects every gate and emits a dated, evidenced, pass/fail `RELEASE_READINESS.md`.

**Why now.** Blockers #4 and #6 are the difference between "green tests" and "a release a downstream team can trust." This slice is the **capstone** of the hardening program: it does not invent product behavior, it (a) makes versioning/changelog/release **disciplined and reproducible**, (b) adds the **supply-chain attestations** an auditor requires, (c) ships the **governance files** that define how vulnerabilities are reported and how contributions flow, and (d) builds the **automated meta-gate** that mechanically encodes the spec's release-readiness model so no PRODUCTION claim can ever be made on un-evidenced or simulated work. It EXTENDS `packages/evaluation` (`forge_eval` — which already owns the regression-gate/scorecard concept), the root build metadata (`pyproject.toml`, `package.json`, `Makefile`), `.github/workflows/`, and adds root governance docs. It touches **no `forge_db` schema** and adds **no `forge_contracts` DTOs/Protocols**.

## 2. User-facing / operator behavior

This is a maintainer / release-manager / auditor-facing slice; there is no end-user runtime UI change. Observable behavior:

- **Maintainer — cut a release with one command.** From a clean `main`, a maintainer runs `make bump` (→ `uv run cz bump`). Commitizen reads the conventional-commit history since the last tag, computes the next SemVer (`feat:` → minor, `fix:` → patch, `BREAKING CHANGE:`/`!` → major), rewrites the version in **every** version-bearing file in lockstep, regenerates `CHANGELOG.md`, commits, and creates an annotated git tag `vX.Y.Z`. Pushing the tag triggers the release workflow.
- **Maintainer / CI — the release pipeline is auditable and signed.** On a `v*` tag, `.github/workflows/release.yml` re-runs the full green gate, runs `forge-release-readiness --bar production --check` (the release **fails** if any production gate is not green/attested), builds the 4 images (reusing HARD-07's `make build-images` + `make pin-digests`), generates a **source-tree SBOM** and aggregates HARD-07's **per-image SBOMs**, produces **SLSA build-provenance attestations**, **signs** the images and the SBOM with `cosign` (keyless, GitHub OIDC), and publishes a **GitHub Release** whose notes are the new `CHANGELOG.md` section, attaching the SBOMs, `build-manifest.json`, `RELEASE_READINESS.md`, and the signed wheels — all without any human-supplied secret.
- **Contributor — the rules are written down and enforced.** `CONTRIBUTING.md` documents the dev setup (`make setup`), the exact green-gate commands, and the **conventional-commit requirement**; a `commit-msg` hook (`cz check`) and a CI lint step reject non-conforming commit messages so the changelog is always derivable. `CODE_OF_CONDUCT.md` (Contributor Covenant) and a PR template/`CODEOWNERS` set expectations.
- **Security reporter — a clear, private channel.** `SECURITY.md` tells a researcher exactly how to report a vulnerability privately (GitHub private security advisory + a contact), the supported-version policy (tied to SemVer), and the response SLA — and points operators to the hardening/rotation runbook (`docs/self-hosting/security.md`, owned by the security-audit slice / HARD-09) and the pentest punch-list.
- **Release manager / auditor — one honest readiness report.** Anyone runs `make release-readiness` (→ `forge-release-readiness --bar production`) and gets `RELEASE_READINESS.md`: a dated table of every gate (id · blocker · owning workstream · **status** · evidence command/artifact · last-checked), an overall **MET / NOT MET** verdict for the requested bar, and a verbatim footer naming the human-only asterisks (pentest, fleet soak, SOC2). The exit code is non-zero if the bar is not met, so it doubles as a CI gate and a human report.

## 3. Vertical slice

### 3.1 Data model

**No database tables, columns, or Alembic migrations. No `forge_db` change. No `forge_contracts` DTO/Protocol.** This slice is release tooling and governance; nothing it produces is request-time runtime state. The only persisted artifacts are **repo-committed evidence/config files** (not DB rows):

- `CHANGELOG.md` (root, generated, committed) — Keep-a-Changelog-format, derived from conventional commits.
- `RELEASE_READINESS.md` (root, generated, committed at release time) — the rendered gate report.
- `release/gates.yaml` (root, hand-authored) — the gate manifest the readiness engine consumes (schema in §4).
- `release/attestations/*.yaml` (root, hand-authored, signed-off) — human-only gate attestations (pentest, fleet soak).
- `release/sbom/forge-source.cdx.json` (generated) — the **source-tree** CycloneDX SBOM (distinct from HARD-07's per-image SBOMs under `deploy/sbom/`).
- `release/provenance/*.intoto.jsonl` (generated in CI) — SLSA build-provenance attestations.

The in-process state the readiness engine builds (a list of `GateResult`s) lives only for the duration of the command and is rendered to Markdown + an exit code.

### 3.2 Backend (FastAPI)

**No route, schema, service, or `apps/api` change.** The readiness engine does **not** run inside the API process and exposes no HTTP surface — it is a build/CI tool. It is, however, allowed to *invoke* the API's test/lint commands as gate checks (e.g. `uv run pytest -m postgres`), which is subprocess execution, not an import-time dependency on `forge_api`. No anonymous endpoint, no new auth surface.

### 3.3 Worker / agent runtime

**No `forge_worker` / `forge_agent` code change.** The readiness gate *checks* worker/agent-runtime coverage (G-COVERAGE) by reading the coverage report those suites already emit, but it does not modify them. The coverage-raising and typecheck work themselves are owned by the spec's coverage/typecheck workstream (spec §HARD-12, surfaced here as the `G-COVERAGE`/`G-TYPES` gate rows) — this slice only consumes its evidence.

### 3.4 Frontend (Next.js)

**No component or UX change.** The only `apps/web` touch is **version metadata**: `apps/web/package.json` (`@forge/web`) version is added to the single-source-of-truth `version_files` set so `cz bump` keeps it in lockstep with the Python packages (today it is a hand-maintained `0.1.0`). The web build itself (`pnpm -r build` / `next build`) is unchanged and is consumed by the readiness gate as part of `G-BUILD`.

### 3.5 Infra / deploy / CI

This is the bulk of the slice. **EXTEND the existing files — do not fork.**

**(a) Single-source-of-truth versioning via Commitizen.** Add a `[tool.commitizen]` block to the **root `pyproject.toml`** with `version_provider = "pep621"` (so the root `[project].version` is the one true version) and a `version_files` list naming **every** file that carries the version. `cz bump` then bumps all of them atomically:

```toml
[tool.commitizen]
name = "cz_conventional_commits"
version_provider = "pep621"          # source of truth = root [project].version
tag_format = "v$version"
update_changelog_on_bump = true
changelog_incremental = true
major_version_zero = true            # we are pre-1.0; feat == minor within 0.x
version_files = [
    "packages/contracts/pyproject.toml:^version",
    "packages/db/pyproject.toml:^version",
    "packages/workflow-engine/pyproject.toml:^version",
    "packages/agent-runtime/pyproject.toml:^version",
    "packages/multi-agent-coordinator/pyproject.toml:^version",
    "packages/spec-engine/pyproject.toml:^version",
    "packages/board-core/pyproject.toml:^version",
    "packages/knowledge-core/pyproject.toml:^version",
    "packages/integration-sdk/pyproject.toml:^version",
    "packages/mcp-sdk/pyproject.toml:^version",
    "packages/policy-sdk/pyproject.toml:^version",
    "packages/skill-sdk/pyproject.toml:^version",
    "packages/evaluation/pyproject.toml:^version",
    "apps/api/pyproject.toml:^version",
    "apps/worker/pyproject.toml:^version",
    "apps/mcp-gateway/pyproject.toml:^version",
    "package.json:version",
    "apps/web/package.json:version",
    "packages/contracts/forge_contracts/__init__.py:__version__",
    "packages/db/forge_db/__init__.py:__version__",
    "packages/workflow-engine/forge_workflow/__init__.py:__version__",
    "packages/spec-engine/forge_spec/__init__.py:__version__",
    "packages/board-core/forge_board/__init__.py:__version__",
    "packages/knowledge-core/forge_knowledge/__init__.py:__version__",
    "packages/integration-sdk/forge_integrations/__init__.py:__version__",
    "packages/mcp-sdk/forge_mcp/__init__.py:__version__",
    "packages/policy-sdk/forge_policy/__init__.py:__version__",
    "packages/evaluation/forge_eval/__init__.py:__version__",
    "deploy/docker-compose.yml:FORGE_VERSION:-",
]
```

`commitizen` is added to the root **dev dependency group** (`[dependency-groups].dev`) so `uv run cz …` works with no global install. The handful of packages whose `__init__.py` lacks `__version__` today get a `__version__ = "0.1.0"` added so the set is uniform (a guard test, §6.2, enforces uniformity).

**(b) Conventional-commit enforcement.** A `commit-msg` git hook runs `uv run cz check --commit-msg-file "$1"`; it is installed via a `make hooks` target (and documented in `CONTRIBUTING.md`). A CI lint step (`cz check --rev-range origin/main..HEAD` on PRs) blocks non-conforming commits so the changelog stays derivable.

**(c) CHANGELOG.** `uv run cz changelog` (run by `cz bump`) writes/extends root `CHANGELOG.md` in Keep-a-Changelog style, grouped by type. The **first** changelog is backfilled from the existing history; the few non-conforming early commits are curated by hand into an `Initial / pre-history` section. (`git-cliff` with a `cliff.toml` is documented in §12 as a drop-in alternative for richer templating; commitizen is the chosen default because it does commit-lint + bump + changelog as one Python-native tool.)

**(d) The release-readiness engine** — a new subpackage of the existing `forge_eval`, **not a new top-level package**: `packages/evaluation/forge_eval/release/` with `readiness.py` (engine), `model.py` (typed `Gate`/`GateResult`/`Bar` dataclasses), `checks.py` (the `command`/`evidence`/`manual` check runners), and `render.py` (Markdown rendering). A console entry point `forge-release-readiness` is declared in `packages/evaluation/pyproject.toml [project.scripts]`. It reads `release/gates.yaml`, runs/inspects each selected gate, writes `RELEASE_READINESS.md`, and returns a CI-grade exit code. (See §4 for the full CLI + manifest + status model.)

**(e) Source SBOM + provenance + signing scripts.** `release/scripts/source-sbom.sh` runs `syft dir:. -o cyclonedx-json=release/sbom/forge-source.cdx.json` (the dependency closure from `uv.lock` + `pnpm-lock.yaml`). Signing/provenance are CI-native and live in the release workflow (below), not in a committed secret-bearing script.

**(f) Makefile targets** (extend the existing `Makefile`): `bump`, `changelog`, `hooks`, `release-readiness`, `source-sbom` (signatures in §4).

**(g) CI** (`.github/workflows/`):
- **Extend `ci.yml`**: add a `commitlint` step (`uv run cz check` over the PR range) to the `python` job, and a new fast **`readiness`** job that runs `forge-release-readiness --bar beta --report-only` on every PR and uploads `RELEASE_READINESS.md` as an artifact (non-blocking on PRs; the offline gates within it are already blocking via the suite). Also **pin every `uses:` action by commit SHA** (today they float on `@v4`/`@v5` tags — a supply-chain hole; a guard test, §6.10, enforces SHA-pinning).
- **New `release.yml`** (trigger: `push: tags: ['v*']`, plus `workflow_dispatch`): permissions `contents: write`, `id-token: write`, `attestations: write`, `packages: write`. Steps: checkout → `uv sync --frozen` → **assert the tag equals `cz version`** → run the full green gate (lint/format/typecheck/pytest/web) → `forge-release-readiness --bar production --check` (blocking) → `make build-images` + `make pin-digests` (HARD-07) → `make sbom` (HARD-07 per-image) + `make source-sbom` (this slice) → **`actions/attest-build-provenance@<sha>`** for the wheels + images → **`cosign sign`** (keyless OIDC) the images and **`cosign attest`** the SBOMs → `uv build --all-packages` (wheels) + sha256sums → `gh release create "v$VERSION" --notes-file <changelog-section> <artifacts…>`. PyPI/registry *publish* is feature-flagged off by default (see §12).
- **Optional `scorecard.yml`** (OpenSSF Scorecard) on a schedule for supply-chain posture (documented, off the critical path).

## 4. Public interfaces / contracts (exact signatures, env vars, config keys)

This slice adds **no `forge_contracts` Pydantic DTO and no Protocol** to the frozen contract set, and **no FastAPI schema**. Its public surface is CLI commands, a gate-manifest schema, and CI/Make entry points.

**CLI: `forge-release-readiness`** (console script → `forge_eval.release.readiness:main`):

```
forge-release-readiness [--bar {alpha,beta,production}]   # which bar must be MET (default: beta)
                        [--manifest release/gates.yaml]   # gate manifest path
                        [--out RELEASE_READINESS.md]      # rendered report path ("-" = stdout)
                        [--check]                         # exit 1 if the bar is NOT MET (CI mode)
                        [--report-only]                   # always exit 0; just render (PR mode)
                        [--only G-DB,G-TYPES]             # run a subset (debug)
                        [--timeout-seconds 1800]          # per-command-gate timeout
                        [--json]                          # also emit machine-readable JSON to stderr
```

**Gate-manifest schema** — `release/gates.yaml` (hand-authored; the readiness engine's only input besides the live tree):

```yaml
# Bars are cumulative: production ⊇ beta ⊇ alpha. A bar is MET iff every gate
# at-or-below it is GREEN or MANUAL_ATTESTED.
gates:
  - id: G-DB
    bar: beta
    blocker: 6
    workstream: HARD-01           # real Postgres + pgvector
    title: Real Postgres + pgvector exercised (migrations + -m postgres suite)
    check:
      kind: command               # command | evidence | manual
      run: "uv run pytest -m postgres -q"
      required_env: [FORGE_TEST_DATABASE_URL]   # absent → SKIPPED_NO_CREDS (not GREEN)
  - id: G-TYPES
    bar: beta
    blocker: 6
    workstream: HARD-12-typecheck
    title: Whole-workspace mypy is one green command
    check: { kind: command, run: "make typecheck" }
  - id: G-IMG-PINNED
    bar: production
    blocker: 3
    workstream: HARD-07
    title: Every pulled image pinned by @sha256
    check:
      kind: evidence
      artifact: deploy/build-manifest.json
      predicate: { type: json_all, path: "images.*.digest", matches: "^sha256:[0-9a-f]{64}$" }
      max_age_days: 30            # STALE if older
  - id: G-SEC-EVIDENCE
    bar: production
    blocker: 4
    workstream: HARD-09
    title: Security evidence pack present (SBOM + audit + enforcement matrix + punch-list)
    check:
      kind: evidence
      all_of:
        - { artifact: release/sbom/forge-source.cdx.json, predicate: { type: cyclonedx_components_min, min: 1 } }
        - { artifact: docs/self-hosting/security.md, predicate: { type: exists } }
  - id: G-PENTEST                 # human-only — never auto-green
    bar: production
    blocker: 4
    workstream: external
    title: 3rd-party human penetration test (NAMED HONEST ASTERISK)
    check:
      kind: manual
      attestation: release/attestations/pentest.yaml   # signed_off:true|false, by, date, link
  - id: G-SOAK-FLEET             # human/real-fleet-only — never auto-green
    bar: production
    blocker: 6
    workstream: external
    title: Real multi-week multi-tenant fleet soak (NAMED HONEST ASTERISK)
    check: { kind: manual, attestation: release/attestations/fleet-soak.yaml }
```

> The shipped `gates.yaml` enumerates **all 18 spec gates** (G-DB, G-MODEL, G-RAG-REAL, G-GH, G-MCP, G-SLACK, G-BUILD, G-TYPES, G-SEC-AUTOMATED, G-CRYPTO, G-IMG-PINNED, G-PARKED-CLOSED, G-PERF, G-MIGRATE, G-SOAK, G-COVERAGE, G-SEC-EVIDENCE, G-FWD-COMPAT) **plus** the two named human-only items (`G-PENTEST`, `G-SOAK-FLEET`) — a guard test (§6.5) asserts the manifest covers every gate named in `SPEC-PRODUCTION-HARDENING.md`.

**Check kinds & status model** (`forge_eval.release.model`):

| `check.kind` | How it resolves | GREEN when |
|---|---|---|
| `command` | run shell command, capture exit + output tail (bounded by `--timeout-seconds`); if any `required_env` is unset, **do not run** → `SKIPPED_NO_CREDS` | exit 0 |
| `evidence` | stat the `artifact`(s), enforce `max_age_days` freshness, run the `predicate` (`exists` / `json_all` / `json_path_eq` / `regex` / `cyclonedx_components_min` / `coverage_min`) | predicate true & fresh |
| `manual` | read the signed `attestation` YAML | `signed_off: true` with `by` + `date` + `link` → `MANUAL_ATTESTED`; else `MANUAL_PENDING` |

`GateResult.status ∈ {GREEN, RED, SKIPPED_NO_CREDS, STALE, MANUAL_PENDING, MANUAL_ATTESTED, MISSING_EVIDENCE}`. **A bar is MET iff every selected gate is `GREEN` or `MANUAL_ATTESTED`.** `MANUAL_PENDING`, `RED`, `SKIPPED_NO_CREDS`, `STALE`, `MISSING_EVIDENCE` ⇒ NOT MET. The engine **never** infers GREEN for a `manual` gate (the pentest can only go green via a real signed attestation file) — this is the structural guarantee that PRODUCTION is never claimed on un-evidenced work.

**Rendered `RELEASE_READINESS.md`** carries: header (target bar · overall verdict · UTC timestamp · `git rev-parse HEAD` · `cz version`), one table per bar with columns `Gate | Blocker | Workstream | Status | Evidence (cmd/artifact) | Last-checked`, and a verbatim footer reproducing the spec's honest asterisk:
> *"Code- and evidence-ready for production, pending an external human penetration test and a real multi-week multi-tenant fleet soak — neither performable by the build agents; both are named, scoped, and handed off."* Compliance attestation (SOC2 etc.) is out of scope.

**Attestation file schema** — `release/attestations/<gate>.yaml`:
```yaml
gate: G-PENTEST
signed_off: false           # flip to true ONLY after the real engagement
by: ""                      # name + org of the human attester
date: ""                    # ISO-8601
link: ""                    # URL to the report / engagement record
notes: ""
```

**Env vars / config keys** (no secrets; CI-native only):
| Name | Where | Default | Purpose |
|---|---|---|---|
| `FORGE_VERSION` | compose / release tag | `0.1.0` (cz-managed) | image tag; kept in lockstep by `cz bump` |
| `RELEASE_READINESS_BAR` | CI | `beta` | default bar for the `readiness` job |
| `COSIGN_EXPERIMENTAL` | release CI | `1` | keyless OIDC signing (no key material) |
| `GITHUB_TOKEN` / OIDC id-token | release CI | provided by GitHub | release creation + attestation + keyless signing (built-in, **not** user-supplied) |

**Makefile targets** (added): `make bump` → `uv run cz bump`; `make changelog` → `uv run cz changelog`; `make hooks` → install the `commit-msg` hook; `make release-readiness` → `uv run forge-release-readiness --bar production`; `make source-sbom` → `release/scripts/source-sbom.sh`.

## 5. Dependencies (other slices/foundation that must exist first)

- **Foundation tooling (overnight-plan Task 0.1)** — REQUIRED, present: root `pyproject.toml` (`[project].version`, `[dependency-groups].dev`), `Makefile`, `ruff.toml`, `pyproject` pytest config + markers, `.github/workflows/ci.yml`. This slice extends all of them.
- **`packages/evaluation` (`forge_eval`)** — REQUIRED, present: hosts the new `forge_eval.release` subpackage and the `forge-release-readiness` console script. Chosen because `forge_eval` already owns the "aggregate gate that blocks release" concept (golden-set regression gate + scorecard), so the readiness meta-gate is a natural, non-duplicating extension — **no new top-level package** is created (foundation rule).
- **`SPEC-PRODUCTION-HARDENING.md`** — REQUIRED, present: the *source of truth* for the gate set, bar definitions, and the verbatim honest-asterisk text. `release/gates.yaml` is the machine encoding of that spec; the guard test (§6.5) keeps them in sync.
- **HARD-07 (container build + image SBOM + digest manifest)** — SOFT / downstream-coupled: the `G-BUILD` and `G-IMG-PINNED` gate rows read `deploy/build-manifest.json` and the per-image SBOMs HARD-07 produces; the release workflow reuses HARD-07's `make build-images` / `make pin-digests` / `make sbom`. If HARD-07 has not landed, those gate rows simply report `MISSING_EVIDENCE` (honest), the readiness engine still runs, and the offline half of this slice is unaffected. This slice **adds** the signing/provenance HARD-07 deferred.
- **The security-audit slice (spec §HARD-09)** — SOFT / downstream-coupled: the `G-SEC-AUTOMATED` / `G-SEC-EVIDENCE` rows consume its SAST/secret-scan/dep-audit outputs and `docs/self-hosting/security.md` rotation runbook. This slice's root `SECURITY.md` is the **disclosure policy** (how to report) and cross-references that hardening **runbook** (how to harden/rotate) and the pentest punch-list — the two files are complementary, not duplicative, and the boundary is stated in both.
- **The coverage/typecheck slice (spec §HARD-12)** — SOFT: `G-TYPES`/`G-COVERAGE` rows consume `make typecheck` + the coverage report; this slice does not implement them, only checks them.
- **HARD-14 / dependency re-lock** — RECOMMENDED before a real release: a complete, `--frozen`-clean `uv.lock` (+ `pnpm-lock.yaml`) makes the source SBOM and the wheel build reproducible. Stated in the release report if not yet landed.
- **Frozen `forge_contracts` / `forge_db` schema** — UNAFFECTED: this slice adds no DTOs/Protocols and no tables (asserted by the whole-suite green gate).
- **No user-supplied external credentials and no other HARD slice's live integration is required.** The release half uses GitHub-native OIDC + `GITHUB_TOKEN` only.

## 6. Acceptance criteria (numbered, testable)

> Legend: **[offline]** runs in the hermetic suite with no network/creds (part of the whole-suite green gate); **[ci-net]** requires a networked CI runner + GitHub OIDC/`GITHUB_TOKEN` (no user creds), CANNOT run in the no-network sandbox; **[human]** can only be satisfied by a person filing a signed attestation.

1. **[offline]** `release/gates.yaml` parses and every entry validates against the §4 schema (`kind ∈ {command,evidence,manual}`, valid `bar`, `blocker`, `workstream`, well-formed `predicate`).
2. **[offline]** **Version single-source-of-truth:** a guard test (`tests/test_versioning.py`) asserts the root `[project].version`, all 16 member `pyproject.toml` versions, both `package.json` versions, and **every** package `__init__.__version__` are byte-identical, and that the compose `FORGE_VERSION` default matches; `[tool.commitizen].version_files` lists exactly that file set (no file carries a version that cz would not bump).
3. **[offline]** **`cz bump --dry-run`** computes the correct next SemVer from a seeded conventional-commit range (`feat:` → minor, `fix:` → patch, `feat!:`/`BREAKING CHANGE:` → major within the `major_version_zero` policy), and `cz check` accepts a conforming message and rejects `wip stuff` (unit-tested via the commitizen API/CLI).
4. **[offline]** **CHANGELOG generation** produces a Keep-a-Changelog `CHANGELOG.md` from the commit history with `Added`/`Fixed`/etc. groupings; running it twice is idempotent for an unchanged range.
5. **[offline]** **Gate-coverage guard** (`packages/evaluation/tests/release/test_gate_coverage.py`): every gate id named in `SPEC-PRODUCTION-HARDENING.md` (the 18 G-\* gates) appears in `release/gates.yaml`, and the two human-only items `G-PENTEST` + `G-SOAK-FLEET` are present and `kind: manual`.
6. **[offline]** **Bar-met logic** (unit): with a fake manifest + monkeypatched checks, a bar is MET iff every at-or-below gate is `GREEN`/`MANUAL_ATTESTED`; a single `RED`/`SKIPPED_NO_CREDS`/`STALE`/`MANUAL_PENDING` ⇒ NOT MET and exit code 1 under `--check`.
7. **[offline]** **Manual gates never auto-green** (unit): a `manual` gate with `signed_off:false` resolves `MANUAL_PENDING` and is rendered as a NAMED ASTERISK; flipping its attestation to a well-formed `signed_off:true` (with `by`+`date`+`link`) resolves `MANUAL_ATTESTED`; a malformed/missing attestation resolves `MANUAL_PENDING` (never GREEN).
8. **[offline]** **Creds/freshness handling** (unit): a `command` gate whose `required_env` is unset resolves `SKIPPED_NO_CREDS` **without** executing the command; an `evidence` gate whose artifact is older than `max_age_days` resolves `STALE`; a missing artifact resolves `MISSING_EVIDENCE`.
9. **[offline]** **Report rendering** (unit/golden): `forge-release-readiness --bar beta --out -` renders a Markdown table with the required columns, the overall verdict line, the git SHA + cz version header, and the verbatim honest-asterisk footer.
10. **[offline]** **Supply-chain hygiene of CI** (`tests/test_ci_supplychain.py`): every `uses:` in `.github/workflows/*.yml` is pinned to a 40-hex commit SHA (not a floating tag); `release.yml` declares `id-token: write` + `attestations: write` and contains a keyless `cosign sign` and an `attest-build-provenance` step.
11. **[offline]** **Governance files present + minimally valid**: root `SECURITY.md` (has a private-reporting channel + a supported-versions section referencing SemVer), `CONTRIBUTING.md` (has the green-gate commands + conventional-commit rule), and `CODE_OF_CONDUCT.md` (Contributor Covenant) exist and are lint-clean; `SECURITY.md` cross-links `docs/self-hosting/security.md`.
12. **[offline]** **Source SBOM**: `release/scripts/source-sbom.sh` produces `release/sbom/forge-source.cdx.json` that parses as valid CycloneDX with ≥1 component covering both Python (`uv.lock`) and Node (`pnpm-lock.yaml`) dependencies. (Syft present → real run; Syft absent in-sandbox → the test skips cleanly with a clear reason, mirroring the existing Postgres skip pattern.)
13. **[offline]** **Whole-suite green gate holds at slice end**: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `make typecheck` (exit 0), and `cd apps/web && pnpm test` all green, with the new `forge_eval.release` tests included and any Syft/network-dependent tests skipping cleanly.
14. **[ci-net]** On a seeded `v0.0.0-test` tag in CI, `release.yml` runs end-to-end: green gate passes, `forge-release-readiness --bar production --check` runs (may legitimately exit 1 until all prod gates are green — the workflow surfaces it), images build, a source SBOM + per-image SBOMs are produced, **SLSA provenance attestations** are generated, images + SBOM are **`cosign`-signed (keyless)**, wheels build with sha256sums, and a draft GitHub Release is created with the CHANGELOG section as notes and the SBOMs/manifest/readiness report attached.
15. **[ci-net]** **Provenance verifies**: `cosign verify-attestation` (and `gh attestation verify`) succeed against a signed release image using the GitHub OIDC identity, with no key material stored anywhere in the repo or CI secrets.
16. **[human]** `G-PENTEST` and `G-SOAK-FLEET` remain `MANUAL_PENDING` in `RELEASE_READINESS.md` until a real, signed attestation is filed — and the PRODUCTION bar reports **NOT MET** while either is pending, by design.

## 7. Test plan (TDD) — unit + integration (gated on env) + how to run

**Discipline.** Write the engine + guard tests first (they fail RED: there is no `forge_eval.release` module, no `release/gates.yaml`, version files disagree once you intentionally desync one, no governance files), then make them green by adding the code/config/docs. The release-workflow (`[ci-net]`) and human (`[human]`) paths are verified outside the sandbox and self-skip / self-report locally.

**Unit tests (pure, no network)** — `packages/evaluation/tests/release/`:
- `test_model.py` — `Bar` ordering (alpha ⊂ beta ⊂ production); `GateResult` status enum; bar-met aggregation (AC6).
- `test_checks.py` — `command` runner (exit-code → GREEN/RED; `required_env` unset → `SKIPPED_NO_CREDS` without running, AC8; timeout → RED); `evidence` predicates (`exists`, `json_all` regex over a json-path, `cyclonedx_components_min`, `coverage_min`, freshness `STALE`, missing `MISSING_EVIDENCE`, AC8); `manual` (signed/unsigned/malformed → MANUAL_ATTESTED/PENDING, AC7).
- `test_readiness.py` — end-to-end with a temp fake manifest + monkeypatched subprocess: `--check` exit codes (AC6), `--report-only` always 0, `--only` subset, `--bar` selection.
- `test_render.py` — golden Markdown: required columns, verdict line, header (git SHA/cz version), verbatim honest-asterisk footer (AC9).
- `test_gate_coverage.py` — manifest ⊇ all spec gate ids + the two human-only gates (AC5).

**Repo-level guard tests** — `tests/`:
- `tests/test_versioning.py` — version single-source-of-truth across all files + `version_files` completeness (AC2); a deliberate desync (via a tmp copy) is detected.
- `tests/test_changelog_and_commits.py` — `cz check` accepts/rejects sample messages (AC3); `cz changelog` over a tmp git range yields the expected sections and is idempotent (AC4). (Uses commitizen's Python API against a tmp repo; no network.)
- `tests/test_governance_files.py` — `SECURITY.md`/`CONTRIBUTING.md`/`CODE_OF_CONDUCT.md` exist and contain the required anchors (AC11).
- `tests/test_ci_supplychain.py` — all `uses:` SHA-pinned; `release.yml` permissions + cosign + provenance steps present (AC10).

**Env-gated tests** (skip-clean when the tool is absent, never faked):
- `tests/test_source_sbom.py` — `@pytest.mark.integration`; if `shutil.which("syft") is None` → `pytest.skip("requires Syft — not available in this environment")`; else run `source-sbom.sh` and assert valid CycloneDX ≥1 component spanning py+node (AC12).

**Release-workflow tests** — `[ci-net]`, exercised by the `release.yml` run itself on a throwaway tag (AC14–15); there is no in-sandbox unit for `gh release create` / `cosign` / OIDC — those are asserted by the workflow's own success and a `cosign verify-attestation` step, captured as release evidence.

**How to run.**
```bash
# Offline (hermetic, part of the green gate):
uv run pytest packages/evaluation/tests/release -q
uv run pytest tests/test_versioning.py tests/test_governance_files.py tests/test_ci_supplychain.py -q
uv run forge-release-readiness --bar beta --report-only --out -     # render to stdout, never fails

# Version + changelog (local, no network):
uv run cz bump --dry-run                # show the next version + changelog delta
make changelog                          # regenerate CHANGELOG.md

# Source SBOM (needs Syft; skips cleanly without it):
make source-sbom

# The release pipeline (CI only, on a v* tag):  see .github/workflows/release.yml
```

## 8. Security & policy considerations

- **Supply-chain provenance is the core security win (blocker #4).** Build provenance (SLSA via `actions/attest-build-provenance`) records *which commit, which workflow, which runner* produced each artifact; `cosign` keyless signing (Sigstore + GitHub OIDC) lets a consumer cryptographically verify an image/wheel came from this repo's CI and was not swapped. Combined with HARD-07's digest pinning, the chain is: pinned inputs → reproducible build → signed, attested outputs. **No signing key material is ever stored** — keyless OIDC means there is no private key to leak or rotate.
- **Source SBOM closes the auditor's first question.** `release/sbom/forge-source.cdx.json` (CycloneDX, from `uv.lock` + `pnpm-lock.yaml`) plus HARD-07's per-image SBOMs give a complete dependency-attack-surface picture and are the input to the security-audit slice's CVE gate and the human-pentest punch-list (G-SEC-EVIDENCE).
- **`SECURITY.md` is a security control, not just docs.** A clear, private vulnerability-disclosure channel (GitHub private security advisory) with a supported-versions policy and response SLA is what prevents 0-days from being dropped publicly; it is explicitly distinct from `docs/self-hosting/security.md` (operator hardening/rotation runbook, HARD-09) — the boundary is documented in both to avoid drift.
- **No secrets enter the release path.** The release workflow uses only GitHub-native OIDC + `GITHUB_TOKEN`; no user BYOK key, GitHub App `.pem`, Slack token, or `FORGE_SECRET_KEY` is read. The readiness engine **redacts** any captured command output before writing `RELEASE_READINESS.md` (re-applying the shared redaction filter defensively, per the program's cred-handling rule) so a gate command that incidentally echoes an env value cannot leak into a committed report.
- **CI action pinning.** Floating `@v4`/`@v5` action tags are a live supply-chain hole (a tag can be repointed to malicious code). This slice pins every `uses:` by commit SHA and enforces it with a guard test — protecting the very pipeline that signs releases.
- **Least privilege in CI.** `ci.yml` keeps `contents: read`; only `release.yml` is granted `contents: write` + `id-token: write` + `attestations: write`, and only on `v*` tags — minimizing the blast radius of a compromised workflow.
- **Honesty as a policy guarantee.** The readiness engine **structurally cannot** mark a `manual` gate green without a human-signed attestation file. This makes "PRODUCTION declared on simulated work" impossible by construction — exactly the ALPHA debt the program exists to retire — and the verbatim asterisk footer ships in every report and (via the workflow) in the GitHub Release notes.

## 9. Effort & risk (S/M/L + risks)

**Effort: M.** Mechanically broad but low-novelty. Rough split: versioning single-source (commitizen config + guard test) **S**; CHANGELOG + commit-lint hook + CI step **S**; governance docs (`SECURITY`/`CONTRIBUTING`/`CODE_OF_CONDUCT`) **S**; the `forge_eval.release` engine + manifest + unit tests **M** (the only real code); source SBOM script **S**; `release.yml` with signing/provenance + CI SHA-pinning **M** (verification is CI-only). The readiness engine is the load-bearing piece.

**Risks:**
- **CANNOT fully verify in the no-network sandbox (named limitation).** Keyless `cosign` signing, SLSA provenance attestation, `gh release create`, registry/PyPI publish, and `cosign verify-attestation` all need a networked runner + GitHub OIDC. AC14–15 are a **CI-only gate** — exactly the kind of gap the program names rather than hides. The offline half (AC1–13) lands and is green in-sandbox; the env-gated SBOM test skips cleanly without Syft. *(Low engineering risk, high visibility.)*
- **Human-only gates are unclosable by agents (named, by design).** `G-PENTEST` and `G-SOAK-FLEET` stay `MANUAL_PENDING`; PRODUCTION reports NOT MET until a person files a signed attestation. This is not a bug to fix — it is the honest ceiling the spec mandates, made mechanical. *(No mitigation; it is the point.)*
- **Changelog backfill from imperfect history.** A few early commits are not conventional (`checkpoint: …`). *Mitigation:* curate the first `CHANGELOG.md` by hand into an initial section and enforce the format from this slice forward via the `commit-msg` hook + CI `cz check`. *(Low.)*
- **Version desync regression.** Someone could add a 17th version-bearing file and forget `version_files`. *Mitigation:* the §6.2 guard test fails if any version string in the tree is not covered by `version_files` (it scans, it does not just compare a fixed list). *(Low.)*
- **Tool drift / coupling.** Commitizen/Syft/cosign version moves could change output. *Mitigation:* pin them (dev-dep + SHA-pinned action) and re-lock via HARD-14; the readiness engine itself depends only on the stdlib + PyYAML (already in the tree) so it stays robust. *(Low.)*
- **Manual gate freshness.** A signed pentest attestation could go stale (a release ships months later on an old sign-off). *Mitigation:* attestations carry a `date`; a `max_age_days` can be set on the manual gate so an expired sign-off reverts to `STALE` → NOT MET. *(Low.)*
- **Out-of-sandbox / human-only beyond this slice:** the pentest engagement and the multi-week fleet soak themselves (this slice only *tracks and gates on* them); SOC2/compliance attestation (explicitly out of scope, surfaced in the footer, never gated green).

## 10. Key files / paths (exact, in the real monorepo)

- `pyproject.toml` (root) — add `[tool.commitizen]` (version source-of-truth + `version_files`); add `commitizen` to `[dependency-groups].dev`.
- `tests/test_versioning.py` (NEW) — version single-source-of-truth + `version_files` completeness guard.
- `tests/test_changelog_and_commits.py` (NEW) — `cz check` / `cz changelog` behavior over a tmp git repo.
- `tests/test_governance_files.py` (NEW) — governance files present + required anchors.
- `tests/test_ci_supplychain.py` (NEW) — actions SHA-pinned; `release.yml` permissions/cosign/provenance present.
- `tests/test_source_sbom.py` (NEW) — `@pytest.mark.integration` Syft-gated source SBOM test.
- `CHANGELOG.md` (NEW, generated + curated) — Keep-a-Changelog from conventional commits.
- `SECURITY.md` (NEW, root) — vulnerability-disclosure policy + supported-versions; cross-links `docs/self-hosting/security.md`.
- `CONTRIBUTING.md` (NEW, root) — dev setup, green-gate commands, conventional-commit rule, release pointer.
- `CODE_OF_CONDUCT.md` (NEW, root) — Contributor Covenant.
- `.github/PULL_REQUEST_TEMPLATE.md` + `.github/CODEOWNERS` (NEW) — PR checklist + ownership (nice-to-have).
- `release/gates.yaml` (NEW) — the gate manifest (machine encoding of the spec's gates/bars).
- `release/attestations/{pentest,fleet-soak}.yaml` (NEW) — human-only gate attestations (default `signed_off:false`).
- `release/scripts/source-sbom.sh` (NEW, shellcheck-clean) — `syft dir:.` → `release/sbom/forge-source.cdx.json`.
- `release/sbom/forge-source.cdx.json` (generated; release copy committed).
- `RELEASE_READINESS.md` (generated by `forge-release-readiness`; committed at release time).
- `packages/evaluation/forge_eval/release/__init__.py` (NEW).
- `packages/evaluation/forge_eval/release/model.py` (NEW) — `Bar`/`Gate`/`GateResult` + status enum + bar-met logic.
- `packages/evaluation/forge_eval/release/checks.py` (NEW) — command/evidence/manual runners + predicates.
- `packages/evaluation/forge_eval/release/readiness.py` (NEW) — engine + `main()` CLI.
- `packages/evaluation/forge_eval/release/render.py` (NEW) — Markdown renderer.
- `packages/evaluation/pyproject.toml` — add `[project.scripts] forge-release-readiness = "forge_eval.release.readiness:main"`.
- `packages/evaluation/tests/release/` (NEW) — engine unit tests + golden render.
- `Makefile` — add `bump`, `changelog`, `hooks`, `release-readiness`, `source-sbom` targets.
- `.github/workflows/ci.yml` — add `cz check` commit-lint step + a non-blocking `readiness` job; SHA-pin all `uses:`.
- `.github/workflows/release.yml` (NEW) — tag-triggered build + SBOM + provenance + cosign + GitHub Release.
- `.github/workflows/scorecard.yml` (NEW, optional) — OpenSSF Scorecard.
- Touched for version lockstep (by `cz bump`, not hand-edited): all 16 member `pyproject.toml`, `package.json`, `apps/web/package.json`, the package `__init__.py` `__version__`s, `deploy/docker-compose.yml` `FORGE_VERSION`.

## 11. Research references

- Semantic Versioning 2.0.0: https://semver.org/
- Conventional Commits 1.0.0: https://www.conventionalcommits.org/en/v1.0.0/
- Keep a Changelog: https://keepachangelog.com/en/1.1.0/
- Commitizen (commit-lint + SemVer bump + changelog, `version_files`): https://commitizen-tools.github.io/commitizen/
- git-cliff (alternative changelog generator): https://git-cliff.org/
- CycloneDX SBOM specification: https://cyclonedx.org/specification/overview/
- Anchore Syft (SBOM from source dir / images): https://github.com/anchore/syft
- SLSA — Supply-chain Levels for Software Artifacts (provenance): https://slsa.dev/
- Sigstore / cosign — keyless signing + attestation: https://docs.sigstore.dev/cosign/signing/overview/
- GitHub artifact attestations (`actions/attest-build-provenance`): https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations/using-artifact-attestations-to-establish-provenance-for-builds
- Pin GitHub Actions to a full commit SHA (supply-chain hardening): https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#using-third-party-actions
- OpenSSF Scorecard: https://github.com/ossf/scorecard
- GitHub `SECURITY.md` / private security advisories: https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository
- Contributor Covenant (Code of Conduct): https://www.contributor-covenant.org/version/2/1/code_of_conduct/
- Spec/report anchors: `scratchpad/hardening-docs/SPEC-PRODUCTION-HARDENING.md` → "Release readiness model" (ALPHA/BETA/PRODUCTION bars + the 18 G-\* gates), "Definition of Done", the verbatim PRODUCTION asterisk, and the cred-handling rules; `docs/FORGE_SPEC.md` → "Every release runs against the golden test set; regressions block merge" (release gating intent), `security.md` hardening reference, `docs/.../contributing/` doc-tree slot; `docs/MORNING_REPORT.md` §5(10) (`uv lock` re-lock), §7.8 (re-lock + full CI), §8 (the conventional-commit history this changelog draws from); sibling slice `scratchpad/hardening-docs/HARD-07-docker-build-and-pin.md` §3.1/§12 (per-image SBOM precedent + deferred image-signing this slice picks up); sample slice `docs/implementation-slices/v1/F05-hybrid-knowledge-retrieval.md` (12-section format).

## 12. Out of scope / future

- **PyPI / container-registry publishing.** The release workflow *builds, signs, and attaches* wheels + images to a GitHub Release but does **not** push to PyPI or GHCR by default (feature-flagged off). Public package publishing (trusted-publishing/OIDC to PyPI, GHCR push of the digest-pinned images) is a deliberate follow-up once a publish policy + namespace are decided.
- **Reproducible-build bit-for-bit verification.** This slice gives digest-pinned inputs + provenance; proving byte-identical rebuilds (`SOURCE_DATE_EPOCH`, deterministic wheels) is a future hardening step.
- **SLSA Level 3+ (hardened/isolated builders).** Current target is provenance + signing (≈L2 via GitHub-hosted runners + OIDC); reusable hardened-builder workflows (`slsa-framework/slsa-github-generator`) for L3 are future.
- **Automated dependency/digest roll-forward** (Renovate/Dependabot for deps + `@sha256` bumps + action-SHA bumps) — keeps pins fresh without manual runs; pairs with HARD-07 and HARD-14.
- **VEX / CVE-suppression documents** alongside the SBOM (which CVEs are not exploitable and why) — owned with the security-audit slice's CVE gate.
- **Release-readiness as a dashboard / API.** `RELEASE_READINESS.md` is a file + exit code; surfacing it as a web panel or a status badge (and historising results over time) is future once there is demand.
- **Multi-track / LTS release lines** and backport policy — only when the supported-versions table in `SECURITY.md` needs more than a single supported line.
- **Signed git tags / commits (GPG/Sigstore gitsign)** in addition to signed artifacts — a further authenticity layer for the release commits themselves.
- **The pentest engagement and the multi-week fleet soak themselves** — this slice tracks and gates on them via `manual` attestations; performing them is external (named honest asterisk). **SOC2/compliance attestation** is out of scope for the codebase entirely and is surfaced in the report footer so a green gate never implies it.
