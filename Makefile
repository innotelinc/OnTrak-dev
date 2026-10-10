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
        golden golden-import golden-pull templates pool reap demo-serve host-image landing \
        installer-iso installer-iso-smoke installer-iso-test installer-iso-tiers \
        installer-iso-usb \
        build publish-image up up-remote down logs ps exec check-compose setup-log \
        docker-demo docker-shell provision provision-plan console-recreate \
        local-auth local-auth-down

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

installer-iso-tiers: ## Build one installer ISO per sizing tier (dev, class, full)
	@# One image per class of machine, from one source: each tier bakes its own
	@# first-boot settings (the warm pool above all) into the installed host, names
	@# the machine it is for, and says so in its menu — see docs/installer.md,
	@# "Sizing tiers". Each is a full build and a full verification, so this is
	@# three times the work of `make installer-iso`.
	ONTRAK_ISO_TIERS="dev class full" bash infra/build-installer-iso.sh

installer-iso-smoke: ## Build the installer ISO, then boot it in QEMU to prove it installs
	ONTRAK_ISO_SMOKE=1 bash infra/build-installer-iso.sh

installer-iso-test: ## Install a machine from the built ISO in QEMU, then check it over SSH
	@iso="$$(ls -t dist/*.iso 2>/dev/null | head -1)"; \
	test -n "$$iso" || { echo "no ISO in dist/ — run 'make installer-iso' first"; exit 2; }; \
	bash infra/installer/install-test.sh "$$iso"

installer-iso-usb: ## Write an installer ISO to a USB stick, then verify the stick (ARGS=/dev/sdX)
	@# The write docs/installer.md used to spell out as one dd, and the measurement
	@# that replaced it: two plain 3.8 GB writes killed the stick on this host, at
	@# 3.5 GB and at 3.75 GB of 4.07 GB. This writes in chunks, retries a chunk that
	@# fails, and reads the stick back against the ISO — a stick that recovered
	@# mid-write holds a plausible image with a hole in it. Defaults to the newest
	@# ISO in dist/ and to the one removable USB disk; ARGS names the disk.
	@iso="$$(ls -t dist/*.iso 2>/dev/null | head -1)"; \
	test -n "$$iso" || { echo "no ISO in dist/ — run 'make installer-iso' first"; exit 2; }; \
	bash infra/installer/write-usb.sh "$$iso" $(ARGS)

## ---- Docker stack (portal + console gateway) ----------------------------

build: ## Build the portal image
	$(COMPOSE) build

publish-image: ## Build and push the portal image to GHCR (make publish-image PUSH=0 to skip the push)
	@# The image a range host pulls instead of building, and the local twin of
	@# .github/workflows/publish.yml: the same serving stage and the same tags, with
	@# the version read from ontrak/__init__.py rather than typed. VERSION, REGISTRY
	@# and PUSH are read from the environment, as they are by the family's
	@# scripts/publish-images.sh.
	bash scripts/publish-image.sh

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

local-auth: ## Start the local Authentik and point this range at it (deploy/authentik)
	@# A range signed into without the estate being reachable: its own Authentik, its
	@# own database, and the four ONTRAK_PORTAL__OIDC_* values handed to compose as a
	@# second --env-file. See docs/operations.md "A local Authentik".
	bash deploy/authentik/setup.sh --connect

local-auth-down: ## Stop the local Authentik (its database is kept)
	bash deploy/authentik/setup.sh --down

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

## ---- Range content (hours, and not part of a compose up) ----------------
# The golden Windows image every template is cloned from, and the templates
# themselves. Both are long, both need real Windows media, and in practice they
# are re-run separately: the image when evaluation media expires, the templates
# when a scenario changes. `make golden` is what the bootstrap summary, the
# installer's first boot and docs/operations.md all tell an operator to run — so
# it has to exist here.

golden: ## Build the golden Windows image (30-60 min; downloads Windows eval media)
	@test -f .env || { echo "no .env — run 'make secrets' first (or 'make setup')"; exit 2; }
	@# The training account password is the one thing this recipe needs out of
	@# .env, and it is read with sed rather than sourced: .env is a *compose* env
	@# file, so values are unquoted and some of them contain spaces
	@# (ONTRAK_PORTAL__BRAND_NOTE does). Compose takes the rest of the line; a
	@# shell splits it, so `set -a; . ./.env` cheerfully tries to run "support" as
	@# a command and sets the variable to a fragment. The same sed-extract is what
	@# docker/lab-setup.sh and the Guacamole entrypoint use, for the same reason.
	@guest="$$(sed -n 's/^ONTRAK_GUEST__PASSWORD=//p' .env | head -1)"; \
	test -n "$$guest" || { echo "ONTRAK_GUEST__PASSWORD is empty in .env — run 'make secrets'"; exit 2; }; \
	ONTRAK_GUEST__PASSWORD="$$guest" bash infra/build-golden-image.sh

golden-import: ## Publish a golden image built on another host (make golden-import ARGS=/path/to/export)
	@# Not every host can build it: one whose KVM cannot virtualise SMM loses
	@# Windows Setup before it starts (measured on nested AMD; nesting alone does
	@# not decide it). Build where that works and bring the export here — see
	@# docs/operations.md, "Building the golden image on a nested host". The disk is
	@# checked before it is published, because a half-applied image imports fine
	@# and then hangs every template.
	bash infra/import-golden-image.sh $(ARGS)

golden-pull: ## Pull a published golden image (make golden-pull ARGS=win11e-2026-10-06)
	@# The half of the pair that was missing: the image could be published by a
	@# script, but pulling one was a two-step command in docs/operations.md that every
	@# host retyped. With no ARGS it takes the newest published tag, so a host set up
	@# on any other day does not ask for today's date and get `unauthorized`.
	@# Importing stays a separate step: that is the one that verifies the disk.
	bash infra/pull-golden-image.sh $(ARGS)

## ---- Range operations ---------------------------------------------------

templates: ## Build every scenario template (or one: make templates ARGS="sw-app-crash --force")
	@# The guest password has to be in the environment for this, exactly as it does
	@# for `make golden`, and for the same reason: the range reads os.environ, while
	@# .env is a *compose* env file that only `docker compose` parses. Without it
	@# every template build stops at "never became reachable over the winrm
	@# transport" — the WinRM logon is made with an empty password and rejected —
	@# with a Windows guest that is up and answering both ports. Read with sed
	@# rather than sourced; `make golden` has the account of why.
	@test -f .env || { echo "no .env — run 'make secrets' first (or 'make setup')"; exit 2; }
	@guest="$$(sed -n 's/^ONTRAK_GUEST__PASSWORD=//p' .env | head -1)"; \
	test -n "$$guest" || { echo "ONTRAK_GUEST__PASSWORD is empty in .env — run 'make secrets'"; exit 2; }; \
	ONTRAK_GUEST__PASSWORD="$$guest" $(PY) -m ontrak template build $(if $(ARGS),$(ARGS),--all)

pool: ## Show warm-pool depth and template readiness
	$(PY) -m ontrak pool status

prewarm: ## Warm the pool (make prewarm ARGS="--scenario sw-app-crash --count 3")
	@# Same trap as `templates`, and the same code path underneath it: prewarming a
	@# Windows VM waits for the guest transport, so the password has to be in the
	@# environment or the wait fails every time. A prewarm that *fails* also leaves
	@# the machine it was warming behind, running and unclaimed — see
	@# docs/operations.md, "A prewarm that fails leaves its machine in the pool".
	@test -f .env || { echo "no .env — run 'make secrets' first (or 'make setup')"; exit 2; }
	@guest="$$(sed -n 's/^ONTRAK_GUEST__PASSWORD=//p' .env | head -1)"; \
	test -n "$$guest" || { echo "ONTRAK_GUEST__PASSWORD is empty in .env — run 'make secrets'"; exit 2; }; \
	ONTRAK_GUEST__PASSWORD="$$guest" $(PY) -m ontrak pool prewarm $(ARGS)

reap: ## Expire sessions, recycle idle ones, refill pools
	@# `reap` refills pools, which warms Windows VMs, so it needs the guest password
	@# for exactly the reason `make prewarm` and `make templates` do.
	@test -f .env || { echo "no .env — run 'make secrets' first (or 'make setup')"; exit 2; }
	@guest="$$(sed -n 's/^ONTRAK_GUEST__PASSWORD=//p' .env | head -1)"; \
	test -n "$$guest" || { echo "ONTRAK_GUEST__PASSWORD is empty in .env — run 'make secrets'"; exit 2; }; \
	ONTRAK_GUEST__PASSWORD="$$guest" $(PY) -m ontrak reap

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
