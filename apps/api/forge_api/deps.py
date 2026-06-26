"""Shared FastAPI dependencies (auth principal, settings, DB session).

The auth dependency here is a **stub** (plan Task 0.4): it returns a deterministic
test principal so feature routers can declare ``Depends(get_current_principal)``
today and have real authentication swapped in by Task 1.15 (auth & secrets)
without changing any router signatures.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forge_api.db import get_db
from forge_api.settings import Settings, get_settings
from forge_contracts import UserRole

# Deterministic identifiers for the stub principal (stable across calls so tests
# and local development behave predictably).
TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


class Principal(BaseModel):
    """The authenticated actor for a request (user, agent-runner, or API key).

    Task 1.15 fills this from Better Auth / API-key verification; until then the
    stub dependency returns a fixed admin principal.
    """

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    role: UserRole = UserRole.ADMIN
    email: str | None = None
    auth_method: str = "stub"
    scopes: list[str] = Field(default_factory=list)


def get_current_principal() -> Principal:
    """Auth stub: return a deterministic admin test principal.

    PARKED-FOR: real OAuth / API-key verification is implemented in Task 1.15;
    this stub keeps every authenticated route callable in Phase 0.
    """
    return Principal(
        user_id=TEST_USER_ID,
        workspace_id=TEST_WORKSPACE_ID,
        role=UserRole.ADMIN,
        email="test-principal@forge.local",
        auth_method="stub",
        scopes=["*"],
    )


# Ergonomic annotated aliases for use in router signatures.
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[Session, Depends(get_db)]
