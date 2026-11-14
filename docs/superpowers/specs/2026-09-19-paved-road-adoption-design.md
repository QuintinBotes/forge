# E — Paved-road adoption

**Date:** 2026-09-19
**Status:** DRAFT — scope and interfaces only. Revise from A–D before planning.
**Depends on:** A, B, C, D
**Closes:** the "anyone can use it" half

---

## 1. Problem

Verified absence: `paved road|golden path` — 0 files.

Forge has the capabilities but no stated standard path, no measurement of who uses it, and no
answer to the model's adoption diagnostic: *what proportion of eligible teams use the standard
path, and why do the others not?* — *both numbers, and the second one investigated rather than
mandated away.*

The model is emphatic that this is the harder half, that "adoption as an afterthought" is failure
mode #8, and that adoption is won by being faster rather than by being mandated.

## 2. Scope

### 2.1 The golden path, stated

One documented, instrumented route from intent to measured outcome, with the conveniences
voluntary and only the controls mandatory. This is a product decision as much as an
implementation: it means choosing *one* way and making it the fastest.

### 2.2 Adoption instrumentation

Measure what proportion of eligible work travels the standard path, and capture *why* the rest
does not — as recorded reasons, not inferred ones. A team going around the paved road is data
about the road, not a compliance problem.

### 2.3 Onboarding

Reduce time-to-first-successful-change for a new self-hoster. F removes the API-key barrier; this
addresses the rest.

### 2.4 The ten questions as a live surface

The model's §6 diagnostics, each backed by a live query rather than a person's recollection:

| Question | Source |
|---|---|
| Idea to production, and the waiting fraction | C |
| One team, one change, no handover | demonstrated, recorded here |
| What the test suite catches | existing seeded-fault measurement |
| What happens when an automated actor gets stuck | existing leases and escalation artifacts |
| How fast a release can be reversed | B |
| Did the last ten changes work | C |
| What the last change cost | D |
| What may ship without a release event, and who decided | A |
| Proof a deletion request was honoured | existing audit receipts |
| Proportion on the standard path, and why not | this sub-project |

## 3. Interfaces

- `GET /api/factory/diagnostics` — all ten answers, each with its source and freshness
- `GET /api/factory/adoption` — standard-path proportion plus recorded reasons for divergence
- A Factory Health screen in the web app surfacing the above

## 4. Acceptance criteria

1. All ten diagnostics answer from live data, each labelled with its source and how fresh it is.
2. Adoption reports both numbers — the proportion, and the reasons for divergence.
3. A new self-hoster reaches a first successful change measurably faster than before, baselined.
4. Nothing added here mandates a convenience; only controls are non-bypassable.
5. No diagnostic is attributable to an individual.

## 5. Exit

The reference model insists a factory programme declares its own completion. When these
diagnostics answer from live data, the programme is done with its disruptive phase, and the
roadmap document is retired rather than extended.
