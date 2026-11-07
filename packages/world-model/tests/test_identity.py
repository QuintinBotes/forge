"""Pure tests for identity recall channel 1 (spec §3.5)."""

from __future__ import annotations

import pytest

from forge_world import ClassDef, names_match, strip_suffixes, types_compatible
from forge_world.identity import normalise_name

CLASSES = {
    "thing": ClassDef(key="thing", label="Thing"),
    "org": ClassDef(key="org", label="Organisation", parent_key="thing"),
    "company": ClassDef(key="company", label="Company", parent_key="org"),
    "person": ClassDef(key="person", label="Person", parent_key="thing"),
    "project": ClassDef(key="project", label="Project", disjoint_with=("person",)),
    "software_project": ClassDef(
        key="software_project", label="Software project", parent_key="project"
    ),
}


def test_normalise_name() -> None:
    assert normalise_name("  Acme   Corp. ") == "acme corp"
    assert normalise_name("ACME") == "acme"


@pytest.mark.parametrize(
    "suffix",
    ["Inc", "Ltd", "LLC", "GmbH", "Corp", "Corporation", "Company", "Co", "plc", "AG", "SA", "BV"],
)
def test_strip_suffixes(suffix: str) -> None:
    assert strip_suffixes(f"Acme {suffix}") == "acme"
    assert strip_suffixes(f"Acme, {suffix}.") == "acme"


def test_strip_suffixes_repeated_and_sole_token() -> None:
    assert strip_suffixes("Acme Corp Inc") == "acme"
    assert strip_suffixes("INC") == "inc"  # a lone suffix token is kept


def test_names_match() -> None:
    assert names_match("Acme", "acme")
    assert names_match("Acme Inc", "ACME")
    assert names_match("Acme", "Acme GmbH")
    assert not names_match("Acme", "Acme Widgets")
    assert not names_match("", "Acme")
    assert not names_match("Acme", "")


def test_types_compatible() -> None:
    assert types_compatible(None, "org", CLASSES)
    assert types_compatible("org", None, CLASSES)
    assert types_compatible("org", "org", CLASSES)
    assert types_compatible("company", "org", CLASSES)  # descendant
    assert types_compatible("org", "company", CLASSES)  # ancestor
    assert types_compatible("company", "thing", CLASSES)  # grand-ancestor
    assert not types_compatible("org", "person", CLASSES)  # unrelated
    assert not types_compatible("person", "project", CLASSES)  # disjoint
    assert not types_compatible("person", "software_project", CLASSES)  # via ancestor
