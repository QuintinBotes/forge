"""Adapter factory — the single OSS extension point.

Adding a new PM provider (Asana, Monday, GitLab issues, ...) is: implement a
``PMAdapter`` and register it here. The sync engine, hashing, conflict, and
webhook plumbing are reused unchanged.
"""

from __future__ import annotations

from forge_contracts.pm import AdapterContext, PMAdapter, PMProvider, PMTransport
from forge_integrations.pm.asana.adapter import AsanaAdapter
from forge_integrations.pm.asana.client import AsanaClient
from forge_integrations.pm.errors import ProviderError
from forge_integrations.pm.github_projects.adapter import GitHubProjectsAdapter
from forge_integrations.pm.github_projects.client import GitHubProjectsClient
from forge_integrations.pm.jira.adapter import JiraAdapter
from forge_integrations.pm.jira.client import JiraClient
from forge_integrations.pm.linear.adapter import LinearAdapter
from forge_integrations.pm.linear.client import LinearClient
from forge_integrations.pm.monday.adapter import MondayAdapter
from forge_integrations.pm.monday.client import MondayClient


def build_adapter(
    provider: PMProvider | str,
    transport: PMTransport,
    ctx: AdapterContext,
    *,
    auth_header: str | None = None,
) -> PMAdapter:
    """Construct the provider-specific adapter for ``ctx`` bound to ``transport``."""
    provider = PMProvider(provider)
    if provider is PMProvider.jira:
        base_url = ctx.external_base_url or ""
        jira_client = JiraClient(transport, base_url=base_url, auth_header=auth_header)
        return JiraAdapter(jira_client, ctx)
    if provider is PMProvider.linear:
        linear_client = LinearClient(transport, auth_header=auth_header)
        return LinearAdapter(linear_client, ctx)
    if provider is PMProvider.asana:
        asana_client = AsanaClient(transport, auth_header=auth_header)
        return AsanaAdapter(asana_client, ctx)
    if provider is PMProvider.monday:
        monday_client = MondayClient(transport, auth_header=auth_header)
        return MondayAdapter(monday_client, ctx)
    if provider is PMProvider.github_projects:
        gh_client = GitHubProjectsClient(transport, auth_header=auth_header)
        return GitHubProjectsAdapter(gh_client, ctx)
    raise ProviderError(f"unsupported PM provider: {provider}")


__all__ = ["build_adapter"]
