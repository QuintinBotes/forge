"""Schemas for a task's held-out acceptance scenarios (#176)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = ["HeldOutScenarioCreate", "HeldOutScenarioOut", "ScenarioFile"]


class ScenarioFile(BaseModel):
    """One file the scenario overlays on the change's checkout."""

    path: str = Field(min_length=1, max_length=1024)
    content: str

    @field_validator("path")
    @classmethod
    def _inside_the_checkout(cls, path: str) -> str:
        """A path that could leave the checkout, or write into its git data, is refused."""
        parts = path.replace("\\", "/").split("/")
        if (
            path.startswith(("/", "\\"))
            or (len(path) > 1 and path[1] == ":")  # a drive letter
            or "\x00" in path
            or any(part in {"..", ".git"} for part in parts)
            or not any(part not in {"", "."} for part in parts)
        ):
            raise ValueError("the path must stay inside the checkout")
        return path


class HeldOutScenarioCreate(BaseModel):
    """A new scenario. It is created as a draft."""

    title: str = Field(min_length=1, max_length=255)
    files: list[ScenarioFile] = Field(min_length=1)
    command: str = Field(min_length=1)

    @model_validator(mode="after")
    def _one_content_per_path(self) -> HeldOutScenarioCreate:
        """Two files may not name one checkout path, however it is spelled."""
        seen = set()
        for file in self.files:
            parts = [p for p in file.path.replace("\\", "/").split("/") if p not in {"", "."}]
            key = "/".join(parts)
            if key in seen:
                raise ValueError(f"two files name the path {key}")
            seen.add(key)
        return self


class HeldOutScenarioOut(BaseModel):
    """A scenario as people with write access see it. No agent-facing surface returns it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    title: str
    files: list[ScenarioFile]
    command: str
    drafted_by: str
    state: str
    approved_by: uuid.UUID | None = None
    approved_at: datetime | None = None
    created_at: datetime
