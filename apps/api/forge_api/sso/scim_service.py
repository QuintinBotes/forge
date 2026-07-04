"""SCIM 2.0 service-provider services (F33): Users + Groups over SQLAlchemy.

Maps RFC 7643 resources onto the F37 ``app_user`` substrate plus the F33
``external_identity`` / ``scim_group`` tables. The workspace always comes from
the authenticated SCIM token — never from the payload (confused-deputy guard);
unknown resources return 404-shaped SCIM errors without existence leaks.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forge_api.sso.attribute_mapping import resolve_role
from forge_api.sso.errors import ScimApiError
from forge_api.sso.provisioning import (
    SessionRevoker,
    deprovision_user,
    emit_sso_audit,
)
from forge_api.sso.scim_filter import parse_scim_filter
from forge_contracts.sso import (
    ScimEmail,
    ScimGroupRef,
    ScimListResponse,
    ScimMember,
    ScimMeta,
    ScimName,
    ScimPatchRequest,
    ScimUser,
)
from forge_contracts.sso import (
    ScimGroup as ScimGroupResource,
)
from forge_db.models import (
    ExternalIdentity,
    ScimGroup,
    ScimGroupMember,
    SsoConfiguration,
    User,
)
from forge_db.models.enums import ExternalIdentityProvider, UserRole


def _not_found(kind: str) -> ScimApiError:
    return ScimApiError(404, f"{kind} not found")


def _new_scim_id() -> str:
    return uuid.uuid4().hex


class ScimUserService:
    """The SCIM ``/Users`` surface for one workspace-scoped session."""

    def __init__(
        self,
        session: Session,
        *,
        base_url: str,
        revoke_sessions: SessionRevoker,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._revoke_sessions = revoke_sessions

    # -- resource mapping --------------------------------------------------- #

    def _location(self, scim_id: str) -> str:
        return f"{self._base_url}/scim/v2/Users/{scim_id}"

    def _to_resource(self, user: User, link: ExternalIdentity) -> ScimUser:
        groups = self._session.execute(
            select(ScimGroup)
            .join(ScimGroupMember, ScimGroupMember.group_id == ScimGroup.id)
            .where(ScimGroupMember.user_id == user.id)
        ).scalars()
        return ScimUser(
            id=link.scim_resource_id,
            externalId=link.external_id if link.external_id != user.email else None,
            userName=user.email,
            name=ScimName(formatted=user.name) if user.name else None,
            displayName=user.name,
            emails=[ScimEmail(value=user.email)],
            active=user.is_active,
            groups=[
                ScimGroupRef(value=g.scim_id, display=g.display_name) for g in groups
            ],
            meta=ScimMeta(
                resourceType="User",
                created=user.created_at,
                lastModified=user.updated_at,
                location=self._location(link.scim_resource_id or ""),
            ),
        )

    def _get_link(self, workspace_id: uuid.UUID, scim_id: str) -> ExternalIdentity:
        link = self._session.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.workspace_id == workspace_id,
                ExternalIdentity.provider == ExternalIdentityProvider.SCIM,
                ExternalIdentity.scim_resource_id == scim_id,
            )
        ).scalar_one_or_none()
        if link is None:
            raise _not_found("User")
        return link

    def _get_pair(
        self, workspace_id: uuid.UUID, scim_id: str
    ) -> tuple[User, ExternalIdentity]:
        link = self._get_link(workspace_id, scim_id)
        user = self._session.get(User, link.user_id)
        if user is None or user.workspace_id != workspace_id:
            raise _not_found("User")
        return user, link

    @staticmethod
    def _resolve_email(payload: ScimUser) -> str:
        candidate = payload.userName.strip().lower()
        if "@" in candidate:
            return candidate
        for email in payload.emails:
            if email.primary and "@" in email.value:
                return email.value.strip().lower()
        for email in payload.emails:
            if "@" in email.value:
                return email.value.strip().lower()
        raise ScimApiError(400, "userName/emails carry no usable email", "invalidValue")

    @staticmethod
    def _resolve_name(payload: ScimUser) -> str | None:
        if payload.displayName:
            return payload.displayName
        if payload.name:
            if payload.name.formatted:
                return payload.name.formatted
            parts = [p for p in (payload.name.givenName, payload.name.familyName) if p]
            if parts:
                return " ".join(parts)
        return None

    def _default_role(self, workspace_id: uuid.UUID) -> UserRole:
        config = self._session.execute(
            select(SsoConfiguration).where(SsoConfiguration.workspace_id == workspace_id)
        ).scalar_one_or_none()
        return config.default_role if config is not None else UserRole.MEMBER

    # -- CRUD ----------------------------------------------------------------- #

    def create(self, workspace_id: uuid.UUID, payload: ScimUser) -> ScimUser:
        email = self._resolve_email(payload)
        existing = self._session.execute(
            select(User).where(
                User.workspace_id == workspace_id, func.lower(User.email) == email
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ScimApiError(409, f"userName {email!r} already exists", "uniqueness")

        user = User(
            workspace_id=workspace_id,
            email=email,
            name=self._resolve_name(payload),
            role=self._default_role(workspace_id),
            is_active=payload.active,
            external_managed=True,
        )
        self._session.add(user)
        try:
            self._session.flush()
        except IntegrityError as exc:  # concurrent create
            raise ScimApiError(409, f"userName {email!r} already exists", "uniqueness") from exc

        link = ExternalIdentity(
            workspace_id=workspace_id,
            user_id=user.id,
            provider=ExternalIdentityProvider.SCIM,
            external_id=payload.externalId or email,
            scim_resource_id=_new_scim_id(),
        )
        self._session.add(link)
        self._session.flush()
        emit_sso_audit(
            self._session,
            workspace_id=workspace_id,
            action="scim.user_created",
            actor_type="scim",
            target_type="user",
            target_id=user.id,
            details={"email": email},
        )
        return self._to_resource(user, link)

    def get(self, workspace_id: uuid.UUID, scim_id: str) -> ScimUser:
        user, link = self._get_pair(workspace_id, scim_id)
        return self._to_resource(user, link)

    def list(
        self,
        workspace_id: uuid.UUID,
        *,
        filter: str | None = None,
        start_index: int = 1,
        count: int = 100,
    ) -> ScimListResponse:
        stmt = (
            select(User, ExternalIdentity)
            .join(ExternalIdentity, ExternalIdentity.user_id == User.id)
            .where(
                User.workspace_id == workspace_id,
                ExternalIdentity.provider == ExternalIdentityProvider.SCIM,
            )
        )
        if filter:
            stmt = stmt.where(parse_scim_filter(filter))
        stmt = stmt.order_by(User.created_at, User.id)
        rows = self._session.execute(stmt).all()
        start = max(start_index, 1)
        page = rows[start - 1 : start - 1 + max(count, 0)]
        return ScimListResponse(
            totalResults=len(rows),
            startIndex=start,
            itemsPerPage=len(page),
            Resources=[
                self._to_resource(user, link).model_dump(mode="json", exclude_none=True)
                for user, link in page
            ],
        )

    def replace(
        self, workspace_id: uuid.UUID, scim_id: str, payload: ScimUser
    ) -> ScimUser:
        user, link = self._get_pair(workspace_id, scim_id)
        email = self._resolve_email(payload)
        if email != user.email.lower():
            clash = self._session.execute(
                select(User).where(
                    User.workspace_id == workspace_id,
                    func.lower(User.email) == email,
                    User.id != user.id,
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise ScimApiError(409, f"userName {email!r} already exists", "uniqueness")
            user.email = email
        user.name = self._resolve_name(payload)
        if payload.externalId:
            link.external_id = payload.externalId
        self._apply_active(workspace_id, user, payload.active)
        self._session.flush()
        emit_sso_audit(
            self._session,
            workspace_id=workspace_id,
            action="scim.user_updated",
            actor_type="scim",
            target_type="user",
            target_id=user.id,
            details={"op": "replace"},
        )
        return self._to_resource(user, link)

    def patch(
        self, workspace_id: uuid.UUID, scim_id: str, req: ScimPatchRequest
    ) -> ScimUser:
        user, link = self._get_pair(workspace_id, scim_id)
        for operation in req.Operations:
            op = operation.op.lower()
            if op not in ("add", "replace", "remove"):
                raise ScimApiError(400, f"unsupported patch op {operation.op!r}", "invalidValue")
            path = (operation.path or "").strip()
            value = operation.value
            if not path and isinstance(value, dict):
                for key, val in value.items():
                    self._apply_patch_field(workspace_id, user, link, key, val)
            elif path:
                self._apply_patch_field(workspace_id, user, link, path, value)
            else:
                raise ScimApiError(400, "patch op requires a path or object value", "invalidPath")
        self._session.flush()
        emit_sso_audit(
            self._session,
            workspace_id=workspace_id,
            action="scim.user_updated",
            actor_type="scim",
            target_type="user",
            target_id=user.id,
            details={"op": "patch"},
        )
        return self._to_resource(user, link)

    def deactivate(self, workspace_id: uuid.UUID, scim_id: str) -> None:
        user, _link = self._get_pair(workspace_id, scim_id)
        if user.is_active:
            deprovision_user(
                session=self._session,
                workspace_id=workspace_id,
                user_id=user.id,
                revoke_sessions=self._revoke_sessions,
            )

    # -- helpers ---------------------------------------------------------------- #

    def _apply_patch_field(
        self,
        workspace_id: uuid.UUID,
        user: User,
        link: ExternalIdentity,
        raw_path: str,
        value: object,
    ) -> None:
        path = raw_path.strip().lower()
        if path == "active":
            self._apply_active(workspace_id, user, _as_bool(value))
        elif path == "username":
            if not isinstance(value, str) or "@" not in value:
                raise ScimApiError(400, "userName must be an email", "invalidValue")
            user.email = value.strip().lower()
        elif path in ("displayname", "name.formatted"):
            user.name = str(value) if value is not None else None
        elif path == "name.givenname":
            family = (user.name or "").split(" ", 1)
            user.name = f"{value} {family[1]}" if len(family) == 2 else str(value)
        elif path == "name.familyname":
            given = (user.name or "").split(" ", 1)[0]
            user.name = f"{given} {value}".strip()
        elif path == "externalid":
            link.external_id = str(value)
        else:
            raise ScimApiError(400, f"unsupported patch path {raw_path!r}", "invalidPath")

    def _apply_active(self, workspace_id: uuid.UUID, user: User, active: bool) -> None:
        if active and not user.is_active:
            user.is_active = True
            user.deactivated_at = None
        elif not active and user.is_active:
            deprovision_user(
                session=self._session,
                workspace_id=workspace_id,
                user_id=user.id,
                revoke_sessions=self._revoke_sessions,
            )


class ScimGroupService:
    """The SCIM ``/Groups`` surface: CRUD + membership → effective role."""

    def __init__(self, session: Session, *, base_url: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")

    def _location(self, scim_id: str) -> str:
        return f"{self._base_url}/scim/v2/Groups/{scim_id}"

    def _config(self, workspace_id: uuid.UUID) -> SsoConfiguration | None:
        return self._session.execute(
            select(SsoConfiguration).where(SsoConfiguration.workspace_id == workspace_id)
        ).scalar_one_or_none()

    def _get_group(self, workspace_id: uuid.UUID, scim_id: str) -> ScimGroup:
        group = self._session.execute(
            select(ScimGroup).where(
                ScimGroup.workspace_id == workspace_id, ScimGroup.scim_id == scim_id
            )
        ).scalar_one_or_none()
        if group is None:
            raise _not_found("Group")
        return group

    def _to_resource(self, group: ScimGroup) -> ScimGroupResource:
        member_rows = self._session.execute(
            select(ScimGroupMember, ExternalIdentity)
            .join(
                ExternalIdentity,
                (ExternalIdentity.user_id == ScimGroupMember.user_id)
                & (ExternalIdentity.provider == ExternalIdentityProvider.SCIM),
            )
            .where(ScimGroupMember.group_id == group.id)
        ).all()
        return ScimGroupResource(
            id=group.scim_id,
            externalId=group.external_id,
            displayName=group.display_name,
            members=[
                ScimMember(value=link.scim_resource_id or "")
                for _member, link in member_rows
            ],
            meta=ScimMeta(
                resourceType="Group",
                created=group.created_at,
                lastModified=group.updated_at,
                location=self._location(group.scim_id),
            ),
        )

    def _mapped_role(self, workspace_id: uuid.UUID, display_name: str) -> UserRole | None:
        config = self._config(workspace_id)
        if config is None:
            return None
        raw = (config.group_role_map or {}).get(display_name)
        if raw is None:
            return None
        try:
            return UserRole(raw)
        except ValueError:
            return None

    def _resolve_member_user(self, workspace_id: uuid.UUID, member_value: str) -> User:
        link = self._session.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.workspace_id == workspace_id,
                ExternalIdentity.provider == ExternalIdentityProvider.SCIM,
                ExternalIdentity.scim_resource_id == member_value,
            )
        ).scalar_one_or_none()
        if link is None:
            raise ScimApiError(400, f"unknown member {member_value!r}", "invalidValue")
        user = self._session.get(User, link.user_id)
        if user is None:
            raise ScimApiError(400, f"unknown member {member_value!r}", "invalidValue")
        return user

    def recompute_effective_role(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """User's role = highest-privilege mapped group role, else default_role."""
        config = self._config(workspace_id)
        if config is None:
            return
        user = self._session.get(User, user_id)
        if user is None or user.workspace_id != workspace_id:
            return
        group_names = [
            name
            for (name,) in self._session.execute(
                select(ScimGroup.display_name)
                .join(ScimGroupMember, ScimGroupMember.group_id == ScimGroup.id)
                .where(ScimGroupMember.user_id == user_id)
            ).all()
        ]
        resolved = resolve_role(
            group_names, config.group_role_map or {}, config.default_role.value
        )
        if user.role != UserRole(resolved):
            user.role = UserRole(resolved)
            self._session.flush()

    # -- CRUD ------------------------------------------------------------------ #

    def create(self, workspace_id: uuid.UUID, payload: ScimGroupResource) -> ScimGroupResource:
        clash = self._session.execute(
            select(ScimGroup).where(
                ScimGroup.workspace_id == workspace_id,
                ScimGroup.display_name == payload.displayName,
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise ScimApiError(
                409, f"group {payload.displayName!r} already exists", "uniqueness"
            )
        group = ScimGroup(
            workspace_id=workspace_id,
            scim_id=_new_scim_id(),
            external_id=payload.externalId,
            display_name=payload.displayName,
            mapped_role=self._mapped_role(workspace_id, payload.displayName),
        )
        self._session.add(group)
        self._session.flush()
        for member in payload.members:
            member_user_id = self._add_member(workspace_id, group, member.value)
            self.recompute_effective_role(workspace_id, member_user_id)
        emit_sso_audit(
            self._session,
            workspace_id=workspace_id,
            action="scim.group_created",
            actor_type="scim",
            target_type="scim_group",
            target_id=group.id,
            details={"display_name": group.display_name},
        )
        return self._to_resource(group)

    def get(self, workspace_id: uuid.UUID, scim_id: str) -> ScimGroupResource:
        return self._to_resource(self._get_group(workspace_id, scim_id))

    def list(
        self, workspace_id: uuid.UUID, *, start_index: int = 1, count: int = 100
    ) -> ScimListResponse:
        groups = (
            self._session.execute(
                select(ScimGroup)
                .where(ScimGroup.workspace_id == workspace_id)
                .order_by(ScimGroup.created_at, ScimGroup.id)
            )
            .scalars()
            .all()
        )
        start = max(start_index, 1)
        page = groups[start - 1 : start - 1 + max(count, 0)]
        return ScimListResponse(
            totalResults=len(groups),
            startIndex=start,
            itemsPerPage=len(page),
            Resources=[
                self._to_resource(g).model_dump(mode="json", exclude_none=True)
                for g in page
            ],
        )

    def replace(
        self, workspace_id: uuid.UUID, scim_id: str, payload: ScimGroupResource
    ) -> ScimGroupResource:
        group = self._get_group(workspace_id, scim_id)
        group.display_name = payload.displayName
        group.external_id = payload.externalId
        group.mapped_role = self._mapped_role(workspace_id, payload.displayName)
        current = self._member_user_ids(group)
        self._clear_members(group)
        for member in payload.members:
            self._add_member(workspace_id, group, member.value)
        for user_id in current | self._member_user_ids(group):
            self.recompute_effective_role(workspace_id, user_id)
        self._session.flush()
        return self._to_resource(group)

    def patch(
        self, workspace_id: uuid.UUID, scim_id: str, req: ScimPatchRequest
    ) -> ScimGroupResource:
        group = self._get_group(workspace_id, scim_id)
        touched: set[uuid.UUID] = set()
        for operation in req.Operations:
            op = operation.op.lower()
            path = (operation.path or "").strip().lower()
            if path == "members" or path.startswith("members["):
                touched |= self._patch_members(workspace_id, group, op, operation)
            elif path == "displayname" and op in ("replace", "add"):
                group.display_name = str(operation.value)
                group.mapped_role = self._mapped_role(workspace_id, group.display_name)
                touched |= self._member_user_ids(group)
            else:
                raise ScimApiError(400, f"unsupported patch path {operation.path!r}", "invalidPath")
        for user_id in touched:
            self.recompute_effective_role(workspace_id, user_id)
        self._session.flush()
        return self._to_resource(group)

    def delete(self, workspace_id: uuid.UUID, scim_id: str) -> None:
        group = self._get_group(workspace_id, scim_id)
        members = self._member_user_ids(group)
        self._clear_members(group)
        self._session.delete(group)
        self._session.flush()
        for user_id in members:
            self.recompute_effective_role(workspace_id, user_id)
        emit_sso_audit(
            self._session,
            workspace_id=workspace_id,
            action="scim.group_deleted",
            actor_type="scim",
            target_type="scim_group",
            target_id=group.id,
            details={"display_name": group.display_name},
        )

    # -- membership -------------------------------------------------------------- #

    def _member_user_ids(self, group: ScimGroup) -> set[uuid.UUID]:
        return {
            user_id
            for (user_id,) in self._session.execute(
                select(ScimGroupMember.user_id).where(ScimGroupMember.group_id == group.id)
            ).all()
        }

    def _clear_members(self, group: ScimGroup) -> None:
        for member in (
            self._session.execute(
                select(ScimGroupMember).where(ScimGroupMember.group_id == group.id)
            )
            .scalars()
            .all()
        ):
            self._session.delete(member)
        self._session.flush()

    def _add_member(self, workspace_id: uuid.UUID, group: ScimGroup, value: str) -> uuid.UUID:
        user = self._resolve_member_user(workspace_id, value)
        exists = self._session.execute(
            select(ScimGroupMember).where(
                ScimGroupMember.group_id == group.id, ScimGroupMember.user_id == user.id
            )
        ).scalar_one_or_none()
        if exists is None:
            self._session.add(
                ScimGroupMember(
                    workspace_id=workspace_id, group_id=group.id, user_id=user.id
                )
            )
            self._session.flush()
        return user.id

    def _patch_members(
        self,
        workspace_id: uuid.UUID,
        group: ScimGroup,
        op: str,
        operation,
    ) -> set[uuid.UUID]:
        touched: set[uuid.UUID] = set()
        path = (operation.path or "").strip()
        if op == "remove":
            # Okta/Entra style: path='members[value eq "<id>"]' or explicit values.
            match = re.search(r'members\[value\s+eq\s+"([^"]+)"\]', path, re.IGNORECASE)
            values: list[str] = []
            if match:
                values = [match.group(1)]
            elif isinstance(operation.value, list):
                values = [m.get("value") for m in operation.value if isinstance(m, dict)]
            if not values:  # bare `remove members` == clear all
                touched |= self._member_user_ids(group)
                self._clear_members(group)
                return touched
            for value in values:
                user = self._resolve_member_user(workspace_id, value)
                member = self._session.execute(
                    select(ScimGroupMember).where(
                        ScimGroupMember.group_id == group.id,
                        ScimGroupMember.user_id == user.id,
                    )
                ).scalar_one_or_none()
                if member is not None:
                    self._session.delete(member)
                    self._session.flush()
                touched.add(user.id)
            return touched
        if op in ("add", "replace"):
            if op == "replace":
                touched |= self._member_user_ids(group)
                self._clear_members(group)
            members = operation.value if isinstance(operation.value, list) else []
            for member in members:
                value = member.get("value") if isinstance(member, dict) else None
                if not value:
                    raise ScimApiError(400, "member entries need a 'value'", "invalidValue")
                touched.add(self._add_member(workspace_id, group, value))
            return touched
        raise ScimApiError(400, f"unsupported members op {op!r}", "invalidValue")


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    raise ScimApiError(400, "active must be a boolean", "invalidValue")


__all__ = ["ScimGroupService", "ScimUserService"]
