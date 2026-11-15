# The rename from Forge to Swarm Code Factory

On 2026-11-15 the project was renamed from "Forge" to **Swarm Code Factory** (SCF), because
other software-factory projects already use the name Forge. This page is for someone who runs
a deployment or builds on the code. It says what changed, what did not, and what you must
update by hand.

The rename changed names in the source tree, and moved the default published image location
(see [Images](#if-you-run-a-deployment)). It changed no other behaviour, and every name that
a running deployment, stored data or another system depends on was kept. **Once the images are
published at the new location, an existing deployment keeps working with its current `.env`,
database, secrets and dashboards.**

The rename was done by `scripts/rename_project.py`. Every place the old name remains is listed,
with its reason, in `tests/rename_allowlist.toml`, and `tests/test_rename_allowlist.py` fails
if the old name appears anywhere else.

## What changed

| Thing | Before | After |
|---|---|---|
| Product name | Forge | Swarm Code Factory (SCF) |
| Repository | `QuintinBotes/forge` | `QuintinBotes/swarm-code-factory` |
| Python import packages (25) | `forge_api`, `forge_worker`, `forge_contracts`, ... | `scf_api`, `scf_worker`, `scf_contracts`, ... |
| Python distributions | `forge-api`, `forge-db`, ... | `scf-api`, `scf-db`, ... |
| Root project and root web package | `forge` | `swarm-code-factory` |
| Console scripts | `forge-cli`, `forge-verify`, `forge-replay`, `forge-release-readiness` | `scf-cli`, `scf-verify`, `scf-replay`, `scf-release-readiness` |
| Web workspace package | `@forge/web` | `@scf/web` |
| Locally built images | `forge/api`, `forge/worker`, `forge/web`, `forge/mcp-gateway`, `forge/mcp-reference` | `swarm-code-factory/api`, ... |
| Helm chart | `deploy/helm/forge`, chart name `forge` | `deploy/helm/swarm-code-factory`, chart name `swarm-code-factory` |
| Files | `docs/FORGE_SPEC.md`, `.semgrep/forge.yml`, `deploy/nginx/forge.conf` | `docs/SCF_SPEC.md`, `.semgrep/swarm-code-factory.yml`, `deploy/nginx/swarm-code-factory.conf` |
| Published images | `ghcr.io/forge-platform/forge-api`, `.../forge-sandbox-python`, ... | `ghcr.io/quintinbotes/swarm-code-factory/api`, `.../sandbox-python`, ... |
| Helm image defaults | `image.registry: ghcr.io/forge-platform`, `repository: forge-api` | `image.registry: ghcr.io/quintinbotes/swarm-code-factory`, `repository: api` |
| Sandbox image defaults (`FORGE_SANDBOX_IMAGE_*`) | `ghcr.io/forge-platform/forge-sandbox-{python,node,go}:0.1.0` | `ghcr.io/quintinbotes/swarm-code-factory/sandbox-{python,node,go}:0.1.0` |
| Locally built sandbox base | `forge-sandbox-base` | `swarm-code-factory/sandbox-base` |
| Other repository addresses | `github.com/forge-platform/forge`, `service-hive/forge`, `<org>/forge` | `QuintinBotes/swarm-code-factory`, `<org>/swarm-code-factory` |
| Command spellings in help and docs | `forge bench`, `forge cost`, `forge marketplace`, `forge policy`, `forge spec ...` | `scf bench`, `scf cost`, `scf marketplace`, `scf policy`, `scf spec ...`; `forge verify` and `forge replay` are `scf-verify` and `scf-replay` |
| Class and type names (9) | `ForgeApiClient`, `ForgeError`, `ForgeModel`, `ForgeTask`, `ForgeTaskPatch`, `ForgePriority`, `ForgeMetrics`, `ForgeReliableTask`, `ForgeVersionIncompatible` | `ScfApiClient`, `ScfError`, `ScfModel`, ... |
| Internal identifiers | `forge_task`, `forge_content_hash`, `get_by_forge_task`, `default_forge_status`, ... | `scf_task`, `scf_content_hash`, `get_by_scf_task`, `default_scf_status`, ... |
| Copyright holder | The Forge Authors | The Swarm Code Factory Authors (`LICENSE`, package metadata, chart maintainers; the licence text is unchanged) |
| Model-facing prompts | "You are a Forge execution agent", "You are Forge's spec author", "Forge is a software factory" | name Swarm Code Factory |
| Logo | anvil mark, "Forge" wordmark, `apps/web/public/forge-*.svg`, favicon | removed; the sidebar shows a plain-text SCF mark, and there is no favicon until new artwork exists |

The old console script names are still installed as extra entry points, so `forge-cli ...` in
an existing script or cron job still runs.

These change as a side effect:

- **Module paths.** `celery -A forge_worker` is now `celery -A scf_worker`, and
  `python -m forge_api...` is now `python -m scf_api...`. The shipped Compose files, Dockerfiles
  and Helm chart already use the new paths.
- **Logger names.** Loggers named after a module now start with `scf_` (for example
  `scf_api.routers.agent`). Log field names did not change.
- **Python names.** Code that imports the renamed classes (`ForgeError`, `ForgeTask`, ...) or
  calls `forge_content_hash`, or passes `forge_task=` / `forge_project_id=` as a keyword to a
  PM adapter or the sync engine, must use the `Scf*` / `scf_*` names. Fields that are stored or
  sent over the wire kept their names; see below.
- **Prompts.** The agent system prompt, the spec-author prompt and the MCP server
  instructions now name Swarm Code Factory, so model input differs from a run recorded
  before the rename. Replaying such a recording (`scf-replay`) can report a divergence on the
  first model call.

## What did not change

These names are read by systems outside the repository, or are stored in data that already
exists. Renaming any of them would change how an existing deployment behaves, so they are
kept, and a later change will need a migration or a staged rollout.

**Configuration**

- Every `FORGE_*` environment variable, in `.env` files and CI secrets.
- The Compose defaults for the database name, user and volumes (`forge`, `forge_repos`,
  `forge-specs`), the paths inside the images (`/srv/forge`, `/etc/forge`, `/var/lib/forge`)
  and the unix user `forge`.
- The Helm values keys (`forge.domain`, `forge.publicUrl`, ...), and the names of the objects
  the chart renders. See [Helm](#helm) below.

**Stored data**

- Database tables, columns, indexes, constraints and enum values, including columns such as
  `forge_task_id`. Alembic revision identifiers and existing migrations: only their first-party
  import lines changed, and the revision chain is the same.
- Prefixes of issued credentials (`forge_system_`, `forge_svc_`, `forge_pat_`, `forge_scim_`, ...)
  and the key-derivation labels behind stored secrets.
- Marketplace manifest fields (`min_forge_version`) and the `forge-package.yaml` file name
  inside signed packages. The `forge-swe` benchmark suite and the `forge-official` registry slug.
- Fields that are persisted or on the wire, and the Python attributes that carry them (none has
  a serialisation alias): `forge_task_id`, `forge_version`, `min_forge_version`,
  `forge_version_at_sync`, the `forge_version_incompatible` error code, the `forge_wins`
  conflict policy, the `forge_run_marker` webhook payload key.
- Terraform variables (`forge_repo_url`, `forge_repo_ref`), Docker volume, network and secret
  names (`forge_repos`, `forge_sandbox_egress`, `forge_secret_key`), database trigger and
  function names, and the Prometheus alert names (`ForgeApiScrapeDown`, ...).

**Signed and hashed material**

- The attestation predicate type (`https://forge.dev/attestations/changeset/v1`) and statement
  fields. An attestation signed before the rename verifies after it.
- Audit-chain fields and run-event payload values. An exported audit chain still verifies.
- The session JWT audience and issuer (`forge-api`, `forge-web`).

**Wire names**

- HTTP routes, the `X-Forge-Signature` and `X-Forge-PM-Secret` headers, the `forge_session`
  cookie and the `forge.*` browser storage keys.
- MCP tool names and the MCP server and client names.
- Webhook payload fields, the `/forge` Slack command and the `Forge-Run:` commit trailer.
- ctxbench: the `forge` arm (`--arms none,forge`), the MCP server name `forge` and the
  `mcp__forge__*` tool names the Claude CLI derives from it, the `forge_sha` and
  `forge_dirty` keys of a run manifest, and the problem strings that name them
  (`mcp server forge failed`, `forge tool error: ...`). Stored runs stay comparable.
- The command spellings the tervin integration reads: the planned `forge acp` command and the
  JSON CLI mode (`forge-cli --json`, also installed as `scf-cli --json`).

**Names stored in other systems**

- The `forge:run` issue label and the `forge:run:<id>` comment marker.
- `.forge/policy.yaml` and `.forge/policy.tests.yaml` in governed repositories.
- Git branch prefixes (`forge/...`).
- Celery task names and the default queue (`forge.*`, `forge`), Temporal workflow, activity
  and task-queue names.
- Metric names (`forge_*`), recording rules (`forge:*`), alert names, Prometheus job names and
  the OpenTelemetry `service.name` values (`forge-api`, `forge-worker`, `forge-mcp-gateway`).
  Existing dashboards and alert rules keep working.
- Semgrep rule ids (`forge-no-*`).
- Agent role identifiers, skill and line definitions, and everything under `examples/`.

**History**

- `CHANGELOG.md`, the ADRs and the dated specs and plans under `docs/superpowers/` describe the
  project as it was named then. The changelog and the roadmap each carry one line noting the
  rename.

## What you must update by hand

**If you run a deployment**

1. **Git remote.** After the repository is renamed, GitHub redirects the old address, but
   update it: `git remote set-url origin https://github.com/QuintinBotes/swarm-code-factory.git`.
2. **Rebuild images.** The Compose files now build and reference `swarm-code-factory/<service>`.
   Run your usual build and `up`; the old `forge/<service>` images can then be removed. If you
   pin digests, regenerate `deploy/build-manifest.json` with `deploy/scripts/pin-digests.sh`.
3. **Your own commands.** Anything of yours that calls `celery -A forge_worker`,
   `python -m forge_*` or imports a `forge_*` package must use `scf_*`. Console scripts may
   keep the old names.
4. **Images.** The default published images moved to `ghcr.io/quintinbotes/swarm-code-factory`
   (`.../api`, `.../worker`, `.../web`, `.../mcp-gateway`, `.../sandbox-python`,
   `.../sandbox-node`, `.../sandbox-go`). If your `.env` sets `FORGE_SANDBOX_IMAGE_*` or
   `FORGE_SANDBOX_ALLOWED_IMAGES`, or your Helm values set `image.registry` or a workload's
   `image.repository`, either keep your values (they still work while the old images exist) or
   move them to the new names. A Helm values file that sets only `image.registry` to the old
   namespace must also set each `image.repository` back to `forge-api`, ..., because the
   defaults are now `api`, `worker`, `web` and `mcp-gateway`. Upgrade only after the new
   images are published.
5. **Mounted file paths.** If you mount `deploy/nginx/forge.conf` or point a tool at
   `.semgrep/forge.yml`, use the new file names.
6. **Log queries.** Queries or alerts that filter on a logger name starting with `forge_`
   need `scf_`.

Leave your `.env`, your database, your GitHub secrets and your dashboards as they are.

### Helm

The chart directory and chart name changed. The objects it renders did not: `values.yaml` now
sets `nameOverride: forge`, so object names and the immutable selector labels are the same as
before, and `helm upgrade <release> deploy/helm/swarm-code-factory` upgrades an existing
release in place. Keep your release name and namespace. The only rendered difference is the
informational `helm.sh/chart` label. If you override `nameOverride` yourself, keep your value.

**If you maintain the project**

1. **Rename the repository** to `QuintinBotes/swarm-code-factory` once this change is on
   `main` and CI is green. Update branch protection, required check names and any webhook or
   GitHub App that names the repository.
2. **GitHub secrets and variables.** None need renaming: the `FORGE_*` names are kept.
3. **Container registry.** The defaults now point at `ghcr.io/quintinbotes/swarm-code-factory`.
   Publish every image there (`api`, `worker`, `web`, `mcp-gateway`, `sandbox-python`,
   `sandbox-node`, `sandbox-go`) before this change reaches a release, pin the production
   digests in `values-production.yaml`, and keep the old `ghcr.io/forge-platform` images
   available until deployments have moved.
4. **Published packages.** The distributions are now `scf-*` and the web package is
   `@scf/web`. If any `forge-*` distribution was ever published to a package index, publish
   the new name there and deprecate the old one.
5. **CODEOWNERS.** `.github/CODEOWNERS` names `@service-hive/*` teams; the repository
   addresses now point at `QuintinBotes/swarm-code-factory`, but the team handles were not
   changed. Point them at teams that exist in the new owner.
6. **Generated artifacts.** Regenerate the SBOMs at the next release (`make source-sbom`,
   `make sbom`); `release/sbom/forge-source.cdx.json` keeps its name until then. The recorded
   scanner output and SBOM under `docs/security/evidence/` are snapshots that still list the
   old distribution names; re-run `scripts/security/run.sh` to refresh them.
7. **Brand assets.** The anvil mark, wordmark and favicon were removed; the UI shows a
   plain-text SCF mark. Commission new artwork, then add a favicon and update `docs/brand/`.
8. **External dashboards and alerting.** Nothing to change now. When metric names are renamed
   in a later pass, dashboards, alert rules and recording rules change with them.
9. **The tervin integration.** Tervin is a separate repository and was not touched. The names
   the bridge spec (`specs/006-tervin-bridge`) gives it are all unchanged: the `forge acp`
   command spelling, the `TERVIN_FORGE_TOKEN` environment variable, MCP tool names,
   `forge-cli --json` (`scf-cli` is also installed) and the run-event stream. Check tervin for
   any `forge_*` Python import, `Forge*` class import, `forge/<service>` or
   `ghcr.io/forge-platform` image name, and update its documentation to the new product name.
   The MCP server instructions now say "Swarm Code Factory"; if tervin matches that text,
   update it.
10. **Open issues and branches.** Open roadmap issues and in-flight branches name `forge_*`
    paths. Re-run `uv run python scripts/rename_project.py` on a branch after rebasing it.
11. **Diagrams and prose.** Framed ASCII diagrams were renamed and re-padded by the script;
    check that they still read well. Lower-case "forge" in comments, docstrings and help text
    was renamed where it names the product; the English verb was left alone.
