# ==========================================================================
# OnTrak — operator workflow
# Usage: make <target>   (see `make help`)
# ==========================================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python

COMPOSE := docker compose
IMAGE := $(if $(ONTRAK_IMAGE),$(ONTRAK_IMAGE),ontrak:local)
# The base stack is the single-host lab, and `lab-setup` makes it work on a
# first run: secrets, then Incus on the host. `docker-compose.remote.yml` is for
# the deployments that have no host hypervisor to prepare (a remote cluster, or
# demo mode) — see docs/docker.md.
REMOTE_OVERLAY := -f docker-compose.yml -f docker-compose.remote.yml

.PHONY: help setup secrets check doctor validate test lint demo \
        catalog catalog-validate media-status media-fetch generate schedule \
        templates pool reap demo-serve host-image landing \
        installer-iso installer-iso-smoke installer-iso-test \
        build up up-remote down logs ps exec check-compose setup-log \
        docker-demo docker-shell provision provision-plan console-recreate

help: ## Show this help message
	@echo "OnTrak — operator workflow"
	@echo "Usage: make <target>"
	@grep -E '^[a-zA-Z_:-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

## ---- Bootstrap ----------------------------------------------------------

setup: ## Create the venv, install dependencies, install guard hooks, seed .env
	bash scripts/setup.sh

secrets: ## Create .env from .env.example and fill in the generated local secrets
	bash scripts/secrets.sh

check: ## Preflight: Python, Incus, KVM, storage and secrets
	$(PY) -m ontrak doctor

doctor: check ## Alias for `check`

## ---- Installer ISO (bare metal → range host) ---------------------------
# Builds a bootable Ubuntu 24.04 image that installs the host and then provisions
# itself on first boot: Incus, the lab, the OnTrak checkout and the portal stack.
# The operator answers one screen (identity). See docs/installer.md.

installer-iso: ## Build the bootable range-host installer ISO
	bash infra/build-installer-iso.sh

installer-iso-smoke: ## Build the installer ISO, then boot it in QEMU to prove it installs
	ONTRAK_ISO_SMOKE=1 bash infra/build-installer-iso.sh

installer-iso-test: ## Install a machine from the built ISO in QEMU, then check it over SSH
	@iso="$$(ls -t dist/*.iso 2>/dev/null | head -1)"; \
	test -n "$$iso" || { echo "no ISO in dist/ — run 'make installer-iso' first"; exit 2; }; \
	bash infra/installer/install-test.sh "$$iso"

## ---- Docker stack (portal + console gateway) ----------------------------

build: ## Build the portal image
	$(COMPOSE) build

## ---- Trust: DNS + TLS + edge through Cerulean ---------------------------
# This repo owns no nameserver and no CA. Cerulean does (docs/stack.md), and
# these two targets are the only supported way to put OnTrak's names on the
# estate: the plan writes nothing, and --apply is idempotent, so a re-run after
# a backend move is the same command. Needs CERULEAN_API_TOKEN (a ceru_ service
# key with the scopes domains, dns, certs, npm).

provision-plan: ## Show what Cerulean would change for ontrak's names (writes nothing)
	$(PY) scripts/cerulean-provision.py

provision: ## Provision DNS, certificates and edge hosts for OnTrak through Cerulean
	$(PY) scripts/cerulean-provision.py --apply

up: secrets ## One command: first-run setup (secrets, then Incus on the host), start the stack
	@# --build so a checkout that was just pulled does not silently run the
	@# image from an earlier commit; the cache makes this a second when nothing
	@# changed. Secrets are handled twice on purpose: here so an interrupted
	@# first run still leaves a usable .env, and inside lab-setup so that a bare
	@# `docker compose up` — with no .env at all — works the same way.
	$(COMPOSE) up -d --build
	@echo "==> portal    http://localhost:$${ONTRAK_PORTAL__PORT:-8080}"
	@echo "==> console   http://localhost:$${ONTRAK_PORTAL__PORT:-8080}/guacamole/"
	@echo "==> sign in   through Authentik (set the ONTRAK_PORTAL__OIDC_* values in .env)"
	@echo "==> first run make setup-log   lab health: make exec ARGS=doctor"

up-remote: secrets ## Start the stack with no host hypervisor (remote cluster / demo)
	$(COMPOSE) $(REMOTE_OVERLAY) up -d
	@echo "==> started without a host hypervisor — see docs/docker.md § remote"

setup-log: ## Show what the first-run lab setup did (secrets, Incus on the host)
	$(COMPOSE) logs lab-setup

console-recreate: ## Recreate just the console gateway, so its JSON_SECRET_KEY matches .env
	@# The failure this fixes is silent: a gateway created before the key existed (or
	@# with an older one) refuses every console link with "Permission denied", the
	@# iframe never opens, and both containers still report healthy. `make check`
	@# reports it — this is the one-value fix, without restarting the portal mid-class.
	$(COMPOSE) up -d --force-recreate guacamole
	@echo "==> recreated; now confirm: make check   (or: make exec ARGS=doctor)"

down: ## Stop the stack (keeps the state, media and secrets volumes)
	$(COMPOSE) down

logs: ## Follow the stack logs
	$(COMPOSE) logs -f

ps: ## Show stack containers and their health (including the one-shot first-run setup)
	$(COMPOSE) ps -a

check-compose: ## Validate the compose files, their env interpolation and the first-run contract
	@bash scripts/secrets.sh .env.compose-check >/dev/null
	$(COMPOSE) --env-file .env.compose-check config --quiet
	$(COMPOSE) --env-file .env.compose-check $(REMOTE_OVERLAY) config --quiet
	@rm -f .env.compose-check
	$(PY) scripts/check-first-run-contract.py
	@echo "==> compose files are valid"

exec: ## Run a CLI command inside the running portal (make exec ARGS="user list")
	@test -n "$(ARGS)" || { echo "usage: make exec ARGS=\"catalog list\""; exit 2; }
	$(COMPOSE) exec portal python3 -m ontrak $(ARGS)

docker-demo: build ## Run a whole class inside the image, with no hypervisor at all
	docker run --rm $(IMAGE) demo run --students 6

docker-shell: build ## Open a shell in the image (for `ontrak catalog list`, debugging, …)
	docker run --rm -it --entrypoint /bin/bash $(IMAGE)

## ---- Development --------------------------------------------------------

test: ## Run the test suite
	$(PY) -m pytest -q

lint: ## Lint the Python tree
	$(PY) -m ruff check ontrak tests

validate: ## Validate every scenario and the whole workload catalog
	$(PY) -m ontrak scenario validate
	$(PY) -m ontrak catalog validate

## ---- Demo (no hypervisor required) --------------------------------------

demo: ## Run a full class through the portal-free demo (in-memory Incus)
	$(PY) -m ontrak demo run --students 6

demo-serve: ## Run the portal in demo mode (no Incus, no Windows, no secrets)
	$(PY) -m ontrak demo serve

## ---- Workload catalog ---------------------------------------------------

catalog: ## List the catalog (see also: catalog-show, catalog-plan)
	$(PY) -m ontrak catalog list
	$(PY) -m ontrak catalog groups

catalog-validate: ## Validate every catalog manifest
	$(PY) -m ontrak catalog validate

media-status: ## Show which installation media is present, fetchable or operator-supplied
	$(PY) -m ontrak media status
	$(PY) -m ontrak media missing

media-fetch: ## Download the freely redistributable media (evaluation ISOs and images)
	$(PY) -m ontrak media fetch

## ---- Range operations ---------------------------------------------------

templates: ## Build every scenario template (boot, inject fault, snapshot as "clean")
	$(PY) -m ontrak template build --all

pool: ## Show warm-pool depth and template readiness
	$(PY) -m ontrak pool status

reap: ## Expire sessions, recycle idle ones, refill pools
	$(PY) -m ontrak reap

serve: ## Run the student portal on the host (no containers)
	$(PY) -m ontrak serve

## ---- Scenario generation ------------------------------------------------

generate: ## List the fault primitives and the curated combinations
	$(PY) -m ontrak generate list

generate-matrix: ## Generate one scenario per fault primitive and validate it
	$(PY) -m ontrak generate matrix

## ---- Conformity ---------------------------------------------------------

landing: ## Serve the landing page locally for a quick look
	$(PY) -m http.server --directory web/landing 8080
