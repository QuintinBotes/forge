"""SSRF guard for admin-configured outbound URLs (HARD-09, OWASP A10:2021).

Workspace admins can point Forge at BYOK embedder/reranker/MCP/OAuth endpoints.
Without validation such a URL can target the cloud metadata service
(``169.254.169.254``), loopback services, or internal RFC1918 hosts — turning a
config field into an internal port scanner or credential exfiltrator.

:func:`assert_safe_url` is the single guard. It is applied at the API/service
layer and *injected* into the leaf HTTP clients (``forge_knowledge`` embeddings
and reranker take an optional ``url_validator`` callable) so the pure packages
never import ``forge_api``.

This guard is defense-in-depth: the compose/helm network segmentation remains
the primary control (see ``docs/self-hosting/security.md``).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

__all__ = ["SsrfBlockedError", "assert_safe_url", "is_public_host"]

#: Cloud metadata endpoints are always rejected, even with ``allow_private``.
_METADATA_HOSTS: frozenset[str] = frozenset({"169.254.169.254", "metadata.google.internal"})

_DEFAULT_SCHEMES: frozenset[str] = frozenset({"https", "http"})


class SsrfBlockedError(ValueError):
    """Raised when an outbound URL targets a non-public / disallowed host."""


def _classify_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a human-readable rejection reason for ``ip``, or ``None`` if global.

    ``is_global`` alone would accept some special ranges on old stdlibs, so the
    specific checks are spelled out (loopback, private, link-local, ULA,
    unspecified, multicast, reserved).
    """
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address (cloud metadata range)"
    if ip.is_private:
        # Covers RFC1918 (IPv4) and ULA fc00::/7 (IPv6).
        return "private-range address"
    if ip.is_unspecified:
        return "unspecified address (0.0.0.0/::)"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_reserved:
        return "reserved address"
    if not ip.is_global:
        return "non-global address"
    return None


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve ``host`` to every A/AAAA answer; fail closed on resolver errors."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [literal]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SsrfBlockedError(f"outbound host {host!r} did not resolve: {exc}") from exc
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            ips.append(ipaddress.ip_address(info[4][0]))
        except ValueError:  # pragma: no cover - non-IP sockaddr families
            continue
    if not ips:
        raise SsrfBlockedError(f"outbound host {host!r} resolved to no usable address")
    return ips


def is_public_host(host: str) -> bool:
    """True iff ``host`` resolves *only* to global-scope IPs.

    No loopback, RFC1918/ULA, link-local (incl. ``169.254.169.254``),
    unspecified (``0.0.0.0``/``::``), multicast, or reserved answers. A host
    that fails to resolve is treated as non-public (fail closed).
    """
    if host.strip().lower() in _METADATA_HOSTS:
        return False
    try:
        ips = _resolve(host)
    except SsrfBlockedError:
        return False
    return all(_classify_ip(ip) is None for ip in ips)


def assert_safe_url(
    url: str,
    *,
    allow_private: bool = False,
    allowlist: Iterable[str] = (),
    schemes: frozenset[str] = _DEFAULT_SCHEMES,
) -> str:
    """Validate an outbound ``url``; return it unchanged or raise.

    * Only ``schemes`` (default http/https) are permitted — never ``file://``.
    * The host must resolve exclusively to public IPs, unless it is named in
      ``allowlist`` (exact hostname match, case-insensitive) or
      ``allow_private`` is set.
    * ``allow_private`` (dev / intentionally-internal deployments) still rejects
      loopback, link-local (cloud metadata), and unspecified addresses — an
      operator opts in to *their* private network, never to ``169.254.169.254``.

    Raises :class:`SsrfBlockedError` with the reason on any rejection.
    """
    split = urlsplit(url)
    scheme = split.scheme.lower()
    if scheme not in schemes:
        raise SsrfBlockedError(
            f"outbound URL {url!r} uses disallowed scheme {scheme!r} (allowed: {sorted(schemes)})"
        )
    host = (split.hostname or "").strip().lower()
    if not host:
        raise SsrfBlockedError(f"outbound URL {url!r} has no host")
    if host in _METADATA_HOSTS:
        raise SsrfBlockedError(f"outbound URL {url!r} targets the cloud metadata service")
    if host in {h.strip().lower() for h in allowlist}:
        return url
    for ip in _resolve(host):
        reason = _classify_ip(ip)
        if reason is None:
            continue
        if allow_private and reason == "private-range address":
            continue
        raise SsrfBlockedError(f"outbound URL {url!r} resolves to {ip} ({reason})")
    return url
