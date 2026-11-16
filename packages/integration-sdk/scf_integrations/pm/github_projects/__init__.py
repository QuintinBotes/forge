"""GitHub Projects (v2) PM adapter (F40-PM-ADAPTERS-1)."""

from __future__ import annotations

from scf_integrations.pm.github_projects.adapter import GitHubProjectsAdapter
from scf_integrations.pm.github_projects.client import GitHubProjectsClient

__all__ = ["GitHubProjectsAdapter", "GitHubProjectsClient"]
