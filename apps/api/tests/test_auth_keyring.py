"""KeyRing tests (HARD-13): versioned KEK resolution from the secret provider."""

from __future__ import annotations

import pytest

from forge_api.auth.keyring import KeyRing
from forge_api.auth.providers import EnvSecretProvider


def _provider(env: dict[str, str]) -> EnvSecretProvider:
    return EnvSecretProvider(environ=env)


def test_from_provider_reads_current_key_as_version_1() -> None:
    ring = KeyRing.from_provider(_provider({"FORGE_SECRET_KEY": "current-key-material-00"}))
    assert ring is not None
    assert ring.current_version == 1
    assert ring.current_kek() == b"current-key-material-00"
    assert ring.versions() == [1]


def test_from_provider_reads_previous_versions() -> None:
    ring = KeyRing.from_provider(
        _provider(
            {
                "FORGE_SECRET_KEY": "new-current-key-material",
                "FORGE_SECRET_KEY_V1": "old-key-material-version1",
                "FORGE_SECRET_KEY_VERSION": "2",
            }
        )
    )
    assert ring is not None
    assert ring.current_version == 2
    assert ring.kek(1) == b"old-key-material-version1"
    assert ring.kek(2) == b"new-current-key-material"
    assert ring.versions() == [1, 2]


def test_current_version_defaults_to_highest_present() -> None:
    ring = KeyRing.from_provider(
        _provider(
            {
                "FORGE_SECRET_KEY": "current-key-material-xx",
                "FORGE_SECRET_KEY_V1": "v1-material-aaaaaaaaaaaa",
                "FORGE_SECRET_KEY_V3": "v3-material-cccccccccccc",
            }
        )
    )
    assert ring is not None
    # Highest present version is 3, so the current key occupies v3.
    assert ring.current_version == 3
    assert ring.current_kek() == b"current-key-material-xx"


def test_from_provider_requires_a_key_by_default() -> None:
    with pytest.raises(RuntimeError, match="FORGE_SECRET_KEY"):
        KeyRing.from_provider(_provider({}))


def test_from_provider_returns_none_when_not_required() -> None:
    assert KeyRing.from_provider(_provider({}), require=False) is None


def test_unknown_version_raises_keyerror() -> None:
    ring = KeyRing({1: b"k" * 32}, 1)
    with pytest.raises(KeyError, match="FORGE_SECRET_KEY_V9"):
        ring.kek(9)


def test_short_kek_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least"):
        KeyRing({1: b"tooshort"}, 1)


def test_current_version_must_have_material() -> None:
    with pytest.raises(ValueError, match="no KEK material"):
        KeyRing({1: b"k" * 32}, 2)
