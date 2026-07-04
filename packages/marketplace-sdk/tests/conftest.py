"""Fixtures wrapping the pure builders in ``_mp_helpers`` for the SDK tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from _mp_helpers import Keypair, Package, generate_keypair
from _mp_helpers import make_mcp_package as _build_mcp
from _mp_helpers import make_skill_package as _build_skill


@pytest.fixture
def signing_keypair() -> Keypair:
    return generate_keypair()


@pytest.fixture
def make_skill_package(signing_keypair: Keypair) -> Callable[..., Package]:
    def _make(**kwargs: object) -> Package:
        kwargs.setdefault("keypair", signing_keypair)
        return _build_skill(**kwargs)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def make_mcp_package(signing_keypair: Keypair) -> Callable[..., Package]:
    def _make(**kwargs: object) -> Package:
        kwargs.setdefault("keypair", signing_keypair)
        return _build_mcp(**kwargs)  # type: ignore[arg-type]

    return _make
