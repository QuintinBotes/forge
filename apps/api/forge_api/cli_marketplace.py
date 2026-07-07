"""``forge marketplace`` CLI (F32 §2 journey I / AC20).

The OSS publishing path is offline + git-native: an author runs
``forge marketplace package`` to turn a raw F09 ``mcp_connection`` / F11
``SkillProfile`` YAML into a canonical ``forge-package.yaml`` with a computed
``content_hash`` that re-verifies on install, then signs it and opens a PR to a
registry's git repo. The ``package`` command is implemented here (pure, no
network); the read/query subcommands (``search``/``show``/``list``/``install``/
``update``) are thin HTTP clients over ``/api/v1/marketplace`` and are PARKED
(they require a running API) — the offline authoring path is the load-bearing
piece and is what AC20 exercises.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from forge_marketplace.errors import SchemaInvalid
from forge_marketplace.manifest import compute_manifest_hash, dump_manifest
from forge_marketplace.models import ArtifactKind
from forge_marketplace.packaging import build_package


def _cmd_package(args: argparse.Namespace) -> int:
    artifact_path = Path(args.artifact)
    raw = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        print("error: artifact file must be a YAML mapping", file=sys.stderr)
        return 2
    try:
        manifest = build_package(
            kind=ArtifactKind(args.kind),
            artifact=raw,
            slug=args.slug,
            name=args.name,
            version=args.version,
            summary=args.summary,
            description=args.description,
            homepage=args.homepage,
            repository=args.repository,
            tags=list(args.tag or []),
            min_forge_version=args.min_forge_version,
        )
    except SchemaInvalid as exc:
        print(f"error: artifact failed validation: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "forge-package.yaml"
    out_file.write_text(dump_manifest(manifest), encoding="utf-8")

    print(f"wrote {out_file}")
    print(f"content_hash:  {manifest.content_hash}")
    print(f"manifest_hash: {compute_manifest_hash(manifest)}")
    print("next: sign manifest_hash with your Ed25519 key and open a PR to a registry repo")
    return 0


def _requires_api(args: argparse.Namespace) -> int:  # pragma: no cover - parked path
    print(
        f"'{args.command}' talks to a running Forge API (/api/v1/marketplace) — "
        "PARKED in this build; use the HTTP API or web UI.",
        file=sys.stderr,
    )
    return 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge marketplace")
    sub = parser.add_subparsers(dest="command", required=True)

    pkg = sub.add_parser("package", help="build a signed-ready forge-package.yaml")
    pkg.add_argument("artifact", help="path to the F09/F11 artifact YAML")
    pkg.add_argument("--kind", required=True, choices=[k.value for k in ArtifactKind])
    pkg.add_argument("--slug", required=True)
    pkg.add_argument("--name", required=True)
    pkg.add_argument("--version", required=True)
    pkg.add_argument("--summary", required=True)
    pkg.add_argument("--description", default=None)
    pkg.add_argument("--homepage", default=None)
    pkg.add_argument("--repository", default=None)
    pkg.add_argument("--tag", action="append", default=[])
    pkg.add_argument("--min-forge-version", dest="min_forge_version", default=None)
    pkg.add_argument("--out", default="dist")
    pkg.set_defaults(func=_cmd_package)

    for name in ("search", "show", "list", "install", "update"):
        p = sub.add_parser(name, help=f"{name} (requires a running API — parked)")
        p.set_defaults(func=_requires_api)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
