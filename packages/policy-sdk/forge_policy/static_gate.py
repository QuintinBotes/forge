"""A real static ``forbidden_shortcuts`` scanner (F40-POL-GOVERNANCE).

Until now a skill profile's ``forbidden_shortcuts`` were only a *prompt directive*
folded into the agent's system prompt (``forge_agent.context``): nothing checked
that the produced diff actually honoured them. This module is the enforcement
half — a deterministic scanner the verification step runs over the produced files
and that **fails the check** when a forbidden shortcut is present.

Design:

* ``forbidden_shortcuts`` entries are literal substrings (case-insensitive) —
  the same human-readable phrases skill profiles already declare (e.g.
  ``"skip failing tests"``, ``"# type: ignore"``, ``"eslint-disable"``). Literal
  matching keeps the gate total and free of regex-injection / catastrophic
  backtracking (the policy layer stays non-Turing).
* The scan is pure: content in, violations out. No filesystem walk is imposed —
  the caller supplies ``{path: text}`` (from the diff, worktree, or PR) so the
  gate is trivially unit-testable and reusable across surfaces.
* ``StaticGateResult.passed`` is ``False`` iff any violation was found; the
  verification service turns a failing result into a failed check.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "ShortcutViolation",
    "StaticGateResult",
    "scan_forbidden_shortcuts",
]


class ShortcutViolation(BaseModel):
    """One forbidden-shortcut hit located at ``file``:``line``."""

    file: str
    line: int
    shortcut: str
    excerpt: str


class StaticGateResult(BaseModel):
    """Outcome of a static forbidden-shortcuts scan across a set of files."""

    passed: bool = True
    violations: list[ShortcutViolation] = Field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.passed:
            return "static forbidden-shortcuts gate passed"
        return f"static forbidden-shortcuts gate failed: {len(self.violations)} violation(s)"


def _normalized_shortcuts(forbidden: list[str]) -> list[str]:
    """Lower-cased, de-duplicated, non-empty shortcut needles (order-preserving)."""
    seen: dict[str, None] = {}
    for raw in forbidden:
        needle = raw.strip().lower()
        if needle:
            seen.setdefault(needle, None)
    return list(seen)


def scan_forbidden_shortcuts(
    files: dict[str, str],
    forbidden_shortcuts: list[str],
) -> StaticGateResult:
    """Scan ``files`` (``{path: text}``) for any ``forbidden_shortcuts`` substring.

    Returns a :class:`StaticGateResult`; ``passed`` is ``False`` when at least one
    forbidden shortcut appears. Matching is case-insensitive and per-line so the
    violation can be pinned to a ``file``:``line`` for the reviewer.
    """
    needles = _normalized_shortcuts(forbidden_shortcuts)
    if not needles:
        return StaticGateResult(passed=True, violations=[])

    violations: list[ShortcutViolation] = []
    for path in sorted(files):
        for lineno, raw_line in enumerate(files[path].splitlines(), start=1):
            haystack = raw_line.lower()
            for needle in needles:
                if needle in haystack:
                    violations.append(
                        ShortcutViolation(
                            file=path,
                            line=lineno,
                            shortcut=needle,
                            excerpt=raw_line.strip()[:200],
                        )
                    )
    return StaticGateResult(passed=not violations, violations=violations)
