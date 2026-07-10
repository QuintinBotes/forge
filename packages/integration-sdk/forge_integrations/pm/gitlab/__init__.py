"""GitLab issues PM adapter (F40-PM-ADAPTERS-2)."""

from __future__ import annotations

from forge_integrations.pm.gitlab.adapter import GitLabAdapter
from forge_integrations.pm.gitlab.client import GitLabClient

__all__ = ["GitLabAdapter", "GitLabClient"]
