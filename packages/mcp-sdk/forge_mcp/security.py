"""MCP security primitives (plan Task 1.12; spec: MCP Security Rules).

Pure, side-effect-free helpers shared by the client, gateway, and audit log:

* :func:`is_write_tool` — classify a tool as read vs write (rule 1).
* :func:`token_binding` — resolve the RFC 8707 ``resource`` indicator (rule 2).
* :func:`namespace_of` / :func:`resource_in_scope` / :func:`filter_resources` —
  per-connection namespace scoping (rule 5).
* :func:`redact` / :func:`payload_hash` — strip secrets and hash payloads
  before they reach logs, traces, or the audit log (rules 4 & 6).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from forge_contracts import MCPAuthType, MCPResource

if TYPE_CHECKING:
    from forge_contracts import MCPConnection
    from forge_mcp.transport import ToolSpec

#: Placeholder substituted for any redacted secret value.
REDACTED = "[redacted]"

#: Dict keys whose values are treated as secrets and masked before logging.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "credential",
        "credentials",
        "private_key",
        "session",
        "cookie",
    }
)

#: Verb tokens that positively mark a tool name as mutating. This is *not* the
#: only signal — an un-annotated tool whose verb is unrecognised is still treated
#: as a write (see :func:`is_write_tool`); the list just lets an obvious mutating
#: verb override a read-looking leading verb (e.g. ``list_and_merge``).
WRITE_KEYWORDS: frozenset[str] = frozenset(
    {
        "write",
        "create",
        "update",
        "delete",
        "remove",
        "set",
        "put",
        "patch",
        "post",
        "insert",
        "modify",
        "add",
        "edit",
        "append",
        "publish",
        "send",
        "upload",
        "drop",
        "truncate",
        "rename",
        "move",
        "destroy",
        "execute",
        "run",
        "apply",
        "archive",
        "restore",
        # Mutating verbs that fail-closed defaulting now also covers, listed so a
        # read-looking leading token can never whitewash them.
        "merge",
        "approve",
        "reject",
        "decline",
        "sync",
        "deploy",
        "release",
        "cancel",
        "close",
        "reopen",
        "assign",
        "unassign",
        "grant",
        "revoke",
        "enable",
        "disable",
        "trigger",
        "invoke",
        "dispatch",
        "transfer",
        "rollback",
        "reset",
        "revert",
        "import",
        "register",
        "deregister",
        "subscribe",
        "unsubscribe",
    }
)

#: Verb tokens that mark a tool name as clearly read-only. Only a *leading* verb
#: from this set keeps an un-annotated tool a read; everything else fails closed.
READ_KEYWORDS: frozenset[str] = frozenset(
    {
        "get",
        "list",
        "read",
        "search",
        "query",
        "fetch",
        "find",
        "view",
        "show",
        "describe",
        "count",
        "exists",
        "lookup",
        "ping",
        "head",
        "scan",
        "browse",
        "inspect",
        "retrieve",
        "summarize",
        "summarise",
        "preview",
    }
)

# Free-text secret patterns (bearer tokens, key=value secrets, JWTs).
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(?:token|secret|password|api[_-]?key|authorization)\b\s*[:=]\s*\S+"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")


def _split_name(name: str) -> list[str]:
    """Split a tool name into lowercase tokens (snake_case and camelCase)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return [t for t in re.split(r"[\s_\-./]+", spaced.lower()) if t]


def is_write_tool(name: str, spec: ToolSpec | None = None) -> bool:
    """Return ``True`` if ``name`` should be treated as a mutating tool.

    Fail-closed classification (MCP 2025 ``readOnlyHint`` / ``destructiveHint``):

    1. An explicit annotation always wins: ``read_only=True`` -> read;
       ``read_only=False`` or ``destructive=True`` -> write.
    2. Otherwise a write verb anywhere in the name marks it as a write.
    3. Otherwise the tool is a read **only** when its leading verb is a
       recognised read-only verb. Everything else (no annotation, unrecognised
       verb — e.g. ``merge``, ``approve``, ``merge_pull_request``) is assumed
       **destructive**, matching the MCP 2025 convention that ``readOnlyHint``
       defaults to false. This is the security default: an un-annotated tool must
       never slip through a read-only connection.
    """
    if spec is not None:
        if spec.read_only is True:
            return False
        if spec.read_only is False or spec.destructive is True:
            return True
    tokens = _split_name(name)
    if set(tokens) & WRITE_KEYWORDS:
        return True
    # Fail closed: only a recognised leading read verb keeps the tool a read.
    return not (tokens and tokens[0] in READ_KEYWORDS)


def token_binding(conn: MCPConnection) -> str | None:
    """Resolve the RFC 8707 ``resource`` indicator the token is bound to.

    Prefers an explicit ``auth.resource``; falls back to the connection
    ``endpoint``. Unauthenticated connections have no binding.
    """
    if conn.auth.type is MCPAuthType.NONE:
        return None
    return conn.auth.resource or conn.endpoint


def namespace_of(uri: str) -> str | None:
    """Extract the namespace from a ``scheme://namespace/path`` resource URI."""
    if not uri:
        return None
    rest = uri.split("://", 1)[1] if "://" in uri else uri
    rest = rest.lstrip("/")
    if not rest:
        return None
    return rest.split("/", 1)[0] or None


def resource_in_scope(namespace: str | None, allowed_namespaces: Iterable[str]) -> bool:
    """Return ``True`` if ``namespace`` is permitted by the allow-list.

    An empty allow-list means the connection is unscoped (everything allowed).
    """
    allowed = list(allowed_namespaces)
    if not allowed:
        return True
    return namespace in allowed


def _resource_namespace(resource: MCPResource) -> str | None:
    return resource.namespace or namespace_of(resource.uri)


def filter_resources(
    resources: Iterable[MCPResource],
    allowed_namespaces: Iterable[str],
    requested: str | None = None,
) -> list[MCPResource]:
    """Filter resources by the connection allow-list and an optional request."""
    allowed = list(allowed_namespaces)
    out: list[MCPResource] = []
    for r in resources:
        ns = _resource_namespace(r)
        if not resource_in_scope(ns, allowed):
            continue
        if requested is not None and ns != requested:
            continue
        out.append(r)
    return out


def redact(value: Any) -> Any:
    """Return a deep copy of ``value`` with secrets masked (never mutates input)."""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
                out[key] = REDACTED
            else:
                out[key] = redact(val)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    """Mask bearer tokens, ``key=value`` secrets, and JWTs inside a string."""
    text = _BEARER_RE.sub("Bearer " + REDACTED, text)
    text = _KV_SECRET_RE.sub(REDACTED, text)
    text = _JWT_RE.sub(REDACTED, text)
    return text


def payload_hash(arguments: Any) -> str:
    """SHA-256 of the *redacted*, canonicalised payload (rule 4; secret-free)."""
    canonical = json.dumps(redact(arguments), sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "READ_KEYWORDS",
    "REDACTED",
    "SENSITIVE_KEYS",
    "WRITE_KEYWORDS",
    "filter_resources",
    "is_write_tool",
    "namespace_of",
    "payload_hash",
    "redact",
    "redact_text",
    "resource_in_scope",
    "token_binding",
]
