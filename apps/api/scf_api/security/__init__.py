"""Edge security controls for the SCF API (HARD-09).

Net-new hardening that wraps the existing primitives (RBAC, vault, policy,
redaction, audit chain) with request-path controls:

* :mod:`scf_api.security.ssrf` — outbound URL guard (:func:`assert_safe_url`).
* :mod:`scf_api.security.ratelimit` — per-caller token-bucket rate limiting.
* :mod:`scf_api.security.bodylimit` — request body size bound (413).
* :mod:`scf_api.security.headers` — security response headers.

All are mounted by :func:`scf_api.main.create_app`; the SSRF guard is also
injected (DI) into the leaf HTTP clients in ``scf_knowledge`` so the pure
packages stay decoupled from ``scf_api``.
"""

from scf_api.security.bodylimit import DEFAULT_MAX_BODY_BYTES, BodySizeLimitMiddleware
from scf_api.security.headers import SecurityHeadersMiddleware
from scf_api.security.ratelimit import RateLimitMiddleware, TokenBucket
from scf_api.security.ssrf import SsrfBlockedError, assert_safe_url, is_public_host

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
