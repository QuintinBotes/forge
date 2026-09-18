"""An empty environment value must not stop the API from starting.

`docker compose` writes `VAR: ${VAR:-}` to make a variable optional, so an unset
one arrives as `""` rather than being absent. For a string field that is
harmless; for a bool it is a ValidationError raised at import time, so the whole
API refuses to boot — which is exactly what a single
`FORGE_ALLOW_SCRIPTED_AGENT: ${...:-}` did to the dev stack.
"""

from __future__ import annotations

import pytest

from forge_api.settings import Settings


@pytest.mark.parametrize(
    "var,field",
    [
        ("FORGE_ALLOW_SCRIPTED_AGENT", "allow_scripted_agent"),
        ("FORGE_DEBUG", "debug"),
        ("FORGE_RATELIMIT_ENABLED", "ratelimit_enabled"),
        ("FORGE_DOCS_ENABLED", "docs_enabled"),
    ],
)
def test_an_empty_bool_env_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, var: str, field: str
) -> None:
    monkeypatch.setenv(var, "")

    settings = Settings()

    assert getattr(settings, field) == Settings.model_fields[field].default


def test_an_explicit_value_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not swallow a real setting."""
    monkeypatch.setenv("FORGE_ALLOW_SCRIPTED_AGENT", "1")

    assert Settings().allow_scripted_agent is True


def test_an_empty_string_field_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only non-string fields are cleared; "" is a legitimate string value."""
    monkeypatch.setenv("FORGE_SPEC_ROOT", "")

    # Whatever the field's type, constructing must not raise.
    Settings()
