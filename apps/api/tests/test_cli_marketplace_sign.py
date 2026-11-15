"""``forge-cli marketplace sign``: prints manifest_hash + a verifying signature."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from forge_api.cli import build_parser, main
from forge_marketplace.manifest import compute_manifest_hash, dump_manifest
from forge_marketplace.models import ArtifactKind
from forge_marketplace.packaging import build_package
from forge_marketplace.verifier import Ed25519SignatureVerifier


def _manifest():
    return build_package(
        kind=ArtifactKind.skill_profile,
        artifact={"name": "signed-skill", "description": "x"},
        slug="signed-skill",
        name="signed-skill",
        version="1.0.0",
        summary="s",
        validate_artifact=False,
    )


def _key_file(tmp_path: Path, mode: int = 0o600) -> tuple[Path, Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    secret_b64 = base64.b64encode(key.private_bytes_raw()).decode()
    path = tmp_path / "registry.key"
    path.write_text(secret_b64 + "\n")
    path.chmod(mode)
    return path, key, secret_b64


def test_sign_parses() -> None:
    args = build_parser().parse_args(
        ["marketplace", "sign", "--key-file", "k", "--manifest", "m.yaml"]
    )
    assert (args.group, args.command, args.key_file, args.manifest) == (
        "marketplace",
        "sign",
        "k",
        "m.yaml",
    )


@pytest.mark.parametrize("fmt", ["yaml", "json"])
def test_sign_prints_hash_and_verifying_signature(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fmt: str
) -> None:
    manifest = _manifest()
    manifest_file = tmp_path / f"forge-package.{fmt}"
    manifest_file.write_text(
        dump_manifest(manifest) if fmt == "yaml" else json.dumps(manifest.model_dump(mode="json"))
    )
    key_file, key, secret_b64 = _key_file(tmp_path)

    assert (
        main(["marketplace", "sign", "--key-file", str(key_file), "--manifest", str(manifest_file)])
        == 0
    )
    out, err = capsys.readouterr()
    lines = dict(line.split(":", 1) for line in out.strip().splitlines())
    assert lines["manifest_hash"].strip() == compute_manifest_hash(manifest)
    public_b64 = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    assert Ed25519SignatureVerifier().verify(
        payload=compute_manifest_hash(manifest).encode("utf-8"),
        signature_b64=lines["signature"].strip(),
        public_key_b64=public_b64,
    )
    assert lines["public_key"].strip() == public_b64
    assert secret_b64 not in out + err


def test_sign_refuses_a_group_readable_key_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_file = tmp_path / "forge-package.yaml"
    manifest_file.write_text(dump_manifest(_manifest()))
    key_file, _, secret_b64 = _key_file(tmp_path, mode=0o640)

    assert (
        main(["marketplace", "sign", "--key-file", str(key_file), "--manifest", str(manifest_file)])
        == 2
    )
    out, err = capsys.readouterr()
    assert out == ""
    assert "chmod 600" in err
    assert secret_b64 not in err
