"""Fixtures for the Helm chart static test suite (F24).

The heavy lifting (helm/kubeconform shell-outs, profile rendering) lives in
``helm_chart_lib`` so it can be imported directly by the test modules. This file
exposes the rendered profiles as convenience fixtures.
"""

from __future__ import annotations

import pytest
from helm_chart_lib import render_docs


@pytest.fixture(scope="session")
def rendered_default() -> list[dict]:
    return render_docs("default")


@pytest.fixture(scope="session")
def rendered_production() -> list[dict]:
    return render_docs("production")
