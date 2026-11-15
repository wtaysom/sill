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

# --- Python interpreters ------------------------------------------------------
# Three interpreters can be in play, and on a Mac they often differ: python3
# on PATH runs these scripts' helpers and builds the fallback venv; pipx may
# build the package's environment with another; and an environment left by an
# earlier run may point at a third, or at one that has since been removed.
# Each is checked where it is used, and each message names the file it means.

sill_python_ok() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

sill_python_describe() {
  "$1" -c 'import platform, sys; print("%d.%d.%d, %s" % (sys.version_info[:3] + (platform.machine(),)))' 2>/dev/null ||
    printf 'not runnable'
}

sill_require_python() {
  local path minor found=0
  if ! path="$(command -v python3)"; then
    printf 'Sill needs Python 3.10 or newer, and there is no python3 on PATH.\n' >&2
    return 1
  fi
  if sill_python_ok "$path"; then
    printf 'Python: %s (%s)\n' "$path" "$(sill_python_describe "$path")"
    if [[ "$(uname -s)" == Darwin && "$(sysctl -n hw.optional.arm64 2>/dev/null)" == 1 &&
          "$(sill_python_describe "$path")" == *x86_64 ]]; then
      printf '  Note: this is an Intel build of Python running under Rosetta. Sill works with it; a native arm64 Python is faster.\n'
    fi
    return 0
  fi
  printf 'Sill needs Python 3.10 or newer. python3 on PATH is %s (%s).\n' "$path" "$(sill_python_describe "$path")" >&2
  # Newest first, with room for releases that do not exist yet.
  for (( minor = 30; minor >= 10; minor-- )); do
    if path="$(command -v "python3.$minor" 2>/dev/null)" && sill_python_ok "$path"; then
      printf '  %s is new enough. Put its directory first on PATH so that python3 means it, then rerun.\n' "$path" >&2
      found=1
    fi
  done
  if (( ! found )); then
    printf '  No newer python3.x was found on PATH. Install one (python.org, or: brew install python), open a new terminal, then rerun.\n' >&2
  fi
  return 1
}

# pipx builds new environments with its own default interpreter, which is the
# one pipx itself was installed under unless PIPX_DEFAULT_PYTHON says otherwise.
sill_pipx_python() {
  local default
  default="$(pipx environment --value PIPX_DEFAULT_PYTHON 2>/dev/null)" || return 0
  [[ -n "$default" ]] || return 0
  if ! sill_python_ok "$default"; then
    export PIPX_DEFAULT_PYTHON="$(command -v python3)"
    printf 'pipx would build with %s (%s); using %s for this install instead.\n' \
      "$default" "$(sill_python_describe "$default")" "$PIPX_DEFAULT_PYTHON"
  fi
}

sill_check_installed_python() {
  local python="$1"
  if ! sill_python_ok "$python"; then
    printf 'The installed Sill environment runs %s (%s); it needs Python 3.10 or newer.\n' \
      "$python" "$(sill_python_describe "$python")" >&2
    printf '  Remove that environment (pipx uninstall sill-memory, or delete %s), then rerun.\n' \
      "$HOME/.local/share/sill-venv" >&2
    return 1
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
    sill_pipx_python
    sill_run pipx install --force -e "$SILL_BACKEND" || return 1
    if (( ! ${DRY_RUN:-0} )); then
      # Do not accept a stale fallback venv if pipx did not expose this install.
      dir="$(sill_pipx_venvs)" || return 1
      dir="$dir/sill-memory/bin"
      if [[ ! -x "$dir/python" || ! -x "$dir/sill" || ! -x "$dir/sill-mcp" || ! -x "$dir/sill-worker" ]]; then
        printf 'pipx did not expose the Sill package in its configured environment.\n' >&2
        return 1
      fi
      sill_check_installed_python "$dir/python" || return 1
    fi
    return
  fi
  if [[ -e "$venv/bin/python" || -L "$venv/bin/python" ]] && ! sill_python_ok "$venv/bin/python"; then
    # Left by an earlier run under an older Python, or its interpreter has
    # since been removed. Building over it would keep the old interpreter.
    printf 'Rebuilding %s: its interpreter is %s.\n' "$venv" "$(sill_python_describe "$venv/bin/python")"
    sill_run python3 -m venv --clear "$venv" || return 1
  else
    sill_run python3 -m venv "$venv" || return 1
  fi
  sill_run "$venv/bin/python" -m pip install -e "$SILL_BACKEND" || return 1
  if (( ! ${DRY_RUN:-0} )); then
    sill_check_installed_python "$venv/bin/python" || return 1
  fi
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
    printf 'Retaining the existing database image and data volume. PostgreSQL/OS security updates are unchanged; an OS refresh needs a separate backed-up migration.\n'
  fi
  SILL_DATABASE_IDENTITY="$observed"
}

# --- a database container from before this checkout's settings ----------------
# v0.2 published PostgreSQL on every interface, with trust authentication and
# no restart policy. upgrade.sh closes that. The decision is made by
# scripts/upgrade_runtime.py (database_plan); the functions below report it
# and carry it out.
#
# SILL_DATABASE_STEP says what this run has done to the database container, so
# that every exit, an interrupt included, can say what is true:
#   ""         nothing, or finished: the recorded identity names the database
#   stopped    this run stopped it; the old container still exists
#   replacing  Compose was asked to replace it; either container may exist
#   failed     it is not as this run found it, and the messages have said how
SILL_DATABASE_STEP=""
SILL_DATABASE_STEP_CONTAINER=""
SILL_DATABASE_COPY=""
SILL_DATABASE_COPY_HELPER=""

sill_database_plan() {
  sill_compose config --format json |
    "$(sill_python)" -I "$SILL_DIR/scripts/upgrade_runtime.py" "$SILL_ENV_FILE" --database-plan "$SILL_DATABASE_IDENTITY"
}

sill_plan_field() {
  printf '%s' "$1" | python3 -c 'import json,sys
v = json.load(sys.stdin).get(sys.argv[1])
print(", ".join(v) if isinstance(v, list) else "1" if v is True else "" if v is None or v is False else v)' "$2"
}

# Read-only: say what the upgrade will do, before the backup and in a dry run.
# install.sh keeps the container whatever it finds, and says how to change that.
sill_database_notice() {
  local mode="${1:-upgrade}" plan action
  [[ -n "$SILL_DATABASE_IDENTITY" ]] || return 0
  plan="$(sill_database_plan)" || return 1
  action="$(sill_plan_field "$plan" action)" || return 1
  case "$action" in
    recreate|blocked)
      printf 'The database container publishes port 5432 on every network interface (%s) and accepts connections without a password.\n' "$(sill_plan_field "$plan" published)"
      if [[ "$mode" == install ]]; then
        printf '  install.sh leaves it that way. Run ./upgrade.sh to close the port.\n'
      elif [[ "$action" == recreate ]]; then
        printf '  After the backup, the upgrade will stop it, keep a copy of its data volume, and start a new container from the same image on the same volume, published on %s only and restarting %s.\n' \
          "$(sill_plan_field "$plan" bind)" "$(sill_plan_field "$plan" restart)"
        printf '  To keep it reachable from other machines, set SILL_BIND_ADDR=0.0.0.0 in %s before upgrading.\n' "$SILL_ENV_FILE"
      else
        printf '  The upgrade will leave it that way: it could not prove that %s is the image this database runs on, so a new container might not be an equivalent one. See "Closing the database port" in docs/UPGRADING.md.\n' "$(sill_plan_field "$plan" tag)"
      fi
      ;;
    restart)
      if [[ "$mode" != install ]]; then
        printf 'The database container restarts "%s"; the upgrade will set that to %s in place.\n' \
          "$(sill_plan_field "$plan" restart_now)" "$(sill_plan_field "$plan" restart)"
      fi
      ;;
  esac
}

# The old container still exists, stopped by this run: put the database back.
# Returns 1 when it is running again as it was, 2 when it is not.
sill_database_restore() {
  local container="$1"
  if sill_compose start db >/dev/null 2>&1 &&
     [[ "$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null)" == true ]]; then
    SILL_DATABASE_STEP=""
    printf 'The database was started again as it was.\n' >&2
    return 1
  fi
  SILL_DATABASE_STEP=failed
  printf 'The database container %s is stopped and could not be started again. This step did not change its data volume. Start it with: docker start %s\n' \
    "$container" "$container" >&2
  return 2
}

# The replacement is not usable. Stop it, so that nothing keeps restarting on
# the volume, and say only what is known.
sill_database_abandon() {
  local container="$1" copy="$2" env_file="$SILL_ENV_FILE"
  [[ -e "$env_file" || -L "$env_file" ]] || env_file=/dev/null
  SILL_DATABASE_STEP=failed
  sill_compose stop -t 120 db >/dev/null 2>&1 || true
  printf 'The database is stopped. No migration has run, and the installed package and the worker image are the ones from before.\n' >&2
  printf 'A copy of its data volume, made before this change with the database stopped, is in the Docker volume %s.\n' "$copy" >&2
  printf 'See what the container reported with: docker logs %s\n' "$container" >&2
  printf 'Start it with: docker compose --project-directory %q --env-file %q -f %q up -d --no-build --no-deps db\n' \
    "$SILL_BACKEND" "$env_file" "$COMPOSE_FILE" >&2
  printf 'Then run ./upgrade.sh again.\n' >&2
}

# After the backup and before the package, images or schema change.
# $1 names this run (the backup's stamp); $2 is the plan upgrade.sh acted on.
# Returns 0 when done or not needed, 1 when the database is as it was, and 2
# when it is not and the messages say how.
sill_secure_database() {
  local stamp="$1" plan="$2" action container image tag volume copy restart old id status adopted
  local started timeout="${SILL_INSTALL_WAIT_HEALTHY_S:-$SILL_WAIT_HEALTHY_DEFAULT}"
  action="$(sill_plan_field "$plan" action)" || return 1
  container="$(sill_plan_field "$plan" container)" || return 1
  restart="$(sill_plan_field "$plan" restart)" || return 1
  case "$action" in
    keep) return 0 ;;
    restart|blocked)
      if [[ -n "$(sill_plan_field "$plan" restart_differs)" ]]; then
        if ! docker update --restart "$restart" "$container" >/dev/null; then
          printf 'Could not set the restart policy of %s.\n' "$container" >&2
          return 1
        fi
        printf 'Database container %s now restarts %s.\n' "$container" "$restart"
      fi
      if [[ "$action" == blocked ]]; then
        printf 'WARNING: %s still publishes port 5432 on every network interface without a password. See "Closing the database port" in docs/UPGRADING.md.\n' "$container" >&2
      fi
      return 0
      ;;
    recreate) ;;
    *) printf 'Unknown database plan: %s\n' "$action" >&2; return 1 ;;
  esac
  if [[ ! "$timeout" =~ ^[1-9][0-9]*$ || "${#timeout}" -gt 8 ]]; then
    printf 'Readiness timeout must be a positive integer number of seconds.\n' >&2
    return 1
  fi
  image="$(sill_plan_field "$plan" image)" || return 1
  tag="$(sill_plan_field "$plan" tag)" || return 1
  volume="$(sill_plan_field "$plan" volume)" || return 1
  old="$(sill_plan_field "$plan" container_id)" || return 1
  copy="${volume}_before_${stamp}"
  SILL_DATABASE_STEP_CONTAINER="$container"

  printf 'Closing the database port. Stopping %s.\n' "$container"
  SILL_DATABASE_STEP=stopped
  # Compose's default is ten seconds and then SIGKILL; a shutdown checkpoint can take longer.
  if ! sill_compose stop -t 120 db; then
    printf 'Could not stop the database.\n' >&2
    sill_database_restore "$container"
    return $?
  fi

  # The second copy is made with the database stopped, by the image it runs
  # on, so it is the same files with the same owner and modes. The helper is
  # named so that an interrupted copy can be removed rather than left running.
  printf 'Copying its data volume %s to %s.\n' "$volume" "$copy"
  SILL_DATABASE_COPY="$copy"
  SILL_DATABASE_COPY_HELPER="sill_copy_${stamp}"
  if ! docker run --rm --name "$SILL_DATABASE_COPY_HELPER" --network none \
       -v "$volume:/from:ro" -v "$copy:/to" "$image" \
       sh -c 'cp -a /from/. /to/ && chown --reference=/from /to && chmod --reference=/from /to' </dev/null; then
    printf 'Could not copy the database volume.\n' >&2
    sill_discard_database_copy
    sill_database_restore "$container"
    return $?
  fi
  SILL_DATABASE_COPY=""
  SILL_DATABASE_COPY_HELPER=""

  # The tag could have been pointed at another image while the copy ran.
  if [[ "$(docker image inspect --format '{{.Id}}' "$tag" 2>/dev/null)" != "$image" ]]; then
    printf '%s no longer names the image this database runs on; the container was not replaced. The copy in %s is complete and can be removed.\n' "$tag" "$copy" >&2
    sill_database_restore "$container"
    return $?
  fi

  SILL_DATABASE_STEP=replacing
  # --no-build: only the existing image, just checked. No terminal: Compose
  # must never be able to ask a question about the data volume.
  if ! sill_compose up -d --no-build --no-deps db </dev/null; then
    if [[ "$(docker inspect --format '{{.Id}}' "$container" 2>/dev/null)" == "$old" ]]; then
      printf 'Compose did not replace the container.\n' >&2
      sill_database_restore "$container"
      return $?
    fi
    sill_database_abandon "$container" "$copy"
    return 2
  fi

  # The ledger is still the old one, so sill_wait_healthy would refuse it.
  started="$SECONDS"
  while :; do
    status=missing
    if id="$(sill_compose ps -a -q db 2>/dev/null)" && [[ -n "$id" ]]; then
      status="$(docker inspect --format '{{.State.Status}}{{if .State.Health}} {{.State.Health.Status}}{{end}}' "$id" 2>/dev/null)" || status=unavailable
    fi
    printf '  [%ss] db: %s\n' "$(( SECONDS - started ))" "${status#running }"
    case "$status" in
      'running healthy') break ;;
      # With a restart policy a server that exits shows as restarting, not exited.
      missing|unavailable|dead*|exited*|paused*|restarting*|removing*|'running unhealthy')
        printf 'The recreated database reached state: %s.\n' "$status" >&2
        sill_database_abandon "$container" "$copy"
        return 2
        ;;
    esac
    if (( SECONDS - started >= timeout )); then
      printf 'The recreated database was not healthy after %ss (%s).\n' "$timeout" "$status" >&2
      sill_database_abandon "$container" "$copy"
      return 2
    fi
    sleep 2
  done

  if ! adopted="$(sill_compose config --format json |
       "$(sill_python)" -I "$SILL_DIR/scripts/upgrade_runtime.py" "$SILL_ENV_FILE" --adopt-recreated "$SILL_DATABASE_IDENTITY")"; then
    printf 'The new container is not the same image on the same volume with the planned settings.\n' >&2
    sill_database_abandon "$container" "$copy"
    return 2
  fi
  SILL_DATABASE_IDENTITY="$adopted"
  SILL_DATABASE_STEP=""
  printf 'Database recreated from the same image on the same volume: published on %s only, restarting %s.\n' \
    "$(sill_plan_field "$plan" bind)" "$restart"
  printf 'Its data as it was before this upgrade is kept in the Docker volume %s. Remove that volume when you no longer want it.\n' "$copy"
}

# A copy that did not finish must not be left under the name of a good one.
sill_discard_database_copy() {
  local copy="$SILL_DATABASE_COPY" helper="$SILL_DATABASE_COPY_HELPER"
  SILL_DATABASE_COPY=""
  SILL_DATABASE_COPY_HELPER=""
  [[ -n "$copy" ]] || return 0
  if [[ -n "$helper" ]]; then docker rm -f "$helper" >/dev/null 2>&1 || true; fi
  if ! docker volume rm "$copy" >/dev/null 2>&1; then
    printf 'The incomplete copy in the Docker volume %s could not be removed. Do not rely on it; remove it with: docker volume rm %s\n' "$copy" "$copy" >&2
  fi
}

# For an EXIT trap. The step's own failures have already spoken; this covers
# an interrupt that arrives while it holds the database stopped.
sill_interrupted_database_step() {
  case "$SILL_DATABASE_STEP" in
    stopped)
      printf 'Interrupted while the database was stopped for this upgrade.\n' >&2
      sill_discard_database_copy
      sill_database_restore "$SILL_DATABASE_STEP_CONTAINER" || true
      ;;
    replacing)
      SILL_DATABASE_STEP=failed
      printf 'Interrupted while the database container was being replaced. Check it with: docker ps -a --filter name=%s\n' "$SILL_DATABASE_STEP_CONTAINER" >&2
      ;;
  esac
}

sill_check_database_identity() {
  [[ -n "$SILL_DATABASE_IDENTITY" ]] || return 0
  sill_compose config --format json |
    "$(sill_python)" -I "$SILL_DIR/scripts/upgrade_runtime.py" "$SILL_ENV_FILE" --check-database "$SILL_DATABASE_IDENTITY"
}
