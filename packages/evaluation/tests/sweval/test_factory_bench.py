"""forge-factory-bench: the suite contract, the harness and its report (#197, #198).

The held-out tests run for real, offline, in the local worktree sandbox.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Iterator, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
import yaml

from scf_agent.sandbox import LocalSandboxProvider
from scf_eval.benchmark import (
    BenchmarkContentHashMismatch,
    compute_content_hash,
    load_manifest,
    parse_swe_case_fields,
    validate_freezable,
)
from scf_eval.golden import GoldenCase
from scf_eval.sweval import (
    BenchRunReport,
    CaseOutcome,
    FnSolver,
    Solver,
    empty_solver,
    reference_solver,
    run_suite,
    run_swe_case,
)

SUITE = (
    Path(__file__).resolve().parents[2]
    / "scf_eval"
    / "benchmarks"
    / "forge-factory-bench"
    / "1.0.0"
)
_REGRESSED = "tests/test_durations.py::test_hours_and_minutes"
#: The task kinds the suite covers, one per case, recorded in its tags (#198).
_KINDS = {"bugfix", "feature", "refactor", "new_test"}
#: Rejects malformed text (the fail-to-pass test) but no longer adds anything up.
_VALIDATES_BUT_RETURNS_ZERO = (
    "import re\n\n\n"
    "def parse_duration(text: str) -> int:\n"
    '    if not re.fullmatch(r"(?:\\d+[hms])+", text):\n'
    '        raise ValueError(f"not a duration: {text!r}")\n'
    "    return 0\n"
)


def _run(
    suite: Path, solver: Solver, results: Path | None = None, scf_commit: str = "abc123"
) -> BenchRunReport:
    return asyncio.run(
        run_suite(
            suite,
            solver,
            sandbox_provider=LocalSandboxProvider(),
            results_path=results,
            scf_commit=scf_commit,
        )
    )


def _suite_copy(tmp_path: Path, *case_ids: str, **metadata: Any) -> Path:
    """The shipped suite cut down to ``case_ids``, unfrozen, with ``metadata`` set on each."""
    suite = tmp_path / "suite"
    shutil.copytree(SUITE, suite)
    manifest = yaml.safe_load((suite / "manifest.yaml").read_text(encoding="utf-8"))
    manifest["case_files"] = [f"cases/{case_id}.yaml" for case_id in case_ids]
    manifest["frozen"] = False
    (suite / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    for case_id in case_ids:
        path = suite / "cases" / f"{case_id}.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["cases"][0]["metadata"].update(metadata)
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return suite


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


@pytest.fixture(scope="module")
def reference_run() -> tuple[BenchRunReport, list[GoldenCase]]:
    """One reference run over the shipped suite, and every case its solver was handed."""
    seen: list[GoldenCase] = []
    reference = reference_solver(SUITE)

    def solve(case: GoldenCase) -> Mapping[str, str]:
        seen.append(case)
        return reference(case)

    return _run(SUITE, FnSolver(name="reference", fn=solve)), seen


def test_suite_loads_with_twenty_complete_cases() -> None:
    manifest, cases = load_manifest(SUITE)
    assert (manifest.slug, manifest.version) == ("forge-factory-bench", "1.0.0")
    assert len(cases) == 20
    validate_freezable(cases)  # every case ends before merge (FR-018)
    for case in cases:
        fields = parse_swe_case_fields(case)
        held_out = {f.path for f in fields.held_out}
        assert case.query.strip(), case.id
        assert 1 <= len(fields.repo) <= 4, case.id
        assert fields.fail_to_pass and fields.pass_to_pass, case.id
        tests = {t.split("::")[0] for t in fields.fail_to_pass + fields.pass_to_pass}
        assert tests <= held_out, case.id
        assert not held_out & {f.path for f in fields.repo}, case.id


def test_cases_cover_the_four_kinds() -> None:
    _manifest, cases = load_manifest(SUITE)
    kinds = {case.id: set(case.tags) & _KINDS for case in cases}
    assert all(len(k) == 1 for k in kinds.values()), kinds
    assert set().union(*kinds.values()) == _KINDS


def test_suite_is_frozen_by_its_content_hash() -> None:
    manifest, cases = load_manifest(SUITE)
    assert manifest.frozen
    assert manifest.content_hash == compute_content_hash(cases, manifest.scoring)


@pytest.mark.parametrize("case_file", sorted(p.name for p in (SUITE / "cases").iterdir()))
def test_editing_any_case_file_rejects_the_frozen_suite(tmp_path: Path, case_file: str) -> None:
    suite = tmp_path / "suite"
    shutil.copytree(SUITE, suite)
    path = suite / "cases" / case_file
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["cases"][0]["query"] += "Edited.\n"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(BenchmarkContentHashMismatch):
        load_manifest(suite)


def test_reference_solver_resolves_every_case(reference_run: Any) -> None:
    report, _seen = reference_run
    assert [r.outcome for r in report.cases] == [CaseOutcome.RESOLVED] * 20
    assert report.resolve_rate == 1.0


def test_empty_patch_resolves_no_case() -> None:
    report = _run(SUITE, empty_solver())
    assert [r.outcome for r in report.cases] == [CaseOutcome.NOT_RESOLVED] * 20
    assert all(r.regressed == [] for r in report.cases)  # pass-to-pass holds on the base
    assert report.resolve_rate == 0.0


def test_failing_setup_command_is_an_infrastructure_error(tmp_path: Path) -> None:
    suite = _suite_copy(tmp_path, "ffb-chunk-remainder", setup_commands=["exit 3"])
    [result] = _run(suite, reference_solver(suite)).cases
    assert result.outcome is CaseOutcome.INFRASTRUCTURE_ERROR
    assert "exit 3" in (result.reason or "")


def test_crashing_solver_is_an_infrastructure_error(tmp_path: Path) -> None:
    def crash(_case: GoldenCase) -> Mapping[str, str]:
        raise ConnectionError("instance unreachable")

    suite = _suite_copy(tmp_path, "ffb-chunk-remainder")
    [result] = _run(suite, FnSolver(name="crash", fn=crash)).cases
    assert result.outcome is CaseOutcome.INFRASTRUCTURE_ERROR
    assert result.reason == "ConnectionError: instance unreachable"


def test_regressed_pass_to_pass_test_is_not_resolved_and_named(tmp_path: Path) -> None:
    suite = _suite_copy(tmp_path, "ffb-duration-validation")
    solver = FnSolver(name="regresses", fn=lambda _c: {"durations.py": _VALIDATES_BUT_RETURNS_ZERO})
    report = _run(suite, solver)
    [result] = report.cases
    assert result.outcome is CaseOutcome.NOT_RESOLVED
    assert result.regressed == [_REGRESSED]
    assert _REGRESSED in report.to_markdown()


def test_solver_never_receives_held_out_tests(reference_run: Any) -> None:
    _report, seen = reference_run
    _manifest, cases = load_manifest(SUITE)
    assert [c.id for c in seen] == [c.id for c in cases]
    for full, public in zip(cases, seen, strict=True):
        fields = parse_swe_case_fields(full)
        visible = "\n".join(_strings(asdict(public)))
        tests = [*fields.fail_to_pass, *fields.pass_to_pass]
        hidden = [*tests, *(t.split("::")[1] for t in tests)]
        for held in fields.held_out:
            hidden += [held.path, Path(held.path).name, held.content]
        leaked = [h for h in hidden if h in visible]
        assert not leaked, (full.id, leaked)


def test_report_states_provenance_and_serialises(reference_run: Any) -> None:
    report, _seen = reference_run
    manifest, cases = load_manifest(SUITE)
    data = json.loads(report.model_dump_json())
    assert data["suite_slug"] == "forge-factory-bench"
    assert data["suite_version"] == "1.0.0"
    assert data["suite_content_hash"] == compute_content_hash(cases, manifest.scoring)
    assert data["scf_commit"] == "abc123"
    assert data["solver"] == "reference"
    assert data["models"] == []
    assert data["sandbox_tier"] == "worktree"
    assert data["resolve_rate"] == 1.0
    assert data["total_cost_usd"] is None
    assert data["wall_time_s"] > 0
    assert [c["outcome"] for c in data["cases"]] == ["resolved"] * 20
    assert BenchRunReport.model_validate(data) == report

    markdown = report.to_markdown()
    for text in ["1.0.0", data["suite_content_hash"], "abc123", "worktree", "20/20"]:
        assert text in markdown
    for case in cases:
        assert case.id in markdown


def test_report_states_models_and_cost_when_given(tmp_path: Path) -> None:
    suite = _suite_copy(tmp_path, "ffb-chunk-remainder")
    reference = reference_solver(suite)

    class Priced(FnSolver):
        def __call__(self, case: GoldenCase) -> Mapping[str, str]:
            self.last_cost_usd = 0.25
            return super().__call__(case)

    report = _run(suite, Priced(name="priced", fn=reference, models=("model-a",)))
    assert report.models == ["model-a"]
    assert report.total_cost_usd == 0.25
    assert report.cases[0].cost_usd == 0.25
    markdown = report.to_markdown()
    assert "model-a" in markdown
    assert "0.25" in markdown


_SKIP_EVERY_TEST = (
    "import pytest\n\n\n"
    "def pytest_collection_modifyitems(items):\n"
    "    for item in items:\n"
    "        item.add_marker(pytest.mark.skip(reason='skipped'))\n"
)
_COLLECT_ONLY = "addopts = --collect-only\n"
#: Changes that leave chunked() broken but make pytest exit 0 for every held-out test,
#: by writing pytest's configuration or shadowing pytest itself.
_WRITES_PYTEST_FILES = {
    "conftest.py": _SKIP_EVERY_TEST,
    "tests/conftest.py": _SKIP_EVERY_TEST,
    "pytest.ini": "[pytest]\n" + _COLLECT_ONLY,
    "tox.ini": "[pytest]\n" + _COLLECT_ONLY,
    "setup.cfg": "[tool:pytest]\n" + _COLLECT_ONLY,
    "pyproject.toml": '[tool.pytest.ini_options]\naddopts = "--collect-only"\n',
    "pytest.py": "raise SystemExit(0)\n",
}


@pytest.mark.parametrize(("path", "content"), list(_WRITES_PYTEST_FILES.items()))
def test_a_change_that_writes_pytest_files_is_refused(
    tmp_path: Path, path: str, content: str
) -> None:
    suite = _suite_copy(tmp_path, "ffb-chunk-remainder")
    [result] = _run(suite, FnSolver(name="games", fn=lambda _c: {path: content})).cases
    assert result.outcome is CaseOutcome.NOT_RESOLVED
    assert path in (result.reason or "")


def test_a_skipped_held_out_test_does_not_count_as_passing(tmp_path: Path) -> None:
    skips = "import pytest\n\n\ndef chunked(items, size):\n    pytest.skip('x')\n"
    suite = _suite_copy(tmp_path, "ffb-chunk-remainder")
    [result] = _run(suite, FnSolver(name="skips", fn=lambda _c: {"batching.py": skips})).cases
    assert result.outcome is CaseOutcome.NOT_RESOLVED


#: Code under test that, once pytest has written its report, rewrites it with an entity
#: declaration that expands into a passing test, then forces exit 0.
_REWRITES_THE_REPORT_WITH_A_DTD = (
    "import atexit\n"
    "import os\n"
    "import sys\n"
    "\n"
    "_REPORT = (\n"
    "    '<!DOCTYPE testsuites [<!ENTITY ok \"passed\">]>'\n"
    "    '<testsuites><testsuite><testcase name=\"&ok;\"/></testsuite></testsuites>'\n"
    ")\n"
    "\n"
    "\n"
    "def _rewrite():\n"
    "    for arg in sys.argv:\n"
    "        if arg.startswith('--junitxml='):\n"
    "            with open(arg.split('=', 1)[1], 'w') as fh:\n"
    "                fh.write(_REPORT)\n"
    "    os._exit(0)\n"
    "\n"
    "\n"
    "atexit.register(_rewrite)\n"
    "\n"
    "\n"
    "def chunked(items, size):\n"
    "    return []\n"
)


async def test_a_report_that_declares_entities_is_not_counted(tmp_path: Path) -> None:
    _manifest, cases = load_manifest(SUITE)
    [case] = [c for c in cases if c.id == "ffb-chunk-remainder"]
    for f in parse_swe_case_fields(case).repo:
        (tmp_path / f.path).write_text(f.content, encoding="utf-8")
    result = await run_swe_case(
        case=case,
        solve_fn=lambda _c: {"batching.py": _REWRITES_THE_REPORT_WITH_A_DTD},
        sandbox_provider=LocalSandboxProvider(),
        worktree_path=str(tmp_path),
    )
    assert result.resolved is False
    assert result.output_ids == []
    assert "declares a DTD or entities" in result.evidence


def test_a_change_cannot_replace_the_held_out_tests(tmp_path: Path) -> None:
    stubs = (
        "def test_keeps_the_short_last_chunk():\n    pass\n\n\n"
        "def test_even_split_and_bad_size():\n    pass\n"
    )
    suite = _suite_copy(tmp_path, "ffb-chunk-remainder")
    solver = FnSolver(name="stubs", fn=lambda _c: {"tests/test_batching.py": stubs})
    [result] = _run(suite, solver).cases
    assert result.outcome is CaseOutcome.NOT_RESOLVED


def test_resume_refuses_results_stored_under_other_provenance(tmp_path: Path) -> None:
    suite = _suite_copy(tmp_path, "ffb-chunk-remainder", setup_commands=["exit 3"])
    results = tmp_path / "results.jsonl"
    reference = reference_solver(suite)
    _run(suite, reference, results)
    _run(suite, reference, results)  # the same run resumes
    with pytest.raises(ValueError, match="another run"):
        _run(suite, reference, results, scf_commit="def456")
    with pytest.raises(ValueError, match="another run"):
        _run(suite, FnSolver(name="reference", key="elsewhere", fn=reference), results)

    reference_file = suite / "reference.yaml"
    reference_file.write_text(reference_file.read_text() + "\n# edited\n", encoding="utf-8")
    with pytest.raises(ValueError, match="another run"):
        _run(suite, reference_solver(suite), results)


def test_resume_reruns_infrastructure_errors(tmp_path: Path) -> None:
    suite = _suite_copy(tmp_path, "ffb-chunk-remainder")
    results = tmp_path / "results.jsonl"
    reference = reference_solver(suite)

    def down(_case: GoldenCase) -> Mapping[str, str]:
        raise ConnectionError("instance down")

    [first] = _run(suite, FnSolver(name="reference", fn=down), results).cases
    assert first.outcome is CaseOutcome.INFRASTRUCTURE_ERROR

    calls: list[str] = []

    def counting(case: GoldenCase) -> Mapping[str, str]:
        calls.append(case.id)
        return reference(case)

    [second] = _run(suite, FnSolver(name="reference", fn=counting), results).cases
    assert calls == ["ffb-chunk-remainder"]
    assert second.outcome is CaseOutcome.RESOLVED


class _Interrupted(BaseException):
    """Stands in for a killed process: the harness does not catch it."""


def test_interrupted_run_resumes_without_rerunning_stored_cases(tmp_path: Path) -> None:
    suite = _suite_copy(tmp_path, "ffb-chunk-remainder", "ffb-slug-separators")
    results = tmp_path / "results.jsonl"
    reference = reference_solver(suite)

    def interrupt_on_second(case: GoldenCase) -> Mapping[str, str]:
        if case.id == "ffb-slug-separators":
            raise _Interrupted
        return reference(case)

    with pytest.raises(_Interrupted):
        _run(suite, FnSolver(name="reference", fn=interrupt_on_second), results)

    calls: list[str] = []

    def counting(case: GoldenCase) -> Mapping[str, str]:
        calls.append(case.id)
        return reference(case)

    report = _run(suite, FnSolver(name="reference", fn=counting), results)
    assert calls == ["ffb-slug-separators"]
    assert [r.outcome for r in report.cases] == [CaseOutcome.RESOLVED] * 2

    # Results stored for one solver are never mixed into another solver's report.
    with pytest.raises(ValueError, match="another run"):
        _run(suite, empty_solver(), results)
