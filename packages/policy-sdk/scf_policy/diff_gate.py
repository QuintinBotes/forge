"""What the gates on an agent's diff share (#187): their result and the diff's added lines.

The secret and SAST gates (:mod:`scf_policy.secret_gate`, :mod:`scf_policy.sast_gate`)
judge a change on the service side before it is offered for review. Like
:mod:`scf_policy.static_gate` they take content, not a filesystem: the caller reads
the diff and the files.

A gate that cannot finish says so: ``could_not_verify`` counts as failed (spec 003
FR-020). A finding names the file, line and rule, never the text it matched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field

__all__ = ["GateFinding", "GateResult", "GateStatus", "added_lines", "gate_result"]

GateStatus = Literal["passed", "failed", "could_not_verify"]


class GateFinding(BaseModel):
    """One finding at ``file``:``line`` under ``rule``."""

    file: str
    line: int
    rule: str
    message: str = ""


class GateResult(BaseModel):
    status: GateStatus = "passed"
    findings: list[GateFinding] = Field(default_factory=list)
    #: Matches the repository policy allows: recorded, not failed.
    allowed: list[GateFinding] = Field(default_factory=list)
    #: Why the gate could not verify the change: the builder can fix these.
    reasons: list[str] = Field(default_factory=list)
    #: Why the worker could not verify it (no analyser, no registry): a person fixes these.
    unavailable: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def report(self) -> str:
        """The result as the builder's next attempt and the reviewer read it."""
        why = [*self.unavailable, *self.reasons]
        lines = [self.status, *(f"- could not verify: {r}" for r in why)]
        lines += [f"- {f.file}:{f.line} [{f.rule}] {f.message}".rstrip() for f in self.findings]
        lines += [f"- allowed by policy: {f.file}:{f.line} [{f.rule}]" for f in self.allowed]
        return "\n".join(lines)


def gate_result(
    findings: Iterable[GateFinding],
    *,
    allowed: Iterable[GateFinding] = (),
    reasons: Iterable[str] = (),
    unavailable: Iterable[str] = (),
) -> GateResult:
    """``could_not_verify`` with any reason, else ``failed`` with any finding."""
    found, why, down = list(findings), list(reasons), list(unavailable)
    status: GateStatus = "could_not_verify" if why or down else "failed" if found else "passed"
    return GateResult(
        status=status, findings=found, allowed=list(allowed), reasons=why, unavailable=down
    )


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Each file a unified ``git diff`` leaves in place, with its added lines by number.

    A deleted file is left out; a file the diff only removes lines from maps to none.
    """
    files: dict[str, list[tuple[int, str]]] = {}
    current: list[tuple[int, str]] | None = None
    in_header = False
    number = 0
    for line in diff.split("\n"):  # not splitlines(): a "\r" inside a line is content
        if line.startswith("diff --git "):
            current, in_header = None, True
        elif in_header and line.startswith("+++ "):
            # git ends a name holding a space with a TAB. shortcut: a quoted name keeps git's
            # escapes, so an allowance never matches it; the other gates read -z paths
            target = line[4:].rstrip("\t").strip('"')
            current = None if target == "/dev/null" else files.setdefault(target[2:], [])
        elif hunk := _HUNK.match(line):
            in_header, number = False, int(hunk.group(1))
        elif not in_header and current is not None and line.startswith("+"):
            current.append((number, line[1:]))
            number += 1
        elif not in_header and line.startswith(" "):
            number += 1
    return files
