# forge-factory-bench

`forge-factory-bench` scores the whole factory on work it has not seen. Each case is
an issue, a small repository and tests the solver never sees. A solver reads the
issue and the repository and returns the files its change writes. The harness then
runs the held-out tests in a sandbox. The suite is the fixed yardstick that two
factory configurations (a model, a prompt, a gate) are compared on.

## What it measures

A case is **resolved** when every fail-to-pass test passes and no pass-to-pass test
fails. It is **not resolved** otherwise, and an **infrastructure error** when it could
not be scored (a setup command failed, the solver or the sandbox crashed). The
headline figure is the resolve rate: resolved cases over all cases.

Fail-to-pass tests check what the issue asks for and fail on the starting
repository. Pass-to-pass tests check behaviour that must not change and pass on the
starting repository. The solver receives no held-out test id, path or content, and
the held-out files are written over its change before they run, so it cannot replace
them.

## Version 1.0.0

Version 1.0.0 has 20 cases, in
`packages/evaluation/scf_eval/benchmarks/forge-factory-bench/1.0.0/`. It is frozen
with content hash
`sha256:6ea9dff0fdb2209d87fa6ae5b05e25015924b77098da2667bff12b9a2fe5acb8`. Every
benchmark command rejects the suite if a case or the scoring is edited; a change to
the cases needs a new version. Each case's tags record its kind:

| Kind (tag) | Cases | What the change must do |
| --- | --- | --- |
| Bug fix (`bugfix`) | 7 | Correct wrong behaviour the issue describes. |
| Small feature (`feature`) | 5 | Add a function, an argument or a syntax the issue specifies. |
| Refactor (`refactor`) | 4 | Change the structure the issue names, keeping behaviour. |
| Needs a new test (`new_test`) | 4 | Add a test to the repository's own tests that fails on the old code. |

For the `new_test` cases, the held-out tests run the repository's test file in a
subprocess, once against the code as the issue found it (or, for the case that only
pins behaviour, against rewrites that change it) and once against the change. The
repository's tests must fail on the first and pass on the second, so a fix without
a test, or a test that always fails, is not resolved.

Every case was written for this suite (ADR-0010). The five cases of the 0.1.0 seed
are carried into 1.0.0 unchanged; 0.1.0 is removed.

## Reproduce a run

From a checkout of the repository, with the workspace synced (`uv sync`):

```bash
# The suite's content hash; it must equal the one above.
uv run python -m scf_api.cli_bench hash \
  packages/evaluation/scf_eval/benchmarks/forge-factory-bench/1.0.0

# The reference solver applies each case's known-good patch: 20 of 20 resolve.
uv run python -m scf_api.cli_bench run \
  --suite-dir packages/evaluation/scf_eval/benchmarks/forge-factory-bench/1.0.0 \
  --solver reference --out bench-out

# The empty solver changes nothing: 0 of 20 resolve.
uv run python -m scf_api.cli_bench run \
  --suite-dir packages/evaluation/scf_eval/benchmarks/forge-factory-bench/1.0.0 \
  --solver empty
```

`run` prints the report as Markdown. With `--out` it also writes `report.json` and
`report.md`, and keeps `results.jsonl` so an interrupted run resumes. It exits 1 when
any case is an infrastructure error. To score a running SCF instance, use
`ScfInstanceSolver` with `run_suite`, as `packages/evaluation/README.md` shows.

## Report fields

| Field | Meaning |
| --- | --- |
| `suite_slug`, `suite_version` | The suite and the version that ran. |
| `suite_content_hash` | The hash of the cases and scoring that ran. |
| `scf_commit` | The commit of the checkout the harness ran from, or null outside git. |
| `solver` | The solver's name: `reference`, `empty` or an instance adapter. |
| `models` | The model identifiers the solver reported; empty when it reports none. |
| `sandbox_tier` | The sandbox kind the tests ran in (`worktree` offline). |
| `cases` | One row per case: `case_id`, `outcome` (`resolved`, `not_resolved` or `infrastructure_error`), `regressed` (the pass-to-pass tests the change broke), `reason` (why a case was not scored or a change was refused), `cost_usd` and `wall_time_s`. |
| `resolve_rate` | Resolved cases over all cases. |
| `total_cost_usd` | The summed cost the solver reported; null when it reported none. |
| `wall_time_s` | Summed over the cases, so a resumed run counts each case once. |

## Baseline

[`forge-factory-bench-1.0.0-reference.md`](./forge-factory-bench-1.0.0-reference.md)
(and its [JSON](./forge-factory-bench-1.0.0-reference.json)) is the report of the
reference solver over 1.0.0: 20 of 20 resolved. The empty solver resolved 0 of 20 on
the same checkout.

**This baseline is a harness self-test, not a measurement of an agent.** The
reference solver applies the known-good patches stored beside the suite, so its
result shows only that every case is solvable and that the harness scores it as
resolved. No model ran. The report's `scf_commit` is the commit this suite was
added on top of; the harness code is the same in both.

An agent score needs a live run: a development SCF instance with model credentials,
each case's repository pushed to branch `bench/<case id>` of a repository connected
to that instance, and `ScfInstanceSolver`. That belongs with #200 and #201 and is not
published here.

## Limits

- The repositories are small and purpose-written: one or two Python files of a few
  dozen lines. They test whether a change is right, not whether an agent can find
  its way around a large codebase.
- Every case is Python with pytest. Other languages and build systems are not
  covered.
- 20 cases make the resolve rate coarse: one case is five percentage points. Compare
  configurations on the per-case outcomes as well as the rate.
- The offline sandbox tier is `worktree`, which is not isolation. Candidate code runs
  inside the test process, so a change built to tamper with pytest from within can
  still lie; run untrusted solvers in a stronger sandbox.
- Wall time depends on the machine and is not comparable across machines.
- The suite's figures are not comparable with any other benchmark's.
