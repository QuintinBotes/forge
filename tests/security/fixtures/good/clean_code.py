# Negative fixtures — none of these may be flagged by .semgrep/forge.yml
# (the safe spelling of each construct the bad/ tree plants).
import logging
import subprocess

import httpx
import yaml

logger = logging.getLogger(__name__)


def safe_subprocess(args: list[str]) -> None:
    subprocess.run(args, check=True)  # argv list, no shell


def safe_http(url: str) -> None:
    httpx.get(url)  # TLS verification stays on


def safe_yaml(data: str) -> object:
    return yaml.safe_load(data)


def safe_yaml_loader(data: str) -> object:
    return yaml.load(data, Loader=yaml.SafeLoader)


def safe_logging(api_key: str) -> None:
    logger.info("credential accepted (prefix %s)", api_key[:4] + "<redacted>")
    logger.info("workspace ready")


def safe_read_only_connection(MCPConnection) -> object:
    return MCPConnection(id="c", name="n")  # allow_write defaults to False
