"""RFC 7644 §3.4.2.2 SCIM filter → SQLAlchemy predicate (F33, pure).

Supports the operator subset every major IdP's provisioning engine emits:
``eq``, ``ne``, ``co``, ``sw``, ``ew``, ``pr`` combined with ``and`` / ``or``
and parentheses, over ``userName``, ``externalId``, ``active`` and
``emails.value``. Anything else raises the SCIM ``invalidFilter`` error.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import ColumnElement, SQLColumnExpression, and_, func, or_

from forge_api.sso.errors import ScimApiError
from forge_db.models import ExternalIdentity, User

_TOKEN_RE = re.compile(
    r"""\s*(?:(?P<lparen>\()|(?P<rparen>\))|(?P<string>"(?:[^"\\]|\\.)*")|"""
    r"""(?P<word>[A-Za-z0-9_.\-]+))"""
)

_OPS = frozenset({"eq", "ne", "co", "sw", "ew", "pr"})


def _invalid(detail: str) -> ScimApiError:
    return ScimApiError(400, detail, scim_type="invalidFilter")


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if match is None:
            if text[pos:].strip():
                raise _invalid(f"unexpected character at position {pos}")
            break
        tokens.append(match.group(match.lastgroup))  # type: ignore[arg-type]
        pos = match.end()
    return tokens


class _Parser:
    """Recursive-descent parser for the supported filter grammar."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._pos = 0

    def _peek(self) -> str | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _next(self) -> str:
        token = self._peek()
        if token is None:
            raise _invalid("unexpected end of filter")
        self._pos += 1
        return token

    def parse(self) -> ColumnElement[bool]:
        predicate = self._or_expr()
        if self._peek() is not None:
            raise _invalid(f"unexpected trailing token {self._peek()!r}")
        return predicate

    def _or_expr(self) -> ColumnElement[bool]:
        parts = [self._and_expr()]
        while (tok := self._peek()) is not None and tok.lower() == "or":
            self._next()
            parts.append(self._and_expr())
        return or_(*parts) if len(parts) > 1 else parts[0]

    def _and_expr(self) -> ColumnElement[bool]:
        parts = [self._factor()]
        while (tok := self._peek()) is not None and tok.lower() == "and":
            self._next()
            parts.append(self._factor())
        return and_(*parts) if len(parts) > 1 else parts[0]

    def _factor(self) -> ColumnElement[bool]:
        if self._peek() == "(":
            self._next()
            inner = self._or_expr()
            if self._next() != ")":
                raise _invalid("missing closing parenthesis")
            return inner
        return self._attr_expr()

    def _attr_expr(self) -> ColumnElement[bool]:
        attr = self._next().lower()
        op = self._next().lower()
        if op not in _OPS:
            raise _invalid(f"unsupported operator {op!r}")
        if op == "pr":
            return self._compare(attr, "pr", None)
        value = self._value(self._next())
        return self._compare(attr, op, value)

    @staticmethod
    def _value(token: str) -> Any:
        if token.startswith('"') and token.endswith('"') and len(token) >= 2:
            return token[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        lowered = token.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        raise _invalid(f"unsupported literal {token!r}")

    @staticmethod
    def _compare(attr: str, op: str, value: Any) -> ColumnElement[bool]:
        if attr in ("username", "emails.value"):
            column: SQLColumnExpression[str] = func.lower(User.email)
            if op == "pr":
                return User.email.is_not(None)
            if not isinstance(value, str):
                raise _invalid(f"{attr} requires a string value")
            needle = value.lower()
            if op == "eq":
                return column == needle
            if op == "ne":
                return column != needle
            if op == "co":
                return column.contains(needle)
            if op == "sw":
                return column.startswith(needle)
            if op == "ew":
                return column.endswith(needle)
        if attr == "externalid":
            column = ExternalIdentity.external_id
            if op == "pr":
                return column.is_not(None)
            if not isinstance(value, str):
                raise _invalid("externalId requires a string value")
            if op == "eq":
                return column == value
            if op == "ne":
                return column != value
            if op == "co":
                return column.contains(value)
            if op == "sw":
                return column.startswith(value)
            if op == "ew":
                return column.endswith(value)
        if attr == "active":
            if op == "pr":
                return User.is_active.is_not(None)
            if op == "eq":
                return User.is_active.is_(bool(value))
            if op == "ne":
                return User.is_active.is_not(bool(value))
            raise _invalid(f"operator {op!r} is not supported for 'active'")
        raise _invalid(f"unsupported filter attribute {attr!r}")


def parse_scim_filter(text: str) -> ColumnElement[bool]:
    """Parse a SCIM filter into a SQLAlchemy predicate (raises ``invalidFilter``)."""
    tokens = _tokenize(text)
    if not tokens:
        raise _invalid("empty filter")
    return _Parser(tokens).parse()


__all__ = ["parse_scim_filter"]
