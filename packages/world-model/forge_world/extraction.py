"""Document extraction: prompt contract, reply parsing, server verification.

Pure module — the model call itself lives in the service. The model proposes
statements in a fixed JSON shape (quote first); the server is authoritative:
a statement survives only if its quote is verbatim in the chunk, its named
things appear in the text, and its time words appear in the quote. Offsets
are always computed here, never taken from the model. Every rejection is a
coded :class:`ExtractionDrop` the service stores as a row.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from forge_contracts import ModelMessage, ModelRequest
from forge_world.ledger import Mood
from forge_world.time import WorldBound, parse_bound, period_end


class DropReason(enum.StrEnum):
    QUOTE_NOT_IN_CHUNK = "quote_not_in_chunk"
    NAME_NOT_IN_TEXT = "name_not_in_text"
    TIME_NOT_IN_QUOTE = "time_not_in_quote"
    MALFORMED_ITEM = "malformed_item"
    TRUNCATED_REPLY = "truncated_reply"
    CHUNK_UNEXTRACTED = "chunk_unextracted"


@dataclass(frozen=True)
class ExtractionDrop:
    """One rejected item (or chunk) with its coded reason."""

    reason: DropReason
    item: dict[str, Any] | None = None


class TimeWords(BaseModel):
    """The ``time`` object of a raw statement, exactly as stated."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    from_text: str | None = Field(default=None, alias="from")
    to_text: str | None = Field(default=None, alias="to")
    words: str | None = None

    @field_validator("from_text", "to_text", mode="before")
    @classmethod
    def _coerce_scalar(cls, value: object) -> object:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(int(value)) if float(value).is_integer() else str(value)
        return value

    @field_validator("words", mode="before")
    @classmethod
    def _join_words(cls, value: object) -> object:
        if isinstance(value, list):
            return " ".join(str(part) for part in value)
        return value


class RawStatement(BaseModel):
    """One statement exactly as the model returned it (pre-verification)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    quote: str
    subject: str
    subject_kind: Literal["named", "described"] = "named"
    phrase: str
    object_name: str | None = Field(default=None, alias="object")
    value: str | None = None
    object_kind: Literal["entity", "value", "described"] | None = None
    time: TimeWords | None = None
    mood: Mood | None = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, value: object) -> object:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(int(value)) if float(value).is_integer() else str(value)
        return value

    @field_validator("quote", "subject", "phrase", "object_name", "value", mode="after")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _resolve_target(self) -> RawStatement:
        if not self.quote or not self.subject or not self.phrase:
            raise ValueError("quote, subject and phrase must be non-empty")
        object_name = self.object_name or None
        value = self.value or None
        if self.object_kind == "value" and value is None and object_name is not None:
            value, object_name = object_name, None
        elif (
            self.object_kind in ("entity", "described")
            and object_name is None
            and value is not None
        ):
            object_name, value = value, None
        if (object_name is None) == (value is None):
            raise ValueError("exactly one of object (entity) and value (literal) is required")
        self.object_name = object_name
        self.value = value
        if self.object_kind != "described":
            self.object_kind = "entity" if object_name is not None else "value"
        return self

    @field_validator("mood", mode="before")
    @classmethod
    def _empty_mood_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


@dataclass(frozen=True)
class VerifiedStatement:
    """A statement that passed server verification, offsets computed."""

    quote: str
    subject: str
    subject_kind: Literal["named", "described"]
    phrase: str
    object_name: str | None
    value: str | None
    valid_from: WorldBound
    valid_to: WorldBound
    mood: Mood | None
    qualifiers: dict[str, Any] = field(default_factory=dict)
    start_offset: int = 0
    end_offset: int = 0
    object_kind: Literal["entity", "value", "described"] | None = None


_SYSTEM_PROMPT = """\
You extract factual statements from one chunk of a document for a knowledge
ledger. Work only from the chunk text given to you. Use the document title,
the opening of the document and the list of already-recorded names only to
resolve who or what a phrase in the chunk refers to; never state anything
those context parts claim unless the chunk itself says it.

Reply with a single JSON object of exactly this shape and nothing else:

{"statements": [{"quote": "...", "subject": "...", "subject_kind": "named",
"phrase": "...", "object": "...", "value": "...", "object_kind": "entity",
"time": {"from": "...", "to": "...", "words": "..."}, "mood": null,
"qualifiers": {}}]}

Field rules:

- "quote" comes first and must be copied character-for-character from the
  chunk: the smallest span that supports the statement. Statements whose
  quote is not verbatim in the chunk are discarded, so never paraphrase.
- "subject" is who or what the statement is about, written as it appears in
  the text. "subject_kind" is "named" for a proper name, or "described" when
  the text only describes the thing (for example "the vendor") without
  naming it.
- "phrase" is the relation in the text's own words (for example "works for"
  or "is headquartered in").
- Give exactly one target: "object" with "object_kind": "entity" when the
  target is a named thing, or "value" with "object_kind": "value" when it
  is a literal such as a place name used as a value, a number, or a date.
  Leave the other field null. When the target is only described in the
  text (for example "the vendor") without being named, give it as
  "object" with "object_kind": "described".
- "time" says when the statement was true. Use "YYYY", "YYYY-MM",
  "YYYY-MM-DD" or a full ISO-8601 instant for "from" and "to", and copy
  into "words" the exact words inside the quote that carry that timing.
  Leave a bound null when the quote does not state it. Relative wording
  such as "last year" goes in "words" only, with both bounds null.
- "mood" is null for plain claims. Use "plan" for intentions, "requirement"
  for obligations, and "condition" for hypotheticals.
- "qualifiers" holds any extra detail that narrows the statement, as an
  object; use {} when there is none.

Extract every distinct statement the chunk supports. If the chunk contains
no factual statements, reply with {"statements": []}.
"""


def build_request(
    *,
    title: str,
    doc_head: str,
    known_names: Sequence[str],
    chunk_text: str,
    model: str = "",
) -> ModelRequest:
    """The extraction call for one chunk (temperature 0, JSON contract).

    Carries the title, the document's first 1,500 characters, names already
    recorded from this document, and the chunk. No ontology and no document
    date go in the prompt.
    """
    names = "\n".join(f"- {name}" for name in known_names) or "(none yet)"
    user = (
        f"Document title: {title}\n\n"
        f"Opening of the document:\n{doc_head[:1500]}\n\n"
        f"Names already recorded from this document:\n{names}\n\n"
        f"Chunk to extract from:\n{chunk_text}"
    )
    return ModelRequest(
        model=model,
        system=_SYSTEM_PROMPT,
        messages=[ModelMessage(role="user", content=user)],
        temperature=0.0,
    )


def _strip_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _raw_item_dict(item: RawStatement) -> dict[str, Any]:
    return item.model_dump(mode="json", by_alias=True)


def _validate_items(
    raw_items: Sequence[object],
) -> tuple[list[RawStatement], list[ExtractionDrop]]:
    accepted: list[RawStatement] = []
    drops: list[ExtractionDrop] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            drops.append(ExtractionDrop(DropReason.MALFORMED_ITEM, {"item": repr(raw)[:512]}))
            continue
        try:
            accepted.append(RawStatement.model_validate(raw))
        except ValidationError:
            drops.append(ExtractionDrop(DropReason.MALFORMED_ITEM, dict(raw)))
    return accepted, drops


def _salvage_objects(text: str) -> list[object]:
    """Recover complete JSON objects from a truncated statements reply."""
    start = text.find("[")
    decoder = json.JSONDecoder()
    objects: list[object] = []
    pos = start if start >= 0 else 0
    while pos < len(text):
        char = text[pos]
        if char in " \t\r\n,[]}":
            pos += 1
            continue
        if char != "{":
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            break
        objects.append(obj)
        pos = end
    return objects


def parse_reply(content: str) -> tuple[list[RawStatement], list[ExtractionDrop]]:
    """Parse a model reply into raw statements plus coded parse drops.

    Truncated replies are salvaged item by item; a reply that is not JSON at
    all drops the chunk as ``truncated_reply`` (it began like JSON) or
    ``malformed_item``.
    """
    text = _strip_fences(content)
    if not text:
        return [], [ExtractionDrop(DropReason.TRUNCATED_REPLY, {"text": ""})]
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        salvaged = _salvage_objects(text)
        if salvaged:
            items, drops = _validate_items(salvaged)
            drops.append(ExtractionDrop(DropReason.TRUNCATED_REPLY, {"text": text[-512:]}))
            return items, drops
        reason = (
            DropReason.TRUNCATED_REPLY if text.startswith(("{", "[")) else DropReason.MALFORMED_ITEM
        )
        return [], [ExtractionDrop(reason, {"text": text[:512]})]

    if isinstance(parsed, dict) and isinstance(parsed.get("statements"), list):
        return _validate_items(parsed["statements"])
    if isinstance(parsed, list):
        return _validate_items(parsed)
    return [], [ExtractionDrop(DropReason.MALFORMED_ITEM, {"text": text[:512]})]


def _parse_stated_bound(text: str | None, *, is_end: bool, words: str | None = None) -> WorldBound:
    if text is None:
        return WorldBound.unknown()
    try:
        bound = parse_bound(text, is_end=is_end)
    except ValueError:
        # Relative words ("last year") stay unknown until a later pass can
        # resolve them against the document's own date.
        return WorldBound.unknown()
    # The stated bound must be the date the quote's own words carry: its
    # year (from the text, since end bounds shift to the next period)
    # must appear in ``words``. Year only, so "2020-03" satisfies the
    # words "In March 2020".
    if words and text.strip()[:4] not in words:
        return WorldBound.unknown()
    return bound


def verify(
    chunk_text: str,
    doc_head: str,
    items: Sequence[RawStatement],
    known_names: Sequence[str] = (),
) -> tuple[list[VerifiedStatement], list[ExtractionDrop]]:
    """Check each raw statement against the chunk; keep only verbatim ones.

    ``known_names`` are names already recorded from earlier chunks of the
    same document (the prompt tells the model to resolve references to
    them), so they count as present in the text for the name check.
    """
    haystack = "\n".join([doc_head, chunk_text, *known_names]).casefold()
    accepted: list[VerifiedStatement] = []
    drops: list[ExtractionDrop] = []

    for item in items:
        raw = _raw_item_dict(item)
        start = chunk_text.find(item.quote)
        if start < 0:
            drops.append(ExtractionDrop(DropReason.QUOTE_NOT_IN_CHUNK, raw))
            continue
        if item.subject_kind == "named" and item.subject.casefold() not in haystack:
            drops.append(ExtractionDrop(DropReason.NAME_NOT_IN_TEXT, raw))
            continue
        if item.object_name is not None and item.object_name.casefold() not in haystack:
            drops.append(ExtractionDrop(DropReason.NAME_NOT_IN_TEXT, raw))
            continue
        time = item.time
        checks: list[str] = []
        if time is not None:
            if time.words:
                checks.append(time.words)
            else:
                checks.extend(t for t in (time.from_text, time.to_text) if t)
        if any(words.casefold() not in item.quote.casefold() for words in checks):
            drops.append(ExtractionDrop(DropReason.TIME_NOT_IN_QUOTE, raw))
            continue
        words = time.words if time else None
        from_bound = _parse_stated_bound(
            time.from_text if time else None, is_end=False, words=words
        )
        to_bound = _parse_stated_bound(time.to_text if time else None, is_end=True, words=words)
        if (
            from_bound.instant is not None
            and to_bound.instant is not None
            and (
                from_bound.instant >= to_bound.instant
                or (
                    # Equal stated ends: the span would be exactly the
                    # start's own period, not a real stated interval.
                    to_bound.end_after_period
                    and to_bound.precision == from_bound.precision
                    and to_bound.instant == period_end(from_bound.instant, from_bound.precision)
                )
            )
        ):
            drops.append(ExtractionDrop(DropReason.MALFORMED_ITEM, raw))
            continue
        accepted.append(
            VerifiedStatement(
                quote=item.quote,
                subject=item.subject,
                subject_kind=item.subject_kind,
                phrase=item.phrase,
                object_name=item.object_name,
                value=item.value,
                valid_from=from_bound,
                valid_to=to_bound,
                mood=item.mood,
                qualifiers=dict(item.qualifiers),
                start_offset=start,
                end_offset=start + len(item.quote),
                object_kind=item.object_kind,
            )
        )
    return accepted, drops


__all__ = [
    "DropReason",
    "ExtractionDrop",
    "RawStatement",
    "TimeWords",
    "VerifiedStatement",
    "build_request",
    "parse_reply",
    "verify",
]
