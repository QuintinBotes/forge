.DEFAULT_GOAL := help
.PHONY: help setup install dev dev-up dev-down dev-logs dev-seed test lint fmt typecheck migrate seed build clean

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
	forge_worker forge_mcp_gateway

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

clean: ## Remove python caches and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build
