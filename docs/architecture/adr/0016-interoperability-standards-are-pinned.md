# 0016. Interoperability standards are pinned

**Status:** Proposed
**Date:** 2026-11-07

## Context

MCP revisions, A2A (v1.0, March 2026), OpenTelemetry GenAI semantic conventions ("Development"
stability), in-toto/SLSA and CycloneDX/SPDX AI profiles all move. Forge already emits generic OTel
and Ed25519 DSSE attestations.

## Decision (proposed)

Each standard forge speaks is pinned to a version in code and docs: MCP `2025-06-18` (accepting
`2025-03-26`) for spec I; OTel GenAI spans at a pinned semconv version; A2A v1.0 agent cards for
external agents; attestations as in-toto statements with optional Sigstore signing. Upgrades are
explicit changes with tests.

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
