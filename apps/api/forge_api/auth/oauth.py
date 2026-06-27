"""OAuth authorization-code exchange against an external IdP (Task H4).

Spec Security: "Auth — OAuth + API key". The :func:`AuthService.oauth_challenge`
descriptor (see :mod:`forge_api.auth.service`) starts the redirect flow; this
module completes it: it takes the provider ``code`` the IdP hands back, exchanges
it for tokens at the provider *token* endpoint, then calls the provider
*userinfo* endpoint to resolve the external user identity.

Network access goes through an injectable :class:`httpx` transport so the whole
flow is deterministically testable with :class:`httpx.MockTransport` — no real
network call is ever made in tests. Production resolves client credentials from
``FORGE_OAUTH_<PROVIDER>_CLIENT_ID`` / ``..._CLIENT_SECRET`` via
:meth:`OAuthClient.from_env`; an unconfigured provider fails closed at exchange
time (never silently), and the IdP is only ever contacted when an exchange is
actually requested.
"""

from __future__ import annotations

import os
import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from forge_api.auth.models import OAuthTokens, OAuthUser

if TYPE_CHECKING:
    from forge_api.auth.models import OAuthResult

#: Default network timeout (seconds) for IdP calls.
_DEFAULT_TIMEOUT = 10.0


class OAuthError(Exception):
    """Base class for any failure in the OAuth code-exchange flow."""


class UnsupportedOAuthProviderError(OAuthError):
    """The requested provider is not known to Forge (maps to HTTP 400)."""


class OAuthConfigError(OAuthError):
    """The provider is known but Forge has no client credentials for it (HTTP 500)."""


class OAuthStateError(OAuthError):
    """The returned ``state`` did not match the issued one (CSRF; HTTP 400)."""


class OAuthExchangeError(OAuthError):
    """The IdP rejected the exchange or returned an unusable response (HTTP 502)."""


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Static endpoint + claim-mapping configuration for one OAuth provider."""

    name: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    scopes: tuple[str, ...] = ()
    #: userinfo JSON keys to read the stable subject / email / display name from.
    subject_field: str = "sub"
    email_field: str = "email"
    name_fields: tuple[str, ...] = ("name",)


@dataclass(frozen=True)
class OAuthClientCredentials:
    """A provider's confidential client registration (``client_id``/secret)."""

    client_id: str
    client_secret: str


#: V1 providers (spec: Google, GitHub, GitLab). GitHub's subject is the numeric
#: ``id`` and its display name may only be present as ``login``.
DEFAULT_PROVIDERS: dict[str, OAuthProviderConfig] = {
    "google": OAuthProviderConfig(
        name="google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
        scopes=("openid", "email", "profile"),
        subject_field="sub",
        email_field="email",
        name_fields=("name", "given_name"),
    ),
    "github": OAuthProviderConfig(
        name="github",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        userinfo_url="https://api.github.com/user",
        scopes=("read:user", "user:email"),
        subject_field="id",
        email_field="email",
        name_fields=("name", "login"),
    ),
    "gitlab": OAuthProviderConfig(
        name="gitlab",
        authorize_url="https://gitlab.com/oauth/authorize",
        token_url="https://gitlab.com/oauth/token",
        userinfo_url="https://gitlab.com/api/v4/user",
        scopes=("read_user",),
        subject_field="id",
        email_field="email",
        name_fields=("name", "username"),
    ),
}


def _coerce_str(value: object) -> str | None:
    """Return a non-empty string form of ``value`` (numeric ids -> str), else None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass
class OAuthClient:
    """Performs the authorization-code -> tokens -> user exchange against an IdP.

    The ``transport`` is injectable so tests drive the whole flow with
    :class:`httpx.MockTransport`; left unset it uses httpx's default (real
    network), which is only ever exercised when an exchange is requested.
    """

    credentials: dict[str, OAuthClientCredentials] = field(default_factory=dict)
    providers: dict[str, OAuthProviderConfig] = field(
        default_factory=lambda: dict(DEFAULT_PROVIDERS)
    )
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None
    timeout: float = _DEFAULT_TIMEOUT

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    ) -> OAuthClient:
        """Build a client reading ``FORGE_OAUTH_<PROVIDER>_CLIENT_ID/SECRET``.

        Constructs cleanly even with nothing configured: a provider only becomes
        usable once both its id and secret are present.
        """
        source = os.environ if env is None else env
        creds: dict[str, OAuthClientCredentials] = {}
        for provider in DEFAULT_PROVIDERS:
            prefix = f"FORGE_OAUTH_{provider.upper()}_"
            client_id = source.get(f"{prefix}CLIENT_ID")
            client_secret = source.get(f"{prefix}CLIENT_SECRET")
            if client_id and client_secret:
                creds[provider] = OAuthClientCredentials(
                    client_id=client_id, client_secret=client_secret
                )
        return cls(credentials=creds, transport=transport)

    # -- introspection ------------------------------------------------------ #

    def provider_config(self, provider: str) -> OAuthProviderConfig:
        """Return the config for ``provider`` or raise :class:`UnsupportedOAuthProviderError`."""
        config = self.providers.get(provider.lower())
        if config is None:
            raise UnsupportedOAuthProviderError(f"unsupported OAuth provider '{provider}'")
        return config

    def _require_credentials(self, provider: str) -> OAuthClientCredentials:
        creds = self.credentials.get(provider.lower())
        if creds is None:
            raise OAuthConfigError(
                f"OAuth provider '{provider}' has no client credentials configured "
                f"(set FORGE_OAUTH_{provider.upper()}_CLIENT_ID and _CLIENT_SECRET)"
            )
        return creds

    # -- flow --------------------------------------------------------------- #

    async def exchange_code(
        self, provider: str, code: str, redirect_uri: str | None = None
    ) -> OAuthTokens:
        """Trade an authorization ``code`` for tokens at the provider token endpoint."""
        config = self.provider_config(provider)
        creds = self._require_credentials(provider)
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
        }
        if redirect_uri:
            data["redirect_uri"] = redirect_uri
        async with self._async_client() as client:
            try:
                response = await client.post(
                    config.token_url,
                    data=data,
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as exc:  # network/transport failure
                raise OAuthExchangeError(
                    f"token request to '{provider}' failed: {exc}"
                ) from exc
        payload = self._decode(response, provider, "token")
        if response.status_code >= 400 or "error" in payload:
            raise OAuthExchangeError(
                f"'{provider}' rejected the authorization code "
                f"(status {response.status_code})"
            )
        access_token = _coerce_str(payload.get("access_token"))
        if access_token is None:
            raise OAuthExchangeError(
                f"'{provider}' token response did not contain an access_token"
            )
        return OAuthTokens(
            access_token=access_token,
            token_type=_coerce_str(payload.get("token_type")) or "bearer",
            refresh_token=_coerce_str(payload.get("refresh_token")),
            expires_in=_as_int(payload.get("expires_in")),
            scope=_coerce_str(payload.get("scope")),
            id_token=_coerce_str(payload.get("id_token")),
        )

    async def fetch_user(self, provider: str, tokens: OAuthTokens) -> OAuthUser:
        """Resolve the external user identity from the provider userinfo endpoint."""
        config = self.provider_config(provider)
        async with self._async_client() as client:
            try:
                response = await client.get(
                    config.userinfo_url,
                    headers={
                        "Authorization": f"Bearer {tokens.access_token}",
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                raise OAuthExchangeError(
                    f"userinfo request to '{provider}' failed: {exc}"
                ) from exc
        if response.status_code >= 400:
            raise OAuthExchangeError(
                f"'{provider}' userinfo request failed (status {response.status_code})"
            )
        payload = self._decode(response, provider, "userinfo")
        subject = _coerce_str(payload.get(config.subject_field))
        if subject is None:
            raise OAuthExchangeError(
                f"'{provider}' userinfo response did not contain a subject "
                f"('{config.subject_field}')"
            )
        name: str | None = None
        for candidate in config.name_fields:
            name = _coerce_str(payload.get(candidate))
            if name is not None:
                break
        return OAuthUser(
            provider=config.name,
            subject=subject,
            email=_coerce_str(payload.get(config.email_field)),
            name=name,
        )

    async def complete(
        self,
        provider: str,
        code: str,
        *,
        redirect_uri: str | None = None,
        state: str | None = None,
        expected_state: str | None = None,
    ) -> OAuthResult:
        """Run the full flow: verify state, exchange code, fetch user."""
        from forge_api.auth.models import OAuthResult

        if expected_state is not None and (
            state is None or not secrets.compare_digest(state, expected_state)
        ):
            raise OAuthStateError("OAuth state mismatch (possible CSRF)")
        tokens = await self.exchange_code(provider, code, redirect_uri=redirect_uri)
        user = await self.fetch_user(provider, tokens)
        return OAuthResult(provider=user.provider, user=user, tokens=tokens)

    # -- helpers ------------------------------------------------------------ #

    def _async_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, object] = {"timeout": self.timeout}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def _decode(response: httpx.Response, provider: str, stage: str) -> dict[str, object]:
        """Parse a provider response as JSON, falling back to form-encoding.

        GitHub's token endpoint returns ``application/x-www-form-urlencoded`` by
        default; ``Accept: application/json`` requests JSON but we tolerate both.
        """
        try:
            decoded = response.json()
        except ValueError:
            decoded = dict(urllib.parse.parse_qsl(response.text))
        if not isinstance(decoded, dict):
            raise OAuthExchangeError(
                f"'{provider}' {stage} response was not a JSON object"
            )
        return decoded


def _as_int(value: object) -> int | None:
    """Best-effort int coercion for ``expires_in`` (may arrive as a string)."""
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_PROVIDERS",
    "OAuthClient",
    "OAuthClientCredentials",
    "OAuthConfigError",
    "OAuthError",
    "OAuthExchangeError",
    "OAuthProviderConfig",
    "OAuthStateError",
    "UnsupportedOAuthProviderError",
]
