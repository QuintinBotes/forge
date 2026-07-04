"""F33 enterprise SSO: SAML 2.0 SP + SCIM 2.0 service provider.

Pure, FastAPI-free modules (unit-testable, importable by the worker):

* :mod:`forge_api.sso.saml` — AuthnRequest build + SAMLResponse validation.
* :mod:`forge_api.sso.saml_metadata` — XXE-hardened metadata parse/render.
* :mod:`forge_api.sso.attribute_mapping` — assertion attrs → Forge identity.
* :mod:`forge_api.sso.scim_service` / :mod:`forge_api.sso.scim_filter` — SCIM.
* :mod:`forge_api.sso.provisioning` — JIT provision / link / deprovision.
* :mod:`forge_api.sso.config_service` — admin config + SCIM token lifecycle.
* :mod:`forge_api.sso.replay` — replay guards (in-memory + DB fallback).
"""

from forge_api.sso.errors import (
    DomainConflictError,
    LastAdminError,
    SamlValidationError,
    ScimApiError,
    SsoConfigError,
    SsoError,
)

__all__ = [
    "DomainConflictError",
    "LastAdminError",
    "SamlValidationError",
    "ScimApiError",
    "SsoConfigError",
    "SsoError",
]
