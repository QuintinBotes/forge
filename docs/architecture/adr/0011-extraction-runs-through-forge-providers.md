# 0011. Extraction runs through forge's provider layer; the server verifies

**Status:** Accepted
**Date:** 2026-11-07

## Context

Utopia talks only to OpenAI-compatible endpoints and has no native Anthropic client. Forge's
provider layer (`forge_contracts.ModelClient`) supports Anthropic, OpenAI and local CLIs, with
redaction, retries, effort mapping and cost metering. Model output cannot be trusted for quotes,
names, times or offsets.

## Decision

World-model extraction calls `ModelClient.complete` at temperature 0 with a JSON reply contract.
The server is authoritative: a statement is kept only if its quote is verbatim in the chunk, its named
things appear in the text, and its time words appear in the quote; offsets are computed by the server.
Every rejection is a stored drop row with a coded reason. With no model configured, ingest refuses
(ADR-0007).

## Consequences

- Extraction inherits forge's model routing and cost metering; per-task model choice (H4) plugs into
  the existing router.
- Extraction quality is measurable from drop rates per reason.
