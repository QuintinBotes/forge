"""F40-POL-GOVERNANCE — CODEOWNERS wired into the PR-gate authorizer.

Proves the path-scoped CODEOWNERS rule is *enforced* (not just parsed): when the
repo ``review_rules.require_code_owners`` is set and the gate carries the repo's
CODEOWNERS text + changed paths, only an owner of those paths may resolve the gate.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import make_principal, make_request

from forge_approval.authorizer import ApprovalAuthorizer, AuthorizationError
from forge_approval.models import ApprovalAction, ApprovalDecisionRequest, GateType, Role

OWNER = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
OUTSIDER = uuid.UUID("00000000-0000-0000-0000-0000000000d2")

_APPROVE = ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)


def _pr_gate(**payload_over: object):
    payload: dict[str, object] = {
        "review_rules": {
            "approval_required_for_merge": True,
            "require_code_owners": True,
        },
        "codeowners": f"src/ {OWNER}\ndocs/ @docs-team\n",
        "changed_paths": ["src/app.py"],
    }
    payload.update(payload_over)
    return make_request(GateType.PR, requested_actor="system", gate_payload=payload)


def test_non_owner_cannot_resolve_code_owned_paths() -> None:
    authorizer = ApprovalAuthorizer()
    outsider = make_principal(role=Role.MEMBER, principal_id=OUTSIDER)
    with pytest.raises(AuthorizationError, match="CODEOWNERS"):
        authorizer.check(outsider, _pr_gate(), _APPROVE)


def test_owner_of_changed_paths_resolves() -> None:
    authorizer = ApprovalAuthorizer()
    owner = make_principal(role=Role.MEMBER, principal_id=OWNER)
    authorizer.check(owner, _pr_gate(), _APPROVE)  # no raise


def test_handle_owner_resolved_via_identity_map() -> None:
    authorizer = ApprovalAuthorizer()
    owner = make_principal(role=Role.MEMBER, principal_id=OWNER)
    gate = _pr_gate(
        codeowners="src/ @alice\n",
        owner_identities={"@alice": str(OWNER)},
    )
    authorizer.check(owner, gate, _APPROVE)  # handle maps to the owner's id


def test_no_enforcement_when_codeowners_absent() -> None:
    # require_code_owners is set but the gate carries no CODEOWNERS text: nothing
    # to enforce against, so any qualified member may resolve (fail-open on data,
    # not policy — the rule simply has no owners to require).
    authorizer = ApprovalAuthorizer()
    outsider = make_principal(role=Role.MEMBER, principal_id=OUTSIDER)
    authorizer.check(outsider, _pr_gate(codeowners=""), _APPROVE)


def test_flag_off_leaves_gate_unowned() -> None:
    authorizer = ApprovalAuthorizer()
    outsider = make_principal(role=Role.MEMBER, principal_id=OUTSIDER)
    gate = _pr_gate(
        review_rules={"approval_required_for_merge": True, "require_code_owners": False}
    )
    authorizer.check(outsider, gate, _APPROVE)  # no CODEOWNERS gate when flag off


def test_change_outside_owned_paths_is_unrestricted() -> None:
    authorizer = ApprovalAuthorizer()
    outsider = make_principal(role=Role.MEMBER, principal_id=OUTSIDER)
    gate = _pr_gate(changed_paths=["README.md"])  # matches no CODEOWNERS rule
    authorizer.check(outsider, gate, _APPROVE)
