"""Unit tests for the RFC 7644 filter → SQLAlchemy predicate parser."""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import sqlite

from forge_api.sso.errors import ScimApiError
from forge_api.sso.scim_filter import parse_scim_filter


def _sql(text: str) -> str:
    predicate = parse_scim_filter(text)
    return str(
        predicate.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
    )


class TestOperators:
    def test_eq_is_case_insensitive_on_username(self):
        sql = _sql('userName eq "Eve@Acme.com"')
        assert "lower(app_user.email)" in sql
        assert "eve@acme.com" in sql

    def test_ne(self):
        assert "!=" in _sql('userName ne "x@y.z"')

    def test_co_sw_ew(self):
        assert "LIKE '%' || 'acme' || '%'" in _sql('userName co "acme"')
        assert "LIKE 'eve' || '%'" in _sql('userName sw "Eve"')
        assert "LIKE '%' || '.com'" in _sql('userName ew ".com"')

    def test_pr(self):
        assert "IS NOT NULL" in _sql("userName pr")

    def test_external_id_eq_case_exact(self):
        sql = _sql('externalId eq "okta|123"')
        assert "external_identity.external_id = 'okta|123'" in sql

    def test_active_eq_boolean(self):
        assert "app_user.is_active IS 1" in _sql("active eq true")
        assert "app_user.is_active IS 0" in _sql("active eq false")

    def test_emails_value(self):
        assert "lower(app_user.email)" in _sql('emails.value eq "e@a.com"')


class TestBooleanGrammar:
    def test_and_or_precedence(self):
        sql = _sql('userName sw "e" or userName sw "f" and active eq true')
        # 'and' binds tighter than 'or'.
        assert " OR " in sql and " AND " in sql
        assert sql.index(" OR ") < sql.index(" AND ")

    def test_parentheses(self):
        sql = _sql('(userName sw "e" or userName sw "f") and active eq true')
        assert sql.strip().startswith("(")


class TestInvalidFilters:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "userName",
            'userName gt "x"',
            'title eq "boss"',
            'userName eq "unterminated',
            '(userName eq "x"',
            'userName eq "x") or',
            "active eq maybe",
        ],
    )
    def test_invalid_filter_raises_scim_error(self, bad: str):
        with pytest.raises(ScimApiError) as err:
            parse_scim_filter(bad)
        assert err.value.status == 400
        assert err.value.scim_type == "invalidFilter"
