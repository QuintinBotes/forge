"""Pure assertion-attribute → Forge identity mapping (F33 §4, frozen rules).

Role resolution reuses the canonical F30/F37 privilege ordering
(:data:`forge_authz.permissions.ROLE_RANK` — ``admin=2 > member=1 >
{agent-runner, viewer}=0``); F33 does **not** invent its own ordering. Rank
ties are broken by the documented secondary order ``member > agent-runner >
viewer``. An IdP-asserted role (e.g. an attribute literally claiming
``role=admin``) is honored **only** through the admin-configured
``group_role_map`` — never implicitly (non-negotiable: no privilege
self-escalation via IdP).
"""

from __future__ import annotations

from forge_authz.permissions import ROLE_RANK

from forge_contracts.enums import UserRole
from forge_contracts.sso import AttributeMapping, MappedIdentity, SamlAssertion

#: Deterministic tie-break when two mapped roles share a rank.
_SECONDARY_ORDER: dict[UserRole, int] = {
    UserRole.ADMIN: 3,
    UserRole.MEMBER: 2,
    UserRole.AGENT_RUNNER: 1,
    UserRole.VIEWER: 0,
}


def _role_sort_key(role: UserRole) -> tuple[int, int]:
    return (ROLE_RANK[role], _SECONDARY_ORDER[role])


def resolve_role(groups: list[str], group_role_map: dict[str, str], default_role: str) -> str:
    """Highest-privilege role among the *mapped* groups, else ``default_role``."""
    mapped: list[UserRole] = []
    for group in groups:
        raw = group_role_map.get(group)
        if raw is None:
            continue
        try:
            mapped.append(UserRole(raw))
        except ValueError:
            continue  # a misconfigured mapping never grants anything
    if not mapped:
        return default_role
    return max(mapped, key=_role_sort_key).value


def _first(attributes: dict[str, list[str]], key: str | None) -> str | None:
    if not key:
        return None
    values = attributes.get(key) or []
    return values[0] if values else None


def map_assertion(
    assertion: SamlAssertion,
    *,
    mapping: AttributeMapping,
    group_role_map: dict[str, str],
    default_role: str,
) -> MappedIdentity:
    """Apply the admin-configured mapping to a validated assertion."""
    email = _first(assertion.attributes, mapping.email) or assertion.name_id
    name = _first(assertion.attributes, mapping.name)
    if name is None:
        first = _first(assertion.attributes, mapping.first_name)
        last = _first(assertion.attributes, mapping.last_name)
        if first or last:
            name = " ".join(part for part in (first, last) if part)
    groups = list(assertion.attributes.get(mapping.groups, [])) if mapping.groups else []
    role = resolve_role(groups, group_role_map, default_role)
    return MappedIdentity(
        email=email.strip().lower(),
        name=name,
        role=role,
        groups=groups,
        external_id=assertion.name_id,
        name_id_format=assertion.name_id_format,
    )


__all__ = ["map_assertion", "resolve_role"]
