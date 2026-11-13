# Orbit runs for the unified factory

Seven run-sized goals replace `../2026-11-07-unified-factory-orbit-goal.md`, which one Orbit run could
not carry: one run makes one candidate within the policy's size limits, workers cannot combine
branches or call GitHub, and two of its criteria needed credentials Orbit never gives a check.

Every goal uses the same template (GOAL, acceptance criteria mapped to check ids, WORKING PROCESS,
BOUNDARIES, FINAL RESPONSE) and was checked against Orbit 0.3.1's authority matcher
(`src/contract/authority.ts`): no goal asks for more authority than the policy grants, so none stops
at intake for a person.

## Order

| # | Goal file | Needs on the base | Notes |
|---|---|---|---|
| 1 | `run-3a-g-api.md` | G (already merged) | can run before H1 lands |
| 2 | `run-1-h1-conformance.md` | H1 merged and reviewed | the Postgres-only §9 proofs |
| 3 | `run-2-world-tools.md` | H1 | spec I world tools |
| 4 | `run-3b-g-temporal-e2e.md` | run 3a | Temporal wiring, end-to-end test |
| 5 | `run-4a-j-context.md` | H1, run 2; spec J approved | do not start until spec J's Status says approved |
| 6 | `run-4b-j-lifecycle.md` | run 4a | |
| 7 | `run-5-l-cli-sse.md` | run 3a | forge side of the tervin bridge |

Start a run only from a clean `feat/unified-factory` checkout whose HEAD includes the previous run's
result. If a run ends EXHAUSTED on size, split it (3a: API + belts, then CI tools; 3b: dispatcher,
then open_pr + end to end; 5: CLI, then SSE + `/ws`).

## Environment (once per machine)

- Orbit 0.3.1 through `~/.local/bin/orbit031`. The shared `~/.orbit/bin/orbit` launcher follows
  whichever Orbit ran last and may point at a development build.
- Test services the `python-unit-tests` check reaches on loopback:
  `docker start forge-test-pg forge-test-redis` (pgvector Postgres 16 on 5432, Redis 7.4 on 6379).
  Never point `FORGE_TEST_DATABASE_URL` at another database: the test fixtures drop tables.
- The policy is `.orbit/config.yaml` in this worktree (untracked; Orbit's clean-tree check ignores
  `.orbit`). A person sets `providers.codex.data_policy_eligible: true` there so Codex reviews every
  run independently; `review.when_unavailable: ask` stops a run rather than letting Claude review its
  own work.

## Run

```bash
cd ~/Projects/forge-factory
docker start forge-test-pg forge-test-redis
export SSL_CERT_FILE=/etc/ssl/cert.pem
~/.local/bin/orbit031 run --foreground --goal - < docs/superpowers/plans/orbit-runs/<goal-file>.md
```

## After each run

```bash
~/.local/bin/orbit031 status <run-id>
~/.local/bin/orbit031 report <run-id>
# SUCCEEDED: integrate locally (never main, never a remote), then confirm the tree is clean.
git merge --squash orbit/<run-id> && git commit -s -m "<type>(<scope>): <summary>"
git status --porcelain --untracked-files=all -- . ':(exclude).orbit'   # must print nothing
# BLOCKED: answer, then resume.
~/.local/bin/orbit031 questions <run-id> --all
~/.local/bin/orbit031 decide <run-id> <question-id> <option>
~/.local/bin/orbit031 resume <run-id> --foreground
```

### Orbit feedback to GitHub issues

`~/.local/bin/orbit-feedback.sh <run-id>` turns a run's baseline failures, open questions,
unresolved review findings, failed checks and lessons into issues on `QuintinBotes/forge` labelled
`orbit-feedback` plus a kind label (`baseline`, `decision-needed`, `review-finding`,
`unresolved-failure`, `lesson`). It dedupes with a hidden `<!-- orbit-item: KEY -->` marker, so
re-running only comments; it rewrites home paths, refuses secret-shaped text, and runs
`publish-guard scan` on every body because the repository is public. Preview with `DRY_RUN=1`.
Run it after every run and after every resume.
