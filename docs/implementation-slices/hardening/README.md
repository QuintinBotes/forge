# Forge Production Hardening Tier — README

This directory is the **hardening tier**: 14 implementation slices
(**HARD-01 … HARD-14**) plus the engineering contract
([`SPEC-PRODUCTION-HARDENING.md`](./SPEC-PRODUCTION-HARDENING.md)) and the master
[`INDEX.md`](./INDEX.md). Together they specify how to take Forge from a
**trustworthy-on-paper ALPHA** to a **runnable BETA** and an **adoptable
PRODUCTION** release.

---

## 1. What the hardening tier is

The overnight ALPHA stands the whole platform up — 13 `forge_*` packages + 4 apps,
~944 Python tests + 28 web tests green, ruff clean, 96% coverage
(`docs/MORNING_REPORT.md`). But, as that report says honestly in §5 (every PARKED
item) and §6 (known gaps), **every claim that touches the outside world is mock-,
fixture-, or SQLite-backed**: GitHub/Slack/MCP/model/embedder/reranker are simulated,
the eval numbers are deterministic perfect `1.000`s, the images were never built, and
no real security audit has run.

The hardening tier is the engineering program that **exercises the real systems the
ALPHA only simulated**. It does **not** change the product surface — `docs/FORGE_SPEC.md`
remains the source of truth for *what* each subsystem does. Hardening proves that
surface **against reality**.

Two design rules make these slices real rather than a rewrite:

1. **They EXTEND, never fork.** Each slice extends a named existing `forge_*` package
   or app (e.g. HARD-01 extends `forge_integrations`, HARD-04 extends `forge_eval` +
   `forge_knowledge`). No duplicate/parallel packages. They conform to the **real**
   `forge_db` schema (singular tables, `Enum(native_enum=False)` string enums) and the
   **frozen** `forge_contracts` DTOs/Protocols — unchanged.
2. **They keep the tree green and the secrets safe.** Every real-boundary test lives
   behind `@pytest.mark.integration` (or an opt-in CI job) and **skips clean** when its
   credentials/runner are absent, so the **hermetic default suite stays green and
   network-free**. Real creds are read from a gitignored `.env.integration` (and the
   GitHub App key from `deploy/secrets/github-app.pem`), resolved per-call from the
   encrypted vault, redacted everywhere, and **never** committed, logged, or fixtured.

Each slice mirrors the **12-section format** of the sample slice
`docs/implementation-slices/v1/F05-hybrid-knowledge-retrieval.md`:

1. Intent — what & why · 2. User-facing / operator behavior · 3. Vertical slice
(data model / backend / worker / frontend) · 4. Public interfaces & contracts ·
5. Dependencies · 6. Acceptance criteria (numbered, testable) · 7. Test plan (TDD) ·
8. Security & policy · 9. Effort & risk · 10. Key files/paths · 11. Research
references · 12. Out of scope / future.

Every slice defines a **whole-suite green gate** plus concrete, numbered acceptance
criteria split into **offline** (hermetic, runnable now) and **requires-real-creds /
networked** (the live half) — so what can be verified in-sandbox versus what needs a
real key or a CI runner is explicit, never blurred.

---

## 2. How it maps to the 7 release blockers

The program closes the 7 release blockers from the brief. Every blocker is owned by at
least one slice (full mapping in [`INDEX.md`](./INDEX.md)):

| # | Blocker | Closed by |
|---|---|---|
| 1 | No real external systems exercised (GitHub App, model/embedder/reranker, MCP, Slack) | **HARD-01** (GitHub), **HARD-02** (model BYOK), **HARD-03** (reranker), **HARD-05** (MCP), **HARD-06** (Slack) |
| 2 | Eval numbers offline/deterministic (fake embedder + fixture reranker; perfect 1.000s) | **HARD-03** (real reranker) + **HARD-04** (real corpus, learned local embedder, honest recall@k/MRR/nDCG) |
| 3 | `docker compose build` + `next build` never run; images not `@sha256`-pinned | **HARD-07** (build + digest pin) + **HARD-08** (Helm install + smoke on real k8s) |
| 4 | No real security audit / no pentest | **HARD-09** (SAST/secret-scan/dep-audit/enforcement-matrix/threat-model/punch-list) + **HARD-12** (supply-chain SBOM/provenance/signing) + **HARD-13** (secrets/config) |
| 5 | Parked items may stay reverted/parked (LangGraph swap, tree-sitter, Fernet/OAuth) | **HARD-13** (Fernet default + OAuth/secret-key) + **HARD-14** (tree-sitter/LangGraph verify, `uv lock` re-lock, 3.14 lane, F40 backlog machinery) |
| 6 | Maturity gaps: low worker/agent coverage, no load/perf, no migration up/rollback, no soak | **HARD-11** (coverage + migration round-trip + load/perf + multi-tenant soak) + **HARD-08** (k8s install/migrate) + **HARD-10** (telemetry/cost truth) + **HARD-12** (release discipline) |
| 7 | Python 3.14 deferred (RC + pydantic/PEP 649), eslint held at 9 | **HARD-14** (3.14-RC CI lane + eslint go/no-go + re-lock) |

### How it maps to the product spec

`docs/FORGE_SPEC.md` defines the surface; the hardening slices prove each part of it
against reality:

- **Hybrid retrieval** (pgvector + BM25 + RRF + rerank) → HARD-03/HARD-04 run it on a
  learned embedder + real reranker over a real corpus, plus the ablation that proves
  fusion adds recall; the real-pgvector substrate underwrites it.
- **MCP Security Rules** (read-only default, RFC 8707 token binding, namespace scoping,
  redacted audit) → HARD-05 proves them on a live server over real transport.
- **Auth/RBAC/Secrets** (deny-by-default RBAC, encrypt-at-rest BYOK vault, agent-token
  expiry, secret redaction) → HARD-09's enforcement matrix + HARD-13's production
  secrets/crypto/config close the parked Fernet/OAuth seams.
- **Production deploy best-practices** (pinned, non-root, health-checked,
  resource-limited images; Docker Compose + Kubernetes install paths) → HARD-07
  (build + `@sha256` pin) and HARD-08 (Helm chart that installs and serves on a real
  cluster).
- **Observability & Cost metrics** (the F38 cross-cutting module) → HARD-10 realizes a
  real OTLP export path + a durable cost ledger on real Postgres.
- **Eval/benchmark** (the F12 harness) → HARD-04 supersedes the deterministic `1.000`
  headline with honest numbers and wires a regression gate from the real baseline.

The relationship is stated in the spec: *"Hardening does not change the product
surface — it proves the surface against reality."*

### The release-readiness model

`SPEC-PRODUCTION-HARDENING.md` defines three named bars — **ALPHA** (already met),
**BETA** ("real, with named limits"), **PRODUCTION** ("trustworthy, with one honest
asterisk") — and 18 lettered gates / 22 DoD items. A bar is met only when **every**
gate under it is green from **captured command output (evidence), not assertion**. The
BETA and PRODUCTION gate checklists, and which slice realizes each gate, are in
[`INDEX.md`](./INDEX.md). HARD-12 builds the **automated meta-gate**
(`forge-release-readiness`) that mechanically checks every gate and renders a dated
`RELEASE_READINESS.md`, so no PRODUCTION claim can rest on un-evidenced or simulated
work.

---

## 3. The honest ceiling (what agents cannot do)

Stated up front so a green gate never over-claims. The build agents **can** stand up
real Postgres+pgvector, call real model/embedder/reranker/GitHub/Slack/MCP endpoints
with the supplied creds, run a genuine **local open-weight embedder**
(`sentence-transformers`) for honest eval numbers **without any API key**, build the
images, and run bounded load/soak/upgrade tests. They **cannot**:

- Perform a **3rd-party human penetration test** or a formal audit sign-off — HARD-09
  delivers SAST/secret-scan/dependency-audit/RBAC-enforcement evidence + a scoped
  pentest **punch-list**, but the human pentest itself stays an explicit, named gap.
- Operate a **real multi-week, multi-tenant production fleet** — HARD-11 *simulates* a
  bounded soak; a true fleet over weeks is not reproducible in-sandbox.
- Obtain **SOC2 / compliance attestation** — out of scope for the codebase.

These three are surfaced everywhere they touch a gate; in HARD-12's readiness engine
they are `MANUAL-PENDING` forever until a signed attestation is filed — never
auto-greened. The PRODUCTION release notes carry the asterisk verbatim.

A second, narrower limit: the **no-network sandbox** itself cannot run the live halves
(`docker compose build`, kind/k3d install, the OTLP→Grafana pipeline, load/soak, the
signing/publish release step, and the BYOK/GitHub/Slack/MCP integration lanes). Those
run on a **networked/CI runner**; in-sandbox they skip clean and the hermetic suite
stays green. See the Runner column in [`INDEX.md`](./INDEX.md).

---

## 4. Order to implement

The critical path (full dependency graph in `SPEC-PRODUCTION-HARDENING.md` §Sequencing):

1. **Foundation first — real DB substrate + a green typecheck/coverage floor.** Stand
   up live Postgres+pgvector (the cross-cutting **G-DB** substrate woven through
   HARD-08/09/10/**11**/13) and land the whole-workspace `make typecheck` fix
   (**G-TYPES**, checked by HARD-12). These make every later gate trustworthy. Begin
   the worker/agent-runtime coverage lift in **HARD-11**.
2. **Production secrets/crypto — HARD-13.** The real `FernetCipher` default +
   required `FORGE_SECRET_KEY` + the encrypted vault must exist **before** live BYOK
   creds flow through it, because HARD-01/02/03/05/06 all resolve keys from that vault.
3. **Honest eval, no key needed — HARD-03 → HARD-04.** The local `sentence-transformers`
   embedder + real reranker give honest recall@k/MRR/nDCG without waiting on any API
   key — the highest-signal BETA deliverable for blocker #2.
4. **The four real external integrations — HARD-01, HARD-02, HARD-05, HARD-06.**
   Parallelizable, each creds-gated and independent.
5. **Un-park the rest — HARD-14.** tree-sitter + LangGraph verification on the real
   agent path, the F40 backlog machinery (default-off flags + revert-to-green), and
   the spine increments.
6. **Build + deploy for real — HARD-07 → HARD-08.** Build the 4 images and `next
   build`, pin by `@sha256`, then prove the Helm chart installs and serves on kind/k3d.
   Needs a networked runner.
7. **Maturity evidence — HARD-11 (+ HARD-10).** Migration upgrade→rollback→re-upgrade
   on a populated DB, retrieval p50/p95/p99, API hot-path load, and the bounded
   multi-tenant soak — all on the real DB + real embedder, with cost/latency telemetry
   from HARD-10.
8. **Security audit + capstone — HARD-09, then HARD-12.** Run the automated audit
   continuously; write the threat model + punch-list. HARD-12 last: versioning/
   changelog/governance, supply-chain SBOM/provenance/signing, and the automated
   `RELEASE_READINESS` meta-gate that certifies BETA, then PRODUCTION.

> **Rule that never bends:** the whole-suite green gate (`uv run pytest -q` +
> `uv run ruff check .` + `make typecheck` + `cd apps/web && pnpm test`) must be green
> at the **end of every workstream** — no slice may leave the tree red, even
> transiently across a merge. Each future-scope increment ships behind a default-off
> flag with a documented, tested **revert-to-green** procedure (HARD-14).

---

## 5. Files in this directory

- [`INDEX.md`](./INDEX.md) — master table of HARD-01…HARD-14 (ID · Title · Blocker(s) ·
  Needs creds? · Effort · Link) + the BETA and PRODUCTION gate checklists with gate→slice
  mapping + the numbering/cross-reference note.
- [`SPEC-PRODUCTION-HARDENING.md`](./SPEC-PRODUCTION-HARDENING.md) — the engineering
  contract: release-readiness model, 7-blocker→gate map, credential-handling rules,
  sequencing, and the numbered Definition of Done.
- `HARD-01-live-github-app.md` … `HARD-14-future-scope-execution.md` — the 14 full
  12-section slices.
