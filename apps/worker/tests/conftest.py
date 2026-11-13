"""Worker test defaults.

Tests are the one place canned model output is allowed (ADR-0007): opt the
hermetic lane into the scripted client. Tests of the refusal path unset it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _allow_scripted_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_ALLOW_SCRIPTED_AGENT", "1")
