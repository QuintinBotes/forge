"""Protocol surface of the auth SDK — re-exported from the frozen contract layer.

``AuditSink`` is deliberately NOT defined here: it is owned by
``cross-cutting/F39-audit-log`` in :mod:`scf_contracts.audit` and imported
from there by producers.
"""

from __future__ import annotations

from scf_contracts.audit import AuditSink
from scf_contracts.auth import KeyProvider, RateLimiter, SecretRedactor, Vault

__all__ = ["AuditSink", "KeyProvider", "RateLimiter", "SecretRedactor", "Vault"]
