# Public-Readiness — Progress (2026-07-08)

Status of the pre-1.0 public-readiness pass — the work that makes the repository
honest and safe to share for **evaluation and testing** (not production).

## Done — on `main`, green in CI
- **Under-development notice + honest README Status.** A prominent pre-1.0 /
  not-production banner at the top of the README; the stale "UI in progress"
  status replaced with the truth (all 15 web screens shipped, ~3,700 tests green
  on real pgvector Postgres); a live CI badge.
- **`GET /projects/{id}/specs`** — the spec-dashboard projection the web client
  already calls. The `/specs` dashboard now renders live instead of the degraded
  "Live specs are unavailable" state.
- **Adaptive-orchestration foundation** (first slices of the larger finalise,
  landed here green): complexity sizing + a provider-agnostic model router
  (Haiku/Sonnet/Opus by task seniority).

## Deferred to the finalise — covered by the under-development banner
- **Server-side `/ws` websocket** (live board/run/approval push). This is built
  properly as part of the real-time / CRDT work in the finalise (`rt-ws`); until
  then the board loads and refreshes on navigation, only the live push is
  pending. The web hook degrades quietly when the socket is absent.
- **"Coming soon / needs-X" labels** on the few still-gated surfaces (OIDC, some
  CLI-backed flows, real latency percentiles) — folded into the frontend-UX
  polish phase of the finalise.

The under-development banner sets the expectation that some features are still
landing, so nothing here misrepresents the project. See
[`docs/SLICES_PROGRESS.md`](./SLICES_PROGRESS.md) and
[`docs/FRONTEND_PROGRESS.md`](./FRONTEND_PROGRESS.md) for the full ledger.
