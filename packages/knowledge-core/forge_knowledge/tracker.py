"""Issues, pull requests and commits as knowledge chunks (spec J §2.3, §2.4.1).

Pure mappers from raw GitHub REST JSON (and :class:`CommitRecord` from
``git log``) to :class:`Chunk` objects, one item per path: ``issues/<n>``,
``pulls/<n>``, ``commits/<sha>``. Every chunk starts with the item's header
line (number, state, dates, merge commit), repeated on each piece when an item
is split, then the body and comments in time order, each with its author's
login and date. Idempotent by content hash like every chunk.

Bodies are untrusted: every body line is quoted (``> ``) and every title,
label and login is folded onto one line, so no body or title can pose as
another entry or header. With a ``cutoff`` an item renders as it stood then.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from forge_contracts.dtos import Chunk
from forge_contracts.enums import ChunkType
from forge_knowledge.chunking import DEFAULT_MAX_CHARS, _pack, weight_for

__all__ = ["CommitRecord", "at_cutoff", "commit_chunks", "issue_chunks", "pull_chunks"]

#: Touched paths listed in a commit chunk before the rest are only counted.
MAX_COMMIT_PATHS = 30

# `Subject (#123)` (squash merge) or `Merge pull request #123 from ...`.
_PR_REF_RE = re.compile(r"\(#(\d+)\)|^Merge pull request #(\d+)\b")

Item = Mapping[str, Any]


@dataclass(frozen=True)
class CommitRecord:
    """One commit as read from local ``git log`` (see ``sync.git_log``)."""

    sha: str
    committed_at: datetime
    subject: str
    body: str
    paths: tuple[str, ...]


def _day(timestamp: str | None) -> str:
    return (timestamp or "")[:10]


def _one_line(text: object) -> str:
    """``text`` with newlines and runs of whitespace folded to one space."""
    return " ".join(str(text or "").split())


def _after(timestamp: str | None, cutoff: datetime | None) -> bool:
    """Whether ``timestamp`` (GitHub ISO 8601) is later than ``cutoff``."""
    return bool(cutoff and timestamp and datetime.fromisoformat(timestamp) > cutoff)


def _labels(item: Item) -> str:
    names = [_one_line(label["name"]) for label in item.get("labels") or []]
    return f" ({', '.join(names)})" if names else ""


def _login(item: Item) -> str:
    return _one_line((item.get("user") or {}).get("login")) or "ghost"


def _quote(text: str) -> str:
    """Every line of an untrusted body behind ``> ``."""
    return "\n".join(f"> {line}" for line in text.strip().splitlines())


def _entry(item: Item, where: str = "") -> str:
    text = _quote(item.get("body") or "")
    head = f"{_login(item)} ({_day(item.get('created_at'))}){where}:"
    return f"{head}\n{text}" if text else head


def _chunks(path: str, header: str, entries: Sequence[str], max_chars: int) -> list[Chunk]:
    """``header`` on every piece; whole entries packed, an over-long one cut on lines."""
    lines: list[str] = []
    units: list[tuple[int, int]] = []
    for entry in entries:
        if lines:
            lines.append("")
        start = len(lines)
        lines.extend(entry.splitlines() or [""])
        units.append((start, len(lines) - 1))
    budget = max(max_chars - len(header) - 1, max_chars // 2)
    out: list[Chunk] = []
    for body, start, end in _pack(lines, units, budget, max_chars) or [("", 0, 0)]:
        content = f"{header}\n{body}".rstrip()
        out.append(
            Chunk(
                content=content,
                chunk_type=ChunkType.MARKDOWN,
                path=path,
                start_line=start + 1,
                end_line=end + 1,
                weight=weight_for(ChunkType.MARKDOWN),
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        )
    return out


def _in_time_order(cutoff: datetime | None, *groups: Iterable[tuple[Item, str]]) -> list[str]:
    """Comments as entries in time order, without those made after ``cutoff``."""
    merged = [pair for group in groups for pair in group]
    merged = [pair for pair in merged if not _after(pair[0].get("created_at"), cutoff)]
    merged.sort(key=lambda pair: pair[0].get("created_at") or "")
    return [_entry(item, where) for item, where in merged]


def issue_chunks(
    issue: Item,
    comments: Sequence[Item],
    *,
    cutoff: datetime | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[Chunk]:
    """``Issue #n [open|closed <date>] title (labels)``, then body and comments.

    With ``cutoff`` the issue renders as it stood then: closed later reads
    as open, and later comments are left out.
    """
    closed_at = issue.get("closed_at")
    if issue.get("state") == "open" or _after(closed_at, cutoff):
        state = "open"
    else:
        state = f"closed {_day(closed_at)}"
    title = _one_line(issue.get("title"))
    header = f"Issue #{issue['number']} [{state}] {title}{_labels(issue)}"
    entries = [_entry(issue), *_in_time_order(cutoff, ((c, "") for c in comments))]
    return _chunks(f"issues/{issue['number']}", header, entries, max_chars)


def pull_chunks(
    pull: Item,
    comments: Sequence[Item],
    review_comments: Sequence[Item],
    *,
    cutoff: datetime | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[Chunk]:
    """``PR #n [merged <date>, merge commit <sha> | closed <date> | open] title (labels)``.

    Then the description, and the conversation and review-thread comments
    merged in time order (a review comment names the file it is on). With
    ``cutoff`` the PR renders as it stood then: merged or closed later reads
    as open, and later comments are left out.
    """
    merged_at, closed_at = pull.get("merged_at"), pull.get("closed_at")
    if merged_at and not _after(merged_at, cutoff):
        sha = _one_line(pull.get("merge_commit_sha"))
        state = f"merged {_day(merged_at)}, merge commit {sha}"
    elif pull.get("state") == "closed" and not merged_at and not _after(closed_at, cutoff):
        state = f"closed {_day(closed_at)}"
    else:
        state = "open"
    title = _one_line(pull.get("title"))
    header = f"PR #{pull['number']} [{state}] {title}{_labels(pull)}"
    entries = [
        _entry(pull),
        *_in_time_order(
            cutoff,
            ((c, "") for c in comments),
            ((c, f" on {_one_line(c['path'])}" if c.get("path") else "") for c in review_comments),
        ),
    ]
    return _chunks(f"pulls/{pull['number']}", header, entries, max_chars)


def commit_chunks(record: CommitRecord, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """``sha date subject (#PR)``, then the message body and the touched paths."""
    match = _PR_REF_RE.search(record.subject) or _PR_REF_RE.search(record.body)
    pr = next((g for g in match.groups() if g), None) if match else None
    ref = f" (#{pr})" if pr and f"#{pr}" not in record.subject else ""
    day = record.committed_at.date().isoformat()
    header = f"{record.sha} {day} {_one_line(record.subject)}{ref}"
    shown = [_one_line(path) for path in record.paths[:MAX_COMMIT_PATHS]]
    if len(record.paths) > MAX_COMMIT_PATHS:
        shown.append(f"... and {len(record.paths) - MAX_COMMIT_PATHS} more paths")
    entries = [e for e in (_quote(record.body), "\n".join(shown)) if e]
    return _chunks(f"commits/{record.sha}", header, entries, max_chars)


def at_cutoff(items: Iterable[Item], cutoff: datetime) -> Iterator[Item]:
    """The items created at or before ``cutoff`` (an aware datetime)."""
    return (item for item in items if datetime.fromisoformat(item["created_at"]) <= cutoff)
