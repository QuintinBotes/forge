"""The production compose stacks give the API its public address (issue #304).

The SSO callback, the SAML and SCIM addresses, the CLI sign-in link, the
same-origin check on cookie writes and the session cookie's ``Secure`` flag are
all built from ``FORGE_PUBLIC_URL``. Left unset, the API falls back to
``http://localhost:8000``, which is wrong behind any edge. These tests render the
``api`` service's value the way Compose interpolates it, without Docker.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

DEPLOY = Path(__file__).resolve().parent.parent
PROD = DEPLOY / "docker-compose.yml"
SINGLE_USER = DEPLOY / "docker-compose.single-user.yml"
#: An innermost ``${NAME}``, ``${NAME:-default}`` or ``${NAME:?message}``.
_VAR = re.compile(r"\$\{(\w+)(?:(:[-?])([^${}]*))?\}")


def _interpolate(value: str, env: dict[str, str]) -> str:
    """Compose's substitution for the forms these files use, innermost first."""

    def one(match: re.Match[str]) -> str:
        name, op, arg = match.groups()
        if env.get(name):
            return env[name]
        if op == ":?":
            raise ValueError(f"{name}: {arg}")
        return arg or ""

    while _VAR.search(value):
        value = _VAR.sub(one, value)
    return value


def _api_public_url(path: Path, **env: str) -> str:
    api = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["api"]
    assert "FORGE_PUBLIC_URL" in api["environment"], f"{path.name}: the api is not given it"
    return _interpolate(api["environment"]["FORGE_PUBLIC_URL"], env)


def test_production_refuses_to_start_without_the_public_url() -> None:
    # The message names the /api prefix the edge proxy serves the API under.
    with pytest.raises(ValueError, match=r"FORGE_PUBLIC_URL: .*/api"):
        _api_public_url(PROD)


def test_production_passes_the_public_url_to_the_api() -> None:
    url = "https://scf.example/api"
    assert _api_public_url(PROD, FORGE_PUBLIC_URL=url) == url


def test_single_user_defaults_to_its_published_loopback_address() -> None:
    """No edge proxy here, so no prefix: the API is published on 127.0.0.1."""
    assert _api_public_url(SINGLE_USER) == "http://127.0.0.1:8100"
    assert _api_public_url(SINGLE_USER, SCF_API_PORT="9123") == "http://127.0.0.1:9123"
    url = "https://scf.example"
    assert _api_public_url(SINGLE_USER, FORGE_PUBLIC_URL=url, SCF_API_PORT="9123") == url
