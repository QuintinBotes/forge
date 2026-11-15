"""Structural conformance to the frozen ``scf_contracts`` Protocols (Task 1.13).

``runtime_checkable`` Protocols verify the public method surface matches the
frozen contract so DI wiring and the API layer can depend on the protocol type.
"""

from __future__ import annotations

from scf_contracts import (
    GitHubClient as GitHubClientProtocol,
)
from scf_contracts import (
    IntegrationClient as IntegrationClientProtocol,
)
from scf_contracts import (
    PMAdapter as PMAdapterProtocol,
)
from scf_contracts import (
    SlackNotifier as SlackNotifierProtocol,
)
from scf_integrations import (
    GenericPMAdapter,
    GitHubClient,
    SlackNotifier,
)


def test_github_client_conforms() -> None:
    client = GitHubClient(token="t")
    assert isinstance(client, GitHubClientProtocol)
    assert isinstance(client, IntegrationClientProtocol)


def test_slack_notifier_conforms() -> None:
    notifier = SlackNotifier(token="t")
    assert isinstance(notifier, SlackNotifierProtocol)
    assert isinstance(notifier, IntegrationClientProtocol)


def test_pm_adapter_conforms() -> None:
    adapter = GenericPMAdapter()
    assert isinstance(adapter, PMAdapterProtocol)


def test_github_issues_conforms_to_async_pm_contract() -> None:
    from uuid import uuid4

    from scf_contracts.pm import AdapterContext, PMAdapter, PMProvider
    from scf_integrations.pm import GitHubIssuesAdapter
    from scf_integrations.pm.base import BaseAdapter

    ctx = AdapterContext(
        connection_id=uuid4(),
        workspace_id=uuid4(),
        provider=PMProvider.github_issues,
        external_project_key="acme/widgets",
        external_project_id="10",
    )
    with GitHubClient(token="t") as client:
        adapter = GitHubIssuesAdapter(client, ctx)
        assert isinstance(adapter, PMAdapter)
        assert isinstance(adapter, BaseAdapter)


def test_public_exports_present() -> None:
    import scf_integrations as mod

    for name in (
        "GitHubClient",
        "SlackNotifier",
        "BasePMAdapter",
        "GenericPMAdapter",
        "parse_github_webhook",
        "verify_github_signature",
        "verify_slack_signature",
        "sign_github_payload",
        "IntegrationError",
        "GitHubError",
        "SlackError",
    ):
        assert hasattr(mod, name), f"missing public export: {name}"
