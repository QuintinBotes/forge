# Contributing to Swarm Code Factory

Thanks for helping build SCF! This guide covers local setup, the development
workflow, and the conventions we hold PRs to. By participating you agree to
follow our [Code of Conduct](./CODE_OF_CONDUCT.md).

SCF is licensed under [Apache-2.0](./LICENSE). Contributions are accepted
under the same license, and each commit must carry a
[Developer Certificate of Origin](#developer-certificate-of-origin-dco)
sign-off.

## Table of contents

- [Ways to contribute](#ways-to-contribute)
- [Prerequisites](#prerequisites)
- [Development setup](#development-setup)
- [Running the local stack](#running-the-local-stack)
- [Tests, linting, and the green gate](#tests-linting-and-the-green-gate)
- [Development workflow (trunk-based)](#development-workflow-trunk-based)
- [Commit conventions](#commit-conventions)
- [Developer Certificate of Origin (DCO)](#developer-certificate-of-origin-dco)
- [Reporting bugs and security issues](#reporting-bugs-and-security-issues)

## Ways to contribute

- File clear, reproducible bug reports.
- Improve documentation (the `docs/` tree and inline docstrings).
- Add or fix a feature via a pull request.
- Add examples under [`examples/`](./examples) — every file there is executed as
  a test fixture, so an example can never silently drift from the schema.

If you are planning a non-trivial change, please open an issue to discuss it
first so we can agree on the approach before you invest the time.

## Prerequisites

- **Python 3.14** — the pinned version lives in [`.python-version`](./.python-version).
- **[uv](https://docs.astral.sh/uv/)** — manages the Python workspace and virtualenv.
- **Node 22+** and **[pnpm](https://pnpm.io/) 10** — for the `apps/web` frontend.
- **Docker Engine 24+** with the **Docker Compose v2** plugin — to run the stack.
- **`make`** — the task runner entrypoint (see `make help`).

`uv` can install the right Python for you (`uv python install 3.14`).

## Development setup

```bash
git clone https://github.com/<org>/swarm-code-factory.git
cd swarm-code-factory
cp .env.example .env     # fill in local secrets; see the inline docs in .env.example
make setup               # uv sync + pnpm install
```

`make setup` installs the Python workspace (all `scf_*` packages, editable)
and the Node dependencies for `apps/web`. The Python workspace is defined in the
root [`pyproject.toml`](./pyproject.toml); the web app is a separate `pnpm`
workspace ([`pnpm-workspace.yaml`](./pnpm-workspace.yaml)).

## Running the local stack

The whole stack runs in Docker Compose. The one-command path builds the images,
starts every service, applies migrations, seeds a demo workspace, and waits for
health:

```bash
make dev        # docker compose -f deploy/docker-compose.dev.yml, migrate + seed + healthcheck
```

- **SCF (the Caddy edge — use this):** <http://localhost:8080>
- API health check: <http://localhost:8080/api/health>

Caddy serves the UI and `/api/*` from one origin, so the browser needs no CORS
and everything resolves. The web (3000) and API (8000) ports are published for
debugging only — a browser pointed at 3000 has no `/api` to reach.

`make dev` also prints a one-time **admin API key**; paste it into the UI's
Connect dialog. Every API route is authenticated and there is no password login
yet, so this is how you get in. `scripts/dev.sh seed` prints a fresh one.

Useful stack commands:

```bash
make dev-logs               # follow all logs (make dev-logs svc=api for one service)
make dev-seed               # re-run the idempotent demo-workspace seed
make dev-down               # stop the stack (named volumes are kept)
```

If you prefer to run pieces on the host, you can drive the database directly:

```bash
make migrate     # alembic upgrade head
make seed        # seed a demo workspace
```

To validate the **production** compose stack locally, use the digest-pinned
production file and the smoke target:

```bash
docker compose -f deploy/docker-compose.yml config --quiet   # what CI validates
make smoke                                                   # up -> healthy -> /health -> down -v
```

## Tests, linting, and the green gate

CI is the source of truth, and it must be green before a PR can merge. Run the
same checks locally.

**Python** (from the repo root):

```bash
make test        # uv run pytest
make lint        # ruff check . + ruff format --check .
make typecheck   # mypy (module-mode, per the Makefile package list)
make fmt         # auto-format + auto-fix before you push
```

Some Python tests need a live pgvector-enabled Postgres or a Redis (CI runs them
against real `pgvector/pgvector:pg16` and `redis:7.4-alpine` services). Postgres
tests skip, with the reason, when `FORGE_TEST_DATABASE_URL` is unset, but error
when it is set and the server is down. Redis tests skip, with the reason, when
the configured Redis does not answer a ping. With Docker you can run them
locally too:

```bash
make test-services-up    # scripts/test-services.sh up: create/start forge-test-pg + forge-test-redis
make test-full           # start the services, export the two URLs, run uv run pytest
make test-services-down  # stop and remove the two containers
```

The services listen on loopback only: Postgres on `127.0.0.1:5432` and Redis on
`127.0.0.1:6379`. Set `SCF_TEST_PG_PORT` or `SCF_TEST_REDIS_PORT` when a port is
taken. `eval "$(scripts/test-services.sh env)"` exports `FORGE_TEST_DATABASE_URL`
and `FORGE_REDIS_URL` for a plain `uv run pytest`; `scripts/test-services.sh status`
shows whether each container is running.

**The test fixtures drop tables.** Point `FORGE_TEST_DATABASE_URL` only at this
disposable test database, never at one whose data you want to keep.

Tests are marked (`postgres`, `integration`, `docker`, `security`,
`kind`, `gvisor`, `firecracker`) so you can select or skip tiers, e.g.
`uv run pytest -m security`. On a machine with only Docker, these lanes still
skip after `make test-full`, and you opt in to each:

- `docker` (real sandbox containers): `FORGE_SANDBOX_DOCKER_TESTS=1` and a
  curated `FORGE_SANDBOX_IMAGE_PYTHON`.
- `kind` (the Helm chart on a kind cluster): `kind`, `helm` and `kubectl` on
  `PATH` and the images from `make build-images`; `FORGE_KIND_CLUSTER` reuses an
  existing cluster.
- `gvisor` and `firecracker` (kernel-boundary sandboxes): the docker opt-in
  above, plus the `runsc` or `kata-fc` runtime registered with the daemon
  (`firecracker` also needs `/dev/kvm`).
- `live_*` (real external services, for example `live_github`, `live_model`,
  `live_mcp`): the credentials or switch named in each marker's description in
  `pyproject.toml` (`uv run pytest --markers`).

**Web** (`apps/web`):

```bash
pnpm --filter @scf/web lint
pnpm --filter @scf/web build
pnpm --filter @scf/web test
```

**Before opening a PR**, make sure:

- `uv run ruff check .` is clean and `ruff format --check` passes;
- `make typecheck` passes;
- `uv run pytest` passes;
- if you touched `apps/web`, its lint + build + test pass;
- no secrets are committed — a `gitleaks` gate runs in CI and blocks on hits.

We practise test-driven development: add a failing test, implement, then confirm
the green gate. New behaviour should ship with tests.

## Development workflow (trunk-based)

SCF uses **trunk-based development** against a single long-lived branch,
`main`. `main` is always releasable and protected.

1. **Branch short-lived** off up-to-date `main`. Keep branches small and merge
   them quickly — hours to a couple of days, not weeks. Suggested naming:
   `feat/<slug>`, `fix/<slug>`, `docs/<slug>`, `chore/<slug>`.
   ```bash
   git switch main && git pull --ff-only
   git switch -c feat/my-change
   ```
2. **Commit** in small, logical steps using [Conventional Commits](#commit-conventions)
   and a DCO sign-off (`git commit -s`).
3. **Open a pull request** into `main`. Fill in what changed and why, and link
   any related issue. Keep PRs focused and reviewable.
4. **Pass CI + get one review.** A PR merges only when:
   - all required CI jobs are green (Python lint/types/tests, web lint/build,
     the security gate, secrets-config preflight, and compose validation), and
   - it has **at least one approving review**.
5. **Squash-merge to `main`.** We squash so `main` keeps one clean, conventional
   commit per change. Make the squash commit's title a valid Conventional Commit
   and keep its body carrying the DCO sign-off. Delete the branch after merge.

Rebase your branch on `main` rather than merging `main` into it, to keep history
linear and your diff honest.

## Commit conventions

We use [Conventional Commits](https://www.conventionalcommits.org/). The subject
is `type(scope): summary`, imperative mood, lower-case, no trailing period.

Common types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`,
`ci`, `chore`. Scope is optional but encouraged (a package, app, or feature id),
for example:

```
feat(knowledge): add RRF fusion tie-breaking
fix(api): reject expired agent tokens on the wired path
docs: polish the self-host quickstart
```

The conventional-commit format is **enforced**, not just recommended: the
changelog and the SemVer bump are derived from it. Install the local guard once:

```bash
make hooks        # installs a commit-msg hook running `uv run cz check`
```

You can validate a message by hand with `uv run cz check --commit-msg-file <file>`,
and CI runs `cz check` over the PR commit range. Cutting a release is a single
command for maintainers — `make bump` (`uv run cz bump`) computes the next SemVer
from the commit history, rewrites the version in every package in lockstep, and
regenerates [`CHANGELOG.md`](./CHANGELOG.md). See `release/` and
[`.github/workflows/release.yml`](.github/workflows/release.yml).

## Developer Certificate of Origin (DCO)

SCF uses the [Developer Certificate of Origin](https://developercertificate.org/)
instead of a CLA. It is a lightweight statement that you wrote the patch or
otherwise have the right to submit it under the project's license.

**Every commit must be signed off.** Add the sign-off automatically with the
`-s` / `--signoff` flag:

```bash
git commit -s -m "feat(board): add column reordering"
```

This appends a trailer to the commit message with your real name and email:

```
Signed-off-by: Jane Developer <jane@example.com>
```

Configure your identity once with `git config user.name` and
`git config user.email` so the sign-off is accurate. Forgot to sign off? Amend
the last commit with `git commit --amend -s` (or `git rebase --signoff` for a
range), then force-push your branch.

## Reporting bugs and security issues

- **Bugs / features:** open a GitHub issue with a minimal reproduction, the
  affected component, and the version or commit.
- **Security vulnerabilities:** please do **not** open a public issue. Follow
  the private reporting process in [SECURITY.md](./SECURITY.md).

Thanks again for contributing to SCF.
