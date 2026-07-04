"""GitHub App authentication (HARD-01): JWT minting + installation tokens.

The fixture-backed :class:`~forge_integrations.github.GitHubClient` assumed a
caller supplied a ready-made ``token``. Real GitHub App auth is a two-step flow
this module implements:

1. Mint an ``RS256`` JSON Web Token from the App's private key
   (:func:`build_app_jwt`). Claims follow GitHub's rules: ``iss`` is the App id,
   ``iat`` is back-dated 60s for clock skew, and ``exp - iat`` never exceeds the
   600s (10-minute) maximum GitHub enforces.
2. Exchange that App JWT for a short-lived *installation access token* via
   ``POST /app/installations/{id}/access_tokens`` and cache it until ~60s before
   it expires (:class:`InstallationTokenProvider`).

Security invariants (HARD-01 §8, AC6/AC7):

- The private-key **value** is never logged, never placed in an attribute that
  ``repr()`` would expose, and never included in any raised exception (only the
  *path* is surfaced, on a read failure).
- The App JWT and the installation token are never logged. They live only in the
  ``Authorization`` header of the outbound request and, for the token, in the
  in-process cache.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx
import jwt

from .errors import GitHubError

DEFAULT_BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"

#: GitHub rejects an App JWT whose lifetime exceeds 10 minutes. We keep the whole
#: ``exp - iat`` window (including the 60s back-dated ``iat``) at or under this.
_MAX_JWT_WINDOW_SECONDS = 600
#: Back-date ``iat`` to tolerate clock drift between us and GitHub.
_CLOCK_SKEW_SECONDS = 60
#: Re-mint an installation token this many seconds before it actually expires.
_REFRESH_MARGIN_SECONDS = 60


def load_private_key(path: str) -> str:
    """Read a PEM private key from ``path``.

    The key **value** is never returned in any error message — only the path is,
    on a read failure (AC7). Raises :class:`GitHubError` if the file is missing
    or unreadable.
    """
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        # Surface only the path + errno string; never the (absent) key bytes, and
        # suppress the exception chain so nothing downstream re-exposes internals.
        reason = exc.strerror or type(exc).__name__
        raise GitHubError(f"could not read GitHub App private key at {path!r}: {reason}") from None


def build_app_jwt(
    app_id: str,
    private_key_pem: str,
    *,
    now: int | None = None,
    ttl_seconds: int = 540,
) -> str:
    """Return an ``RS256`` GitHub App JWT signed with ``private_key_pem``.

    Claims: ``iat = now - 60`` (clock-skew back-date), ``exp = now + ttl`` with
    ``ttl`` capped so ``exp - iat <= 600`` (GitHub's 10-minute maximum), and
    ``iss = app_id``. ``now`` (epoch seconds) is injectable for deterministic
    tests.
    """
    issued = int(now if now is not None else time.time())
    # exp - iat == ttl + CLOCK_SKEW; cap ttl so the whole window stays <= 600s.
    ttl = min(int(ttl_seconds), _MAX_JWT_WINDOW_SECONDS - _CLOCK_SKEW_SECONDS)
    payload = {
        "iat": issued - _CLOCK_SKEW_SECONDS,
        "exp": issued + ttl,
        "iss": str(app_id),
    }
    token = jwt.encode(payload, private_key_pem, algorithm="RS256")
    # PyJWT >= 2 returns str; be defensive for older shims.
    return token if isinstance(token, str) else token.decode("utf-8")


def _parse_expiry(value: str | None, now: float) -> float:
    """Parse GitHub's ISO-8601 ``expires_at`` to epoch seconds.

    Falls back to ``now + 3600`` (GitHub's default token lifetime) when the field
    is missing or unparseable.
    """
    if value:
        try:
            text = value.replace("Z", "+00:00")
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            pass
    return now + 3600.0


class InstallationTokenProvider:
    """Mints and caches a short-lived installation access token.

    :meth:`token` returns a cached token until ~60s before its expiry, then
    re-mints the App JWT and calls ``POST /app/installations/{id}/access_tokens``.
    Refreshes are guarded by a lock so concurrent callers mint at most once.
    Neither the App JWT nor the installation token is ever logged.
    """

    def __init__(
        self,
        *,
        app_id: str,
        private_key_pem: str,
        installation_id: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        timeout: float = 10.0,
    ) -> None:
        self._app_id = app_id
        # Stored under a name-mangled attribute and deliberately excluded from
        # __repr__ so the key value cannot leak through reprs/tracebacks (AC7).
        self.__private_key_pem = private_key_pem
        self._installation_id = installation_id
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._mint_count = 0
        self._client = httpx.Client(
            base_url=self._base_url,
            transport=transport,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )

    def __repr__(self) -> str:  # pragma: no cover - trivial, but must not leak
        return (
            f"InstallationTokenProvider(app_id={self._app_id!r}, "
            f"installation_id={self._installation_id!r})"
        )

    def token(self) -> str:
        """Return a valid installation token, minting/refreshing as needed."""
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._expires_at - _REFRESH_MARGIN_SECONDS:
                return self._token
            return self._refresh(now)

    def _refresh(self, now: float) -> str:
        app_jwt = build_app_jwt(self._app_id, self.__private_key_pem, now=int(now))
        try:
            resp = self._client.post(
                f"/app/installations/{self._installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {app_jwt}"},
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"installation token request failed: {exc}") from exc
        if resp.status_code >= 400:
            message = "installation token request rejected"
            with contextlib.suppress(ValueError, AttributeError):
                message = str(resp.json().get("message") or message)
            raise GitHubError(message, status_code=resp.status_code)
        data = resp.json()
        token = data.get("token")
        if not token:
            raise GitHubError("installation token response missing 'token'")
        self._token = str(token)
        self._expires_at = _parse_expiry(data.get("expires_at"), now)
        self._mint_count += 1
        return self._token

    def invalidate(self) -> None:
        """Force the next :meth:`token` call to re-mint (used on a 401 retry)."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    @property
    def mint_count(self) -> int:
        """Number of successful token mints (test observability only)."""
        return self._mint_count

    def close(self) -> None:
        self._client.close()


__all__ = [
    "API_VERSION",
    "DEFAULT_BASE_URL",
    "InstallationTokenProvider",
    "build_app_jwt",
    "load_private_key",
]
