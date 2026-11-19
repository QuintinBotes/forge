# Factory metrics

Swarm Code Factory reports five numbers for a workspace and a time window: autonomous merge rate, cost per merged change, revert rate, human intervention rate and cycle time. This document is their definition (spec `specs/005-proof/spec.md`, FR-001 to FR-011). The formulas below are normative: the code in `packages/observability/scf_obs/analytics/factory.py` implements them, and anyone can recompute a reported value from the evidence rows listed at the end.

Read the metrics with:

- the API: `GET /observability/analytics/factory?since=…&until=…&repository=…&source=…` (any role with read permission; the caller's workspace only);
- the CLI: `scf-cli metrics --since … --until … [--repository owner/name] [--source api|tracker] --json`, which prints the same report in the `forge.cli/v1` envelope with `observed_by`, `recorded_at` and `enforceable: false`.

These numbers are measurements. Nothing here changes how or whether a change lands.

## Terms

**Attempt.** One top-level agent run: an `agent_run` row with role `primary`. A sub-agent run of a supervised run (an `agent_run` named by `sub_agent_run.agent_run_id`) is never an attempt: its cost and interventions belong to the attempt at the top of its parent chain. The verifier and reviewer runs of a governed workflow (roles `verifier` and `reviewer`) check an attempt's work and are not attempts; their cost is not attributed to one.

**Change.** The pull request an attempt opened (`output.artifacts.pull_request.number`). An attempt has at most one change.

**Terminal.** A run whose status is `succeeded`, `failed`, `cancelled` or `escalated`. Its end time is `completed_at`, or the row's last update when `completed_at` was never written (a run that failed before it started).

**Final outcome.** A change has a final outcome when its pull request was merged (*landed*) or closed without being merged (*closed*). A pull request that is closed, reopened and then merged ends *landed*; the attempt is counted once.

**Settled.** An attempt is settled when it is terminal and its change, if it opened one, has a final outcome. Its **settle time** is its end time when it opened no pull request, else the later of its end time and its change's outcome time. An attempt whose pull request is still open is not settled and is in no window.

**Window.** A half-open interval `[since, until)`. A window selects attempts by settle time. Without `since` and `until` the window is the 30 days that end now.

- `S` is the set of attempts settled in the window.
- `L` is the subset of `S` whose change landed.

**Intervention.** One act by a person that altered or rejected an attempt's work. There are four kinds:

| Kind | What counts | Recorded from |
| --- | --- | --- |
| `gate_decision` | an approval gate tied to the attempt ends `rejected` or `changes_requested` | `approval_request` whose `agent_run_id` is the attempt (or one of its sub-agent runs), or whose `workflow_run_id` is the attempt's workflow run |
| `review_changes_requested` | a review on the change with state `changes_requested` | GitHub `pull_request_review` `submitted` delivery, one per review id |
| `foreign_commit` | a commit pushed to the change's branch by an identity other than SCF | GitHub `pull_request` `synchronize` delivery whose sender is not the account that opened the pull request, one per pushed head |
| `escalated` | the run ended escalated to a person | `agent_run.status = 'escalated'` |

Approving a gate is governance, not an intervention. A gate that is escalated to an administrator is not an intervention either; only its rejection or a request for changes is. SCF's own identity is the account that opened the run's pull request (the GitHub App's bot account or the configured token's user), so a push by that account is SCF's own work.

`interventions(a)` is the number of interventions of attempt `a`, of any kind.

**Cost.** `cost(a)` is the sum of `cost_event.cost_usd` over the events whose `agent_run_id` is the attempt or one of its sub-agent runs, at any time. An event whose `price_id` is empty had no price when it was recorded: it is counted and reported as unpriced, and its cost (zero) is in the sum. The report never drops it and never estimates it.

**Revert.** `reverted(a)` holds when a later landed pull request reverted the attempt's change within 30 days of the change landing. A pull request reverts change `N` when it lands and its body carries GitHub's revert reference, the line `Reverts owner/repo#N`, for the same repository. A revert is recorded against the original change and is never cleared: when the revert is itself reverted, the original stays reverted. The revert counts in the window in which the original change landed, because that is the window in which the attempt settled.

## The five metrics

Every metric is returned with `value`, `numerator`, `denominator`, `sample_size`, `available` and, when it is not available, `reason`. A metric whose denominator is zero is reported as not available with its reason, never as zero.

### 1. Autonomous merge rate

```
autonomous merge rate = |{a in L : interventions(a) = 0}| / |S|
```

- Population: `S`. Numerator: landed attempts with no intervention. Denominator and sample size: `|S|`.
- Exclusions: sub-agent runs; attempts not settled in the window.
- Not available when `|S| = 0`: "no settled attempt in the window".

### 2. Cost per merged change

```
cost per merged change = sum(cost(a) for a in S) / |L|
```

- Population: `S` for the numerator, `L` for the denominator. The cost of attempts that did not land is in the numerator.
- Numerator: the total cost in USD. Denominator: `|L|`. Sample size: `|S|`, the attempts whose cost was summed.
- Also reported: `cost_events` (events summed) and `unpriced_cost_events` (events with no price).
- Exclusions: infrastructure cost and human time; cost events tied to no agent run.
- Not available when `|L| = 0`: "no landed change in the window". The numerator is still reported.

### 3. Revert rate

```
revert rate = |{a in L : reverted(a)}| / |L|
```

- Population: `L`. Numerator: landed attempts reverted within 30 days of landing. Denominator and sample size: `|L|`.
- **Provisional**: when the newest landed change in the window landed less than 30 days ago, a revert may still arrive, so the report sets `provisional: true`. `observation_days` states the period.
- Exclusions: reverts that land more than 30 days after the original; reverts made other than through a pull request with GitHub's revert reference.
- Not available when `|L| = 0`: "no landed change in the window".

### 4. Human intervention rate

```
human intervention rate = |{a in S : interventions(a) >= 1}| / |S|
```

- Population: `S`, landed or not. Numerator: attempts with at least one intervention. Denominator and sample size: `|S|`.
- Not available when `|S| = 0`: "no settled attempt in the window".

### 5. Cycle time

```
cycle time(a) = landed_at(a) - created_at(a), for a in L
reported: median and 90th percentile
```

- Population: `L`. `created_at` is the agent run's creation; `landed_at` is the time the pull request was merged. Cycle time ends when the change lands, not when it reaches production.
- `value` and `median_seconds` are the median; `p90_seconds` is the 90th percentile, interpolated linearly between the closest ranks: with the `n` cycle times sorted ascending as `x[0] … x[n-1]`, take position `p = 0.9 × (n − 1)` and return `x[floor(p)] + (p − floor(p)) × (x[floor(p) + 1] − x[floor(p)])`. The median is the same rule at `0.5`.
- A percentile is not a ratio: `numerator` is empty; `denominator` and `sample_size` are `|L|`.
- Not available when `|L| = 0`: "no landed change in the window".

## Worked example

Ten attempts settle in a window that ended more than 30 days ago:

| Attempt | Outcome | Landed after creation | Interventions | Cost (USD) | Reverted |
| --- | --- | --- | --- | --- | --- |
| A1 | landed | 1 h | none | 1.10 | no |
| A2 | landed | 2 h | none | 2.20 | yes, 3 days after landing |
| A3 | landed | 3 h | none | 0.30 | no |
| A4 | landed | 4 h | none | 0.40 | no |
| A5 | landed | 5 h | `review_changes_requested` | 0.50 | no |
| A6 | landed | 10 h | `foreign_commit` | 1.00 | no |
| A7 | closed | | `gate_decision` | 0.70 | |
| A8 | closed | | none | 0.80 | |
| A9 | no pull request | | `escalated` | 0.90 | |
| A10 | no pull request | | none | 0.10 | |

`|S| = 10`, `|L| = 6` (A1 to A6).

1. **Autonomous merge rate.** Landed with no intervention: A1, A2, A3, A4. `4 / 10 = 0.40`.
2. **Cost per merged change.** All ten costs sum to `8.00`. `8.00 / 6 = 1.33` USD. A7 to A10 landed nothing but their cost is in the numerator.
3. **Revert rate.** A2 was reverted within 30 days. `1 / 6 = 0.17`. Not provisional: the newest landing is more than 30 days old. Had A6 landed 10 days ago, the same value would be reported with `provisional: true`.
4. **Human intervention rate.** A5, A6, A7 and A9 had an intervention. `4 / 10 = 0.40`.
5. **Cycle time.** Sorted: 1, 2, 3, 4, 5, 10 hours. Median at `p = 0.5 × 5 = 2.5`: `3 + 0.5 × (4 − 3) = 3.5` hours. 90th percentile at `p = 0.9 × 5 = 4.5`: `5 + 0.5 × (10 − 5) = 7.5` hours.

With no landed change (only A7 to A10), cost per merged change, revert rate and cycle time are not available, with the reason "no landed change in the window". Cost per merged change still reports its numerator, `2.50`. Autonomous merge rate is `0 / 4 = 0` and human intervention rate is `2 / 4 = 0.5`.

This example is the fixture of `packages/observability/tests/test_analytics_factory.py`.

## Filters

- `repository` (`owner/name`, case-insensitive): attempts whose change is in that repository, or, for an attempt with no change, whose run context names it.
- `source`: `tracker` for attempts started by a tracker dispatch (the run context carries the tracker reference as its server-set `source`), `api` for every other attempt.

Both narrow `S` and therefore `L`. A report never includes another workspace's runs, changes, interventions or cost.

## Evidence rows

Every value can be recomputed from these rows of one workspace:

| Table | What it holds |
| --- | --- |
| `agent_run` | attempts: `id`, `role`, `status`, `created_at`, `completed_at`, `workflow_run_id`, `inputs.context.repo_full_name`, `inputs.context.source`, `output.artifacts.pull_request.number` |
| `sub_agent_run` | `agent_run_id` (the sub-agent run) and `parent_agent_run_id` |
| `change_outcome` | one row per attempt whose pull request closed: `outcome` (`landed` or `closed`), `outcome_at`, `repo_full_name`, `pr_number`, `reverted_at`, `reverted_by_pr_number` |
| `run_intervention` | interventions from GitHub deliveries: `agent_run_id`, `kind`, `source_key` (the review id or pushed head), `occurred_at` |
| `approval_request` | gate outcomes: `agent_run_id`, `workflow_run_id`, `status` |
| `cost_event` | `agent_run_id`, `cost_usd`, `price_id` (empty when unpriced) |

`change_outcome` and `run_intervention` are written by the GitHub webhook (`POST /integration/github/webhooks`) after its signature check. A redelivery writes nothing new. A pull request from a fork never resolves to a run through its branch name.

## Not measured here

- The cost of a governed workflow's verifier and reviewer runs.
- Lead time to production and production outcomes per change (roadmap R46).
- Infrastructure cost and human time.
- Trackers and code hosts other than GitHub.
