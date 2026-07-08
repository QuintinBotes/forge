.DEFAULT_GOAL := help
.PHONY: help setup install dev dev-up dev-down dev-logs dev-seed test lint fmt typecheck migrate seed build clean \
	compose-build build-images pin-digests sbom smoke security load-smoke perf soak \
	bump changelog hooks release-readiness source-sbom

# Every typed first-party package/app, one mypy module each. mypy runs in
# *module mode* (``-p``) so each ``forge_*`` package resolves to its single
# installed location, avoiding the "Source file found twice under different
# module names" ambiguity that ``mypy packages apps`` hits in this uv workspace
# (every package dir is on ``sys.path`` via its editable install, so directory
# mode maps the same file to both ``forge_x`` and ``packages.x.forge_x``).
MYPY_PACKAGES := \
	forge_contracts forge_db forge_workflow forge_agent forge_coordinator \
	forge_spec forge_board forge_knowledge forge_integrations forge_mcp \
	forge_policy forge_authz forge_skill forge_eval forge_approval forge_api \
	forge_worker forge_mcp_gateway forge_orchestration_policy

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: install ## Install all deps (python + node), then prepare local env
	@echo "Setup complete. Start Postgres, then run 'make migrate' and 'make seed'."

install: ## Resolve and install python (uv) and node (pnpm) dependencies
	uv sync
	pnpm install

dev: dev-up ## Bring up the whole dev stack (alias for dev-up)

dev-up: ## Build + start the whole dev stack, migrate, seed, wait for healthy
	scripts/dev.sh up

dev-down: ## Stop and remove the dev stack (named volumes are kept)
	scripts/dev.sh down

dev-logs: ## Follow logs for the dev stack (make dev-logs svc=api -> one service)
	scripts/dev.sh logs $(svc)

dev-seed: ## Re-run the idempotent demo-workspace seed against the dev stack
	scripts/dev.sh seed

test: ## Run the python test suite
	uv run pytest

lint: ## Lint python sources with ruff (check + format verification)
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-format and auto-fix python sources with ruff
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Static type-check python packages and apps with mypy
	uv run mypy $(addprefix -p ,$(MYPY_PACKAGES))

migrate: ## Apply database migrations (alembic upgrade head)
	uv run alembic -c packages/db/alembic.ini upgrade head

seed: ## Seed a demo workspace
	uv run python -m forge_api.scripts.seed

build: ## Build web assets and python wheels
	pnpm -r build
	uv build --all-packages

compose-build: ## Build all 4 first-party production images (docker compose build)
	docker compose -f deploy/docker-compose.yml build

build-images: compose-build ## Alias of compose-build (HARD-07 G-BUILD)

pin-digests: ## Resolve + rewrite @sha256 digests, write deploy/build-manifest.json
	deploy/scripts/pin-digests.sh

sbom: ## Generate a CycloneDX SBOM per built image (deploy/sbom/<image>.cdx.json)
	deploy/scripts/sbom.sh

smoke: ## Production-compose smoke: up -> healthy -> /health -> down -v
	deploy/scripts/smoke.sh

security: ## HARD-09 security audit roll-up (SAST + deps + secrets + SBOM + matrix)
	scripts/security/run.sh

load-smoke: ## HARD-11 non-blocking k6 API load smoke (needs k6 + a running API)
	@command -v k6 >/dev/null 2>&1 || { echo "k6 not installed — see docs/self-hosting/performance.md"; exit 0; }
	k6 run -e SMOKE=1 -e BASE_URL=$${FORGE_LOAD_BASE_URL:-http://localhost:8000} deploy/load/k6/api_hotpaths.js

perf: ## HARD-11 retrieval-latency bench (resourced runner; writes deploy/load/reports)
	FORGE_RUN_PERF=1 uv run pytest -m perf packages/evaluation -q

soak: ## HARD-11 bounded multi-tenant soak (resourced runner)
	FORGE_RUN_SOAK=1 uv run pytest -m soak packages/evaluation -q

clean: ## Remove python caches and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build

# --------------------------------------------------------------------------- #
# HARD-12 — Release engineering                                               #
# --------------------------------------------------------------------------- #
bump: ## Cut a release: compute next SemVer, bump every version file, changelog + tag
	uv run cz bump

changelog: ## (Re)generate CHANGELOG.md from the conventional-commit history
	uv run cz changelog

hooks: ## Install the commit-msg hook that enforces Conventional Commits (cz check)
	@mkdir -p .git/hooks
	@printf '#!/usr/bin/env sh\nexec uv run cz check --commit-msg-file "$$1"\n' > .git/hooks/commit-msg
	@chmod +x .git/hooks/commit-msg
	@echo "Installed .git/hooks/commit-msg (uv run cz check)."

release-readiness: ## Run the automated RELEASE_READINESS gate at the PRODUCTION bar
	uv run forge-release-readiness --bar production

source-sbom: ## Generate the source-tree CycloneDX SBOM (release/sbom/forge-source.cdx.json)
	release/scripts/source-sbom.sh
