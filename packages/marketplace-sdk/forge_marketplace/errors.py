"""Marketplace SDK errors.

These map (in the API router) to HTTP status codes and to
``marketplace_audit_log.error_code`` values (F32 §3.1). The distinction that
matters for the trust boundary: :class:`HashMismatch` / :class:`SignatureInvalid`
/ :class:`SchemaInvalid` / :class:`SignatureRequired` /
:class:`ForgeVersionIncompatible` / :class:`NameCollision` are **install-blocking**
(422/409); :class:`SsrfBlocked` / :class:`RegistryFetchError` are sync/fetch
failures; :class:`YankedVersion` is a soft warning surfaced to the admin.
"""

from __future__ import annotations


class MarketplaceError(Exception):
    """Base class for all marketplace SDK errors."""

    #: Stable code recorded in ``marketplace_audit_log.error_code``.
    error_code: str = "marketplace_error"


class HashMismatch(MarketplaceError):
    error_code = "hash_mismatch"


class SignatureInvalid(MarketplaceError):
    error_code = "signature_invalid"


class SignatureRequired(MarketplaceError):
    """An unsigned / untrusted package installed without acknowledgement."""

    error_code = "signature_required"


class UntrustedRegistry(MarketplaceError):
    error_code = "untrusted_registry"


class SchemaInvalid(MarketplaceError):
    error_code = "schema_invalid"


class NameCollision(MarketplaceError):
    error_code = "name_collision"


class ForgeVersionIncompatible(MarketplaceError):
    error_code = "forge_version_incompatible"


class RegistryFetchError(MarketplaceError):
    error_code = "registry_fetch_error"


class SsrfBlocked(RegistryFetchError):
    error_code = "ssrf_blocked"


class YankedVersion(MarketplaceError):
    error_code = "yanked"


class UnknownArtifactKind(MarketplaceError):
    """The kind has no registered installer (e.g. a reserved workflow/policy kind)."""

    error_code = "unknown_kind"


__all__ = [
    "ForgeVersionIncompatible",
    "HashMismatch",
    "MarketplaceError",
    "NameCollision",
    "RegistryFetchError",
    "SchemaInvalid",
    "SignatureInvalid",
    "SignatureRequired",
    "SsrfBlocked",
    "UnknownArtifactKind",
    "UntrustedRegistry",
    "YankedVersion",
]
