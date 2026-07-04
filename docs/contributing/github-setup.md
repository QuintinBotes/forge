# GitHub repository hardening

Forge ships `scripts/setup-github.sh`, an **idempotent** helper that applies a
safe baseline of GitHub repository configuration to the canonical repo
(`service-hive/forge`) via `gh api`. It codifies the branch-protection, code
review, secret-scanning, Dependabot, and merge-policy settings so they are
reviewable, reproducible, and easy to re-apply after any manual drift.

Run it **once**, after the first pull request has merged. It is safe to re-run
at any time — every call sets a *desired state*, so nothing errors on "already
configured".

## Prerequisites

- **GitHub CLI** (`gh`) installed — <https://cli.github.com>.
- Authenticated as a **repository administrator**:

  ```bash
  gh auth login          # choose GitHub.com, HTTPS, and authenticate
  gh auth status         # confirm you are logged in
  ```

  You must have the **admin** role on `service-hive/forge` (org owner or a repo
  admin). The script verifies this up front and refuses to continue otherwise.

- The protected branch (`main`) must already **exist with at least one commit**,
  and CI must have run at least once so GitHub knows the required check names.
  This is why the script is meant to run *after the first PR merges* rather than
  on an empty repo.

## Running it

From the repository root:

```bash
scripts/setup-github.sh
```

Optional overrides (defaults shown):

```bash
scripts/setup-github.sh service-hive/forge main
# or, equivalently:
REPO=service-hive/forge BRANCH=main scripts/setup-github.sh
```

The script prints each change as it applies it and finishes with a read-back of
the resulting configuration so you can confirm it landed.

## What it configures

### Branch protection on `main`

| Setting | Value |
| --- | --- |
| Require a pull request before merging | yes |
| Required approving reviews | 1 |
| Dismiss stale approvals on new pushes | yes |
| Require conversation resolution before merging | yes |
| Require status checks to pass | yes |
| Require branches to be up to date (strict) | yes |
| Require linear history | yes |
| Block force-pushes | yes |
| Block branch deletion | yes |
| Enforce for administrators | yes |

**Required status checks** are pinned to the always-on jobs in
`.github/workflows/ci.yml`. GitHub keys required checks on the job **name**
(not the job id), so these strings must match exactly:

- `python (lint + types + tests)`
- `web (lint + build)`
- `security (sast + deps + secrets + sbom + matrix)`
- `secrets-config (fail-closed preflight)`
- `compose (config validation)`
- `build (images + sbom + smoke)`

> The Helm jobs in `.github/workflows/helm-chart.yml` are **intentionally
> excluded**. They are path-filtered to `deploy/helm/**`, so requiring them would
> leave every non-Helm PR stuck waiting on a check that never runs. Keep this
> list in sync with the CI job names if a job is renamed, added, or removed —
> the list lives in `REQUIRED_CHECKS` at the top of the script.

### Secret scanning

- **Secret scanning** enabled.
- **Push protection** enabled (blocks commits that contain detected secrets).

This is free on public repositories. On a private repository it requires GitHub
Advanced Security; if that is unavailable the script warns and continues rather
than aborting the rest of the hardening. This complements the repo's existing
`gitleaks` CI gate (`.gitleaks.toml`).

### Dependabot

- **Vulnerability alerts** enabled.
- **Automated security updates** enabled (Dependabot opens PRs to patch
  vulnerable dependencies across the Python `uv` workspace and the `apps/web`
  pnpm workspace).

### Merge policy

- **Squash merge** is the **only** allowed merge method (merge commits and
  rebase merges are disabled) — this keeps `main` linear, matching the
  `Require linear history` protection above.
- **Auto-delete head branches** on merge.

## Idempotency

Every operation is a full-replace `PUT` (branch protection) or a `PATCH` /
enable-`PUT` (repo settings, secret scanning, Dependabot). Re-running the script
simply reconverges to the same state — use it to re-assert the baseline whenever
someone changes a setting by hand in the GitHub UI.

## Notes

- Forge is Apache-2.0 licensed. All settings applied here are non-destructive
  repository configuration; the script never touches code, history, or secrets.
- No secrets are read or written. Authentication is delegated entirely to `gh`.
