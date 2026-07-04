"""Operator CLI for the secret subsystem (HARD-13).

Runnable as ``python -m forge_api.cli_secrets <cmd>`` (wired as the future
``forge-cli secrets`` subcommand; this module conforms to the repo's established
``cli_<name>.py`` sibling convention — cli_bench / cli_cost / cli_marketplace —
rather than the slice's ``cli/secrets.py`` package path, which would shadow the
existing ``forge_api.cli`` module). Three commands:

- ``check-config`` — a fail-closed preflight: asserts ``FORGE_SECRET_KEY``
  resolves, envelope encryption is on in production, and no deprecated
  ``SECRET_KEY``/``FORGE_ENV`` alias is in use. Exits non-zero on any violation
  so it can gate a deploy in CI (the ``secrets-config`` job).
- ``rotate-kek`` — re-wraps every stored data key under the current KEK
  (``FORGE_SECRET_KEY``), reading previous KEKs from ``FORGE_SECRET_KEY_V<n>``.
  No BYOK plaintext is decrypted. Prints ``{"rewrapped": N, "skipped": M}``.
- ``sweep-expired`` — flags (or, with ``--purge``, deletes) BYOK secrets past
  their ``expires_at``.

``rotate-kek``/``sweep-expired`` operate on the process ``AuthService`` vault; a
Postgres-backed store (the live rotation drill, HARD-13 AC14) is integration-gated
and documented in ``docs/self-hosting/security.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from forge_api.auth.providers import resolve_secret
from forge_api.auth.service import (
    _envelope_enabled,
    _keyring_for_master,
    _resolve_environment,
    _resolve_master_key,
)

#: Legacy env names that must not be used once an operator has migrated.
_DEPRECATED_ALIASES = ("SECRET_KEY", "FORGE_ENV")


def _check_config() -> int:
    """Preflight: return 0 when the config is production-safe, else 1."""
    problems: list[str] = []
    environment = _resolve_environment()

    if not resolve_secret("FORGE_SECRET_KEY"):
        problems.append(
            "FORGE_SECRET_KEY does not resolve (env / *_FILE / provider). "
            "Generate one: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )

    in_use = [name for name in _DEPRECATED_ALIASES if os.environ.get(name)]
    if in_use:
        problems.append(
            f"deprecated alias(es) in use: {', '.join(in_use)} — replace with "
            "FORGE_SECRET_KEY / FORGE_ENVIRONMENT."
        )

    if environment == "production" and not _envelope_enabled():
        problems.append(
            "FORGE_ENVELOPE_ENCRYPTION is off in production; envelope encryption "
            "is required for rotatable, per-secret data keys."
        )

    if problems:
        print(f"check-config FAILED (environment={environment!r}):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"check-config OK (environment={environment!r}, envelope=on)")
    return 0


def _rotate_kek(to_version: int | None) -> int:
    """Re-wrap every stored DEK under the current (or ``to_version``) KEK."""
    from forge_api.auth.service import AuthService

    service = AuthService()
    master = _resolve_master_key(None)
    keyring = _keyring_for_master(master)
    result = service.vault.rewrap_all(keyring=keyring, to_version=to_version)
    print(json.dumps(result))
    return 0


def _sweep_expired(purge: bool) -> int:
    """Flag (or delete) BYOK secrets past their ``expires_at``."""
    from forge_api.auth.service import AuthService

    service = AuthService()
    count = service.vault.sweep_expired(purge=purge)
    print(json.dumps({"expired": count, "purged": purge}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge_api.cli_secrets",
        description="Forge secret subsystem operator commands (HARD-13).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-config", help="Fail-closed production config preflight.")

    rotate = sub.add_parser("rotate-kek", help="Re-wrap all data keys under the current KEK.")
    rotate.add_argument("--to-version", type=int, default=None, help="Target KEK version.")

    sweep = sub.add_parser("sweep-expired", help="Flag/delete expired BYOK secrets.")
    sweep.add_argument("--purge", action="store_true", help="Delete instead of flag.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check-config":
        return _check_config()
    if args.command == "rotate-kek":
        return _rotate_kek(args.to_version)
    if args.command == "sweep-expired":
        return _sweep_expired(args.purge)
    return 2  # pragma: no cover - argparse enforces a known subcommand


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
