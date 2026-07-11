"""F33 enterprise-SSO error taxonomy (FastAPI-free; routers map to HTTP)."""

from __future__ import annotations


class SsoError(Exception):
    """Base class for every F33 SSO/SCIM error."""


class SamlValidationError(SsoError):
    """A ``SAMLResponse`` failed validation. ``reason`` is a stable code."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"saml validation failed: {reason}" + (f" ({detail})" if detail else ""))


class OidcValidationError(SsoError):
    """An OIDC token/callback failed validation. ``reason`` is a stable code."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"oidc validation failed: {reason}" + (f" ({detail})" if detail else ""))


class SsoConfigError(SsoError):
    """Invalid or missing SSO configuration for the requested operation."""


class DomainConflictError(SsoError):
    """A HRD domain is already bound to another workspace's IdP config."""

    def __init__(self, domain: str) -> None:
        self.domain = domain
        super().__init__(f"domain '{domain}' is already bound to another SSO configuration")


class LastAdminError(SsoError):
    """The operation would leave the workspace without a break-glass admin."""


class ScimApiError(SsoError):
    """A SCIM-protocol error carrying the RFC 7644 error shape."""

    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        self.status = status
        self.detail = detail
        self.scim_type = scim_type
        super().__init__(f"scim error {status}: {detail}")


__all__ = [
    "DomainConflictError",
    "LastAdminError",
    "OidcValidationError",
    "SamlValidationError",
    "ScimApiError",
    "SsoConfigError",
    "SsoError",
]
