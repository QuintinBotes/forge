# Makefile — convenience wrappers around deploy.sh for the fly-agents module.
#
# Scoped to this module only (not the repo-root Makefile). Usage:
#   make -C infra/modules/fly-agents deploy ENV=prod ORG=my-org
#
# FLY_API_TOKEN must be exported by the caller for deploy/scale/regions/status.
# ORG is optional — deploy.sh falls back to scaling.env's FLY_ORG (which
# mirrors variables.tf's `fly_org`) when unset.

ENV ?= dev
ORG ?=
ORG_FLAG := $(if $(ORG),--org $(ORG),)

.PHONY: deploy scale regions status render help

help:
	@./deploy.sh --help

deploy:
	@./deploy.sh deploy --env $(ENV) $(ORG_FLAG)

scale:
	@./deploy.sh scale --env $(ENV)

regions:
	@./deploy.sh regions --env $(ENV)

status:
	@./deploy.sh status --env $(ENV)

render:
	@./deploy.sh render --env $(ENV)
