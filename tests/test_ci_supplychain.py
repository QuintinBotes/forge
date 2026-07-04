"""Supply-chain hygiene of CI (HARD-12, AC10).

Every `uses:` in every workflow must be pinned to a 40-hex commit SHA (a floating
`@v4` tag can be silently repointed to malicious code), and the release workflow
must declare the OIDC/attestation permissions and carry the keyless signing +
provenance steps that make a release verifiable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))

# owner/repo(/subdir)@ref  — capture the ref; local (./…) and docker:// uses are exempt.
_USES_RE = re.compile(r"^\s*-?\s*uses:\s*([^\s@]+)@([^\s#]+)", re.MULTILINE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def test_workflows_exist() -> None:
    names = {p.name for p in WORKFLOWS}
    assert {"ci.yml", "release.yml"} <= names


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_sha_pinned(workflow: Path) -> None:
    text = workflow.read_text(encoding="utf-8")
    unpinned: list[str] = []
    for ref_owner, ref in _USES_RE.findall(text):
        if ref_owner.startswith("./") or ref_owner.startswith("docker://"):
            continue
        if not _SHA_RE.match(ref):
            unpinned.append(f"{ref_owner}@{ref}")
    assert not unpinned, f"{workflow.name}: floating (non-SHA) action pins: {unpinned}"


def test_release_workflow_has_oidc_permissions_and_signing() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "id-token: write" in text, "release.yml must request OIDC id-token for keyless signing"
    assert "attestations: write" in text, "release.yml must request attestations:write"
    assert "contents: write" in text, "release.yml must be able to create the Release"
    assert "attest-build-provenance" in text, "release.yml must generate SLSA provenance"
    assert "cosign sign" in text, "release.yml must keyless-sign the images"
    # Keyless: no private key material must be referenced.
    assert "cosign.key" not in text
    assert "COSIGN_PRIVATE_KEY" not in text


def test_ci_workflow_lints_commits_and_runs_readiness() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "cz check" in text, "ci.yml must lint commit messages with cz check"
    assert "forge-release-readiness" in text, "ci.yml must run the readiness report on PRs"
