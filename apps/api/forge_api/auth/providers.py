"""Secret-provider abstraction (HARD-13 — production secrets & config).

A single, testable ingress for every secret the platform reads. Instead of ad-hoc
``os.environ[...]`` reads scattered across ``settings.py``, ``service.py``,
``oauth.py``, and the worker, all secret resolution flows through
:func:`resolve_secret` (12-factor, factor III — config in the environment), so the
backend (env / Docker-secret file / HashiCorp Vault) can be swapped in exactly one
place without touching call sites.

Providers implement the tiny :class:`SecretProvider` protocol (``name`` + ``get``)
and never raise for a missing key — a missing secret is ``None`` so callers decide
fail-open vs fail-closed. The default chain is env-first, then a Docker/K8s
``/run/secrets`` file lookup, so an operator can mount secrets as files without any
app change (Journey D). The Vault provider is import-and-run gated
(:mod:`hvac` only imported when constructed) and never touched on the hermetic
test path.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from forge_api.settings import Settings

#: Default root for :class:`FileSecretProvider` (the Docker/K8s secret mount).
DEFAULT_SECRET_FILE_ROOT = Path("/run/secrets")


@runtime_checkable
class SecretProvider(Protocol):
    """Minimal secret-ingress surface (a missing key returns ``None``)."""

    name: str

    def get(self, key: str) -> str | None: ...


class EnvSecretProvider:
    """Read a secret from ``os.environ``, honouring ``<KEY>_FILE`` indirection.

    ``FOO`` resolves from ``os.environ["FOO"]``; if that is absent but
    ``os.environ["FOO_FILE"]`` names a readable file, its (stripped) contents are
    returned. This makes *any* ``FORGE_*`` var supplyable as a mounted secret
    (the Docker ``_FILE`` convention) without a bespoke setting.
    """

    name = "env"

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = environ

    def _source(self) -> dict[str, str]:
        # Read ``os.environ`` live (not cached) so tests using ``monkeypatch``
        # and rotation windows that mutate the env are observed immediately.
        return self._environ if self._environ is not None else dict(os.environ)

    def get(self, key: str) -> str | None:
        source = self._source()
        value = source.get(key)
        if value is not None:
            return value
        file_path = source.get(f"{key}_FILE")
        if file_path:
            try:
                return Path(file_path).read_text(encoding="utf-8").strip()
            except OSError:
                return None
        return None


class FileSecretProvider:
    """Read a secret from ``root / key`` (default ``/run/secrets`` — Docker/K8s).

    An operator mounts each secret as a file named after the config key
    (``/run/secrets/FORGE_SECRET_KEY``) and the value is the stripped file body.
    """

    name = "file"

    def __init__(self, root: Path = DEFAULT_SECRET_FILE_ROOT) -> None:
        self._root = Path(root)

    def get(self, key: str) -> str | None:
        path = self._root / key
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None


class VaultSecretProvider:
    """HashiCorp Vault KV-v2 provider (integration-gated; never on the hermetic path).

    ``hvac`` is imported lazily inside ``__init__`` so importing this module stays
    dependency-free; an injectable ``client`` keeps the provider unit-testable
    without a live Vault. Reads ``mount``/``path`` once and serves keys from the
    cached KV-v2 ``data`` map.
    """

    name = "vault"

    def __init__(
        self,
        *,
        addr: str,
        token: str,
        mount: str,
        path: str,
        client: object | None = None,
    ) -> None:
        if client is None:  # pragma: no cover - requires hvac + a live Vault
            import hvac

            client = hvac.Client(url=addr, token=token)
        self._client: Any = client
        self._mount = mount
        self._path = path
        self._cache: dict[str, str] | None = None

    def _data(self) -> dict[str, str]:
        if self._cache is None:
            read = self._client.secrets.kv.v2.read_secret_version(
                path=self._path, mount_point=self._mount
            )
            self._cache = dict(read["data"]["data"])
        return self._cache

    def get(self, key: str) -> str | None:
        value = self._data().get(key)
        return None if value is None else str(value)


class ChainSecretProvider:
    """Try each provider in order; the first non-``None`` result wins."""

    name = "chain"

    def __init__(self, providers: Sequence[SecretProvider]) -> None:
        self._providers = tuple(providers)

    @property
    def providers(self) -> tuple[SecretProvider, ...]:
        return self._providers

    def get(self, key: str) -> str | None:
        for provider in self._providers:
            value = provider.get(key)
            if value is not None:
                return value
        return None


def _default_chain() -> ChainSecretProvider:
    return ChainSecretProvider([EnvSecretProvider(), FileSecretProvider()])


def build_provider(settings: Settings) -> SecretProvider:
    """Construct the configured secret provider from :class:`Settings`.

    ``env`` (default) uses the env-first chain; ``file`` reads from
    ``FORGE_SECRET_FILE_ROOT``; ``vault`` builds a :class:`VaultSecretProvider`
    from the ``FORGE_VAULT_*`` knobs (integration-only). Unknown values fall back
    to the safe env chain rather than raising at import time.
    """
    kind = (settings.secret_provider or "env").strip().lower()
    if kind == "file":
        return ChainSecretProvider(
            [EnvSecretProvider(), FileSecretProvider(Path(settings.secret_file_root))]
        )
    if kind == "vault":  # pragma: no cover - requires hvac + a live Vault
        if not (settings.vault_addr and settings.vault_token):
            raise RuntimeError(
                "FORGE_SECRET_PROVIDER=vault requires FORGE_VAULT_ADDR and "
                "FORGE_VAULT_TOKEN to be set."
            )
        return ChainSecretProvider(
            [
                EnvSecretProvider(),
                VaultSecretProvider(
                    addr=settings.vault_addr,
                    token=settings.vault_token,
                    mount=settings.vault_mount,
                    path=settings.vault_path,
                ),
            ]
        )
    return _default_chain()


#: Process-wide default provider (swappable for tests / configured backends).
_default_provider: SecretProvider | None = None


def get_default_provider() -> SecretProvider:
    """Return the module-level default provider (env-first chain unless set)."""
    global _default_provider
    if _default_provider is None:
        _default_provider = _default_chain()
    return _default_provider


def set_default_provider(provider: SecretProvider | None) -> None:
    """Install (or clear, with ``None``) the process-wide default provider."""
    global _default_provider
    _default_provider = provider


def resolve_secret(key: str, *, provider: SecretProvider | None = None) -> str | None:
    """Resolve a single secret through the (given or default) provider.

    This is the *single ingress* the settings surface, the auth service, the
    OAuth client, and the worker all read secrets through, so there is exactly
    one place to swap the backend or assert the resolution path in a test.
    """
    return (provider or get_default_provider()).get(key)


__all__ = [
    "ChainSecretProvider",
    "EnvSecretProvider",
    "FileSecretProvider",
    "SecretProvider",
    "VaultSecretProvider",
    "build_provider",
    "get_default_provider",
    "resolve_secret",
    "set_default_provider",
]
