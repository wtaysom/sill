#!/usr/bin/env bash
# Upgrade this installed Sill runtime and its data in place.
# Usage: ./upgrade.sh [--client TARGET] [--adopt-legacy] [--hooks-for PATH]
#        [--force-hooks] [--dry-run] [--no-verify] [--schema-only]
#        [--archive-duplicates]
# --hooks-only --hooks-for PATH: refresh hooks without Docker.
# --archive-duplicates: after the backup, archive the newer copy of each live
# memory that repeats the same content from the same speaker.
# --schema-only: raw PostgreSQL fixtures, no Compose or package installation.
# --no-verify: skip extra smoke checks; still rebuild/recreate and check versions.
# .codex/hooks.json changes show a diff; --force-hooks applies it while preserving
# unrelated commands. Edited Sill hooks also require --force-hooks.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/scripts/install_common.sh"
DRY_RUN=0
NO_VERIFY=0
SCHEMA_ONLY=0
HOOKS_ONLY=0
HOOKS_FOR=""
FORCE_HOOKS=0
ADOPT_LEGACY=0
ARCHIVE_DUPLICATES=0
CLIENTS=()
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
while (( $# )); do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --no-verify) NO_VERIFY=1; shift ;;
    --schema-only) SCHEMA_ONLY=1; shift ;;
    --hooks-only) HOOKS_ONLY=1; shift ;;
    --force-hooks) FORCE_HOOKS=1; shift ;;
    --adopt-legacy) ADOPT_LEGACY=1; shift ;;
    --archive-duplicates) ARCHIVE_DUPLICATES=1; shift ;;
    --hooks-for) [[ $# -ge 2 ]] || fail "--hooks-for requires a path"; HOOKS_FOR="$2"; shift 2 ;;
    --client)
      [[ $# -ge 2 ]] || fail "--client requires a target"
      case "$2" in claude-code|codex|claude-desktop) ;; *) fail "unsupported client: $2" ;; esac
      CLIENTS[${#CLIENTS[@]}]="$2"; shift 2 ;;
    --help|-h) sed -n '2,12p' "$0"; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done
if (( FORCE_HOOKS || HOOKS_ONLY )) && [[ -z "$HOOKS_FOR" ]]; then fail "--hooks-only/--force-hooks requires --hooks-for"; fi
sill_require_python || exit 1
if (( SCHEMA_ONLY )) && (( HOOKS_ONLY || ${#CLIENTS[@]} )); then fail "--schema-only cannot register clients or combine with --hooks-only"; fi
if (( SCHEMA_ONLY )) && [[ -n "$HOOKS_FOR" ]]; then fail "--schema-only cannot refresh hooks"; fi

refresh_hooks() {
  local target py flags=()
  target="$(cd "$HOOKS_FOR" && pwd -P)" || return 1
  py="$(sill_python)" || return 1
  if (( FORCE_HOOKS )); then flags[${#flags[@]}]=--force; fi
  if (( DRY_RUN )); then flags[${#flags[@]}]=--dry-run; fi
  python3 "$SILL_DIR/scripts/install_hooks.py" "$SILL_DIR/plugin/codex.hooks.json.template" \
    "$target/.claude/settings.local.json" "$SILL_DIR/plugin" "$py" "$SILL_ENV_FILE" ${flags[@]+"${flags[@]}"} "$@"
  python3 "$SILL_DIR/scripts/install_hooks.py" "$SILL_DIR/plugin/codex.hooks.json.template" \
    "$target/.codex/hooks.json" "$SILL_DIR/plugin" "$py" "$SILL_ENV_FILE" --codex ${flags[@]+"${flags[@]}"} "$@"
}
# v0.2 ran on defaults with no backend/.env. Registrations and hook commands
# name that file, so it must exist; an empty one means the same defaults.
ensure_env_file() {
  if [[ -e "$SILL_ENV_FILE" || -L "$SILL_ENV_FILE" ]]; then return 0; fi
  sill_create_env_file || return 1
  printf 'Created an empty private %s; this install ran on defaults without one.\n' "$SILL_ENV_FILE"
}
if (( HOOKS_ONLY )); then
  if (( ! DRY_RUN )); then ensure_env_file; fi
  refresh_hooks
  exit 0
fi

load_env_file() {
  local f="$SILL_DIR/backend/.env" line key val
  [[ -f "$f" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    val="${line#*=}"
    key="${key//[[:space:]]/}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!key+x}" ]] && continue
    val="${val#\"}"; val="${val%\"}"
    val="${val#\'}"; val="${val%\'}"
    export "$key=$val"
  done < "$f"
}
load_env_file

CONTAINER="${SILL_DB_CONTAINER:-sill_db}"
DB_USER="${POSTGRES_USER:-sill}"
DB_NAME="${POSTGRES_DB:-sill}"
MIGRATIONS_DIR="$SILL_BACKEND/migrations"
BACKUP_DIR="$SILL_DIR/backups"
backup=""
partial=""
worker_stopped=0
migration_started=0
SILL_PREFLIGHT_TEMP=""
SILL_PREFLIGHT_DEPS=""
ENV_FILE_PENDING=0
cleanup() {
  local code=$?
  sill_cleanup_instance
  if [[ -n "$SILL_PREFLIGHT_TEMP" ]]; then rm -rf -- "$SILL_PREFLIGHT_TEMP"; fi
  if [[ -n "$partial" ]]; then rm -f -- "$partial"; fi
  if (( code )); then sill_interrupted_database_step; fi
  if (( code && worker_stopped && ! migration_started )); then
    if [[ -n "$SILL_DATABASE_STEP" ]]; then
      printf 'The maintenance worker was left stopped.\n' >&2
    else
      # start uses the prior existing container, never a newly built image.
      sill_check_database_identity && sill_compose start maintenance_worker || true
    fi
  fi
  if (( code )) && [[ -n "$backup" ]]; then
    printf 'Upgrade stopped. Valid pre-upgrade backup: %s\n' "$backup" >&2
  fi
}
trap cleanup EXIT
migration() {
  local flags=()
  if (( ARCHIVE_DUPLICATES )); then flags[0]=--archive-duplicates; fi
  python3 "$SILL_BACKEND/scripts/migrate.py" "$1" --container "$CONTAINER" \
    --user "$DB_USER" --database "$DB_NAME" --migrations "$MIGRATIONS_DIR" ${flags[@]+"${flags[@]}"}
}
prepare_registration_preflight() {
  if (( ${#CLIENTS[@]} == 0 )); then return; fi
  local py
  py="$(sill_python)"
  if "$py" -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import client_config' "$SILL_BACKEND" >/dev/null 2>&1; then return; fi
  printf 'Preparing private temporary registry dependencies; the installed Sill package is unchanged.\n'
  SILL_PREFLIGHT_TEMP="$(mktemp -d "${TMPDIR:-/tmp}/sill-upgrade-preflight-XXXXXXXX")"
  SILL_PREFLIGHT_DEPS="$SILL_PREFLIGHT_TEMP/deps"
  mkdir "$SILL_PREFLIGHT_DEPS"
  if "$py" -m pip --version >/dev/null 2>&1; then
    PIP_CACHE_DIR="$SILL_PREFLIGHT_TEMP/pip-cache" "$py" -m pip install --target "$SILL_PREFLIGHT_DEPS" 'tomlkit>=0.13.2' 'tomli>=2.0.1' 'simplejson>=3.19' 'python-dotenv>=1.0.0'
  elif command -v uv >/dev/null 2>&1; then
    UV_CACHE_DIR="$SILL_PREFLIGHT_TEMP/uv-cache" uv pip install --python "$py" --target "$SILL_PREFLIGHT_DEPS" 'tomlkit>=0.13.2' 'tomli>=2.0.1' 'simplejson>=3.19' 'python-dotenv>=1.0.0'
  else
    "$py" -m venv "$SILL_PREFLIGHT_TEMP/bootstrap" || fail "Cannot stage registry dependencies; install uv or enable Python venv support, then retry. No runtime changed."
    PIP_CACHE_DIR="$SILL_PREFLIGHT_TEMP/pip-cache" "$SILL_PREFLIGHT_TEMP/bootstrap/bin/python" -m pip install --target "$SILL_PREFLIGHT_DEPS" 'tomlkit>=0.13.2' 'tomli>=2.0.1' 'simplejson>=3.19' 'python-dotenv>=1.0.0'
  fi
}

register_clients() {
  local cli mcp target flags=()
  if (( ${#CLIENTS[@]} == 0 )); then return; fi
  cli="$(sill_bin sill)"; mcp="$(sill_bin sill-mcp)"
  if (( ADOPT_LEGACY )); then flags[0]=--adopt-legacy; fi
  if (( ENV_FILE_PENDING )) && [[ "${1:-}" == --dry-run ]]; then flags[${#flags[@]}]=--pending-env-file; fi
  for target in "${CLIENTS[@]}"; do
    if [[ "${1:-}" == --dry-run ]]; then
      "$(sill_python)" -I -c 'import runpy,sys; paths=[sys.argv.pop(1),sys.argv.pop(1)]; sys.path[:0]=[p for p in paths if p]; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__")' \
        "$SILL_BACKEND" "$SILL_PREFLIGHT_DEPS" "$SILL_BACKEND/sill_cli.py" connect "$target" --env-file "$SILL_ENV_FILE" --mcp-command "$mcp" ${flags[@]+"${flags[@]}"} "$@"
    else
      "$cli" connect "$target" --env-file "$SILL_ENV_FILE" --mcp-command "$mcp" ${flags[@]+"${flags[@]}"} "$@"
    fi
  done
}
printf 'Read-only preflight for %s.\n' "$CONTAINER"
docker exec "$CONTAINER" true 2>/dev/null || fail "Database $CONTAINER is stopped or absent. Start the installed instance with its existing configuration; no substitute will be created."
state="$(migration preflight)" || fail "The database preflight stopped the upgrade; nothing changed"
pending="$(printf '%s' "$state" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["pending"]))')"
dimension="$(printf '%s' "$state" | python3 -c 'import json,sys; print(json.load(sys.stdin)["dimension"])')"
if (( ! SCHEMA_ONLY )); then
  if (( ${#CLIENTS[@]} == 0 )); then
    if command -v claude >/dev/null 2>&1; then CLIENTS[0]=claude-code; fi
    if command -v codex >/dev/null 2>&1; then CLIENTS[${#CLIENTS[@]}]=codex; fi
  fi
  mode=shell
  if (( ${#CLIENTS[@]} )) || [[ -n "$HOOKS_FOR" ]]; then mode=persist; fi
  sill_prepare_instance "$mode"
  if [[ "$mode" == persist && ! -e "$SILL_ENV_FILE" && ! -L "$SILL_ENV_FILE" ]]; then
    if (( DRY_RUN )); then
      # A dry run writes nothing. The persist selection above has already
      # checked the instance; run the read-only checks against its effective
      # default settings and preview registration with the file still absent.
      printf 'DRY: no %s yet; the upgrade would create an empty private one.\n' "$SILL_ENV_FILE"
      sill_prepare_instance shell
      ENV_FILE_PENDING=1
    else
      ensure_env_file
    fi
  fi
  sill_check_running_instance
  settings="$(sill_compose config --format json | "$(sill_python)" -I "$SILL_DIR/scripts/upgrade_runtime.py" "$SILL_ENV_FILE" "$dimension")" || fail "Compose instance does not match the installed data"
  sill_capture_database_identity "$settings"
  sill_database_notice
  # Preserve the running embedding space, including a custom revision that
  # predates the new defaults. Omitted fields are pinned after backup below.
  EMBEDDING_MODEL_ID="$(printf '%s' "$settings" | python3 -c 'import json,sys; print(json.load(sys.stdin)["EMBEDDING_MODEL_ID"])')"
  EMBEDDING_REVISION="$(printf '%s' "$settings" | python3 -c 'import json,sys; print(json.load(sys.stdin)["EMBEDDING_REVISION"])')"
  EMBEDDING_DIMENSION="$dimension"
  export EMBEDDING_MODEL_ID EMBEDDING_REVISION EMBEDDING_DIMENSION
  sill_select_embeddings
  prepare_registration_preflight
  register_clients --dry-run
fi
if [[ -n "$HOOKS_FOR" ]]; then refresh_hooks --dry-run; fi
printf '%s pending migrations.\n' "$pending"
if (( DRY_RUN )); then printf 'DRY: no backup, data, runtime, package or registration changed.\n'; exit 0; fi

# Publish only a complete, validated archive; pipefail checks the producer.
umask 077
mkdir -p "$BACKUP_DIR"
partial="$(mktemp "$BACKUP_DIR/.preupgrade-XXXXXXXX")"
if ! docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" | gzip > "$partial"; then
  fail "pg_dump/gzip failed; nothing was applied"
fi
gzip -t "$partial" || fail "Backup checksum/integrity failed; nothing was applied"
[[ -s "$partial" ]] || fail "Empty backup"
backup="$BACKUP_DIR/preupgrade-$(date -u +%Y%m%dT%H%M%SZ)-${partial##*.preupgrade-}.sql.gz"
mv -- "$partial" "$backup"
partial=""
printf 'Valid private backup: %s\n' "$backup"
if (( ! SCHEMA_ONLY )); then
  # First, so that backend/.env alone starts every service as this run does.
  sill_preserve_embedding_identity "$dimension"
  # A v0.2 database container publishes its port on every interface. Close
  # that as a step of its own, while the package, images and schema are
  # still the old ones.
  if [[ -n "$SILL_DATABASE_IDENTITY" ]]; then
    plan="$(sill_database_plan)" || fail "Could not read the database container's settings; no service was changed"
    if [[ "$(sill_plan_field "$plan" action)" == recreate ]]; then
      printf 'Close connected clients now: the database is about to restart.\n'
      sill_compose stop maintenance_worker
      worker_stopped=1
    fi
    stamp="${backup##*/preupgrade-}"
    secured=0
    sill_secure_database "${stamp%%-*}" "$plan" || secured=$?
    case "$secured" in
      0) ;;
      1) fail "The database is as it was; nothing was migrated" ;;
      *) fail "The database needs attention before the upgrade can continue; nothing was migrated" ;;
    esac
  fi
  # No services are changed until backup succeeds. Build/install failure
  # leaves the prior worker container available and running.
  sill_build_runtime
  sill_install_backend
  printf 'Pause any enabled beat schedule and close connected clients during this upgrade. Database sessions will be fenced during migration.\n'
  sill_check_database_identity
  sill_compose stop maintenance_worker
  worker_stopped=1
fi
if (( ! SCHEMA_ONLY )); then sill_check_database_identity; fi
migration_started=1
if ! migration apply; then
  # A failed first migration (including the client-session fence) rolls back
  # completely. Restore the old worker only if its schema is still unchanged.
  if (( worker_stopped )); then
    after="$(migration preflight 2>/dev/null)" || after=""
    if [[ "$after" == "$state" ]]; then
      sill_check_database_identity && sill_compose start maintenance_worker
    fi
  fi
  fail "Migration stopped; see last successful stamp and backup above"
fi
if (( ! SCHEMA_ONLY )); then
  sill_start_runtime
  sill_wait_healthy "${SILL_INSTALL_WAIT_HEALTHY_S:-$SILL_WAIT_HEALTHY_DEFAULT}" db embeddings rabbitmq
  sill_check_database_identity
  sill_compose up -d --no-deps --force-recreate maintenance_worker
  sill_wait_healthy "${SILL_INSTALL_WAIT_HEALTHY_S:-$SILL_WAIT_HEALTHY_DEFAULT}" db embeddings rabbitmq maintenance_worker
  export SILL_EXPECTED_VERSION="$(sill_checkout_version)"
  "$SILL_DIR/verify.sh" --runtime-only --no-seed
  register_clients
  if [[ -n "$HOOKS_FOR" ]]; then refresh_hooks; fi
  if (( ! NO_VERIFY )); then "$SILL_DIR/verify.sh" --no-seed; fi
  sill_check_database_identity
  printf 'Restart Claude Code/Codex sessions and fully quit/reopen Claude Desktop when selected, then resume the paused beat schedule.\n'
fi
printf 'Upgrade complete: version %s; %s migrations applied. Backup: %s\n' "$(sill_checkout_version)" "$pending" "$backup"
