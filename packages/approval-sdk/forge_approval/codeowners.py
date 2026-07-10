"""CODEOWNERS parsing + required-approver resolution (F40-POL-GOVERNANCE).

The PR gate previously honoured ``review_rules.required_reviewers`` (a flat repo
list) but ignored a repo's ``CODEOWNERS`` file — the standard, path-scoped way to
say *who* must approve *which* paths. This module parses ``CODEOWNERS`` (GitHub
syntax) and, together with ``review_rules.min_approvals``, resolves the concrete
set of owners whose approval a change to a set of paths requires.

Semantics (matching GitHub):

* one rule per non-comment line: ``<pattern> <owner> [<owner> ...]``;
* the **last** matching rule for a path wins (later rules override earlier);
* a rule with a pattern but no owners clears ownership for matching paths;
* patterns are gitignore-style globs (``*`` does not cross ``/`` unless the
  pattern is a bare ``*``; a leading ``/`` anchors to the repo root; a trailing
  ``/`` matches a directory subtree).

Parsing/resolution is pure and total (no regex over untrusted input, no I/O).
"""

from __future__ import annotations

import fnmatch

from pydantic import BaseModel, Field

__all__ = [
    "CodeownersRule",
    "CodeownersRuleset",
    "parse_codeowners",
    "required_owners_for_paths",
]


class CodeownersRule(BaseModel):
    """One ``CODEOWNERS`` line: a path ``pattern`` and its ``owners``."""

    pattern: str
    owners: list[str] = Field(default_factory=list)


class CodeownersRuleset(BaseModel):
    """An ordered set of ``CODEOWNERS`` rules (last match wins)."""

    rules: list[CodeownersRule] = Field(default_factory=list)

    def owners_for(self, path: str) -> list[str]:
        """Return the owners for ``path`` (last matching rule wins; else empty)."""
        matched: list[str] = []
        for rule in self.rules:
            if _matches(rule.pattern, path):
                matched = list(rule.owners)
        return matched


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def _matches(pattern: str, path: str) -> bool:
    """True if ``path`` matches a gitignore-style CODEOWNERS ``pattern``."""
    norm = _normalize_path(path)
    pat = pattern.strip()
    if pat in ("*", "/*"):
        return True

    anchored = pat.startswith("/")
    pat = pat.lstrip("/")

    if pat.endswith("/"):
        # Directory subtree: match the dir itself and anything beneath it.
        base = pat.rstrip("/")
        return norm == base or norm.startswith(base + "/")

    # A bare directory name (no glob, no slash) matches its whole subtree.
    is_bare = not any(ch in pat for ch in "/*?[")
    if is_bare and (norm == pat or norm.startswith(pat + "/")):
        return True

    if fnmatch.fnmatch(norm, pat):
        return True
    if not anchored and fnmatch.fnmatch(norm, f"*/{pat}"):
        return True
    # ``dir/**`` style: also match the subtree prefix.
    return pat.endswith("/**") and norm.startswith(pat[:-3].rstrip("/") + "/")


def parse_codeowners(text: str) -> CodeownersRuleset:
    """Parse ``CODEOWNERS`` file ``text`` into an ordered :class:`CodeownersRuleset`."""
    rules: list[CodeownersRule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern, owners = parts[0], parts[1:]
        rules.append(CodeownersRule(pattern=pattern, owners=owners))
    return CodeownersRuleset(rules=rules)


def required_owners_for_paths(ruleset: CodeownersRuleset, paths: list[str]) -> list[str]:
    """Union (order-preserving) of owners across every path in ``paths``."""
    seen: dict[str, None] = {}
    for path in paths:
        for owner in ruleset.owners_for(path):
            seen.setdefault(owner, None)
    return list(seen)
