#!/usr/bin/env bash
# Shared by the checkout's runtime scripts. Compatible with Bash 3.2.

SILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SILL_BACKEND="$SILL_DIR/backend"
SILL_ENV_FILE="$SILL_BACKEND/.env"
COMPOSE_FILE="$SILL_BACKEND/docker-compose.yml"
SILL_WAIT_HEALTHY_DEFAULT=1800
# Only this invocation's validated observation may select the existing-db lane.
# In particular, reset and a fresh installer must ignore inherited shell flags.
SILL_DATABASE_IDENTITY=""

sill_compose() {
  local env_file="$SILL_ENV_FILE"
  # Without an explicit env file Compose can load one from the caller's CWD.
  [[ -e "$env_file" || -L "$env_file" ]] || env_file=/dev/null
  docker compose --project-directory "$SILL_BACKEND" --env-file "$env_file" -f "$COMPOSE_FILE" "$@"
}

sill_run() {
  if (( ${DRY_RUN:-0} )); then
    printf 'DRY:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

sill_pipx_venvs() {
  local path
  path="$(pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null)" || return 1
  [[ "$path" == /* ]] || return 1
  printf '%s\n' "$path"
}

sill_backend_bin_dir() {
  local venvs dir
  if command -v pipx >/dev/null 2>&1 && venvs="$(sill_pipx_venvs)"; then
    dir="$venvs/sill-memory/bin"
    if [[ -x "$dir/python" && -x "$dir/sill" && -x "$dir/sill-mcp" ]]; then
      printf '%s\n' "$dir"
      return
    fi
  fi
  dir="$HOME/.local/share/sill-venv/bin"
  if [[ -x "$dir/python" && -x "$dir/sill" && -x "$dir/sill-mcp" ]]; then
    printf '%s\n' "$dir"
    return
  fi
  printf 'Could not locate the installed Sill environment. Run %s/install.sh.\n' "$SILL_DIR" >&2
  return 1
}

sill_bin() {
  local name="$1" dir exposed
  dir="$(sill_backend_bin_dir)" || return 1
  # Never choose a same-named executable from PATH.
  if [[ "$dir" != "$HOME/.local/share/sill-venv/bin" ]]; then
    exposed="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null)" || exposed=""
    if [[ "$exposed" == /* && -x "$exposed/$name" && "$exposed/$name" -ef "$dir/$name" ]]; then
      printf '%s\n' "$exposed/$name"
      return
    fi
  fi
  if [[ ! -x "$dir/$name" ]]; then
    printf 'Missing installed console script: %s\n' "$dir/$name" >&2
    return 1
  fi
  printf '%s\n' "$dir/$name"
}

sill_python() {
  local dir
  if [[ -n "${SILL_PYTHON:-}" ]]; then
    command -v "$SILL_PYTHON"
  elif dir="$(sill_backend_bin_dir 2>/dev/null)"; then
    printf '%s\n' "$dir/python"
  else
    command -v python3
  fi
}

sill_install_backend() {
  local dir venv="$HOME/.local/share/sill-venv" localbin="$HOME/.local/bin" name
  if command -v pipx >/dev/null 2>&1; then
    sill_run pipx install --force -e "$SILL_BACKEND" || return 1
    if (( ! ${DRY_RUN:-0} )); then
      # Do not accept a stale fallback venv if pipx did not expose this install.
      dir="$(sill_pipx_venvs)" || return 1
      dir="$dir/sill-memory/bin"
      if [[ ! -x "$dir/python" || ! -x "$dir/sill" || ! -x "$dir/sill-mcp" || ! -x "$dir/sill-worker" ]]; then
        printf 'pipx did not expose the Sill package in its configured environment.\n' >&2
        return 1
      fi
    fi
    return
  fi
  sill_run python3 -m venv "$venv" || return 1
  sill_run "$venv/bin/python" -m pip install -e "$SILL_BACKEND" || return 1
  sill_run mkdir -p "$localbin" || return 1
  for name in sill sill-mcp sill-worker; do
    if [[ -e "$localbin/$name" || -L "$localbin/$name" ]] &&
       [[ ! "$localbin/$name" -ef "$venv/bin/$name" ]]; then
      printf 'Leaving existing command alone: %s\n' "$localbin/$name"
    else
      sill_run ln -sfn "$venv/bin/$name" "$localbin/$name" || return 1
    fi
  done
}

sill_select_embeddings() {
  local engine os arch settings line model platform image
  if (( ${DRY_RUN:-0} )); then
    printf 'DRY: select the pinned embedding image for the Docker engine.\n'
    return 0
  fi
  engine="$(docker info --format '{{.OSType}} {{.Architecture}}')" || return 1
  read -r os arch <<< "$engine"
  printf 'Docker engine: %s\n' "$engine"
  if [[ "$os" != linux ]]; then
    printf 'Sill requires a Linux container engine; found %s.\n' "$os" >&2
    return 1
  fi
  case "$arch" in
    aarch64|arm64) platform=linux/arm64 ;;
    x86_64|amd64) platform=linux/amd64 ;;
    *) platform="" ;;
  esac
  # Use Compose's precedence; keep the values internal.
  if ! settings="$(sill_compose config --environment 2>/dev/null)"; then
    printf 'Could not resolve Compose settings in %s.\n' "$SILL_ENV_FILE" >&2
    return 1
  fi
  model=unsloth/embeddinggemma-300m
  image=""
  while IFS= read -r line; do
    case "$line" in
      EMBEDDING_PLATFORM=?*) platform="${line#*=}" ;;
      EMBEDDING_MODEL_ID=?*) model="${line#*=}" ;;
      EMBEDDING_IMAGE=?*) image="${line#*=}" ;;
    esac
  done <<< "$settings"
  case "$platform" in
    linux/arm64)
      SILL_DEFAULT_EMBEDDING_IMAGE=ghcr.io/huggingface/text-embeddings-inference@sha256:798cd7615e060a72ccbdf19aa0ada52289c6a0147490195bafaece6ce04d70f1
      ;;
    linux/amd64)
      SILL_DEFAULT_EMBEDDING_IMAGE=ghcr.io/huggingface/text-embeddings-inference@sha256:8de25e75ce39617f17f2f6c77d60a4f75b65e779ed5005420eb1400072a15c1c
      ;;
    *)
      if [[ -z "$platform" || -z "$image" ]]; then
        printf 'No pinned image for this Docker target. Set EMBEDDING_IMAGE and EMBEDDING_PLATFORM together.\n' >&2
        return 1
      fi
      SILL_DEFAULT_EMBEDDING_IMAGE="$image"
      ;;
  esac
  SILL_DEFAULT_EMBEDDING_PLATFORM="$platform"
  SILL_DEFAULT_EMBEDDING_REVISION=main
  if [[ "$model" == unsloth/embeddinggemma-300m ]]; then
    SILL_DEFAULT_EMBEDDING_REVISION=bfa3c846ac738e62aa61806ef9112d34acb1dc5a
  fi
  export SILL_DEFAULT_EMBEDDING_PLATFORM SILL_DEFAULT_EMBEDDING_IMAGE SILL_DEFAULT_EMBEDDING_REVISION
  printf 'Embeddings: %s (%s)\n' "${image:-$SILL_DEFAULT_EMBEDDING_IMAGE}" "$platform"
}

sill_create_env_file() {
  if [[ ! -e "$SILL_ENV_FILE" && ! -L "$SILL_ENV_FILE" ]]; then
    (umask 077; set -o noclobber; : > "$SILL_ENV_FILE")
  fi
}

sill_prepare_instance() {
  local mode="${1:-shell}" dir resolved
  dir="$(sill_backend_bin_dir)" || return 1
  if ! resolved="$(sill_compose config --format json 2>/dev/null |
    "$dir/python" -I "$SILL_DIR/scripts/install_instance.py" "$SILL_ENV_FILE" "$mode")"; then
    printf 'Could not select the database for %s.\n' "$COMPOSE_FILE" >&2
    return 1
  fi
  SILL_RUNTIME_ENV_FILE="$resolved"
  if [[ "$resolved" != "$SILL_ENV_FILE" ]]; then
    SILL_TEMP_ENV_FILE="$resolved"
  fi
}

sill_cleanup_instance() {
  if [[ -n "${SILL_TEMP_ENV_FILE:-}" ]]; then
    rm -f -- "$SILL_TEMP_ENV_FILE"
  fi
}

sill_check_running_instance() {
  local id dir identity
  if ! id="$(sill_compose ps -q db)" || [[ -z "$id" ]]; then
    printf 'Could not find the running database for the selected Compose instance.\n' >&2
    return 1
  fi
  dir="$(sill_backend_bin_dir)" || return 1
  docker inspect --format '{{json (index .NetworkSettings.Ports "5432/tcp")}}' "$id" |
    "$dir/python" -I "$SILL_DIR/scripts/install_instance.py" "$SILL_RUNTIME_ENV_FILE" check-port || return 1
  if ! identity="$(sill_db_query     'SELECT json_build_array(system_identifier::text, current_database()) FROM pg_catalog.pg_control_system()' 2>/dev/null)"; then
    printf 'Could not read the selected Compose database identity. Check the db service and POSTGRES_USER.\n' >&2
    return 1
  fi
  printf '%s\n' "$identity" |
    "$dir/python" -I "$SILL_DIR/scripts/install_instance.py" "$SILL_RUNTIME_ENV_FILE" check-identity
}

sill_db_query() {
  # TCP avoids accepting initdb's socket-only temporary server.
  sill_compose exec -T db sh -c \
    'exec psql -h 127.0.0.1 -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -c "$1"' \
    sill-db-query "$1"
}

sill_db_ready() {
  local file id expected="" actual ledgers
  if ! ledgers="$(sill_db_query "SELECT count(*) FROM pg_catalog.pg_class
    WHERE relname = 'schema_migrations' AND relkind IN ('r', 'p')" 2>/dev/null)"; then
    printf 'db: could not inspect the schema_migrations ledger.\n' >&2
    return 1
  fi
  if [[ "$ledgers" =~ ^[0-9]+$ && "$ledgers" -gt 1 ]]; then
    printf 'db: multiple schema_migrations ledgers exist. Resolve the ambiguity before starting Sill.\n' >&2
    return 1
  fi
  for file in "$SILL_BACKEND"/migrations/[0-9][0-9][0-9]_*.sql; do
    [[ -f "$file" ]] || continue
    id="${file##*/}"
    expected="${expected:+$expected$'\n'}${id%%_*}"
  done
  if [[ -z "$expected" ]] ||
     ! actual="$(sill_db_query 'SELECT migration_id FROM schema_migrations ORDER BY migration_id' 2>/dev/null)" ||
     [[ "$actual" != "$expected" ]]; then
    printf 'db: schema_migrations does not match this checkout. Check database initialization or run ./upgrade.sh.\n' >&2
    return 1
  fi
  if ! sill_db_query 'SELECT speaker FROM public.memories LIMIT 0' >/dev/null 2>&1; then
    printf 'db: public.memories.speaker is missing or inaccessible despite the migration ledger.\n' >&2
    return 1
  fi
}

sill_runtime_diagnostics() {
  local env_file="$SILL_ENV_FILE"
  [[ -e "$env_file" || -L "$env_file" ]] || env_file=/dev/null
  sill_compose ps -a "$@" >&2 || true
  printf 'Inspect this instance with: docker compose --project-directory %q --env-file %q -f %q logs --tail 60' \
    "$SILL_BACKEND" "$env_file" "$COMPOSE_FILE" >&2
  printf ' %q' "$@" >&2
  printf '\n' >&2
}

sill_wait_healthy() {
  local timeout="$1" started="$SECONDS" service id status all_ready db_checked=0
  shift
  if [[ ! "$timeout" =~ ^[1-9][0-9]*$ || "${#timeout}" -gt 8 ]]; then
    printf 'Readiness timeout must be a positive integer number of seconds.\n' >&2
    return 1
  fi
  while :; do
    all_ready=1
    for service in "$@"; do
      if ! id="$(sill_compose ps -a -q "$service" 2>/dev/null)" || [[ -z "$id" ]]; then
        status=missing
      elif ! status="$(docker inspect --format '{{.State.Status}}{{if .State.Health}} {{.State.Health.Status}}{{end}}' "$id" 2>/dev/null)"; then
        status=unavailable
      fi
      printf '  [%ss] %s: %s\n' "$(( SECONDS - started ))" "$service" "${status#running }"
      case "$service:$status" in
        maintenance_worker:running|*:'running healthy')
          if [[ "$service" == db ]] && (( ! db_checked )); then
            if ! sill_db_ready; then
              sill_runtime_diagnostics db
              return 1
            fi
            db_checked=1
          fi
          ;;
        *:missing|*:unavailable|*:dead*|*:exited*|*:paused*|*:restarting*|*:removing*|*:'running unhealthy')
          printf '%s reached terminal readiness state: %s.\n' "$service" "$status" >&2
          sill_runtime_diagnostics "$service"
          return 1
          ;;
        *)
          all_ready=0
          if (( SECONDS - started >= timeout )); then
            printf 'Readiness timed out after %ss: %s is %s.\n' "$timeout" "$service" "$status" >&2
            sill_runtime_diagnostics "$service"
            return 1
          fi
          ;;
      esac
    done
    if (( all_ready )); then
      return 0
    fi
    sleep 5
  done
}

sill_build_runtime() {
  sill_check_database_identity || return 1
  if [[ -n "$SILL_DATABASE_IDENTITY" ]]; then
    sill_run sill_compose build maintenance_worker
  else
    sill_run sill_compose build db maintenance_worker
  fi
}

sill_start_runtime() {
  sill_check_database_identity || return 1
  local services=(db embeddings rabbitmq) flags=()
  if [[ -n "$SILL_DATABASE_IDENTITY" ]]; then
    # Do not offer Compose a chance to replace the existing database, even
    # when its generated image tag or the current service definition changed.
    services=(embeddings rabbitmq)
    flags=(--no-deps)
  fi
  if ! sill_run sill_compose up -d ${flags[@]+"${flags[@]}"} "${services[@]}"; then
    sill_runtime_diagnostics db embeddings rabbitmq
    return 1
  fi
}

sill_finish_runtime() {
  local timeout="$1"
  sill_wait_healthy "$timeout" db embeddings rabbitmq || return 1
  sill_start_maintenance "$timeout"
}

sill_start_maintenance() {
  local timeout="$1"
  sill_check_database_identity || return 1
  # Core dependencies were checked above. Let our shared wait report
  # startup state rather than waiting without progress inside Compose.
  if ! sill_compose up -d --no-deps maintenance_worker; then
    sill_runtime_diagnostics maintenance_worker
    return 1
  fi
  sill_wait_healthy "$timeout" maintenance_worker
}

sill_import_seed() {
  local cli
  cli="$(sill_bin sill)" || return 1
  sill_check_running_instance || return 1
  "$cli" seed import "$SILL_DIR/seed/methodology.jsonl" --env-file "$SILL_RUNTIME_ENV_FILE"
}

# A full upgrade must check the actual processes, even under --no-verify.
sill_checkout_version() {
  python3 - "$SILL_BACKEND/pyproject.toml" <<'PYVERSION'
import pathlib, re, sys
print(re.search(r'^version = "([^" ]+)"', pathlib.Path(sys.argv[1]).read_text(), re.M).group(1))
PYVERSION
}

sill_check_runtime_versions() {
  local expected cli installed worker image_ref built running worker_version
  expected="$(sill_checkout_version)" || return 1
  cli="$(sill_bin sill-mcp)" || return 1
  installed="$("$cli" --version)" || return 1
  if [[ "$installed" != "sill-mcp $expected" ]]; then
    printf 'Installed sill-mcp version differs: %s (expected %s).\n' "$installed" "$expected" >&2
    return 1
  fi
  image_ref="$(sill_compose config --format json | python3 -c 'import json,sys; c=json.load(sys.stdin); print(c["services"]["maintenance_worker"].get("image", c["name"]+"-maintenance_worker"))')" || return 1
  built="$(docker image inspect --format '{{.Id}}' "$image_ref")" || return 1
  worker="$(sill_compose ps -q maintenance_worker)" || return 1
  running="$(docker inspect --format '{{.Image}}' "$worker")" || return 1
  worker_version="$(docker exec "$worker" python -c 'from importlib.metadata import version; print(version("sill-memory"))')" || return 1
  if [[ -z "$built" || "$built" != "$running" || "$worker_version" != "$expected" ]]; then
    printf 'Maintenance worker is stale: image %s, package %s; expected image %s, package %s.\n' "$running" "$worker_version" "$built" "$expected" >&2
    return 1
  fi
  printf 'Runtime version %s verified: installed MCP, MCP initialize, worker image %s and package.\n' "$expected" "$built"
}

# Only install.sh's explicit fresh-profile option calls this companion.
sill_install_profile() {
  local dir
  dir="$(sill_backend_bin_dir)" || return 1
  sill_compose config --format json |
    "$dir/python" -I "$SILL_DIR/scripts/install_profile.py" "$1" "$SILL_ENV_FILE" "$SILL_RUNTIME_ENV_FILE"
}


sill_preserve_embedding_identity() {
  local settings mode="$1" flag="" profile_flag="" key value
  if (( ! ${DRY_RUN:-0} )); then flag=--persist; fi
  if [[ "$mode" == --install && "${EMBEDDING_PROFILE:-raw}" != raw ]]; then profile_flag=--fresh-profile; fi
  sill_check_database_identity || return 1
  settings="$(sill_compose config --format json | "$(sill_python)" -I "$SILL_DIR/scripts/upgrade_runtime.py" "$SILL_ENV_FILE" "$mode" ${flag:+"$flag"} ${profile_flag:+"$profile_flag"})" || return 1
  sill_capture_database_identity "$settings" || return 1
  while IFS='=' read -r key value; do
    case "$key" in
      EMBEDDING_MODEL_ID|EMBEDDING_REVISION|EMBEDDING_DIMENSION) export "$key=$value" ;;
    esac
  done < <(printf '%s' "$settings" | python3 -c 'import json,sys; print("\n".join(k+"="+v for k,v in json.load(sys.stdin).items() if k != "_database"))')
}


sill_capture_database_identity() {
  local observed
  observed="$(printf '%s' "$1" | python3 -c 'import json,sys; d=json.load(sys.stdin).get("_database"); print(json.dumps(d,sort_keys=True) if d else "")')" || return 1
  if [[ -n "$SILL_DATABASE_IDENTITY" && "$SILL_DATABASE_IDENTITY" != "$observed" ]]; then
    printf 'Database identity changed during this operation; no replacement will be started.\n' >&2
    return 1
  fi
  if [[ -z "$SILL_DATABASE_IDENTITY" && -n "$observed" ]]; then
    printf 'Retaining the existing database container and immutable image. PostgreSQL/OS security updates, ports and restart settings are unchanged; an OS refresh needs a separate backed-up migration.\n'
  fi
  SILL_DATABASE_IDENTITY="$observed"
}

sill_check_database_identity() {
  [[ -n "$SILL_DATABASE_IDENTITY" ]] || return 0
  sill_compose config --format json |
    "$(sill_python)" -I "$SILL_DIR/scripts/upgrade_runtime.py" "$SILL_ENV_FILE" --check-database "$SILL_DATABASE_IDENTITY"
}
