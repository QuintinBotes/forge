"""Per-kind install policy (spec K §4.4 "Kind policy").

``workflow_template`` installs and updates only when its signature verified,
at every registry trust level, and never from a yanked version.
``acknowledge_unverified`` is deliberately not an input: it cannot relax this.
Every other kind keeps the trust-level / acknowledgement gates in the service.
"""

from __future__ import annotations

from forge_marketplace.models import ArtifactKind, VerificationStatus


def kind_policy_violation(
    kind: ArtifactKind, verification_status: VerificationStatus, yanked: bool
) -> str | None:
    """The 422 error code the kind policy refuses with, or ``None`` if allowed."""
    if kind != ArtifactKind.workflow_template:
        return None
    if verification_status != VerificationStatus.verified:
        return "signature_required"
    if yanked:
        return "version_yanked"
    return None


__all__ = ["kind_policy_violation"]
