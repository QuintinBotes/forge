# 0003. Absorb lore and utopia by native re-implementation

**Status:** Accepted
**Date:** 2026-11-07

## Context

Utopia is ~184k lines of Rust with its own auth, users, Vite SPA, a REST surface with "no stability
promise", an on-disk Tantivy index outside Postgres backups and an OpenAI-dialect-only model layer.
Lore is TypeScript on Kubernetes + GCP, depends on Dgraph and on its external `floor` engine (no licence), and
serves one GitHub org per install. Running either beside forge duplicates identity, UI, APIs, model
routing and storage, and imports their open defects. Lore itself already switched its knowledge
reads to utopia, and its own docs list its knowledge layer's failures.

## Decision

Rebuild utopia's essential behaviour (spec H) and lore's capabilities (specs J, K) natively in forge's
Python packages on forge's Postgres, from their design documents and observed behaviour. Consume
as-is only: `lore-code-trace` reports (a Go CLI run in CI), lore's `.lore/*.yml` formats as import
sources, and ontology packs under their own licences (H2). A utopia instance may run **only as a
parity oracle** outside production while the rebuild is measured.

## Consequences

- One stack, one auth model, one store, one audit trail.
- Forge owns the maintenance of the rebuilt behaviour and cannot pull upstream fixes; parity is kept
  by tests derived from the upstream designs and by the oracle comparison.
- Thresholds tuned upstream on DeepSeek/Qwen must be re-measured on forge's models.
