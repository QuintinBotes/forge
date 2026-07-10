"""F40-POL-GOVERNANCE — CODEOWNERS parsing + owner resolution."""

from __future__ import annotations

from forge_approval import parse_codeowners, required_owners_for_paths
from forge_approval.authorizer import required_approvals
from forge_contracts.dtos import ReviewRules

CODEOWNERS = """
# Default owners for everything
*           @org/platform
/docs/      @org/docs
app/api/**  @org/backend
*.tf        @org/infra
"""


def test_default_rule_matches_any_path() -> None:
    rs = parse_codeowners(CODEOWNERS)
    assert rs.owners_for("random/file.py") == ["@org/platform"]


def test_last_matching_rule_wins() -> None:
    rs = parse_codeowners(CODEOWNERS)
    # app/api/** overrides the default * rule.
    assert rs.owners_for("app/api/routes.py") == ["@org/backend"]


def test_directory_subtree_rule() -> None:
    rs = parse_codeowners(CODEOWNERS)
    assert rs.owners_for("docs/guide/intro.md") == ["@org/docs"]


def test_extension_glob_rule() -> None:
    rs = parse_codeowners(CODEOWNERS)
    assert rs.owners_for("infra/main.tf") == ["@org/infra"]


def test_comments_and_blank_lines_ignored() -> None:
    rs = parse_codeowners(CODEOWNERS)
    assert len(rs.rules) == 4


def test_required_owners_union_is_order_preserving() -> None:
    rs = parse_codeowners(CODEOWNERS)
    owners = required_owners_for_paths(rs, ["app/api/x.py", "infra/y.tf", "app/api/z.py"])
    assert owners == ["@org/backend", "@org/infra"]


def test_required_approvals_reads_min_approvals() -> None:
    assert required_approvals(ReviewRules(min_approvals=1)) == 1
    assert required_approvals(ReviewRules(min_approvals=3)) == 3
    assert required_approvals(None) == 1
    # Never below one, even if a policy sets a nonsensical zero.
    assert required_approvals(ReviewRules(min_approvals=0)) == 1
