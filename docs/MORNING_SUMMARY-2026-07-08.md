# Forge — Progress Summary (2026-07-08)

_Autonomous finalise run. Priority #1 = finish the full solution for distribution._

## ✅ Landed on `main` (green in CI) — repo is now **PUBLIC**
- **CI is real and green**; the whole GitHub Actions gate was fixed and now runs (incl. CodeQL/code-scanning on the public repo).
- **PR #28** — persistence (Postgres repos) + CI fixes.
- **PR #30 — public-readiness**: under-development banner + honest README status + live spec dashboard (`GET /projects/{id}/specs`). **→ You marked the repo public.** 🎉
- **PR #32 — Adaptive Orchestration**: automatic model routing (Anthropic junior=Haiku / medior=Sonnet / senior=Opus, provider-agnostic), per-role effort levels, a "Models & Effort" settings API + UI with live routing preview, and cost-by-tier observability. Local gate: mypy 0, **3,868 tests**, web lint/build/test clean.
- Dependabot #25 auto-merged when green.

## ▶ In progress — the hard finalise, chunk by chunk
- **Spec Studio** (building now, ~14 slices): dual-format `spec.md` ⇄ `manifest.yaml` round-trip, the Guided/Markdown/YAML/Read modes, BYOK AI draft (`POST /spec/draft`), the full SDD lifecycle, versioning/diff, import. Design doc: `docs/spec-studio/DESIGN.md`.
- **Then:** Realtime co-editing (delivers the real `/ws` websocket + Yjs CRDT) → F40 backlog → IaC (OpenTofu) → frontend-UX pass (ui-ux-pro, incl. the deferred "coming soon" labels) → docs site + real screenshots.

## ⚠️ Pipeline lessons (all fixed, main stayed green)
- Spurious green-slice reverts → gate now treats a green full suite as authoritative.
- Workflow `args` don't pass → slice filter hardcoded in the script.
- A verifier `git stash`'d WIP → verify prompt forbids touching git state; recovered via `git stash pop`.
- One two-swarm race + a seams-gatekeeper misfire → caught, no damage; iron rule = one swarm at a time.

## 📌 Tracked follow-up
- **Wire `ExecutionPlan` tier/strategy into `ModelUsage`** at the live model-client call sites — the cost-by-tier observability is built and ready but not yet populated from real agent runs (pre-existing gap, documented in `docs/ADAPTIVE_SPEC_PROGRESS.md`).

## ❓ Open questions
- None blocking. The finalise is compute-bound by the weekly limit and lands in chunks over the next day(s).

## Honest ceiling (cannot close autonomously)
- Cred-gated live integrations (GitHub App / model BYOK / reranker / MCP / Slack) — code + tests + runbooks exist; need your keys to verify live.
- Real cloud / K8s-at-scale, multi-week soak, and the human penetration test.
