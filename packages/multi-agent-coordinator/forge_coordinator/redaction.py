"""Secret redaction for persisted supervision state (F27 §8, AC 20).

Reuses the single canonical redactor (``forge_knowledge.redaction.redact_secrets``)
when importable, with an identical-pattern local fallback so the coordinator never
persists a model key / configured secret into ``sub_agent_run``, ``supervision``,
the returned ``AgentRunResult``, or logs.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["REDACTED", "redact_obj", "redact_secrets"]

REDACTED = "[redacted]"

try:  # pragma: no cover - exercised when forge_knowledge is installed
    from forge_knowledge.redaction import REDACTED as REDACTED
    from forge_knowledge.redaction import redact_secrets as redact_secrets
except Exception:  # pragma: no cover - hermetic fallback
    _PEM_RE = re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    )
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


_SECRET_KEY_SUBSTRINGS = (
    "password",
    "passwd",
    "secret",
    "authorization",
    "api_key",
    "apikey",
    "api-key",
    "private_key",
    "client_secret",
    "credential",
)
_SECRET_KEY_EXACT = {"token", "access_token", "refresh_token", "api_token", "auth_token", "key"}


def _is_secret_key(key: str) -> bool:
    k = key.strip().lower()
    if k in _SECRET_KEY_EXACT or k.endswith("_token") or k.endswith("_secret"):
        return True
    return any(sub in k for sub in _SECRET_KEY_SUBSTRINGS)


def redact_obj(value: Any) -> Any:
    """Recursively redact secrets in a JSON-ish structure.

    Keys whose name looks secret (``api_key``, ``api_key_ref``, ``token``,
    ``secret`` ...) are masked wholesale; string values everywhere are passed
    through :func:`redact_secrets`. Matching is precise enough that benign keys
    like ``token_usage`` are NOT masked.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _is_secret_key(k):
                out[k] = REDACTED
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value
