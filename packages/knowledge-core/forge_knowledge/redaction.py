"""Defensive secret redaction for persisted retrieval content (F20).

F09's gateway redacts MCP resource content on egress, but F20 persists that
content *at rest* in ``retrieval_chunk`` — so a redaction miss would be durable.
:func:`redact_secrets` is the defence-in-depth pass run again immediately before
persistence (idempotent). It covers the generic patterns the gateway already
strips (bearer tokens, ``key=value`` secrets, JWTs) plus two high-value patterns
common in indexed docs/runbooks: AWS access-key ids and PEM private-key blocks.
"""

from __future__ import annotations

import re

__all__ = ["REDACTED", "redact_secrets"]

#: Placeholder substituted for any redacted secret (matches forge_mcp.security).
REDACTED = "[redacted]"

# PEM private-key blocks (RSA/EC/OPENSSH/PGP/etc.) — matched first and whole.
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
# AWS access key ids (AKIA/ASIA/... + 16 base32 chars).
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[0-9A-Z]{16}\b")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(?:token|secret|password|passwd|api[_-]?key|authorization|"
    r"aws_secret_access_key|aws_access_key_id|private_key|client_secret)\b\s*[:=]\s*\S+"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")


def redact_secrets(text: str) -> str:
    """Return ``text`` with secrets masked. Idempotent and never raises."""
    if not text:
        return text
    text = _PEM_RE.sub(REDACTED, text)
    text = _BEARER_RE.sub("Bearer " + REDACTED, text)
    text = _KV_SECRET_RE.sub(REDACTED, text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _AWS_KEY_RE.sub(REDACTED, text)
    return text
