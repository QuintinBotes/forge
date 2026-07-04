"""forge_eval.release — the automated RELEASE_READINESS meta-gate (HARD-12).

This subpackage does **not** invent product behaviour. It mechanically encodes
``SPEC-PRODUCTION-HARDENING.md``'s release-readiness model — the ALPHA/BETA/
PRODUCTION *bars* and the 18 lettered gates plus the two named human-only items
(``G-PENTEST`` / ``G-SOAK-FLEET``) — into one command that runs or inspects every
gate and emits a dated, evidenced ``RELEASE_READINESS.md`` with a CI-grade exit
code.

The load-bearing honesty guarantee lives in :mod:`forge_eval.release.model`: a
bar is *met* only when every gate at-or-below it is ``GREEN`` or
``MANUAL_ATTESTED``. A ``manual`` gate can reach ``MANUAL_ATTESTED`` **only** via
a real, signed attestation file — the engine never infers it — so PRODUCTION can
never be declared on un-evidenced or simulated work.

Public surface:

- :class:`~forge_eval.release.model.Bar`, :class:`~forge_eval.release.model.Status`,
  :class:`~forge_eval.release.model.Gate`, :class:`~forge_eval.release.model.GateResult`,
  and :func:`~forge_eval.release.model.bar_met`.
- :func:`~forge_eval.release.readiness.evaluate` / :func:`~forge_eval.release.readiness.main`.
- :func:`~forge_eval.release.render.render_markdown`.
"""

from __future__ import annotations

from forge_eval.release.model import (
    MET_STATUSES,
    Bar,
    Gate,
    GateResult,
    Status,
    bar_met,
    load_gates,
)

__all__ = [
    "MET_STATUSES",
    "Bar",
    "Gate",
    "GateResult",
    "Status",
    "bar_met",
    "load_gates",
]
