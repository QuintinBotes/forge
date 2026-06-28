"""JSON-Schema validation of the shipped values files (AC3).

``helm install`` enforces ``values.schema.json`` automatically; this mirrors that
check in pytest so a broken schema/values pair is caught even without helm. The
example/production profiles are partial overrides, so each is deep-merged onto the
base ``values.yaml`` (exactly as Helm coalesces them) before validation.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")
yaml = pytest.importorskip("yaml")

CHART_DIR = Path(__file__).resolve().parents[1] / "forge"
SCHEMA_PATH = CHART_DIR / "values.schema.json"
BASE_VALUES = CHART_DIR / "values.yaml"
EXAMPLE_VALUES = CHART_DIR / "values.example.yaml"
PRODUCTION_VALUES = CHART_DIR / "values-production.yaml"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _deep_merge(base: dict, override: dict) -> dict:
    """Coalesce ``override`` onto ``base`` the way Helm merges values files."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _merged(profile: Path) -> dict:
    return _deep_merge(_load_yaml(BASE_VALUES), _load_yaml(profile))


def test_schema_file_is_valid_json_schema() -> None:
    jsonschema.Draft7Validator.check_schema(_schema())


@pytest.mark.parametrize(
    "profile",
    [BASE_VALUES, EXAMPLE_VALUES, PRODUCTION_VALUES],
    ids=["default", "example", "production"],
)
def test_schema_accepts_shipped_profiles(profile: Path) -> None:
    values = _load_yaml(profile) if profile is BASE_VALUES else _merged(profile)
    jsonschema.validate(instance=values, schema=_schema())


def test_schema_rejects_missing_domain() -> None:
    values = _load_yaml(BASE_VALUES)
    del values["forge"]["domain"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=values, schema=_schema())


def test_schema_rejects_bad_digest() -> None:
    values = _load_yaml(BASE_VALUES)
    values["api"]["image"]["digest"] = "not-a-sha256-digest"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=values, schema=_schema())


def test_schema_rejects_bad_loglevel() -> None:
    values = _load_yaml(BASE_VALUES)
    values["forge"]["logLevel"] = "verbose"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=values, schema=_schema())
