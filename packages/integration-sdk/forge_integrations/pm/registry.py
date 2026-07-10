"""Adapter factory — the single OSS extension point.

Adding a new PM provider (Asana, Monday, GitLab issues, ...) is: implement a
``PMAdapter`` and register it here. The sync engine, hashing, conflict, and
webhook plumbing are reused unchanged.
"""

from __future__ import annotations

from forge_contracts.pm import (
    AdapterContext,
    GenericAdapterConfig,
    PMAdapter,
    PMProvider,
    PMTransport,
)
from forge_integrations.pm.asana.adapter import AsanaAdapter
from forge_integrations.pm.asana.client import AsanaClient
from forge_integrations.pm.clickup.adapter import ClickUpAdapter
from forge_integrations.pm.clickup.client import ClickUpClient
from forge_integrations.pm.errors import ProviderError
from forge_integrations.pm.generic.adapter import GenericAdapter
from forge_integrations.pm.github_projects.adapter import GitHubProjectsAdapter
from forge_integrations.pm.github_projects.client import GitHubProjectsClient
from forge_integrations.pm.gitlab.adapter import GitLabAdapter
from forge_integrations.pm.gitlab.client import API as GITLAB_API
from forge_integrations.pm.gitlab.client import GitLabClient
from forge_integrations.pm.jira.adapter import JiraAdapter
from forge_integrations.pm.jira.client import JiraClient
from forge_integrations.pm.linear.adapter import LinearAdapter
from forge_integrations.pm.linear.client import LinearClient
from forge_integrations.pm.monday.adapter import MondayAdapter
from forge_integrations.pm.monday.client import MondayClient
from forge_integrations.pm.trello.adapter import TrelloAdapter
from forge_integrations.pm.trello.client import TrelloClient


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
    if provider is PMProvider.clickup:
        clickup_client = ClickUpClient(transport, auth_header=auth_header)
        return ClickUpAdapter(clickup_client, ctx)
    if provider is PMProvider.trello:
        trello_client = TrelloClient(transport, auth_header=auth_header)
        return TrelloAdapter(trello_client, ctx)
    if provider is PMProvider.gitlab:
        gitlab_client = GitLabClient(
            transport, base_url=ctx.external_base_url or GITLAB_API, auth_header=auth_header
        )
        return GitLabAdapter(gitlab_client, ctx)
    if provider is PMProvider.generic:
        generic_config = GenericAdapterConfig.model_validate(ctx.config.get("generic_config") or {})
        return GenericAdapter(transport, ctx, generic_config, auth_header=auth_header)
    raise ProviderError(f"unsupported PM provider: {provider}")


__all__ = ["build_adapter"]
