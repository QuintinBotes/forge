"""Loopback HTTP bridge exposing the host's local agent CLIs to a containerised worker.

Development and self-host convenience only — never for a shared or production
deployment. See ``README.md`` for the security model: bound to 127.0.0.1 unless
``FORGE_CLI_BRIDGE_ALLOW_NON_LOOPBACK=1`` is set, and refuses to start without
``FORGE_CLI_BRIDGE_TOKEN``. The bridge never reads or forwards CLI credentials —
those stay in the host keychain, used by the CLI itself.
"""

from __future__ import annotations

__version__ = "0.1.0"
