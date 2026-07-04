"""HARD-01 unit tests: App JWT minting + installation-token caching (offline).

Every HTTP interaction is served by an ``httpx.MockTransport`` from synthetic
fixtures — no live GitHub calls, no network. The RSA keypair is generated
in-test (a throwaway key, never the real App key).
"""

from __future__ import annotations

import json

import httpx
import jwt
import pytest
from conftest import load_fixture, make_transport
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from forge_integrations import InstallationTokenProvider, build_app_jwt
from forge_integrations.github_auth import load_private_key

APP_ID = "123456"
INSTALL_ID = "987654"


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    """A throwaway RS256 keypair (PEM private, PEM public) for signing tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


# --------------------------------------------------------------------------- #
# build_app_jwt (AC1)                                                          #
# --------------------------------------------------------------------------- #


def test_app_jwt_claims_and_signature(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    now = 1_700_000_000
    token = build_app_jwt(APP_ID, private_pem, now=now)

    # Header advertises RS256.
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"

    # Verifying with the matching public key succeeds. ``now`` is a fixed epoch
    # in the past, so disable liveness (exp) verification — we assert the exp
    # window explicitly below.
    claims = jwt.decode(token, public_pem, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == APP_ID
    assert claims["iat"] <= now
    assert claims["exp"] - claims["iat"] <= 600


def test_app_jwt_wrong_key_fails(keypair: tuple[str, str]) -> None:
    private_pem, _ = keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pub = (
        other.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    token = build_app_jwt(APP_ID, private_pem, now=1_700_000_000)
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, other_pub, algorithms=["RS256"])


def test_app_jwt_exp_capped_at_600s(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    now = 1_700_000_000
    # Ask for a wildly long TTL — it must still be capped so exp-iat <= 600.
    token = build_app_jwt(APP_ID, private_pem, now=now, ttl_seconds=100_000)
    claims = jwt.decode(token, public_pem, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["exp"] - claims["iat"] <= 600
    assert claims["exp"] <= now + 600


# --------------------------------------------------------------------------- #
# InstallationTokenProvider (AC2)                                              #
# --------------------------------------------------------------------------- #


class _FakeClock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _token_transport(counter: list[int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/access_tokens")
        # The App JWT must be presented as a bearer to mint an installation token.
        assert request.headers["authorization"].startswith("Bearer ")
        counter.append(1)
        return httpx.Response(201, json=load_fixture("installation_token"))

    return make_transport(handler)


def _provider(keypair: tuple[str, str], clock: _FakeClock, counter: list[int]):
    private_pem, _ = keypair
    return InstallationTokenProvider(
        app_id=APP_ID,
        private_key_pem=private_pem,
        installation_id=INSTALL_ID,
        transport=_token_transport(counter),
        clock=clock,
    )


def test_installation_token_minted_and_cached(keypair: tuple[str, str]) -> None:
    counter: list[int] = []
    clock = _FakeClock(1_700_000_000.0)
    provider = _provider(keypair, clock, counter)

    tok1 = provider.token()
    tok2 = provider.token()
    assert tok1 == tok2 == "ghs_synthetic_installation_token_000000000000"
    # Exactly one mint — the second call is served from cache.
    assert len(counter) == 1
    assert provider.mint_count == 1


def test_token_refresh_near_expiry(keypair: tuple[str, str]) -> None:
    counter: list[int] = []
    # Token expires at epoch of the fixture's 2099 date; force the clock to just
    # inside the 60s refresh margin so the next call re-mints.
    clock = _FakeClock(1_700_000_000.0)
    provider = _provider(keypair, clock, counter)
    provider.token()
    assert len(counter) == 1

    # Fast-forward to 30s before expiry (inside the 60s margin) -> re-mint.
    from forge_integrations.github_auth import _parse_expiry

    expiry = _parse_expiry("2099-01-01T00:00:00Z", clock.now)
    clock.now = expiry - 30
    provider.token()
    assert len(counter) == 2


def test_invalidate_forces_remint(keypair: tuple[str, str]) -> None:
    counter: list[int] = []
    clock = _FakeClock(1_700_000_000.0)
    provider = _provider(keypair, clock, counter)
    provider.token()
    assert len(counter) == 1

    provider.invalidate()
    provider.token()
    assert len(counter) == 2


def test_token_request_error_raises_githuberror(keypair: tuple[str, str]) -> None:
    from forge_integrations import GitHubError

    private_pem, _ = keypair

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    provider = InstallationTokenProvider(
        app_id=APP_ID,
        private_key_pem=private_pem,
        installation_id=INSTALL_ID,
        transport=make_transport(handler),
    )
    with pytest.raises(GitHubError) as exc:
        provider.token()
    assert exc.value.status_code == 401


# --------------------------------------------------------------------------- #
# load_private_key (AC7) — path handling never leaks key bytes                 #
# --------------------------------------------------------------------------- #


def test_load_private_key_reads_file(tmp_path, keypair: tuple[str, str]) -> None:
    private_pem, _ = keypair
    pem_path = tmp_path / "app.pem"
    pem_path.write_text(private_pem)
    assert load_private_key(str(pem_path)) == private_pem


def test_load_private_key_missing_file_mentions_only_path(tmp_path) -> None:
    from forge_integrations import GitHubError

    missing = tmp_path / "nope.pem"
    with pytest.raises(GitHubError) as exc:
        load_private_key(str(missing))
    message = str(exc.value)
    assert "nope.pem" in message
    # No private-key material of any kind in the error.
    assert "PRIVATE KEY" not in message
    assert "BEGIN" not in message


def test_provider_repr_does_not_leak_key(keypair: tuple[str, str]) -> None:
    private_pem, _ = keypair
    provider = InstallationTokenProvider(
        app_id=APP_ID,
        private_key_pem=private_pem,
        installation_id=INSTALL_ID,
    )
    text = repr(provider)
    assert "PRIVATE KEY" not in text
    assert private_pem[40:80] not in text
    # And the key is not exposed via a plainly-named attribute.
    assert not hasattr(provider, "private_key_pem")


def test_provider_vars_do_not_expose_key_under_plain_name(
    keypair: tuple[str, str],
) -> None:
    private_pem, _ = keypair
    provider = InstallationTokenProvider(
        app_id=APP_ID,
        private_key_pem=private_pem,
        installation_id=INSTALL_ID,
    )
    # The key is stored name-mangled; assert no obvious attribute holds it and
    # that a JSON dump of the public-looking state carries no PEM.
    public_state = {k: v for k, v in vars(provider).items() if not k.endswith("__private_key_pem")}
    dumped = json.dumps(public_state, default=str)
    assert "PRIVATE KEY" not in dumped
