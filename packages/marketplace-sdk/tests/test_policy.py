"""Kind policy (spec K §4.4): ``workflow_template`` needs ``verified`` and no yank."""

from __future__ import annotations

import pytest

from forge_marketplace.models import ArtifactKind, VerificationStatus
from forge_marketplace.policy import kind_policy_violation

_NOT_VERIFIED = [s for s in VerificationStatus if s is not VerificationStatus.verified]


def test_workflow_template_verified_and_not_yanked_is_allowed() -> None:
    assert (
        kind_policy_violation(ArtifactKind.workflow_template, VerificationStatus.verified, False)
        is None
    )


@pytest.mark.parametrize("status", _NOT_VERIFIED)
@pytest.mark.parametrize("yanked", [False, True])
def test_workflow_template_requires_a_verified_signature(
    status: VerificationStatus, yanked: bool
) -> None:
    assert (
        kind_policy_violation(ArtifactKind.workflow_template, status, yanked)
        == "signature_required"
    )


def test_workflow_template_yanked_version_is_refused() -> None:
    assert (
        kind_policy_violation(ArtifactKind.workflow_template, VerificationStatus.verified, True)
        == "version_yanked"
    )


@pytest.mark.parametrize(
    "kind", [k for k in ArtifactKind if k is not ArtifactKind.workflow_template]
)
@pytest.mark.parametrize("status", list(VerificationStatus))
@pytest.mark.parametrize("yanked", [False, True])
def test_other_kinds_are_unaffected(
    kind: ArtifactKind, status: VerificationStatus, yanked: bool
) -> None:
    assert kind_policy_violation(kind, status, yanked) is None
