# C — Outer-loop measurement

**Date:** 2026-09-19
**Status:** DRAFT — scope and interfaces only. Revise from A's shadow-mode learnings before planning.
**Depends on:** A (changeset identity)
**Closes:** capability 8

---

## 1. Problem

Forge measures the promotion, not the idea. `forge_obs/analytics/dora.py` says so itself:

> "'lead time for changes' is properly commit-to-production; this foundation's `deployment` row
> carries no commit-authored/merged timestamp (only `commit_sha`), so `lead_time_seconds` uses
> `requested_at -> finished_at` (the promotion's own request-to-live duration) as the measurable
> proxy."

The honesty is right; the measurement is inner-loop. Two of the model's diagnostics are currently
unanswerable: *how long from idea to production, and how much of that is waiting?* and *did the
last ten changes work?*

Verified absence: `experiment` — 2 matches, neither an experimentation system. No semantic layer.

## 2. Scope

### 2.1 True lead time, decomposed

Replace the proxy with real commit-to-production timing, and — the part that actually matters —
separate **waiting** from **active work**. A single elapsed number hides the constraint; the model
names the large gap between active and elapsed as the measurable symptom of optimising the wrong
loop.

Requires capturing intent-created, commit-authored and merged timestamps against A's changeset
identity.

### 2.2 Telemetry bound to what shipped

Join production telemetry to the changeset that caused it, so a per-change outcome verdict is
computable rather than assumed.

### 2.3 Per-change outcome verdict

Answer *"did the last ten changes work?"* with ten verdicts derived from telemetry within days of
each release — not a dashboard a human interprets, a verdict per change.

### 2.4 Semantic layer

So the same question returns the same number regardless of who asks. Scope limited to the metrics
this programme defines; not a general BI layer.

## 3. Interfaces

- `packages/observability/forge_obs/analytics/lead_time.py` — decomposed timing, replacing the
  proxy in `dora.py` (which is kept, corrected, and its docstring caveat removed once true)
- `packages/observability/forge_obs/outcome/` — per-change verdicts
- `GET /api/observability/lead-time` — with the waiting fraction broken out
- `GET /api/observability/outcomes?limit=10` — the last ten verdicts

## 4. Acceptance criteria

1. Lead time is commit-to-production, with waiting and active fractions reported separately.
2. `dora.py`'s proxy caveat is removed because it is no longer a proxy.
3. The last ten changes each carry an outcome verdict derived from telemetry.
4. Verdicts arrive within days of release, not at quarter end.
5. No metric introduced here is attributable to an individual.

## 5. Open questions for after A

- Where does "intent created" come from — spec creation, board card, or PM adapter intake?
- What is a verdict when a change ships behind a flag that is never enabled? Probably `unmeasured`
  rather than `failed`, but B's flag model decides this.
- Does the semantic layer need its own store, or is a view over existing tables enough?
