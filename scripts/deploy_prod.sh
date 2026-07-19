#!/usr/bin/env bash
#
# deploy_prod.sh — REDEPLOY of an already-initialised Second Brain stack.
#
# This script is NOT first-time initialisation. It assumes the database role
# `second_brain_app`, the schema, and the owner enrollment already exist (that
# is done once, by hand, following Part B of the deploy plan). A mandatory guard
# below refuses to start worker/api on an uninitialised database so a stray
# run can never produce a restart-loop.
#
# It pulls the requested image, (re)starts postgres, verifies the role exists,
# then does a short cutover: stop the app, reconcile the schema with `init-db`
# (idempotent, ADD-only), and bring everything back up on the new image.
# The stop is required — init-db briefly REVOKEs and re-GRANTs the app role's
# privileges, so a running bot would hit permission errors mid-reconcile. Running
# init-db on EVERY deploy means a schema-changing release can never land new code
# on an un-migrated schema; the price is a few seconds of downtime per deploy. It
# never touches the ./data volume and never logs secrets. First-time
# initialisation (creating the role/owner enrollment) is still manual, once — the
# guard below aborts on an uninitialised database.
#
# Updates reach the bot through the WEBHOOK served by `api`. `polling` sits
# behind the `rollback` compose profile and is deliberately NOT part of a normal
# deploy: a plain `up -d` does not start it, and this script does not wait for
# it. Deploying WHILE a rollback to polling is active is not supported — see the
# warning printed at the end of this script and "Rolling back to polling" in
# README.md.
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

# --- cutover: stop the app, reconcile the schema, restart -------------------
# Stop polling+worker+api FIRST: init-db briefly REVOKEs then re-GRANTs the app
# role's privileges (identity does a broad REVOKE ALL before the slice grants),
# so a live old bot would hit "permission denied" during that window. api is in
# that list for exactly the same reason — a webhook arriving mid-reconcile would
# fail its INSERT on permissions; stopped, it simply does not answer and
# Telegram retries the update afterwards. With the app stopped, init-db
# (idempotent, ADD-only) reconciles the schema safely; on a no-schema release it
# is a near-instant no-op. Running it every deploy means a schema-adding release
# never starts new code on an un-migrated schema.
#
# `polling` stays in this list even though a normal deploy never starts it.
# Naming a service explicitly auto-enables its profile, so this reliably stops a
# polling container that a rollback left running — and during a rollback polling
# IS the live bot, so init-db's REVOKE/GRANT window would hit it exactly like
# any other app service. When no polling container exists (the normal case) the
# command is a silent no-op and still exits 0.
log "stopping polling, worker and api for the schema reconcile"
docker compose -f "$COMPOSE_FILE" stop polling worker api

log "reconciling schema (init-db: idempotent, ADD-only)"
docker compose -f "$COMPOSE_FILE" run --rm -T worker second-brain-identity init-db

# --- bring up all services (recreates worker+api on the new image) ----------
# No service is named here on purpose: a bare `up -d` starts only the services
# in the ACTIVE profiles, so `polling` (profile `rollback`) stays down. Naming it
# — even as `up -d polling` — would auto-enable its profile and start it, which
# is exactly what must not happen while the webhook owns the update stream.
#
# `--remove-orphans` keeps its normal job of pruning containers whose service no
# longer exists in the file. It does NOT touch `polling` on current Compose
# (verified on v2.32.4: a defined service in an inactive profile is not an
# orphan), but Compose versions before ~2.21 did delete such containers. Either
# way the rule is the same: do not deploy while a rollback is running.
log "starting all services"
docker compose -f "$COMPOSE_FILE" up -d --remove-orphans

# --- wait for app services to become healthy --------------------------------
# api is waited on by name like worker: it publishes no port, so an api that
# came back broken would otherwise be invisible here and only show up as a 502
# from the server's shared traefik. `polling` is NOT waited on — a normal deploy
# does not start it, so waiting would burn the whole timeout and then fail a
# perfectly good deploy.
log "waiting for worker and api to become healthy"
deadline_ok=0
for _ in $(seq 1 45); do
  worker_h="$(docker compose -f "$COMPOSE_FILE" ps worker --format '{{.Health}}' 2>/dev/null || true)"
  api_h="$(docker compose -f "$COMPOSE_FILE" ps api --format '{{.Health}}' 2>/dev/null || true)"
  if [ "$worker_h" = "healthy" ] && [ "$api_h" = "healthy" ]; then
    deadline_ok=1
    break
  fi
  sleep 4
done

if [ "$deadline_ok" != "1" ]; then
  log "services did not become healthy in time; recent logs follow:"
  docker compose -f "$COMPOSE_FILE" ps || true
  docker compose -f "$COMPOSE_FILE" logs --tail 50 worker api || true
  fail "worker/api unhealthy after redeploy"
fi

# --- did this deploy just interrupt a rollback? -----------------------------
# A polling container that EXISTS but is not running means the cutover above
# stopped a live rollback and, `polling` being outside the active profiles, did
# not bring it back. The bot would be deaf: the webhook is deleted (that is what
# a rollback does) and nothing is calling getUpdates either. Say so loudly
# instead of reporting a clean deploy.
polling_state="$(docker compose -f "$COMPOSE_FILE" ps -a polling --format '{{.State}}' 2>/dev/null || true)"
if [ -n "$polling_state" ] && [ "$polling_state" != "running" ]; then
  log "WARNING: a stopped 'polling' container is present — this deploy interrupted a"
  log "WARNING: rollback and did NOT restart it. If the webhook is still deleted the"
  log "WARNING: bot receives nothing right now. Restore ONE of the two doors:"
  log "WARNING:   polling : export APP_IMAGE=\"\$APP_IMAGE_POLLING_FALLBACK\" && \\"
  log "WARNING:             docker compose -f $COMPOSE_FILE --profile rollback up -d polling"
  log "WARNING:   webhook : re-run setWebhook, then docker compose -f $COMPOSE_FILE rm -sf polling"
  log "WARNING: See 'Rolling back to polling' in README.md."
fi

log "redeploy complete; worker and api healthy"
