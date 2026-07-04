# SAST rule fixtures — every construct here MUST be flagged by
# .semgrep/forge.yml (tests/security/test_scanners_and_waivers.py copies this
# tree to a tmp dir and asserts findings land ONLY in bad/). Never imported.
import logging
import subprocess

import httpx
import requests
import yaml

logger = logging.getLogger(__name__)


def planted_shell_true(cmd: str) -> None:
    subprocess.run(cmd, shell=True)  # forge-no-subprocess-shell-true
    subprocess.Popen(cmd, shell=True)  # forge-no-subprocess-shell-true


def planted_verify_false(url: str) -> None:
    httpx.get(url, verify=False)  # forge-no-tls-verify-false
    requests.get(url, verify=False)  # forge-no-tls-verify-false
    httpx.Client(verify=False)  # forge-no-tls-verify-false


def planted_yaml_load(data: str) -> object:
    return yaml.load(data)  # forge-no-unsafe-yaml-load


def planted_yaml_full_loader(data: str) -> object:
    return yaml.load(data, Loader=yaml.FullLoader)  # forge-no-unsafe-yaml-loader-class


def planted_eval(expr: str) -> object:
    return eval(expr)  # forge-no-eval-exec


def planted_secret_logging(api_key: str, client_secret: str) -> None:
    logger.info("using key %s", api_key)  # forge-no-secret-logging
    logger.error("oauth failed for %s", client_secret)  # forge-no-secret-logging


def planted_write_enabled_connection(MCPConnection, FernetCipher):
    conn = MCPConnection(id="c", name="n", allow_write=True)  # forge-no-mcp-write-default
    cipher = FernetCipher(b"hardcoded-master-key-material")  # forge-no-literal-cipher-key
    return conn, cipher
