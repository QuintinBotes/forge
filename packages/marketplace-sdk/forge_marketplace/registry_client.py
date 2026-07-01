"""SSRF-bounded registry fetch + concrete registry clients.

Registry indexes/manifests are fetched from *outbound, admin-supplied* URLs, so
the fetch path is a classic SSRF sink. :func:`guard_url` resolves the host and
**refuses private / loopback / link-local / cloud-metadata addresses**
(``10/8``, ``127.0.0.1``, ``169.254.169.254``, ``::1`` …) unless the host is in
an explicit allowlist (AC17). Every fetch is size-capped and timeout-bounded.

The concrete clients keep network I/O behind the :class:`RegistryClient` Protocol
so the SDK/service tests inject a pure ``FakeRegistryClient`` (no sockets).
``file://`` URLs are permitted for air-gapped / offline official registries
(F32 §3.5) and bypass the DNS guard (no network involved).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse
from urllib.request import url2pathname

from forge_marketplace.errors import RegistryFetchError, SsrfBlocked
from forge_marketplace.index import parse_index
from forge_marketplace.manifest import load_manifest
from forge_marketplace.models import PackageManifest, RegistryIndex

DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_INDEX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_MANIFEST_BYTES = 256 * 1024

# Cloud-metadata endpoints that must never be reachable via a registry URL.
_METADATA_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal", "fd00:ec2::254"})


def _is_blocked_ip(ip: str) -> bool:
    """True iff ``ip`` is a private / loopback / link-local / reserved address."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable -> refuse, fail-closed
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def guard_url(
    url: str,
    *,
    allowed_hosts: frozenset[str] | set[str] | None = None,
    resolver: object | None = None,
) -> None:
    """Raise :class:`SsrfBlocked` if ``url`` resolves to a non-public address.

    ``allowed_hosts`` bypasses the guard for explicitly trusted internal registry
    hosts. ``resolver`` (defaults to :func:`socket.getaddrinfo`) is injectable so
    tests can simulate DNS answers without real name resolution.
    """
    allowed = {h.lower() for h in (allowed_hosts or set())}
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()

    if scheme == "file":
        return  # local path — no network, no SSRF surface
    if scheme not in ("http", "https"):
        raise SsrfBlocked(f"unsupported registry URL scheme: {scheme!r}")

    host = (parsed.hostname or "").lower()
    if not host:
        raise SsrfBlocked("registry URL has no host")
    if host in allowed:
        return
    if host in _METADATA_HOSTS:
        raise SsrfBlocked(f"registry host {host!r} is a cloud-metadata endpoint")

    getaddrinfo = resolver if resolver is not None else socket.getaddrinfo
    try:
        infos = getaddrinfo(host, parsed.port or (443 if scheme == "https" else 80))  # type: ignore[operator]
    except OSError as exc:
        raise RegistryFetchError(f"could not resolve registry host {host!r}: {exc}") from exc

    for info in infos:
        sockaddr = info[4]
        ip = sockaddr[0]
        if ip in _METADATA_HOSTS or _is_blocked_ip(ip):
            raise SsrfBlocked(
                f"registry host {host!r} resolves to a blocked address {ip} "
                "(private/loopback/link-local/metadata)"
            )


def _read_capped(url: str, *, timeout: float, max_bytes: int) -> bytes:
    """Fetch a URL body with a hard size cap and timeout. ``file://`` supported."""
    parsed = urlparse(url)
    if parsed.scheme == "file":
        path = url2pathname(parsed.path)
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise RegistryFetchError(f"registry response exceeds {max_bytes} bytes")
        return data

    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "forge-marketplace/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RegistryFetchError(f"registry response exceeds {max_bytes} bytes")
    return data


class HttpIndexRegistryClient:
    """Fetches a registry that publishes a single ``index.json`` over HTTP(S)/file."""

    def __init__(
        self,
        *,
        index_url: str,
        base_url: str | None = None,
        allowed_hosts: frozenset[str] | set[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_index_bytes: int = DEFAULT_MAX_INDEX_BYTES,
        max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
        resolver: object | None = None,
    ) -> None:
        self._index_url = index_url
        # Manifests are resolved relative to the index location by default.
        self._base_url = base_url or index_url.rsplit("/", 1)[0] + "/"
        self._allowed = frozenset(allowed_hosts or set())
        self._timeout = timeout
        self._max_index_bytes = max_index_bytes
        self._max_manifest_bytes = max_manifest_bytes
        self._resolver = resolver

    async def fetch_index(self) -> RegistryIndex:
        guard_url(self._index_url, allowed_hosts=self._allowed, resolver=self._resolver)
        raw = _read_capped(
            self._index_url, timeout=self._timeout, max_bytes=self._max_index_bytes
        )
        return parse_index(raw)

    async def fetch_manifest(self, manifest_uri: str) -> tuple[PackageManifest, bytes]:
        url = manifest_uri if "://" in manifest_uri else self._base_url + manifest_uri
        guard_url(url, allowed_hosts=self._allowed, resolver=self._resolver)
        raw = _read_capped(url, timeout=self._timeout, max_bytes=self._max_manifest_bytes)
        return load_manifest(raw.decode("utf-8")), raw


class GitRegistryClient(HttpIndexRegistryClient):
    """Placeholder for a git-backed registry.

    PARKED: real ``git clone`` fetch of a registry repo is deferred; a git
    registry served over an HTTP(S) raw path (e.g. a raw.githubusercontent URL to
    ``index.json``) is handled by the HTTP client above. Live git-transport
    support lands with the worker egress network policy.
    """


__all__ = [
    "DEFAULT_MAX_INDEX_BYTES",
    "DEFAULT_MAX_MANIFEST_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "GitRegistryClient",
    "HttpIndexRegistryClient",
    "guard_url",
]
