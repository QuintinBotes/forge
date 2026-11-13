# 0009. Migration numbers are assigned at merge time

**Status:** Accepted
**Date:** 2026-11-07

## Context

Alembic revisions in `packages/db/migrations/versions/` form one linear chain (`0001` … `0040`).
Programme spec A reserved `0041_risk_classification` on 2026-09-19 but has not been implemented.
Sub-project H is implemented first. Reservations in specs cannot be honoured without either blocking
on A or creating two heads.

## Decision

A migration takes the next free number when its branch is merged, chaining to the current head.
Specs name the migration by slug and may suggest a number, but a suggestion is not a reservation. H1's
migration is `0041_world_model` (`down_revision = 0040_self_eval_baseline`); A takes the next free
number when it lands.

## Consequences

- No multiple heads; no work blocks on an unimplemented spec.
- A spec that hard-codes a number must be read as "the next free one".
