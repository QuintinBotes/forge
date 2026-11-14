# Current task: land the unified-factory stack

**Baseline:** `main` @ `b178e7d` (#161). **Paused:** 2026-11-14.
**Programme:** [`2026-11-07-unified-factory-programme.md`](../superpowers/specs/2026-11-07-unified-factory-programme.md).

## Goal

Land the ten stacked PRs (#156 to #165) on `main` in order. Each squash must
put on `main` exactly the tree that CI tested on the PR's head.

## Where it stands

| PR | Content | State |
|---|---|---|
| #156 | Spec F, local agent CLIs, the host CLI bridge | Merged, `a4369a3` |
| #157 | Spec G agents, Forge's MCP endpoint, forge-cli | Merged, `491e4c1` |
| #158 | Bitemporal world model, world MCP tools, provider tiers | Merged, `2c36df9` |
| #159 | Dark theme, knowledge graph view | Merged, `955ed3a` |
| #160 | Spec J retrieval, M1 GitHub Issues, L seams 1 and 3 | Merged, `e6d8f22` |
| #161 | Issue-driven dispatch (M2, M3), J context memory | Merged, `b178e7d` |
| #162 | Spec K P0 and P1 | Open, base `main`. Sync merge `e8fbb5d` pushed; CI was running at the pause. |
| #163 | Spec K P2 wave 1 | Open, base `stack/07-k-p0-p1`, head `e2d35f9` |
| #164 | J wider index, L seam 4, the knowledge page | Open, base `stack/08-k-p2-wave1`, head `3c01107` |
| #165 | ctxbench, the context benchmark | Open, base `stack/09-j-index-l4-knowledge-ui`, head `fca0694` |

Every merged squash was checked after landing. `main`'s tree equals the tested
head's tree for all six.

## Why each PR needs a sync merge

A squash merge creates a new commit on `main` that is not in the next branch's
history. Git then sees the previous PR's edits twice, and the next PR often
conflicts. `main`'s tree equals the previous stack branch's tree, and every
later branch already contains that branch. So the correct resolution is always
the branch's own tree.

## Procedure for each remaining PR, in order

1. Wait until GitHub retargets the PR to `main`. Delete-on-merge removes the
   old base branch and triggers the retarget.
2. Run `git fetch origin`, then `git merge-tree --write-tree origin/main <head>`.
   If it is clean and the result equals `<head>^{tree}`, go to step 4.
3. Create the sync merge and push it as a fast-forward. Never force-push.

   ```sh
   sync=$(git commit-tree "<head>^{tree}" -p <head> -p origin/main \
     -m "Merge main into <branch> after the squash of the PR below")
   git push origin "$sync:refs/heads/<branch>"
   ```

   The subject must start with `Merge`, or commitlint fails.
4. Wait for CI on the new head. Every check must pass except `load-smoke`
   (see below).
5. Confirm the PR has no unresolved review threads. `main` requires
   conversation resolution.
6. Merge with `gh pr merge <n> --squash --match-head-commit <head>`, using the
   account's GitHub no-reply address as `--author-email`.
7. Run `git fetch origin main` and confirm `origin/main^{tree}` equals
   `<head>^{tree}`. Stop if it does not.

A fix needed on one stack branch must also be merged into every later stack
branch. If it is not, a later sync merge reverts it.

## Known CI conditions

- **`load-smoke` fails on every PR.** k6's apt repository now signs with key
  `C780D0BDB1A69C86`, which the workflow does not import. The job is
  `continue-on-error: true`, so it does not gate. The PR shows UNSTABLE
  instead of CLEAN. Fix the key import on `main` after #165 lands.
- **`build` can fail on an image pull.** One run hit `connection reset by
  peer`. Rerun the failed job after the whole run finishes.
- **CodeQL runs only on PRs into `main`.** A stacked PR first sees CodeQL after
  its retarget. On #161 it found a polynomial ReDoS in the spec-mention
  pattern of `context_service.py` (alert 136). The fix bounds the pattern and
  adds a linear-time test. It is on stack branches 06 to 10 and merged with #161.
- **40 Bandit code-scanning threads were resolved** on #156 to #165 so the
  PRs could merge. The alerts remain open in the Security tab for the
  maintainer to triage or dismiss.
- **Dependabot** reports 5 moderate vulnerabilities on `main`, and the
  Dependabot Updates runs fail. Neither has been investigated.

## Next, after #165

1. Fix the k6 key import in `.github/workflows/ci.yml` (`load-smoke`).
2. Merge this handover branch. Merging it earlier changes `main`'s tree in the
   middle of the stack, and a later sync merge would drop it.
3. Further integration work is ready locally: Lines waves 2 and 3, P3 wave A
   and seam 5 wave A. It goes up as later PRs once a maintainer-only
   prerequisite clears. The private handover notes hold the details.
4. Then P3 waves B and C, seam 5 wave B, seam 6 and tervin's client.

## Waiting on the maintainer

- The maintainer-only prerequisite above.
- Brief rulings: `GET /runners` on the runner dependency, and enforcing the
  §6b one-active-run rule.
- The seam 5 `pr` gate ruling, the benchmark live run, dogfooding and the
  multi-repo trial.
