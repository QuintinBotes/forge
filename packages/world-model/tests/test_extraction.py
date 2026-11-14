"""Pure tests for extraction: prompt, reply parsing, verification (§3.7)."""

from __future__ import annotations

import json

from forge_world import DropReason, Precision, build_request, parse_reply, verify
from forge_world.extraction import RawStatement

CHUNK = "Acme moved to Berlin in 2020. The vendor supplies Acme with widgets."
HEAD = "Annual report. Acme is a widget company."


def _item(**overrides: object) -> RawStatement:
    payload: dict[str, object] = {
        "quote": "Acme moved to Berlin in 2020",
        "subject": "Acme",
        "subject_kind": "named",
        "phrase": "moved to",
        "object": None,
        "value": "Berlin",
        "object_kind": "value",
        "time": {"from": "2020", "to": None, "words": "in 2020"},
        "mood": None,
        "qualifiers": {},
    }
    payload.update(overrides)
    return RawStatement.model_validate(payload)


def test_build_request_contract() -> None:
    request = build_request(
        title="Annual report",
        doc_head=HEAD,
        known_names=["Acme"],
        chunk_text=CHUNK,
    )
    assert request.temperature == 0.0
    prompt = (request.system or "") + request.messages[0].content
    assert "Annual report" in prompt
    assert HEAD in prompt
    assert "Acme" in prompt
    assert CHUNK in prompt
    assert '"statements"' in prompt  # the JSON reply contract


def test_verify_accepts_verbatim_and_computes_offsets() -> None:
    accepted, drops = verify(CHUNK, HEAD, [_item()])
    assert drops == []
    assert len(accepted) == 1
    item = accepted[0]
    assert item.start_offset == CHUNK.index(item.quote)
    assert CHUNK[item.start_offset : item.end_offset] == item.quote
    assert item.valid_from.precision is Precision.YEAR
    assert item.valid_from.instant is not None
    assert item.valid_from.instant.year == 2020


def test_verify_quote_not_in_chunk() -> None:
    accepted, drops = verify(CHUNK, HEAD, [_item(quote="Acme moved to Paris")])
    assert accepted == []
    assert [drop.reason for drop in drops] == [DropReason.QUOTE_NOT_IN_CHUNK]


def test_verify_name_not_in_text() -> None:
    accepted, drops = verify(CHUNK, HEAD, [_item(subject="Globex")])
    assert accepted == []
    assert drops[0].reason is DropReason.NAME_NOT_IN_TEXT

    accepted, drops = verify(
        CHUNK,
        HEAD,
        [_item(object="Globex", value=None, object_kind="entity")],
    )
    assert accepted == []
    assert drops[0].reason is DropReason.NAME_NOT_IN_TEXT


def test_verify_name_may_come_from_doc_head() -> None:
    chunk = "It moved to Berlin in 2020."
    item = _item(quote="moved to Berlin in 2020")
    accepted, drops = verify(chunk, HEAD, [item])
    assert drops == []  # "Acme" is in the document head
    assert len(accepted) == 1


def test_verify_described_subject_skips_name_check() -> None:
    item = _item(
        quote="The vendor supplies Acme with widgets",
        subject="the vendor",
        subject_kind="described",
        phrase="supplies",
        object="Acme",
        value=None,
        object_kind="entity",
        time=None,
    )
    accepted, drops = verify(CHUNK, HEAD, [item])
    assert drops == []
    assert accepted[0].subject_kind == "described"


def test_verify_time_not_in_quote() -> None:
    item = _item(time={"from": "2019", "to": None, "words": "in 2019"})
    accepted, drops = verify(CHUNK, HEAD, [item])
    assert accepted == []
    assert drops[0].reason is DropReason.TIME_NOT_IN_QUOTE


def test_verify_relative_time_stays_unknown() -> None:
    item = _item(time={"from": "last year", "to": None, "words": "in 2020"})
    accepted, drops = verify(CHUNK, HEAD, [item])
    assert drops == []
    assert accepted[0].valid_from.is_unknown


def test_verify_stated_year_must_appear_in_time_words() -> None:
    # M2 case 1: the words carry no year, so the model's "2025" is
    # invented and the bound falls back to unknown (not dropped).
    chunk = "Last year Alice joined Acme."
    item = _item(
        quote="Last year Alice joined Acme",
        subject="Alice",
        phrase="joined",
        object="Acme",
        value=None,
        object_kind="entity",
        time={"from": "2025", "to": None, "words": "Last year"},
    )
    accepted, drops = verify(chunk, HEAD, [item])
    assert drops == []
    assert accepted[0].valid_from.is_unknown


def test_verify_stated_bounds_contradicting_words_become_unknown() -> None:
    # M2 case 2: the quote says 2020; bounds 1990-1991 are invented.
    accepted, drops = verify(
        CHUNK,
        HEAD,
        [_item(time={"from": "1990", "to": "1991", "words": "2020"})],
    )
    assert drops == []
    assert accepted[0].valid_from.is_unknown
    assert accepted[0].valid_to.is_unknown


def test_verify_month_bound_satisfied_by_year_in_words() -> None:
    chunk = "In March 2020 Alice joined Acme."
    item = _item(
        quote="In March 2020 Alice joined Acme",
        subject="Alice",
        phrase="joined",
        object="Acme",
        value=None,
        object_kind="entity",
        time={"from": "2020-03", "to": None, "words": "In March 2020"},
    )
    accepted, drops = verify(chunk, HEAD, [item])
    assert drops == []
    assert accepted[0].valid_from.precision is Precision.MONTH
    assert accepted[0].valid_from.instant is not None
    assert (accepted[0].valid_from.instant.year, accepted[0].valid_from.instant.month) == (
        2020,
        3,
    )


def test_verify_equal_stated_ends_are_malformed() -> None:
    item = _item(time={"from": "2020", "to": "2020", "words": "in 2020"})
    accepted, drops = verify(CHUNK, HEAD, [item])
    assert accepted == []
    assert drops[0].reason is DropReason.MALFORMED_ITEM


def test_verify_known_names_from_earlier_chunks_count_as_present() -> None:
    # The prompt gives the model already-recorded names to resolve
    # references; verify must accept them too (minor 12).
    chunk = "It moved to Berlin in 2020."
    item = _item(quote="moved to Berlin in 2020", subject="Globex")
    accepted, drops = verify(chunk, HEAD, [item])
    assert accepted == []
    assert drops[0].reason is DropReason.NAME_NOT_IN_TEXT
    accepted, drops = verify(chunk, HEAD, [item], known_names=["Globex"])
    assert drops == []
    assert len(accepted) == 1


def test_verify_described_object_kind_passes_through() -> None:
    item = _item(
        quote="Acme moved to Berlin in 2020",
        object="the vendor",
        value=None,
        object_kind="described",
    )
    accepted, drops = verify(CHUNK, HEAD, [item])
    assert drops == []
    assert accepted[0].object_kind == "described"
    assert accepted[0].object_name == "the vendor"


def test_parse_reply_valid_and_fenced() -> None:
    body = {"statements": [_item().model_dump(mode="json", by_alias=True)]}
    items, drops = parse_reply(json.dumps(body))
    assert len(items) == 1 and drops == []
    items, drops = parse_reply(f"```json\n{json.dumps(body)}\n```")
    assert len(items) == 1 and drops == []


def test_parse_reply_malformed_item() -> None:
    items, drops = parse_reply('{"statements": [{"quote": "x"}]}')
    assert items == []
    assert [drop.reason for drop in drops] == [DropReason.MALFORMED_ITEM]


def test_parse_reply_truncated_is_salvaged() -> None:
    good = _item().model_dump(mode="json", by_alias=True)
    text = (
        '{"statements": ['
        + json.dumps(good)
        + ', {"quote": "Acme moved to Berlin in 2020", "subject": "Ac'
    )
    items, drops = parse_reply(text)
    assert len(items) == 1
    assert [drop.reason for drop in drops] == [DropReason.TRUNCATED_REPLY]


def test_parse_reply_not_json_at_all() -> None:
    items, drops = parse_reply("I could not find any statements.")
    assert items == []
    assert drops[0].reason is DropReason.MALFORMED_ITEM

    items, drops = parse_reply('{"statements": [{"quote": "cut off')
    assert items == []
    assert drops[0].reason is DropReason.TRUNCATED_REPLY


def test_parse_reply_wrong_shape() -> None:
    items, drops = parse_reply('{"facts": []}')
    assert items == []
    assert drops[0].reason is DropReason.MALFORMED_ITEM
