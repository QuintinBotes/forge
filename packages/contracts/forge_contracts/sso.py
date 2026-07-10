"""Enterprise SSO contracts (F33): SAML + SCIM DTOs and Protocols.

Canonical DTOs for the per-workspace SAML 2.0 service-provider configuration,
the in-memory SAML runtime types, and the SCIM 2.0 (RFC 7643/7644) resource
models Forge serves as a SCIM service provider. Like ``forge_contracts.audit``
this module is additive: it is imported as ``forge_contracts.sso`` and does not
alter the frozen ``forge_contracts.__all__`` surface.

Deviation from the idealized slice doc: ``EmailStr`` is not used (it would pull
the ``email-validator`` dependency into the frozen contracts package); email
fields are plain ``str`` validated at the service layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# SAML config DTOs
# ---------------------------------------------------------------------------

#: Default SAML NameID format (email address).
NAMEID_FORMAT_EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"


class AttributeMapping(BaseModel):
    """Where in the assertion each Forge identity field comes from.

    An empty string / ``None`` for ``email`` means "use the NameID".
    """

    email: str = ""
    name: str | None = None
    first_name: str | None = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname"
    last_name: str | None = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname"
    groups: str | None = None


class SamlIdpConfig(BaseModel):
    """The identity-provider half of a SAML federation."""

    entity_id: str
    sso_url: str
    slo_url: str | None = None
    x509_certs: list[str] = Field(min_length=1)
    name_id_format: str = NAMEID_FORMAT_EMAIL


#: Default OIDC scopes — ``openid`` is mandatory; ``email``/``profile`` back the
#: identity claims Forge maps.
OIDC_DEFAULT_SCOPES = ["openid", "email", "profile"]


class OidcIdpConfig(BaseModel):
    """The identity-provider half of an OpenID Connect federation.

    Endpoints are resolved from OpenID discovery
    (``{issuer}/.well-known/openid-configuration`` or an explicit
    ``discovery_url``); the optional ``*_endpoint`` / ``jwks_uri`` overrides let
    an admin pin them when an IdP does not publish discovery. The client secret
    is **never** carried in this DTO — only ``client_secret_ref``, an opaque
    handle to the ciphertext the config service holds in the vault.
    """

    issuer: str
    discovery_url: str | None = None
    client_id: str
    #: Vault handle for the OAuth client secret (never the plaintext).
    client_secret_ref: str
    scopes: list[str] = Field(default_factory=lambda: list(OIDC_DEFAULT_SCOPES))
    #: ID-token / userinfo claim names Forge maps onto its identity fields.
    email_claim: str = "email"
    name_claim: str = "name"
    groups_claim: str = "groups"
    #: Optional role mapping (mirrors the SAML config's group→role resolution).
    default_role: Literal["admin", "member", "viewer", "agent-runner"] = "member"
    group_role_map: dict[str, str] = Field(default_factory=dict)
    #: Discovery overrides (used verbatim when present; else discovery is fetched).
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    jwks_uri: str | None = None


class SsoConfigIn(BaseModel):
    """Create/replace payload for a workspace SSO configuration (SAML or OIDC)."""

    protocol: Literal["saml", "oidc"] = "saml"
    enabled: bool = False
    metadata_url: str | None = None
    metadata_xml: str | None = None
    idp: SamlIdpConfig | None = None
    oidc: OidcIdpConfig | None = None
    domains: list[str] = Field(default_factory=list)
    allow_idp_initiated: bool = False
    sign_authn_requests: bool = True
    want_assertions_signed: bool = True
    want_name_id_encrypted: bool = False
    attribute_mapping: AttributeMapping = Field(default_factory=AttributeMapping)
    default_role: Literal["admin", "member", "viewer", "agent-runner"] = "member"
    group_role_map: dict[str, str] = Field(default_factory=dict)
    jit_provisioning: bool = True


class SsoConfigOut(BaseModel):
    """Public view of a workspace SAML configuration.

    The SP private key is **never** part of this model — only the public
    signing certificate is exposed.
    """

    id: str
    workspace_id: str
    protocol: Literal["saml", "oidc"]
    enabled: bool
    idp: SamlIdpConfig | None = None
    oidc: OidcIdpConfig | None = None
    sp_entity_id: str
    sp_acs_url: str
    sp_slo_url: str
    sp_metadata_url: str
    sp_cert_pem: str
    domains: list[str]
    allow_idp_initiated: bool
    sign_authn_requests: bool
    want_assertions_signed: bool
    attribute_mapping: AttributeMapping
    default_role: str
    group_role_map: dict[str, str]
    jit_provisioning: bool
    last_metadata_refresh_at: datetime | None = None


# ---------------------------------------------------------------------------
# SAML runtime DTOs
# ---------------------------------------------------------------------------


class SamlAssertion(BaseModel):
    """A validated SAML assertion (in-memory only; never persisted)."""

    assertion_id: str
    name_id: str
    name_id_format: str
    session_index: str | None = None
    issuer: str
    attributes: dict[str, list[str]] = Field(default_factory=dict)
    not_on_or_after: datetime
    in_response_to: str | None = None


class MappedIdentity(BaseModel):
    """The Forge identity resolved from an assertion / SCIM payload."""

    email: str
    name: str | None = None
    role: str
    groups: list[str] = Field(default_factory=list)
    external_id: str
    name_id_format: str = NAMEID_FORMAT_EMAIL


# ---------------------------------------------------------------------------
# SAML Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class SamlValidator(Protocol):
    """Builds signed ``AuthnRequest``s and validates ``SAMLResponse``s."""

    def build_authn_request(
        self,
        config: SamlIdpConfig,
        *,
        sp_entity_id: str,
        acs_url: str,
        relay_state: str,
        sign: bool,
        sp_private_key_pem: str | None,
    ) -> tuple[str, str]:
        """Return ``(redirect_url_with_SAMLRequest, request_id)``."""
        ...

    def validate_response(
        self,
        *,
        saml_response_b64: str,
        config: SamlIdpConfig,
        sp_entity_id: str,
        acs_url: str,
        want_signed: bool,
        expected_in_response_to: str | None,
        now: datetime,
        clock_skew_seconds: int,
    ) -> SamlAssertion:
        """Validate and parse a base64 ``SAMLResponse`` (raises on failure)."""
        ...


@runtime_checkable
class ReplayGuard(Protocol):
    """One-time stores for outstanding request ids and seen assertion ids."""

    def register_request(self, request_id: str, ttl_seconds: int) -> None: ...

    def consume_request(self, request_id: str) -> bool:
        """True if the request id was outstanding (and consume it)."""
        ...

    def seen_assertion(self, assertion_id: str, ttl_seconds: int) -> bool:
        """True if the assertion id was already accepted (replay)."""
        ...


# ---------------------------------------------------------------------------
# SCIM 2.0 resources (RFC 7643)
# ---------------------------------------------------------------------------

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCHOP_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


class ScimName(BaseModel):
    givenName: str | None = None
    familyName: str | None = None
    formatted: str | None = None


class ScimEmail(BaseModel):
    value: str
    type: str | None = "work"
    primary: bool = True


class ScimMeta(BaseModel):
    resourceType: Literal["User", "Group"]
    created: datetime
    lastModified: datetime
    location: str
    version: str | None = None


class ScimGroupRef(BaseModel):
    value: str
    display: str | None = None


class ScimUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schemas: list[str] = Field(default_factory=lambda: [USER_SCHEMA])
    id: str | None = None
    externalId: str | None = None
    userName: str
    name: ScimName | None = None
    displayName: str | None = None
    emails: list[ScimEmail] = Field(default_factory=list)
    active: bool = True
    groups: list[ScimGroupRef] = Field(default_factory=list)
    meta: ScimMeta | None = None


class ScimMember(BaseModel):
    value: str
    display: str | None = None


class ScimGroup(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schemas: list[str] = Field(default_factory=lambda: [GROUP_SCHEMA])
    id: str | None = None
    externalId: str | None = None
    displayName: str
    members: list[ScimMember] = Field(default_factory=list)
    meta: ScimMeta | None = None


class ScimListResponse(BaseModel):
    schemas: list[str] = Field(default_factory=lambda: [LIST_SCHEMA])
    totalResults: int
    startIndex: int = 1
    itemsPerPage: int
    Resources: list[dict[str, Any]] = Field(default_factory=list)


class ScimError(BaseModel):
    schemas: list[str] = Field(default_factory=lambda: [ERROR_SCHEMA])
    status: str
    scimType: str | None = None
    detail: str | None = None


class ScimPatchOperation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    op: str
    path: str | None = None
    value: Any | None = None


class ScimPatchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schemas: list[str] = Field(default_factory=lambda: [PATCHOP_SCHEMA])
    Operations: list[ScimPatchOperation]


# ---------------------------------------------------------------------------
# SCIM service Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ScimUserServiceProtocol(Protocol):
    """The SCIM ``/Users`` surface (implemented in ``forge_api.sso``)."""

    def create(self, workspace_id: str, payload: ScimUser) -> ScimUser: ...

    def get(self, workspace_id: str, scim_id: str) -> ScimUser: ...

    def list(
        self, workspace_id: str, *, filter: str | None, start_index: int, count: int
    ) -> ScimListResponse: ...

    def replace(self, workspace_id: str, scim_id: str, payload: ScimUser) -> ScimUser: ...

    def patch(self, workspace_id: str, scim_id: str, req: ScimPatchRequest) -> ScimUser: ...

    def deactivate(self, workspace_id: str, scim_id: str) -> None: ...


__all__ = [
    "ERROR_SCHEMA",
    "GROUP_SCHEMA",
    "LIST_SCHEMA",
    "NAMEID_FORMAT_EMAIL",
    "OIDC_DEFAULT_SCOPES",
    "PATCHOP_SCHEMA",
    "USER_SCHEMA",
    "AttributeMapping",
    "MappedIdentity",
    "OidcIdpConfig",
    "ReplayGuard",
    "SamlAssertion",
    "SamlIdpConfig",
    "SamlValidator",
    "ScimEmail",
    "ScimError",
    "ScimGroup",
    "ScimGroupRef",
    "ScimListResponse",
    "ScimMember",
    "ScimMeta",
    "ScimName",
    "ScimPatchOperation",
    "ScimPatchRequest",
    "ScimUser",
    "ScimUserServiceProtocol",
    "SsoConfigIn",
    "SsoConfigOut",
]
