# 0012. Two executor modes: native loop and delegated CLI agents

**Status:** Proposed
**Date:** 2026-11-07

## Context

Forge runs its own LangGraph loop and, on the local-CLI branch, uses Claude Code and Codex as text
oracles with tools switched off. Tervin drives the same CLIs as full agents behind permission gates
and judges forge's loop "do not port". Full agents are more capable; forge's loop is more governable.

## Decision (proposed)

Keep the native loop as the default and add a delegated executor that runs Claude Code
(stream-json + PreToolUse hook), Codex (`exec --json`) and ACP agents as full agents inside forge's
container sandbox. Every hook or ACP permission request calls forge's policy endpoint and **fails
closed**. Ungated runs are labelled `observed`, never `gated`.

## Consequences

- Forge gains full-agent capability without giving up policy enforcement.
- Open before acceptance: whether provider terms allow subscription-authenticated CLIs on shared
  servers; CLI flag and hook drift per version.
