import pytest

from forge_agent.providers import ModelClientConfig, ProviderName, build_model_client
from forge_agent.providers.bridge_client import LocalCliBridgeClient
from forge_agent.providers.local_cli.client import LocalCliModelClient
from forge_agent.providers.local_cli.detect import CliName


def test_factory_builds_local_cli_client():
    config = ModelClientConfig(
        provider=ProviderName.local_cli, model="claude", cli_name=CliName.claude
    )
    assert isinstance(build_model_client(config), LocalCliModelClient)


def test_factory_rejects_local_cli_without_a_cli_name():
    from forge_agent.providers.base import ModelClientError

    config = ModelClientConfig(provider=ProviderName.local_cli, model="local-cli")
    with pytest.raises(ModelClientError):
        build_model_client(config)


def test_factory_builds_bridge_client_wired_from_config_when_bridge_url_set():
    """The `bridge_url` branch (base.py) was a lazy import until Task 8 added
    the module it imports; this is the seam between Task 6 (module did not
    exist yet) and Task 8 (scope was the client itself), and nothing exercised
    it end to end through the factory until now.
    """

    def redactor(value: str) -> str:
        return value

    config = ModelClientConfig(
        provider=ProviderName.local_cli,
        model="local-cli",
        bridge_url="http://host.docker.internal:8099",
        bridge_token="t0ken",
        timeout_s=42.0,
    )
    client = build_model_client(config, redactor=redactor)
    assert isinstance(client, LocalCliBridgeClient)
    assert client._base_url == "http://host.docker.internal:8099"
    assert client._timeout_s == 42.0
    assert client._redactor is redactor


def test_factory_bridge_url_with_blank_token_raises():
    """Amendment B changed behaviour here: a `bridge_url` with a blank token
    used to degrade to a per-call 401 (LocalCliBridgeClient would build fine
    and simply fail on every request); it now raises `ModelClientError` from
    inside `build_model_client` itself, before a client is ever returned.
    """
    from forge_agent.providers.base import ModelClientError

    config = ModelClientConfig(
        provider=ProviderName.local_cli,
        model="local-cli",
        bridge_url="http://host.docker.internal:8099",
        bridge_token="  ",
    )
    with pytest.raises(ModelClientError):
        build_model_client(config)
