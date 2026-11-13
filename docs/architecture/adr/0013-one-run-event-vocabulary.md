# 0013. One run-event vocabulary with an enforceability flag

**Status:** Proposed
**Date:** 2026-11-07

## Context

Tervin normalises every agent session into 32 event types and states, per event, whether it was
gated or only observed. Forge's run steps carry no such flag, so a client cannot tell an enforced
denial from a logged one.

## Decision (proposed)

Define the run-event vocabulary as Pydantic models in `forge_contracts`, re-implemented from tervin's
published event list (not copied), each with `enforcement: gated | observed`. Forge's runs, the SSE
stream (spec L) and the runner protocol all use it.

## Consequences

- Tervin renders forge runs without a translation layer.
- Honesty is structural: an event cannot claim enforcement it did not have.
