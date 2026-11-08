"""The ``workflow_template`` installer (spec K §4.4): one refusal per validation step."""

from __future__ import annotations

import base64
import traceback
from pathlib import Path
from typing import Any

import pytest
import yaml

from forge_marketplace.errors import SchemaInvalid
from forge_marketplace.installer import (
    INSTALLERS,
    RESERVED_LINE_NAMES,
    WorkflowTemplateInstaller,
    default_secret_like,
)
from forge_marketplace.models import ArtifactKind
from forge_marketplace.packaging import build_package

LOOP = (
    Path(__file__).parents[2] / "contracts" / "tests" / "fixtures" / "implementation_loop.yaml"
).read_text()


_GHP = "ghp_" + "a1B2" * 9
_AKIA = "AKIAABCDEFGHIJKLMNOP"
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlMTIz"


def _artifact(dsl_yaml: str = LOOP, **overrides: Any) -> dict[str, Any]:
    return {
        "name": "implementation_loop",
        "title": "Implementation loop",
        "dsl_yaml": dsl_yaml,
        **overrides,
    }


def _refusal(artifact: dict[str, Any]) -> str:
    with pytest.raises(SchemaInvalid) as info:
        WorkflowTemplateInstaller().validate(artifact)
    return str(info.value)


def test_registered_for_workflow_template() -> None:
    installer = INSTALLERS[ArtifactKind.workflow_template]
    assert isinstance(installer, WorkflowTemplateInstaller)
    assert installer.target_kind == "workflow_definition"


def test_implementation_loop_validates_with_summary_and_followups() -> None:
    result = WorkflowTemplateInstaller().validate(_artifact())

    assert result.target_kind == "workflow_definition"
    assert result.resolved_config == {
        "name": "implementation_loop",
        "title": "Implementation loop",
        "description": None,
        "dsl_yaml": LOOP,  # verbatim
        "summary": {
            "agents": ["assess", "define_done", "implement"],
            "services": [
                "forge.prepare_worktree",
                "forge.tracker_comment",
                "forge.confirm_red",
                "forge.open_pr",
                "forge.run_checks",
                "forge.push_branch",
                "forge.await_ci",
                "forge.mark_pr_ready",
            ],
            "human_gates": [],
            "budget": {"max_tokens": 3000000, "max_usd": "60"},
        },
    }
    assert result.requires_admin_followup == [
        "supply the 'repo_host' service credential",
        "supply the 'tracker' service credential",
        "provide skill profile 'implementer' in the workspace",
        "requires the temporal engine",
        "bind services on the worker",
    ]
    assert result.warnings == []
    assert result.overrides_builtin is False


def test_summary_lists_human_gates() -> None:
    doc = yaml.safe_load(LOOP)
    doc["stations"]["review"] = {"kind": "human", "gate": "pr", "sla_seconds": 86400}
    for t in doc["transitions"]:
        if t["from"] == "mark_ready":
            t["to"] = "review"
    doc["transitions"].append({"from": "review", "to": "ready_for_review", "when": "approved"})

    result = WorkflowTemplateInstaller().validate(_artifact(yaml.safe_dump(doc)))
    assert result.resolved_config["summary"]["human_gates"] == ["pr"]


# --- step 1: the strict artifact model -------------------------------------- #


def test_refuses_an_extra_key() -> None:
    assert _refusal(_artifact(services=["forge.open_pr"])).startswith("artifact_invalid:")


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "Impl-Loop"},  # start's name rule
        {"name": "a" * 80},  # workflow_definition.name is String(64)
        {"name": ""},
        {"title": ""},
        {"title": "T" * 161},  # workflow_definition.title is String(160)
        {"description": "d" * 4097},
    ],
    ids=["name_case", "name_long", "name_empty", "title_empty", "title_long", "description_long"],
)
def test_refuses_fields_the_definition_row_cannot_hold(overrides: dict[str, str]) -> None:
    name = overrides.get("name", "implementation_loop")
    dsl = LOOP.replace("workflow: implementation_loop", f"workflow: {name}")
    assert _refusal(_artifact(dsl, **overrides)).startswith("artifact_invalid:")


def test_refuses_dsl_yaml_over_128_kib_in_utf8_bytes() -> None:
    # 65,537 characters but 131,074 bytes: the cap counts UTF-8 bytes.
    with pytest.raises(SchemaInvalid) as info:
        WorkflowTemplateInstaller().validate(_artifact(LOOP + "# " + "é" * 65537))
    assert str(info.value).startswith("artifact_invalid:")
    # The oversized input is not echoed, not even by a chained traceback.
    assert "é" * 10 not in "".join(traceback.format_exception(info.value))


# --- step 2: secrets --------------------------------------------------------- #


def test_refuses_a_secret_in_dsl_yaml() -> None:
    token = "ghp_" + "a1B2" * 9
    dsl = LOOP.replace("Cite files and tests.", f"Use {token}.")
    assert _refusal(_artifact(dsl)) == "secret_like_content"


def test_refuses_a_secret_named_param_key() -> None:
    # Without the key scan this would be station_params_invalid from validate_line.
    dsl = LOOP.replace("params: {draft: true}", "params: {draft: true, api_token: x}")
    assert _refusal(_artifact(dsl)) == "secret_like_content"


def test_uses_the_injected_detector() -> None:
    installer = WorkflowTemplateInstaller(secret_like=lambda text: "prepare_worktree" in text)
    with pytest.raises(SchemaInvalid, match=r"^secret_like_content$"):
        installer.validate(_artifact())


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "ghp_" + "x" * 36,
        "gho_" + "x" * 36,
        "github_pat_" + "x" * 30,
        "AKIAABCDEFGHIJKLMNOP",
        "xoxb-1234567890-abcdef",
        "sk-" + "a" * 24,
        _JWT,
    ],
)
def test_default_detector_flags(text: str) -> None:
    assert default_secret_like(f"objective: use {text} here") is True


def test_default_detector_passes_line_text() -> None:
    assert default_secret_like(LOOP) is False
    assert default_secret_like("task-key max_tokens sk-short ghp_short") is False


def test_ordinary_words_are_not_secrets() -> None:
    """The detector matches token shapes, not words like "basic" or "bearer"."""
    dsl = LOOP.replace("Cite files and tests.", "Cite files and tests. Write basic tests.")
    dsl = dsl.replace("No production code.", "No production code. Bearer of bad news.")
    assert "Write basic tests." in dsl and "Bearer of bad news." in dsl
    WorkflowTemplateInstaller().validate(_artifact(dsl, description="Bearer of bad news"))


@pytest.mark.parametrize(
    "escaped",
    [  # YAML double-quote escapes hide each token from the raw-text scan
        "gh\\x70_" + "a1B2" * 9,
        "\\x41KIAABCDEFGHIJKLMNOP",
        "\\x65yJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlMTIz",
    ],
    ids=["ghp", "akia", "jwt"],
)
@pytest.mark.parametrize("where", ["objective", "mapping_key"])
def test_refuses_a_secret_in_any_decoded_string(escaped: str, where: str) -> None:
    if where == "objective":
        dsl = LOOP.replace('Cite files and tests."', f'Cite files and tests. Use {escaped}."')
    else:
        dsl = LOOP + f'extra: {{"{escaped}": 1}}\n'
    assert dsl != LOOP
    assert not default_secret_like(dsl)  # only the decoded scan can see it
    assert _refusal(_artifact(dsl)) == "secret_like_content"


def test_refuses_a_secret_in_a_binary_scalar() -> None:
    dsl = LOOP + f"extra: !!binary {base64.b64encode(_GHP.encode()).decode()}\n"
    assert not default_secret_like(dsl)
    assert _refusal(_artifact(dsl)) == "secret_like_content"


@pytest.mark.parametrize("secret", [_GHP, _AKIA, _JWT])
@pytest.mark.parametrize("field", ["title", "description"])
def test_refuses_a_secret_in_title_or_description(secret: str, field: str) -> None:
    assert _refusal(_artifact(**{field: f"Loop {secret}"})) == "secret_like_content"


# --- step 3: a DSL v2 line named like the artifact --------------------------- #


def test_refuses_bad_yaml() -> None:
    assert _refusal(_artifact("workflow: [unclosed")).startswith("invalid_yaml:")


def test_refuses_a_non_mapping() -> None:
    assert _refusal(_artifact("- a\n- b\n")).startswith("not_a_line:")


def test_refuses_a_v1_document() -> None:
    v1 = "workflow: implementation_loop\nversion: '1'\ntransitions: []\n"
    message = _refusal(_artifact(v1))
    assert message.startswith("not_a_line:")
    assert "dsl_version must be '2'" in message


def test_refuses_a_line_that_does_not_parse() -> None:
    dsl = LOOP.replace("concurrency:", "retry_policy: {}\nconcurrency:")
    assert _refusal(_artifact(dsl)).startswith("line_parse_error:")


@pytest.mark.parametrize("field", ["budget", "dsl_version"])
def test_alias_bomb_refusal_never_prints_the_expansion(field: str) -> None:
    # 9**6 leaves when printed; loading and validating share the references.
    bomb = ['l0: &l0 ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]']
    bomb += [f"l{i}: &l{i} [" + ",".join([f"*l{i - 1}"] * 9) + "]" for i in range(1, 7)]
    doc = {"workflow": "implementation_loop", "dsl_version": "2", "initial": "a", "stations": {}}
    doc[field] = "BOMB"
    dsl = "\n".join(bomb) + "\n" + yaml.safe_dump(doc).replace("BOMB", "*l6")

    with pytest.raises(SchemaInvalid) as info:
        WorkflowTemplateInstaller().validate(_artifact(dsl))
    assert "lol" not in "".join(traceback.format_exception(info.value))


def test_refuses_a_name_mismatch() -> None:
    assert _refusal(_artifact(name="other_loop")).startswith("name_mismatch:")


# --- step 4: validate_line ---------------------------------------------------- #


def test_refuses_validate_line_errors_listing_codes() -> None:
    dsl = LOOP.replace("service: forge.mark_pr_ready", "service: forge.merge_pr")
    message = _refusal(_artifact(dsl))
    assert message.startswith("line_invalid:")
    assert "unknown_service" in message


# --- step 5: reserved names --------------------------------------------------- #


@pytest.mark.parametrize("name", ["default_feature", "incident"])
def test_refuses_reserved_names(name: str) -> None:
    dsl = LOOP.replace("workflow: implementation_loop", f"workflow: {name}")
    assert _refusal(_artifact(dsl, name=name)).startswith("reserved_name:")


def test_reserved_names_are_the_bundled_definitions() -> None:
    store = pytest.importorskip("forge_workflow.editor.store")
    from forge_workflow.default_workflow import DEFAULT_WORKFLOW_NAME
    from forge_workflow.incident.definition import INCIDENT_DEFINITION_NAME

    assert {DEFAULT_WORKFLOW_NAME, INCIDENT_DEFINITION_NAME} == RESERVED_LINE_NAMES
    assert all(store.default_bundled_loader(n) is not None for n in RESERVED_LINE_NAMES)


# --- build_package validates through the installer ---------------------------- #


def test_build_package_uses_the_installer() -> None:
    common: dict[str, Any] = {
        "kind": ArtifactKind.workflow_template,
        "slug": "implementation-loop",
        "name": "Implementation loop",
        "version": "1.0.0",
        "summary": "TDD implementation loop",
    }
    manifest = build_package(artifact=_artifact(), **common)
    assert manifest.kind is ArtifactKind.workflow_template
    assert manifest.content_hash.startswith("sha256:")

    with pytest.raises(SchemaInvalid, match=r"^reserved_name"):
        build_package(
            artifact=_artifact(
                LOOP.replace("workflow: implementation_loop", "workflow: incident"),
                name="incident",
            ),
            **common,
        )


# --- re-check: scan cost, parser limits and what a refusal echoes ------------------ #


def test_aliases_of_one_large_string_are_scanned_once() -> None:
    import time

    dsl = LOOP + f'x_big: &a "{"word " * 12000}"\n' + "x_refs: [" + ",".join(["*a"] * 21000) + "]\n"
    assert len(dsl.encode()) <= 128 * 1024
    started = time.perf_counter()
    assert _refusal(_artifact(dsl)).startswith("line_parse_error:")  # x_ keys are not a line
    assert time.perf_counter() - started < 5  # was 60 s: one scan per alias


@pytest.mark.parametrize("opener", ["[", "{a: "])
def test_deep_nesting_is_invalid_yaml(opener: str) -> None:
    closer = "]" if opener == "[" else "}"
    dsl = LOOP + "x_nested: " + opener * 3000 + "1" + closer * 3000 + "\n"
    assert _refusal(_artifact(dsl)) == "invalid_yaml: nesting too deep"


def test_a_duplicate_key_is_refused() -> None:
    """YAML keeps the last value, so an escaped token in the first one is never decoded."""
    dsl = LOOP + f'x_note: "gh\\x70_{"a1B2" * 9}"\nx_note: plain\n'
    assert not default_secret_like(dsl)
    assert _refusal(_artifact(dsl)).startswith("invalid_yaml:")


def test_a_merge_key_is_not_a_duplicate() -> None:
    dsl = LOOP + "x_base: &b {a: 1}\nx_use: {<<: *b, a: 2}\n"  # an override, not a repeat
    assert _refusal(_artifact(dsl)).startswith("line_parse_error:")  # parsed, then not a line


def test_an_extra_key_holding_a_token_is_never_echoed() -> None:
    message = _refusal(_artifact(**{f"notes {_GHP}": 1}))
    assert message == "secret_like_content"


@pytest.mark.parametrize(
    "text",
    [f"use_{_GHP}", f"key{_AKIA}", f"use_sk-{'a' * 24}", f"x_{_JWT}"],
    ids=["ghp_glued", "akia_glued", "sk_underscore", "jwt_underscore"],
)
def test_a_token_glued_to_a_word_is_flagged(text: str) -> None:
    assert default_secret_like(text) is True


def test_sk_and_eyj_inside_words_are_not_flagged() -> None:
    assert default_secret_like("task-" + "implementation_and_review" * 2) is False
    assert default_secret_like("theyJust.went.home") is False
