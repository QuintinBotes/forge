"""F22 — merge-plan builder + cross-repo dependency cycle detection (AC 14)."""

from __future__ import annotations

import pytest

from forge_contracts import RepoTarget
from forge_workflow.multi_repo import (
    CyclicRepoDependencyError,
    MergePlanBuilder,
    MultipleOrNoPrimaryError,
    UnknownDependencyRepoError,
)


def _t(repo: str, *, role: str = "secondary", depends_on: list[str] | None = None) -> RepoTarget:
    return RepoTarget(repo=repo, role=role, depends_on=depends_on or [])


def test_topo_order_simple() -> None:
    """web depends_on api => order [api, web]."""
    plan = MergePlanBuilder.build(
        [
            _t("api", role="primary"),
            _t("web", depends_on=["api"]),
        ]
    )
    assert plan.primary_repo_id == "api"
    assert plan.merge_order == ["api", "web"]
    assert plan.edges == {"api": [], "web": ["api"]}


def test_single_repo_is_primary_regardless_of_role() -> None:
    """V1 single-repo: the lone target is the primary even if role=secondary."""
    plan = MergePlanBuilder.build([_t("only")])
    assert plan.primary_repo_id == "only"
    assert plan.merge_order == ["only"]


def test_primary_first_among_equals() -> None:
    """Independent repos: primary sorts first, others by input order."""
    plan = MergePlanBuilder.build(
        [
            _t("web"),
            _t("api", role="primary"),
            _t("proto"),
        ]
    )
    assert plan.merge_order[0] == "api"
    assert set(plan.merge_order) == {"api", "web", "proto"}


def test_diamond_topo_order() -> None:
    """proto <- api, proto <- web, api/web <- app ; proto first, app last."""
    plan = MergePlanBuilder.build(
        [
            _t("app", role="primary", depends_on=["api", "web"]),
            _t("api", depends_on=["proto"]),
            _t("web", depends_on=["proto"]),
            _t("proto"),
        ]
    )
    order = plan.merge_order
    assert order.index("proto") < order.index("api")
    assert order.index("proto") < order.index("web")
    assert order.index("api") < order.index("app")
    assert order.index("web") < order.index("app")


def test_cycle_detected() -> None:
    with pytest.raises(CyclicRepoDependencyError) as exc:
        MergePlanBuilder.build(
            [
                _t("api", role="primary", depends_on=["web"]),
                _t("web", depends_on=["api"]),
            ]
        )
    assert "api" in exc.value.cycle
    assert "web" in exc.value.cycle


def test_requires_exactly_one_primary_none() -> None:
    with pytest.raises(MultipleOrNoPrimaryError):
        MergePlanBuilder.build([_t("api"), _t("web")])


def test_requires_exactly_one_primary_two() -> None:
    with pytest.raises(MultipleOrNoPrimaryError):
        MergePlanBuilder.build([_t("api", role="primary"), _t("web", role="primary")])


def test_unknown_dependency_repo() -> None:
    with pytest.raises(UnknownDependencyRepoError) as exc:
        MergePlanBuilder.build(
            [
                _t("api", role="primary"),
                _t("web", depends_on=["ghost"]),
            ]
        )
    assert exc.value.repo == "web"
    assert exc.value.missing == "ghost"


def test_self_dependency_is_dropped() -> None:
    plan = MergePlanBuilder.build([_t("api", role="primary", depends_on=["api"])])
    assert plan.edges["api"] == []
