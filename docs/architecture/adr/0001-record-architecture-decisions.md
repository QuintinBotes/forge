# 0001. Record architecture decisions

**Status:** Accepted
**Date:** 2026-11-07

## Context

Forge has design specs under `docs/superpowers/specs/` and plans under `docs/superpowers/plans/`,
but no architecture decision records. Decisions are restated, sometimes inconsistently, across specs.
`docs/FORGE_SPEC.md` plans a `docs/architecture/` home that was never created. The spec engine
already renders in-product decisions in a Context / Decision / Consequences + Status shape
(`forge_spec.templates.render_decisions_md`).

## Decision

Architecture decisions live in `docs/architecture/adr/NNNN-<slug>.md`, numbered from 0001, in the
Context / Decision / Consequences shape with a **Status** (`Proposed`, `Accepted`, `Superseded by
NNNN`) and **Date**. Specs cite ADRs by number and do not restate them. An ADR is never edited after
acceptance except to change its status; a change of mind is a new ADR that supersedes it.
System-level architecture lives beside them in `docs/architecture/`.

## Consequences

- One place to look for why forge is shaped the way it is.
- Specs get shorter: they reference decisions instead of re-arguing them.
- Reviewers check that a spec's decisions match an accepted ADR.
