#!/usr/bin/env bash
#
# deploy_prod.sh — REDEPLOY of an already-initialised Second Brain stack.
#
# This script is NOT first-time initialisation. It assumes the database role
# `second_brain_app`, the schema, and the owner enrollment already exist (that
# is done once, by hand, following Part B of the deploy plan). A mandatory guard
# below refuses to start polling/worker on an uninitialised database so a stray
# run can never produce a restart-loop.
#
# It pulls the requested image, (re)starts postgres, verifies the role exists,
# then brings up all services and waits for them to become healthy.
# It never runs init-db, never touches the ./data volume, and never logs secrets.
#
# Required environment:
#   APP_IMAGE   full image reference to deploy (e.g. ghcr.io/timurtsedik/double-brained:sha-xxxxxxx)
# Required files (in the current working directory):
#   docker-compose.prod.yml
#   .env                     (contains SCHEMA_DATABASE_URL, DATABASE_URL, secrets)
# Optional environment (private image pull):
#   GHCR_USERNAME, GHCR_TOKEN
#
set -euo pipefail

COMPOSE_FILE="docker-compose.prod.yml"

log() { printf '[deploy] %s\n' "$1"; }
fail() { printf '[deploy] ERROR: %s\n' "$1" >&2; exit 1; }

# --- preconditions ----------------------------------------------------------
: "${APP_IMAGE:?APP_IMAGE is required (full image reference to deploy)}"
export APP_IMAGE
[ -f "$COMPOSE_FILE" ] || fail "$COMPOSE_FILE not found in $(pwd)"
[ -f ".env" ] || fail ".env not found in $(pwd) (put it there once, see Part B)"

# Read SCHEMA_DATABASE_URL from .env WITHOUT echoing secrets.
set -a
# shellcheck disable=SC1091
. ./.env
set +a
: "${SCHEMA_DATABASE_URL:?SCHEMA_DATABASE_URL must be set in .env}"

# libpq DSN for psql (strip the SQLAlchemy async driver suffix).
PSQL_DSN="${SCHEMA_DATABASE_URL/+asyncpg/}"

logged_in=0
cleanup() {
  if [ "$logged_in" = "1" ]; then
    docker logout ghcr.io >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# --- optional private registry login ---------------------------------------
if [ -n "${GHCR_USERNAME:-}" ] && [ -n "${GHCR_TOKEN:-}" ]; then
  log "logging in to ghcr.io as ${GHCR_USERNAME}"
  printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin >/dev/null
  logged_in=1
fi

# --- pull requested image ---------------------------------------------------
log "pulling image ${APP_IMAGE}"
docker compose -f "$COMPOSE_FILE" pull

# --- ensure postgres is up (data service, safe to start before the guard) ---
log "starting postgres and waiting for health"
docker compose -f "$COMPOSE_FILE" up -d postgres

# Wait until the postgres container reports healthy (bounded).
for _ in $(seq 1 30); do
  status="$(docker compose -f "$COMPOSE_FILE" ps postgres --format '{{.Health}}' 2>/dev/null || true)"
  [ "$status" = "healthy" ] && break
  sleep 2
done
[ "${status:-}" = "healthy" ] || fail "postgres did not become healthy"

# --- MANDATORY guard: refuse to start the app on an uninitialised database --
log "verifying application role exists (guard against first-time init)"
role_present="$(docker compose -f "$COMPOSE_FILE" run --rm -T postgres \
  psql "$PSQL_DSN" -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'second_brain_app'" \
  2>/dev/null || true)"

if [ "$(printf '%s' "$role_present" | tr -d '[:space:]')" != "1" ]; then
  fail "role second_brain_app is missing — the database is not initialised.
       Do the first-time initialisation (Part B: init-db + bootstrap invite)
       before running a redeploy. Aborting to avoid a restart-loop."
fi

# --- bring up all services --------------------------------------------------
log "starting all services"
docker compose -f "$COMPOSE_FILE" up -d --remove-orphans

# --- wait for app services to become healthy --------------------------------
log "waiting for polling and worker to become healthy"
deadline_ok=0
for _ in $(seq 1 45); do
  polling_h="$(docker compose -f "$COMPOSE_FILE" ps polling --format '{{.Health}}' 2>/dev/null || true)"
  worker_h="$(docker compose -f "$COMPOSE_FILE" ps worker --format '{{.Health}}' 2>/dev/null || true)"
  if [ "$polling_h" = "healthy" ] && [ "$worker_h" = "healthy" ]; then
    deadline_ok=1
    break
  fi
  sleep 4
done

if [ "$deadline_ok" != "1" ]; then
  log "services did not become healthy in time; recent logs follow:"
  docker compose -f "$COMPOSE_FILE" ps || true
  docker compose -f "$COMPOSE_FILE" logs --tail 50 polling worker || true
  fail "polling/worker unhealthy after redeploy"
fi

log "redeploy complete; all services healthy"
