"""Facts about a diff (data only; the caller reads the diff, spec A §5.1).

The model lives in ``scf_contracts`` so every package can carry it.
"""

from __future__ import annotations

from scf_contracts import ChangeSignals

__all__ = ["ChangeSignals"]
