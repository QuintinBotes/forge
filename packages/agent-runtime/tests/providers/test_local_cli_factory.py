import pytest

from forge_agent.providers import ModelClientConfig, ProviderName, build_model_client
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
