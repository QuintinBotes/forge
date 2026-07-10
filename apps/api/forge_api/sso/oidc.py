"""OpenID Connect authorization-code flow engine (F33 companion to SAML).

Mirrors ``forge_api.sso.saml`` for the OIDC ``response_type=code`` flow with
PKCE (S256):

* :func:`build_authorize_url` — the browser redirect to the IdP, carrying
  ``state`` (CSRF/txn binding), ``nonce`` (ID-token replay binding), and the
  PKCE ``code_challenge``.
* :class:`OidcClient` — resolves OpenID discovery
  (``{issuer}/.well-known/openid-configuration``), exchanges the code at the
  token endpoint, fetches + caches the IdP JWKS, and **validates** the returned
  ID token: RS256 signature against the JWKS (matched by ``kid``), ``iss`` ==
  issuer, ``aud`` == ``client_id``, ``exp``/``iat`` within skew, and ``nonce``
  equal to the value minted at login.
* :class:`InMemoryOidcStateStore` — the process-local one-shot store binding a
  ``state`` to its ``(nonce, code_verifier, relay_state)`` between login and
  callback (the OIDC analogue of the SAML request-id replay guard; a
  Redis/DB-backed store for multi-process deployments is PARKED exactly like
  ``forge_api.sso.replay``, no fakes for infrastructure).

Signature verification is delegated to ``PyJWT`` (``pyjwt[crypto]``) over
``cryptography`` — never hand-rolled. The HTTP surface is an injectable
``httpx`` transport seam so the suite runs fully offline against a locally
signed IdP (mirroring ``config_service.fetch_idp_metadata``).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
import time
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from jwt import PyJWKClientError, algorithms

from forge_api.sso.errors import OidcValidationError, SsoConfigError
from forge_contracts.sso import MappedIdentity, OidcIdpConfig

#: Cap for fetched discovery / JWKS documents (SSRF / resource guard).
MAX_DOC_BYTES = 1_000_000
_SIGNING_ALGS = ["RS256"]


# --------------------------------------------------------------------------- #
# Discovery + login transaction data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OidcDiscovery:
    """The endpoints Forge needs from an IdP (discovery doc or admin overrides)."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True)
class OidcTransaction:
    """State carried between ``/login`` and ``/callback`` (never persisted long)."""

    state: str
    nonce: str
    code_verifier: str
    relay_state: str


class InMemoryOidcStateStore:
    """Process-local one-shot ``state`` → transaction store with TTL expiry."""

    def __init__(self) -> None:
        self._txns: dict[str, tuple[float, OidcTransaction]] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._txns.items() if exp <= now]:
            del self._txns[key]

    def put(self, txn: OidcTransaction, ttl_seconds: int) -> None:
        self._prune()
        self._txns[txn.state] = (time.monotonic() + ttl_seconds, txn)

    def consume(self, state: str) -> OidcTransaction | None:
        """Return (and remove) the transaction for ``state`` if still valid."""
        self._prune()
        entry = self._txns.pop(state, None)
        if entry is None:
            return None
        expires_at, txn = entry
        return txn if expires_at > time.monotonic() else None


# --------------------------------------------------------------------------- #
# PKCE + authorize-URL helpers
# --------------------------------------------------------------------------- #


def _b64url(raw: bytes) -> str:
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_state() -> str:
    return _b64url(secrets.token_bytes(32))


def new_nonce() -> str:
    return _b64url(secrets.token_bytes(32))


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE S256 (RFC 7636)."""
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url(
    discovery: OidcDiscovery,
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    """Build the IdP authorization endpoint URL for the code flow (PKCE S256)."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in discovery.authorization_endpoint else "?"
    return f"{discovery.authorization_endpoint}{sep}{urlencode(params)}"


# --------------------------------------------------------------------------- #
# Identity extraction
# --------------------------------------------------------------------------- #


def _as_groups(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def extract_identity(claims: dict[str, object], config: OidcIdpConfig) -> MappedIdentity:
    """Resolve validated ID-token claims to a Forge identity (groups → role)."""
    email = claims.get(config.email_claim) or claims.get("email")
    if not isinstance(email, str) or not email:
        raise OidcValidationError("missing_email", f"claim {config.email_claim!r} absent")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise OidcValidationError("missing_subject", "claim 'sub' absent")
    name_value = claims.get(config.name_claim)
    name = name_value if isinstance(name_value, str) and name_value else None
    groups = _as_groups(claims.get(config.groups_claim))

    role = config.default_role
    for group in groups:
        mapped = config.group_role_map.get(group)
        if mapped:
            role = mapped  # type: ignore[assignment]
            break

    return MappedIdentity(
        email=email,
        name=name,
        role=role,
        groups=groups,
        external_id=subject,
        name_id_format="oidc",
    )


# --------------------------------------------------------------------------- #
# The HTTP-facing client (discovery / token exchange / JWKS / validation)
# --------------------------------------------------------------------------- #


def _guard_url(url: str, *, field: str) -> None:
    """HTTPS-only, no private/loopback hosts (SSRF guard, matching SAML metadata)."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SsoConfigError(f"{field} must use https")
    host = parsed.hostname or ""
    try:
        address = ipaddress.ip_address(host)
        if address.is_private or address.is_loopback or address.is_link_local:
            raise SsoConfigError(f"{field} resolves to a private address")
    except ValueError:
        pass  # hostname, not a literal IP
    if host == "localhost":
        raise SsoConfigError(f"{field} must not target localhost")


class OidcClient:
    """OIDC relying-party HTTP operations over an injectable ``httpx`` transport.

    Discovery + JWKS documents are cached in-process (TTL) so the token flow
    does not re-fetch on every callback. ``transport`` is the offline seam:
    the suite injects an ``httpx.MockTransport`` serving a locally-signed IdP.
    """

    #: Process-wide caches (URL → (expires_monotonic, document)).
    _discovery_cache: ClassVar[dict[str, tuple[float, OidcDiscovery]]] = {}
    _jwks_cache: ClassVar[dict[str, tuple[float, dict[str, object]]]] = {}

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
        jwks_cache_ttl: int = 3600,
    ) -> None:
        self._transport = transport
        self._timeout = timeout
        self._jwks_cache_ttl = jwks_cache_ttl

    def _get_json(self, url: str, *, field: str) -> dict[str, object]:
        _guard_url(url, field=field)
        with httpx.Client(
            transport=self._transport, timeout=self._timeout, follow_redirects=False
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            if len(response.content) > MAX_DOC_BYTES:
                raise SsoConfigError(f"{field} document exceeds the size cap")
            data = response.json()
        if not isinstance(data, dict):
            raise SsoConfigError(f"{field} did not return a JSON object")
        return data

    # -- discovery --------------------------------------------------------------- #

    def discover(self, config: OidcIdpConfig) -> OidcDiscovery:
        """Resolve the IdP endpoints (admin overrides win; else discovery doc)."""
        if config.authorization_endpoint and config.token_endpoint and config.jwks_uri:
            return OidcDiscovery(
                issuer=config.issuer,
                authorization_endpoint=config.authorization_endpoint,
                token_endpoint=config.token_endpoint,
                jwks_uri=config.jwks_uri,
            )
        url = config.discovery_url or (
            config.issuer.rstrip("/") + "/.well-known/openid-configuration"
        )
        cached = self._discovery_cache.get(url)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        doc = self._get_json(url, field="discovery_url")
        try:
            discovery = OidcDiscovery(
                issuer=str(doc["issuer"]),
                authorization_endpoint=str(doc["authorization_endpoint"]),
                token_endpoint=str(doc["token_endpoint"]),
                jwks_uri=str(doc["jwks_uri"]),
            )
        except KeyError as exc:
            raise SsoConfigError(f"discovery document missing {exc.args[0]!r}") from exc
        # OIDC Core §3.1.3.7: the discovered issuer must equal the configured one.
        if discovery.issuer != config.issuer:
            raise SsoConfigError(
                f"discovery issuer {discovery.issuer!r} != configured {config.issuer!r}"
            )
        self._discovery_cache[url] = (time.monotonic() + self._jwks_cache_ttl, discovery)
        return discovery

    # -- token exchange ---------------------------------------------------------- #

    def exchange_code(
        self,
        discovery: OidcDiscovery,
        *,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
        code_verifier: str,
    ) -> dict[str, object]:
        """Exchange an authorization code for tokens at the token endpoint."""
        _guard_url(discovery.token_endpoint, field="token_endpoint")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": code_verifier,
        }
        with httpx.Client(
            transport=self._transport, timeout=self._timeout, follow_redirects=False
        ) as client:
            response = client.post(discovery.token_endpoint, data=data)
        if response.status_code != httpx.codes.OK:
            raise OidcValidationError("token_exchange_failed", f"status={response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise OidcValidationError("token_exchange_failed", "non-JSON token response") from exc
        if not isinstance(body, dict) or "id_token" not in body:
            raise OidcValidationError("token_exchange_failed", "no id_token in token response")
        return body

    # -- JWKS + ID-token validation ---------------------------------------------- #

    def fetch_jwks(self, jwks_uri: str) -> dict[str, object]:
        cached = self._jwks_cache.get(jwks_uri)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        doc = self._get_json(jwks_uri, field="jwks_uri")
        self._jwks_cache[jwks_uri] = (time.monotonic() + self._jwks_cache_ttl, doc)
        return doc

    def _signing_key(self, jwks: dict[str, object], id_token: str) -> object:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.InvalidTokenError as exc:
            raise OidcValidationError("malformed_token", str(exc)) from exc
        kid = header.get("kid")
        keys = jwks.get("keys")
        if not isinstance(keys, list) or not keys:
            raise OidcValidationError("jwks_empty", "no keys in JWKS")
        candidates = [k for k in keys if isinstance(k, dict)]
        match = next((k for k in candidates if kid is None or k.get("kid") == kid), None)
        if match is None:
            raise OidcValidationError("unknown_kid", f"kid={kid!r} not in JWKS")
        try:
            return algorithms.RSAAlgorithm.from_jwk(json.dumps(match))
        except (PyJWKClientError, jwt.InvalidKeyError, ValueError) as exc:
            raise OidcValidationError("invalid_jwk", str(exc)) from exc

    def validate_id_token(
        self,
        id_token: str,
        *,
        discovery: OidcDiscovery,
        client_id: str,
        nonce: str,
        jwks: dict[str, object] | None = None,
        clock_skew_seconds: int = 120,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Validate signature + standard claims + nonce; return the claim set."""
        del now  # PyJWT validates exp/iat against wall-clock; kept for parity.
        keyset = jwks if jwks is not None else self.fetch_jwks(discovery.jwks_uri)
        key = self._signing_key(keyset, id_token)
        try:
            claims = jwt.decode(
                id_token,
                key=key,  # type: ignore[arg-type]
                algorithms=_SIGNING_ALGS,
                audience=client_id,
                issuer=discovery.issuer,
                leeway=clock_skew_seconds,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise OidcValidationError("expired", str(exc)) from exc
        except jwt.ImmatureSignatureError as exc:
            raise OidcValidationError("not_yet_valid", str(exc)) from exc
        except jwt.InvalidAudienceError as exc:
            raise OidcValidationError("audience_mismatch", str(exc)) from exc
        except jwt.InvalidIssuerError as exc:
            raise OidcValidationError("issuer_mismatch", str(exc)) from exc
        except jwt.InvalidSignatureError as exc:
            raise OidcValidationError("invalid_signature", str(exc)) from exc
        except jwt.MissingRequiredClaimError as exc:
            raise OidcValidationError("missing_claim", str(exc)) from exc
        except jwt.InvalidTokenError as exc:
            raise OidcValidationError("invalid_token", str(exc)) from exc

        token_nonce = claims.get("nonce")
        if token_nonce != nonce:
            raise OidcValidationError("nonce_mismatch", "id_token nonce != login nonce")
        return dict(claims)

    @classmethod
    def clear_caches(cls) -> None:
        """Drop the process-wide discovery/JWKS caches (test isolation seam)."""
        cls._discovery_cache.clear()
        cls._jwks_cache.clear()


def _utcnow() -> datetime:  # pragma: no cover - trivial seam
    return datetime.now(UTC)


__all__ = [
    "MAX_DOC_BYTES",
    "InMemoryOidcStateStore",
    "OidcClient",
    "OidcDiscovery",
    "OidcTransaction",
    "build_authorize_url",
    "extract_identity",
    "generate_pkce",
    "new_nonce",
    "new_state",
]
