"""Tests for ``forge_knowledge.text.strip_uri_credentials``: no token leaves in a uri."""

from __future__ import annotations

import pytest

from forge_knowledge.text import strip_uri_credentials


@pytest.mark.parametrize(
    ("uri", "shown"),
    [
        ("oauth2:glpat-x@gitlab.test/o/r", "gitlab.test/o/r"),
        ("x-access-token:ghp_x@github.test/o/r", "github.test/o/r"),
        ("https://u:p@host/o/r", "https://host/o/r"),
        ("https://u:p@ss@host/o/r", "https://host/o/r"),  # an unescaped @ in the password
        ("https://user@host", "https://host"),
        ("git@github.test:o/r.git", "github.test:o/r.git"),
        ("https://github.test/o/r?access_token=t#frag", "https://github.test/o/r"),
        ("https://host/o/r?next=a@b", "https://host/o/r"),
        # Nothing to strip: an @ after the host is part of the path.
        ("github.test/org/api", "github.test/org/api"),
        ("https://registry.test/@scope/pkg", "https://registry.test/@scope/pkg"),
        ("docs/@team/handbook", "docs/@team/handbook"),
        ("mcp://notion", "mcp://notion"),
        ("", ""),
        # Shapes that leaked when the query was cut before the userinfo.
        (" https://u:SEKRET@host/o/a", "https://host/o/a"),
        ("//u:SEKRET@host/o/a", "//host/o/a"),
        ("https://u:SEK?RET@host/o/a", "https://host/o/a"),
        ("https://u:SEK#RET@host/o/a", "https://host/o/a"),
        ("ssh://git@github.test/o/a.git", "ssh://github.test/o/a.git"),
    ],
)
def test_strip_uri_credentials(uri: str, shown: str) -> None:
    assert strip_uri_credentials(uri) == shown
