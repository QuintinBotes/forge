"""Benchmark-suite error types (F35).

Small, dependency-free exception hierarchy shared by the manifest loader, the
freeze path, and the verification pipeline.
"""

from __future__ import annotations

__all__ = [
    "BenchmarkContentHashMismatch",
    "BenchmarkError",
    "BenchmarkFrozenError",
    "BenchmarkVerificationError",
]


class BenchmarkError(Exception):
    """Base error for the F35 benchmark package."""


class BenchmarkFrozenError(BenchmarkError):
    """A frozen suite was mutated, or a suite is not freezable as authored.

    Raised when re-freezing a frozen manifest whose recomputed ``content_hash``
    differs (the only resolution is a ``version`` bump), and when a suite
    contains an ``agent_task`` case that declares ``expected_terminal_state:
    merged`` (the human-approval-before-merge non-negotiable, AC24).
    """


class BenchmarkContentHashMismatch(BenchmarkError):
    """On-disk cases no longer hash to the frozen manifest's ``content_hash``."""


class BenchmarkVerificationError(BenchmarkError):
    """A submission could not be verified (malformed bundles, missing data)."""
