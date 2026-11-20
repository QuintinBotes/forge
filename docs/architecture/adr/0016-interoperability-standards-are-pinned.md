# 0016. Interoperability standards are pinned

**Status:** Proposed
**Date:** 2026-11-07

## Context

MCP revisions, A2A (v1.0, March 2026), OpenTelemetry GenAI semantic conventions ("Development"
stability), in-toto/SLSA and CycloneDX/SPDX AI profiles all move. Forge already emits generic OTel
and Ed25519 DSSE attestations.

## Decision (proposed)

Each standard forge speaks is pinned to a version in code and docs: MCP `2026-07-28` for spec I,
still accepting the handshake-era `2025-06-18` and `2025-03-26`; OTel GenAI spans at a pinned
semconv version; A2A v1.0 agent cards for external agents; attestations as in-toto statements with
optional Sigstore signing. Upgrades are explicit changes with tests.

MCP `2026-07-28` was the revision marked Current on the MCP versioning page when checked on
2026-11-17 (issue #210). It is named once in code, as `CURRENT_VERSION` in
`apps/api/scf_api/mcp_server/protocol.py`. The published conformance suite is pinned with it:
`@modelcontextprotocol/conformance` `0.2.0-alpha.12`, run by `scripts/mcp-conformance.sh`
(`docs/integrations/mcp-conformance.md`). On 2026-11-17 the npm `latest` tag, `0.1.16`, had no
`2026-07-28` scenarios; `0.2.0-alpha.12` was the newest release and ships the frozen `2026-07-28`
requirement set.

## Pinned for attestations, commits and SBOMs (#214)

Re-checked against the primary sources on **2026-11-17**:

| Standard | Pin | In code | Source read |
|----------|-----|---------|-------------|
| in-toto Statement | `_type` `https://in-toto.io/Statement/v1`; `subject` entries each with a `digest`; `predicateType`; optional `predicate` | `INTOTO_STATEMENT_TYPE` in `packages/contracts/scf_contracts/attestation.py` | `spec/v1/statement.md` in `github.com/in-toto/attestation` (latest release v1.2.0, 2026-03-18; the Statement layer unchanged since 2024-05-06) |
| in-toto DigestSet | `gitCommit`: the lowercase hex SHA-1 (40) or SHA-256 (64) of a git commit object | the commit subject in `AttestationService.attest_changeset` | `spec/v1/digest_set.md`, same repository |
| DSSE | `payloadType` `application/vnd.in-toto+json`, signed over the PAE | `DSSE_PAYLOAD_TYPE_INTOTO` | unchanged from the original pin |
| CycloneDX | `bomFormat` `CycloneDX`, `specVersion` `1.7` | `CYCLONEDX_SPEC_VERSION` in `tests/test_sbom_format_pin.py` | `github.com/CycloneDX/specification` releases: 1.7 (2025-10-21) is the newest minor version, with patch release 1.7.2 (2026-09-17); `cyclonedx.org/schema/bom-1.7.schema.json` defines both fields, and no `bom-1.8` schema is published |
| Git trailers | `<key>: <value>` lines in the trailer block that ends a commit message | `git_commit` in `packages/agent-runtime/scf_agent/tools.py` | `Documentation/git-interpret-trailers.adoc` in `github.com/git/git` |
| `Signed-off-by` from agents | never written | `git_commit` drops the line in any letter case | Linux kernel `Documentation/process/coding-assistants.rst`: "AI agents MUST NOT add Signed-off-by tags. Only humans can legally certify the Developer Certificate of Origin (DCO)." |

**`Assisted-by` is SCF's own format.** No primary source defines it today. The
Linux kernel defined `Assisted-by: AGENT_NAME:MODEL_VERSION [TOOL1] [TOOL2]` in
commit 78d979db6cef (2025-12-23), reduced it to `Assisted-by: LLM` in 816d9992d9ed
(2026-07-01) and removed it from its documentation and `checkpatch.pl` in
1e035297ffda (2026-10-13). The kernel now asks for tool use to be described in the
cover letter instead. SCF writes `Assisted-by: scf-agent:<model>`, or
`Assisted-by: scf-agent` when the run's model is unknown. The shape follows the
kernel's first definition, but the format is SCF's own. It is one line with no
email address.

## Pinned for GenAI spans (#213)

The GenAI conventions moved out of the core semantic-conventions repository into
`open-telemetry/semantic-conventions-genai`. On **2026-11-20** that repository had no
tag or release, so SCF pins a commit: `622b2b6073b15e5ed886c3c12a65a892b9cd5c70`
(the head of `main` that day). The pin and every `gen_ai.*` name SCF emits are
declared once, in `packages/observability/scf_obs/genai.py` (`SEMCONV_COMMIT`,
`GEN_AI_ATTRIBUTES`); a test fails on any other `gen_ai.*` key.

Read at that commit (`docs/gen-ai/gen-ai-agent-spans.md`, `client-inference.md`,
`execute-tool.md`; all Development status):

| Span | Name | Kind | Attributes SCF sets |
|------|------|------|---------------------|
| Invoke agent internal | `{gen_ai.operation.name} {gen_ai.agent.name}`, or `{gen_ai.operation.name}` when no meaningful agent name exists | INTERNAL | `gen_ai.operation.name` (`invoke_agent`, or `invoke_workflow` for the supervised run's deterministic graph), `gen_ai.agent.name` when known, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |
| Client inference | `{gen_ai.operation.name} {gen_ai.request.model}` | CLIENT | `gen_ai.operation.name` (`chat`), `gen_ai.provider.name` (required), `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |
| Execute tool | `execute_tool {gen_ai.tool.name}` | INTERNAL | `gen_ai.operation.name` (`execute_tool`), `gen_ai.tool.name` (required) |

`error.type` is the stable core attribute (semantic-conventions v1.45.0, which the
snapshot links). The snapshot defines no cost attribute, so cost is SCF's own
`scf.cost.usd`. Every name the issue expected (`gen_ai.tool.name`,
`gen_ai.tool.call.id`, `gen_ai.request.model`, `gen_ai.provider.name`, the two usage
names) matches the snapshot. `gen_ai.tool.call.id` is not emitted: the tool registry
never sees the model's call id.

`docs/self-hosting/observability.md` describes the span tree and SCF's own names.

## Consequences

- No silent behaviour change when a standard moves.
- Forge lags new revisions by design until someone upgrades them deliberately.
- Upgrading CycloneDX means regenerating `release/sbom/` and `deploy/sbom/` and
  changing the pin in the test and here in the same change. The generators
  (`deploy/scripts/sbom.sh`, `release/scripts/source-sbom.sh`) ask Syft for
  `cyclonedx-json` without a version, and the CI and release workflows install
  Syft from its `main` branch, so a newer Syft can emit a newer version. The test
  fails once such a file is committed. It does not check the SBOMs the release
  workflow generates and signs at release time.
