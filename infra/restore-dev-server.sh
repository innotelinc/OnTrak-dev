#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# restore-dev-server.sh — stand this development server back up on a new machine.
#
#   infra/restore-dev-server.sh --check            # is this host, and GitHub, ready?
#   infra/restore-dev-server.sh --dry-run          # print every step, change nothing
#   sudo infra/restore-dev-server.sh               # do it (the family stack)
#   sudo infra/restore-dev-server.sh --with-lab    # ... plus the lab control plane
#   sudo infra/restore-dev-server.sh --with-lab --with-templates
#
# The point of this script is that a replacement host does as little building and
# downloading as possible. Everything it needs is already on GitHub, in one of
# three forms, and it pulls what can be pulled before it compiles anything:
#
#   the two repositories   innotelinc/OnTrak and innotelinc/OnTrak-dev, cloned at the
#                          refs named below (this script lives in the second one)
#   the product images     ghcr.io/innotelinc/ontrak-* — published by
#                          `make publish-images` on the OnTrak side, one tag per
#                          product plus `sha-<commit>`; pulled here and tagged into
#                          the names the family stack expects, so `up` starts them
#                          instead of building them
#   the golden image       ghcr.io/innotelinc/ontrak-golden, pulled by
#                          `make golden-pull` (--with-templates only: it is the
#                          Windows image the lab's templates are built from)
#
# What it brings up, in order, and what each half is:
#
#   the family stack       OnTrak @ main: ITS (training), Tix, Sentinel, Sync,
#                          Genie, Portal, three databases, one network — see
#                          OnTrak/docker-compose.all.yml, and `make all-up-lan`
#                          there for the same stack by hand
#   the local Authentik    OnTrak-dev @ main: deploy/authentik, the IdP the *lab*
#                          portal signs in through (the family has its own)
#   the lab control plane  OnTrak-dev @ main: portal, gateway, Guacamole and Incus
#                          on the host (--with-lab; needs /dev/kvm)
#
# What it does **not** bring back, deliberately:
#
#   * the Docker volumes. Their contents are a development server's data — seeded
#     accounts, attempts, tickets, sessions — not artefacts, and the .env this
#     script generates has new secrets, so an old database's rows would be signed
#     with keys that no longer exist. `--no-demo` skips even the seed data. Copy
#     volumes across by hand (`docker volume` + `tar`) if a class's results matter.
#   * the installer ISO. It is built where it is needed (`make installer-iso` /
#     `make installer-iso-tiers`), and the published release
#     (`installer-24.04.5` on OnTrak-dev) exists for a range host that has no
#     internet yet. Neither is this host's job to restore.
#
# Environment (all optional; the flags win):
#   ONTRAK_ROOT        where the two checkouts go        (default /srv/ontrak)
#   ONTRAK_REGISTRY    image registry                    (default ghcr.io/innotelinc)
#   ONTRAK_IMAGE_TAG   tag to pull                       (default sha-<commit>, else latest)
#   ONTRAK_LAB_IMAGE   the lab portal image name         (default ontrak)
#   ONTRAK_GOLDEN_PULL_DIR  where `make golden-pull` lands it (default golden-export)
#   ONTRAK_LAN_IP      this host's LAN address           (default: detected)
#   GH_TOKEN           a token for a private package     (default: `gh auth token`)
#
# Exit: 0 when every product answered, 1 when one did not, 2 on a usage error or a
# host that cannot be prepared.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── what this host is being restored to ────────────────────────────────────
ROOT="${ONTRAK_ROOT:-/srv/ontrak}"
FAMILY_REPO="${ONTRAK_REPO:-https://github.com/innotelinc/OnTrak.git}"
LAB_REPO="${ONTRAK_DEV_REPO:-https://github.com/innotelinc/OnTrak-dev.git}"
FAMILY_REF="${ONTRAK_REF:-main}"
LAB_REF="${ONTRAK_DEV_REF:-main}"
REGISTRY="${ONTRAK_REGISTRY:-ghcr.io/innotelinc}"
TAG="${ONTRAK_IMAGE_TAG:-}"
LAB_IMAGE_NAME="${ONTRAK_LAB_IMAGE:-ontrak}"

WITH_LAB=0
WITH_TEMPLATES=0
WITH_DEMO=1
FORCE_BUILD=0
DRY=0
CHECK=0

FAMILY_DIR="$ROOT/OnTrak"
LAB_DIR="$ROOT/OnTrak-dev"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m── %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit "${2:-2}"; }

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)          ROOT="${2:?--root needs a directory}"; FAMILY_DIR="$ROOT/OnTrak"; LAB_DIR="$ROOT/OnTrak-dev"; shift ;;
    --family-ref)    FAMILY_REF="${2:?--family-ref needs a ref}"; shift ;;
    --lab-ref)       LAB_REF="${2:?--lab-ref needs a ref}"; shift ;;
    --tag)           TAG="${2:?--tag needs a tag}"; shift ;;
    --registry)      REGISTRY="${2:?--registry needs a registry}"; shift ;;
    --with-lab)      WITH_LAB=1 ;;
    --with-templates) WITH_LAB=1; WITH_TEMPLATES=1 ;;
    --no-demo)       WITH_DEMO=0 ;;
    --build)         FORCE_BUILD=1 ;;
    --dry-run)       DRY=1 ;;
    --check)         CHECK=1 ;;
    -h|--help)       usage; exit 0 ;;
    *)               die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

# `--check` is a dry run that also reports what it found: it is the question "would
# this work here?", and the only honest answer is one that changes nothing. It
# still reads: the commit both checkouts are on, whether a tag is published with
# that commit's name, and what this host's LAN address is.
if [[ $CHECK = 1 ]]; then DRY=1; fi

# One wrapper decides whether a step happens, so `--dry-run` shows the same script
# an operator would run — the point of a plan is that it is the thing that runs,
# not a description of it that can drift from it.
run() {
  if [[ $DRY = 1 ]]; then
    printf '    would run: %s\n' "$*"
    return 0
  fi
  "$@"
}

# The same, in a directory: every compose command here is "from this checkout",
# and writing that as a `bash -c` string would both print as one and hide the
# quoting from a reader of the plan.
run_in() { # run_in <directory> <command...>
  local dir="$1"
  shift
  if [[ $DRY = 1 ]]; then
    printf '    would run: (cd %s && %s)\n' "$dir" "$*"
    return 0
  fi
  ( cd "$dir" && "$@" )
}

# ═══ 1. the host ═══════════════════════════════════════════════════════════
step "1/8 host prerequisites"

# The commands this script runs, and the packages that provide them. Kept as two
# lists because they are not the same list: `ca-certificates` is a package with no
# command of its own, and `command -v ca-certificates` is false on every host that
# has it — the check that used to stand here reported it missing everywhere and
# asked apt for something already installed.
TOOLS=(git curl jq make)
PACKAGES=(git curl ca-certificates jq make)

missing_tools() {
  local tool
  for tool in "${TOOLS[@]}"; do
    command -v "$tool" >/dev/null 2>&1 || printf '%s\n' "$tool"
  done
}

install_packages() {
  local missing
  missing="$(missing_tools)"
  [[ -n "$missing" ]] || return 0
  log "installing: $(printf '%s' "$missing" | tr '\n' ' ')"
  if command -v apt-get >/dev/null 2>&1; then
    run env DEBIAN_FRONTEND=noninteractive apt-get update -qq
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${PACKAGES[@]}"
  else
    warn "no apt-get: install $(printf '%s' "$missing" | tr '\n' ' ') by hand, then run this again"
  fi
}

if [[ $CHECK = 1 ]]; then
  while read -r tool; do
    [[ -n "$tool" ]] && warn "$tool is missing (the script installs it when it runs for real)"
  done < <(missing_tools)
  command -v docker >/dev/null 2>&1 || warn "docker is missing (the script installs it when it runs for real)"
else
  install_packages
fi

if command -v docker >/dev/null 2>&1; then
  if ! docker info >/dev/null 2>&1; then
    if [[ $CHECK = 1 ]]; then
      warn "docker is installed but not answering (start it, or run this as root / in the docker group)"
    else
      die "docker is installed but not answering. Start the daemon, or run this as root or in the 'docker' group."
    fi
  fi
else
  if [[ $CHECK = 1 ]]; then
    warn "docker is not installed — see https://docs.docker.com/engine/install/ (or this script's --help for the one-liner)"
  else
    die "docker is not installed. Install Docker Engine and the compose plugin (https://docs.docker.com/engine/install/), then run this again."
  fi
fi

# The family stack's database overlay (`docker-compose.lan-db.yml`) uses the
# `!override` tag, so this is a precondition rather than a footnote: an older
# compose fails on the tag with a YAML error that reads nothing like a version
# problem. Asked as a *feature* question rather than by comparing version numbers:
# the plugin here reports v5.6.0, and a `minor < 24` test would refuse the newest
# compose in existence. Rendering one throwaway service that uses the tag is the
# same question compose will ask of the real file.
compose_has_override() {
  local probe
  probe="$(mktemp -d)"
  cat > "$probe/probe.yml" <<'YAML'
services:
  probe:
    image: scratch
    ports: !override
      - "127.0.0.1:1:1"
YAML
  local rc=0
  docker compose -f "$probe/probe.yml" config --quiet >/dev/null 2>&1 || rc=1
  rm -rf "$probe"
  return $rc
}

COMPOSE_VERSION="$(docker compose version --short 2>/dev/null || true)"
if [[ -n "$COMPOSE_VERSION" ]]; then
  log "docker compose $COMPOSE_VERSION"
fi
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  if compose_has_override; then
    log "compose renders !override, which the database overlay uses"
  else
    die "this docker compose does not support the !override tag, so the family stack's database overlay (docker-compose.lan-db.yml) cannot be rendered — upgrade the compose plugin (2.24+) and run this again"
  fi
else
  warn "docker compose is not answering, so !override support could not be checked"
fi

if [[ $WITH_LAB = 1 ]]; then
  if [[ -r /dev/kvm ]]; then
    log "/dev/kvm is present — the lab can run real machines"
  else
    warn "no /dev/kvm: the lab control plane will start, but sessions cannot boot a machine (demo mode still works)"
  fi
fi

log "checkouts will live in $ROOT"

# ═══ 2. the two repositories ═══════════════════════════════════════════════
step "2/8 the two repositories"

checkout() { # checkout <dir> <repo> <ref>
  local dir="$1" repo="$2" ref="$3"
  if [[ -d "$dir/.git" ]]; then
    log "$dir: fetching and moving to $ref"
    run git -C "$dir" fetch --prune origin
    run git -C "$dir" checkout "$ref"
    run git -C "$dir" merge --ff-only "origin/$ref"
  else
    log "$dir: cloning $repo ($ref)"
    run git clone --branch "$ref" "$repo" "$dir"
  fi
}

if [[ $DRY = 1 ]]; then
  run mkdir -p "$ROOT"
else
  mkdir -p "$ROOT"
fi
checkout "$FAMILY_DIR" "$FAMILY_REPO" "$FAMILY_REF"
checkout "$LAB_DIR" "$LAB_REPO" "$LAB_REF"

# Nothing below can be read from a directory that is not there, and `--dry-run` on
# a bare host has no checkout to read — so the rest of the plan stops here rather
# than inviting the reader to believe steps it cannot name the inputs of.
if [[ ! -d "$FAMILY_DIR" ]] || [[ ! -d "$LAB_DIR" ]]; then
  step "the rest of the plan needs the checkouts"
  log "$FAMILY_DIR and $LAB_DIR are not both present yet"
  if [[ $DRY = 1 ]]; then
    log "run this again after the clone, or let a real run do it: the script is the plan."
    exit 0
  fi
  die "a checkout is missing after cloning: $FAMILY_DIR / $LAB_DIR"
fi

FAMILY_COMMIT="$(git -C "$FAMILY_DIR" rev-parse HEAD)"
LAB_COMMIT="$(git -C "$LAB_DIR" rev-parse HEAD)"
log "OnTrak      $FAMILY_REF @ ${FAMILY_COMMIT:0:12}"
log "OnTrak-dev  $LAB_REF @ ${LAB_COMMIT:0:12}"

# The default tag is the commit the two repositories are on: the publish script
# tags every image it builds with `sha-<full commit>`, so one hash names the whole
# set a tree produced. `latest` is the fallback for a host restoring a tree whose
# images were never published by commit — and the difference matters enough to say
# which one was used.
if [[ -z "$TAG" ]]; then
  if [[ $CHECK = 1 ]] && ! command -v docker >/dev/null 2>&1; then
    TAG="sha-${FAMILY_COMMIT}(or latest)"
  else
    TAG="sha-${FAMILY_COMMIT}"
    for probe in ontrak-training ontrak-tix ontrak-sentinel ontrak-genie ontrak-portal ontrak-sync-api ontrak-sync-web; do
      if ! docker manifest inspect "$REGISTRY/$probe:$TAG" >/dev/null 2>&1; then
        warn "$REGISTRY/$probe:$TAG is not published — falling back to 'latest'"
        TAG="latest"
        break
      fi
    done
  fi
fi
log "image tag  $TAG in $REGISTRY"

# ═══ 3. the family stack's env ═════════════════════════════════════════════
step "3/8 the family stack's .env"

ENV_FILE="$FAMILY_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  run cp "$FAMILY_DIR/.env.example" "$ENV_FILE"
  log "created .env from the template"
else
  log ".env exists — left alone apart from missing values"
fi

# This host's LAN address, for the ports a browser or a LAN client dials. The
# repo's own script owns the detection (default-route source address, never
# loopback, never 172.x), so this asks it rather than repeating it.
if [[ $DRY = 0 ]]; then
  ( cd "$FAMILY_DIR" && scripts/lan-env.sh --file .env ) || warn "lan-env.sh did not provision an address"
fi

LAN_IP="${ONTRAK_LAN_IP:-$(cd "$FAMILY_DIR" && bash -c '. scripts/stack-lib.sh; stack_lib_lan_ip' 2>/dev/null || true)}"
if [[ -n "$LAN_IP" ]]; then
  log "this host's LAN address: $LAN_IP"
  # The databases are the one thing `docker-compose.lan-db.yml` needs by name: the
  # overlay publishes each on loopback *and* on this address, which is the only
  # legal way to publish one port twice (two specific, different addresses).
  for key in ONTRAK_DB_BIND_HOST ONTRAK_TIX_DB_BIND_HOST ONTRAK_SENTINEL_DB_BIND_HOST; do
    if [[ $DRY = 1 ]]; then
      printf '    would set: %s=%s in .env\n' "$key" "$LAN_IP"
    else
      ( cd "$FAMILY_DIR" && bash -c '. scripts/stack-lib.sh; stack_lib_env_set "$1" "$2" "$3"' _ .env "$key" "$LAN_IP" )
    fi
  done
else
  warn "no LAN address detected: the databases stay on the wildcard, and only this host can reach the family"
fi

# The three values the stack refuses to start without. Generated here because a
# restored host has no vault to resolve them from, and left alone when they are
# already set: re-running this script must not invalidate a running family's
# sessions or its Sync token.
secret() { openssl rand -base64 48 | tr -d '\n=+/' | cut -c1-48; }
ensure_secret() { # ensure_secret <file> <key>
  local file="$1" key="$2"
  local current
  current="$(sed -n "s/^${key}=//p" "$file" | head -1)"
  if [[ -n "$current" && "$current" != change-me* && "$current" != "vault://"* ]]; then
    return 0
  fi
  if [[ $DRY = 1 ]]; then
    printf '    would generate: %s in %s\n' "$key" "${file##*/}"
    return 0
  fi
  ( cd "$FAMILY_DIR" && bash -c '. scripts/stack-lib.sh; stack_lib_env_set "$1" "$2" "$3" --force"' _ "$file" "$key" "$(secret)" )
  log "generated $key"
}
command -v openssl >/dev/null 2>&1 || command -v node >/dev/null 2>&1 || warn "neither openssl nor node is here: the secrets below cannot be generated"
ensure_secret "$ENV_FILE" AUTH_SECRET          # signs the training app's session cookies
ensure_secret "$ENV_FILE" TIX_AUTH_SECRET      # signs the desk's
ensure_secret "$ENV_FILE" ONTRAK_API_TOKEN     # Sync's API refuses to start without one

# ═══ 4. the images ═════════════════════════════════════════════════════════
step "4/8 the product images"

# Published name -> the name the family stack expects. The three products without
# an `image:` in the compose file are built by `up` and tagged `<project>-<service>`;
# pulling the published image and tagging it under that name is what lets `up`
# start the product instead of compiling it. The migration images are the same
# Dockerfiles' `builder` stages — which is what carries the Prisma CLI — and the
# seeds are that stage again with a different command.
IMAGES=(
  "ontrak-training|ontrak-family-training-app:latest"
  "ontrak-training-migrate|ontrak-family-training-migrate:latest"
  "ontrak-training-migrate|ontrak-family-training-seed:latest"
  "ontrak-tix|ontrak-family-tix-app:latest"
  "ontrak-tix-migrate|ontrak-family-tix-migrate:latest"
  "ontrak-tix-migrate|ontrak-family-tix-seed:latest"
  "ontrak-sentinel|ontrak-family-sentinel-app:latest"
  "ontrak-sentinel-migrate|ontrak-family-sentinel-migrate:latest"
  "ontrak-sentinel|ontrak-family-sentinel-signing-key:latest"
  "ontrak-genie|innotel/ontrak-genie:main"
  "ontrak-portal|innotel/ontrak-portal:main"
  "ontrak-sync-api|innotel/ontrak-sync-api:main"
  "ontrak-sync-web|innotel/ontrak-sync-web:main"
)

# A new GHCR package is private by default, so the pull needs a token even for a
# repository anyone can read. `gh auth token` is the same sign-in `make
# publish-images` uses in the other direction, and the same fallback: an operator
# who has already run `docker login` is left alone.
registry_login() {
  [[ "$REGISTRY" = ghcr.io* ]] || return 0
  command -v gh >/dev/null 2>&1 || return 0
  if [[ $DRY = 1 ]]; then
    printf '    would sign in to %s with the gh token, if the packages are private\n' "${REGISTRY%%/*}"
    return 0
  fi
  # A manifest read costs nothing and answers the only question that matters: can
  # this host see the package? An operator who has already run `docker login` is
  # left alone, which is also what the publish scripts in both repositories do.
  if docker manifest inspect "$REGISTRY/ontrak-training:$TAG" >/dev/null 2>&1; then
    return 0
  fi
  local token user
  token="${GH_TOKEN:-$(gh auth token 2>/dev/null || true)}"
  user="$(gh api user --jq .login 2>/dev/null || true)"
  [[ -n "$token" ]] || { warn "no gh token: if the packages are private the pulls below will fail"; return 0; }
  [[ -n "$user" ]] || user="token"
  log "signing in to ${REGISTRY%%/*} as $user"
  printf '%s' "$token" | docker login "${REGISTRY%%/*}" -u "$user" --password-stdin >/dev/null \
    || warn "docker login failed — private packages will not pull"
}

if [[ $CHECK = 1 ]]; then
  for image in "${IMAGES[@]}"; do
    printf '    %-46s <- %s/%s:%s\n' "${image#*|}" "$REGISTRY" "${image%%|*}" "$TAG"
  done
  log "run without --check to pull and tag these"
else
  registry_login
  if [[ $DRY = 0 ]] && ! docker manifest inspect "$REGISTRY/ontrak-training:latest" >/dev/null 2>&1; then
    warn "the registry is not answering for $REGISTRY (a private package needs a token: gh auth token | docker login ghcr.io -u <user> --password-stdin)"
  fi
  PULLED=0
  FAILED=()
  for image in "${IMAGES[@]}"; do
    published="$REGISTRY/${image%%|*}:$TAG"
    local_name="${image#*|}"
    if [[ $DRY = 1 ]]; then
      printf '    would pull %s and tag it %s\n' "$published" "$local_name"
      continue
    fi
    if docker pull "$published" >/dev/null 2>&1; then
      docker tag "$published" "$local_name"
      PULLED=$((PULLED + 1))
    else
      FAILED+=("${image%%|*}")
    fi
  done
  if [[ ${#FAILED[@]} -gt 0 ]]; then
    warn "not published at $TAG, so these are built from source: ${FAILED[*]}"
  fi
  [[ $DRY = 1 ]] || log "pulled and tagged $PULLED image(s)"
fi

# ═══ 5. the family stack ═══════════════════════════════════════════════════
step "5/8 the family stack"

# The overlay publishes each database on loopback *and* this host's address; it is
# the same pair `make all-up-lan` uses in the OnTrak checkout.
FAMILY_COMPOSE=(-f docker-compose.all.yml -f docker-compose.lan-db.yml)
if [[ $FORCE_BUILD = 1 ]]; then
  run_in "$FAMILY_DIR" docker compose "${FAMILY_COMPOSE[@]}" up -d --build
else
  run_in "$FAMILY_DIR" docker compose "${FAMILY_COMPOSE[@]}" up -d
fi

# ═══ 6. the lab's identity and control plane ═══════════════════════════════
step "6/8 the lab control plane"

if [[ $WITH_LAB != 1 ]]; then
  log "skipped — pass --with-lab to bring up the local Authentik and the lab stack"
else
  # The lab's portal is SSO-only, so its IdP comes up first: `setup.sh` is
  # idempotent, keeps its own .env, and is the step that registers the OIDC client.
  run_in "$LAB_DIR" ./deploy/authentik/setup.sh

  # The lab's own image is published too (`make publish-image`), so the control
  # plane can start from it rather than compile a 2.6 GiB image on the new host.
  # The lab's compose names it `ontrak:local` and declares `pull_policy: never`,
  # so the registry image is pulled under its own name and tagged into that one.
  LAB_IMAGE="${REGISTRY}/${LAB_IMAGE_NAME:-ontrak}:$TAG"
  LAB_PULLED=0
  if [[ $DRY = 1 ]]; then
    printf '    would pull %s and tag it ontrak:local\n' "$LAB_IMAGE"
  elif docker pull "$LAB_IMAGE" >/dev/null 2>&1; then
    docker tag "$LAB_IMAGE" ontrak:local
    LAB_PULLED=1
    log "lab image: $LAB_IMAGE -> ontrak:local"
  else
    warn "$LAB_IMAGE is not published at $TAG: the lab image is built here"
  fi

  # Then the control plane itself: lab-setup writes the shared secrets and
  # bootstraps Incus on this host, and the portal and console start when it exits.
  if [[ $LAB_PULLED = 1 ]]; then
    run_in "$LAB_DIR" docker compose up -d
  else
    run_in "$LAB_DIR" docker compose up -d --build
  fi

  if [[ $WITH_TEMPLATES = 1 ]]; then
    # The golden image is the expensive half and the one worth pulling: it is
    # published so a new host adopts it rather than rebuilding Windows.
    run_in "$LAB_DIR" make golden-pull
    run_in "$LAB_DIR" make golden-import ARGS=golden-export

    # The templates themselves are published as well
    # (`infra/publish-templates.sh` -> ghcr.io/innotelinc/ontrak-template), and
    # importing them is minutes against the hours each Windows template costs to
    # build — so the pull is the step here and `make templates` is the fallback for
    # whatever the registry does not hold. Importing needs the two tools the lab's
    # own bootstrap does not install: this says which one is missing instead of
    # leaving it to fail inside `incus`.
    if [[ $DRY = 1 ]]; then
      printf '    would run: (cd %s && infra/import-templates.sh || make templates)\n' "$LAB_DIR"
    elif ! command -v incus >/dev/null 2>&1 || ! command -v "${ONTRAK_ORAS:-oras}" >/dev/null 2>&1; then
      warn "no incus or no oras on this host, so the published templates cannot be imported — building them instead"
      run_in "$LAB_DIR" make templates
    elif ( cd "$LAB_DIR" && infra/import-templates.sh ); then
      log "published templates imported"
    else
      warn "the registry did not hold every template — building the rest (this is the slow half)"
      run_in "$LAB_DIR" make templates
    fi
  fi
fi

# ═══ 7. the data a development server starts with ══════════════════════════
step "7/8 demo data"

if [[ $WITH_DEMO = 1 ]]; then
  # The seed profiles are `demo`, which `up` does not start: they are one-shot
  # jobs, and every product's accounts come from them.
  run_in "$FAMILY_DIR" docker compose -f docker-compose.all.yml --profile demo run --rm training-seed
  run_in "$FAMILY_DIR" docker compose -f docker-compose.all.yml --profile demo run --rm tix-seed
  if [[ $WITH_LAB = 1 ]]; then
    run_in "$LAB_DIR" make catalog-validate
  fi
else
  log "skipped — --no-demo was given, so no accounts exist yet"
fi

# ═══ 8. does it answer? ════════════════════════════════════════════════════
step "8/8 every product answers"

# The same seven probes the `Family stack` CI job waits for, from the same list —
# minus the lab, which is the optional product no deployment has to run.
PROBES=(
  "training|http://127.0.0.1:3000/health"
  "tix|http://127.0.0.1:3001/health"
  "sentinel|http://127.0.0.1:8787/health"
  "portal|http://127.0.0.1:3300/health"
  "genie|http://127.0.0.1:3400/health"
  "sync-api|http://127.0.0.1:8420/api/health"
  "sync-web|http://127.0.0.1:8421/health"
)

if [[ $DRY = 1 ]]; then
  for probe in "${PROBES[@]}"; do
    printf '    would ask %-10s %s\n' "${probe%%|*}" "${probe#*|}"
  done
  [[ $WITH_LAB = 1 ]] && printf '    would ask %-10s %s\n' lab "http://127.0.0.1:${ONTRAK_LAB_PORT:-8080}/healthz"
  log "dry run: nothing was changed"
  exit 0
fi

MISSING=""
for _ in $(seq 1 60); do
  MISSING=""
  for probe in "${PROBES[@]}"; do
    curl -fsS --max-time 5 "${probe#*|}" >/dev/null 2>&1 || MISSING="$MISSING ${probe%%|*}"
  done
  [[ -z "$MISSING" ]] && break
  sleep 5
done

if [[ -n "$MISSING" ]]; then
  warn "still not answering after five minutes:$MISSING"
  log "their logs say why: cd $FAMILY_DIR && docker compose -f docker-compose.all.yml -f docker-compose.lan-db.yml logs --tail 40"
  exit 1
fi

log "every product is up"
echo
printf '  family    http://%s:3300/            (the portal, which fronts the rest)\n' "${LAN_IP:-127.0.0.1}"
printf '  training  http://%s:3000/\n' "${LAN_IP:-127.0.0.1}"
printf '  the desk  http://%s:3001/\n' "${LAN_IP:-127.0.0.1}"
printf '  sentinel  http://%s:8787/\n' "${LAN_IP:-127.0.0.1}"
printf '  genie     http://%s:3400/\n' "${LAN_IP:-127.0.0.1}"
printf '  sync      http://%s:8421/\n' "${LAN_IP:-127.0.0.1}"
if [[ $WITH_LAB = 1 ]]; then
  printf '  lab       http://%s:%s/        (portal and console on one port)\n' "${LAN_IP:-127.0.0.1}" "${ONTRAK_LAB_PORT:-8080}"
fi
echo
log "the checkouts are $FAMILY_DIR and $LAB_DIR; the .env files hold freshly generated secrets"
log "not restored on purpose: the Docker volumes (see this script's header), and the installer ISO"
