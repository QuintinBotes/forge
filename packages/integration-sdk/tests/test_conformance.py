"""Structural conformance to the frozen ``forge_contracts`` Protocols (Task 1.13).

``runtime_checkable`` Protocols verify the public method surface matches the
frozen contract so DI wiring and the API layer can depend on the protocol type.
"""

from __future__ import annotations

from forge_contracts import (
    GitHubClient as GitHubClientProtocol,
)
from forge_contracts import (
    IntegrationClient as IntegrationClientProtocol,
)
from forge_contracts import (
    PMAdapter as PMAdapterProtocol,
)
from forge_contracts import (
    SlackNotifier as SlackNotifierProtocol,
)
from forge_integrations import (
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


def test_public_exports_present() -> None:
    import forge_integrations as mod

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
