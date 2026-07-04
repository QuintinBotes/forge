"""Edge security controls for the Forge API (HARD-09).

Net-new hardening that wraps the existing primitives (RBAC, vault, policy,
redaction, audit chain) with request-path controls:

* :mod:`forge_api.security.ssrf` — outbound URL guard (:func:`assert_safe_url`).
* :mod:`forge_api.security.ratelimit` — per-caller token-bucket rate limiting.
* :mod:`forge_api.security.bodylimit` — request body size bound (413).
* :mod:`forge_api.security.headers` — security response headers.

All are mounted by :func:`forge_api.main.create_app`; the SSRF guard is also
injected (DI) into the leaf HTTP clients in ``forge_knowledge`` so the pure
packages stay decoupled from ``forge_api``.
"""

from forge_api.security.bodylimit import DEFAULT_MAX_BODY_BYTES, BodySizeLimitMiddleware
from forge_api.security.headers import SecurityHeadersMiddleware
from forge_api.security.ratelimit import RateLimitMiddleware, TokenBucket
from forge_api.security.ssrf import SsrfBlockedError, assert_safe_url, is_public_host

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "BodySizeLimitMiddleware",
    "RateLimitMiddleware",
    "SecurityHeadersMiddleware",
    "SsrfBlockedError",
    "TokenBucket",
    "assert_safe_url",
    "is_public_host",
]
