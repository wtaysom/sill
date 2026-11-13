#!/usr/bin/env bash
# install.sh — one-shot Sill installer.
#
# Installs the backend, starts and checks the local runtime, imports the
# optional seed, and connects the selected MCP clients. Run it again to
# reuse the same Compose project and backend/.env.
#
# Flags:
#   --scope home|project   Where hooks are wired (default: project). This is
#                          a real tradeoff, not a style choice — see step 9
#                          above and docs/extending.md:
#                            project — narrower blast radius, no
#                              cross-project mixing, but only the project(s)
#                              you --hooks-for get recall/guards.
#                            home    — every prompt in every project pays
#                              the recall hook's latency, and any project's
#                              work can reach the one store; in exchange you
#                              never re-wire hooks per project again.
#   --no-seed              Skip the methodology seed.
#   --client TARGET        Select claude-code, codex, or claude-desktop.
#                          Repeat to select more than one. If omitted,
#                          connect the detected Claude Code and Codex CLIs.
#   --adopt-legacy         Replace an ambiguous bare sill-mcp registration.
#   --hooks-for <project>  Write per-project hook configs into <project>/.claude
#                          and <project>/.codex. Applies with --scope project
#                          (the default); additive, not exclusive, with
#                          --scope home.
#   --dry-run              Print each step without doing it.
#   --help                 Show this help.

set -euo pipefail

# --- argparse -----------------------------------------------------------------
NO_SEED=0
DRY_RUN=0
HOOKS_FOR=""
SCOPE="project"
ADOPT_LEGACY=0
CLIENTS=()

usage() {
  sed -n '2,/^$/p' "$0"
}

while (( $# )); do
  case "$1" in
    --no-seed)
      NO_SEED=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --hooks-for)
      [[ $# -ge 2 ]] || { echo "install.sh: --hooks-for requires a path" >&2; exit 2; }
      HOOKS_FOR="$2"
      shift 2
      ;;
    --client)
      [[ $# -ge 2 ]] || { echo "install.sh: --client requires a target" >&2; exit 2; }
      case "$2" in
        claude-code|codex|claude-desktop) ;;
        *) echo "install.sh: unknown client: $2 (use claude-code, codex, or claude-desktop)" >&2; exit 2 ;;
      esac
      CLIENTS[${#CLIENTS[@]}]="$2"
      shift 2
      ;;
    --adopt-legacy)
      ADOPT_LEGACY=1
      shift
      ;;
    --scope)
      [[ $# -ge 2 ]] || { echo "install.sh: --scope requires a value (home or project)" >&2; exit 2; }
      SCOPE="$2"
      case "$SCOPE" in
        home|project) ;;
        *)
          echo "install.sh: invalid --scope '$SCOPE' — valid values: home, project" >&2
          exit 2
          ;;
      esac
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "install.sh: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# shellcheck source=scripts/install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/scripts/install_common.sh"

say() { printf '\n=== %s ===\n' "$*"; }
note() { printf '    %s\n' "$*"; }

step_preflight() {
  say "Step 1/10: preflight"
  local command
  for command in docker python3; do
    if ! command -v "$command" >/dev/null 2>&1; then
      echo "install.sh: missing prerequisite: $command" >&2
      return 1
    fi
  done
  if (( ${#CLIENTS[@]} == 0 )); then
    if command -v claude >/dev/null 2>&1; then CLIENTS[0]=claude-code; fi
    if command -v codex >/dev/null 2>&1; then CLIENTS[${#CLIENTS[@]}]=codex; fi
  fi
  sill_select_embeddings
}

step_install_backend() {
  say "Step 2/10: install backend (sill / sill-mcp / sill-worker)"
  sill_install_backend
}

step_prepare_instance() {
  if (( DRY_RUN )); then
    note "DRY: would select the Compose instance and create a private $SILL_ENV_FILE if absent"
    return
  fi
  local mode=install
  if (( ${#CLIENTS[@]} )); then mode=persist; fi
  sill_prepare_instance "$mode"
  sill_create_env_file
  if (( ${#CLIENTS[@]} )); then
    register_clients --dry-run
  fi
}

step_docker_up() {
  say "Step 3/10: build images and start db, embeddings, rabbitmq"
  sill_run sill_compose build
  sill_start_runtime
}

step_wait_healthy() {
  say "Step 4/10: wait for the schema and start the maintenance worker"
  sill_run sill_finish_runtime "${SILL_INSTALL_WAIT_HEALTHY_S:-$SILL_WAIT_HEALTHY_DEFAULT}"
}

step_import_seed() {
  say "Step 5/10: import methodology seed"
  if (( NO_SEED )); then
    note "--no-seed: skipping"
  else
    sill_run sill_import_seed
  fi
}

step_plugin_symlink() {
  say "Step 6/10: symlink plugin into ~/.claude/plugins/local"
  sill_run mkdir -p "$HOME/.claude/plugins/local"
  sill_run ln -sfn "$SILL_DIR/plugin" "$HOME/.claude/plugins/local/sill-plugin"
}

register_clients() {
  local cli mcp target
  if (( ${#CLIENTS[@]} == 0 )); then
    note "No agent CLI detected. Use --client to select a client explicitly."
    return
  fi
  if (( DRY_RUN )); then
    for target in "${CLIENTS[@]}"; do
      note "DRY: would connect $target to $SILL_ENV_FILE"
    done
    return
  fi
  cli="$(sill_bin sill)" || return 1
  mcp="$(sill_bin sill-mcp)" || return 1
  if (( ADOPT_LEGACY )); then set -- "$@" --adopt-legacy; fi
  for target in "${CLIENTS[@]}"; do
    "$cli" connect "$target" --env-file "$SILL_ENV_FILE" --mcp-command "$mcp" "$@" || return 1
  done
}

step_mcp_wiring() {
  say "Step 8/10: register Sill with the selected clients"
  register_clients
}

# Render Codex hooks.json at $2 from template $1, skipping (with a note) if
# the destination already exists — re-rendering an *existing* install is
# ./upgrade.sh's job (it shows a diff and supports --force-hooks; see
# upgrade.sh's own header), not this first-wiring step's. Shared by both
# --scope project (per-project .codex/hooks.json) and --scope home
# (~/.codex/hooks.json).
_render_codex_hooks() {
  local template="$1" dst="$2" plugin_dir="$3" sill_py="$4"
  if (( DRY_RUN )); then
    note "DRY: would render $template -> $dst with SILL_PLUGIN_DIR=$plugin_dir SILL_PYTHON=$sill_py"
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  if [[ -f "$dst" ]]; then
    note "  $dst already exists; leaving as-is (see ./upgrade.sh --hooks-for to refresh)"
    return 0
  fi
  python3 - "$template" "$dst" "$plugin_dir" "$sill_py" <<'PY'
import json, pathlib, shlex, sys
src, dst, plugin, py = sys.argv[1:5]
data = json.loads(pathlib.Path(src).read_text())
for entries in data["hooks"].values():
    for entry in entries:
        for hook in entry["hooks"]:
            command = hook["command"]
            prefix = "{{SILL_PYTHON}} {{SILL_PLUGIN_DIR}}/"
            if command.startswith(prefix):
                hook["command"] = shlex.join([py, str(pathlib.Path(plugin) / command[len(prefix):])])
pathlib.Path(dst).write_text(json.dumps(data, indent=2) + "\n")
print(f"  wrote {dst}")
PY
}

# Idempotently merge the template's hooks block into a Claude Code hooks
# JSON file. Both destinations this installer writes share the same
# top-level {"hooks": {...}} shape: <project>/.claude/settings.local.json
# (project scope) and ~/.claude/settings.json (home scope — Claude Code's
# own "globally" location; see docs/extending.md's "Adding your own hooks"
# section, and this repo's own live ~/.claude/settings.json, which already
# carries unrelated hooks under the same key).
_merge_claude_hooks() {
  local template="$1" dst="$2" plugin_dir="$3" sill_py="$4"
  if (( DRY_RUN )); then
    note "DRY: would idempotently merge hooks block into $dst"
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  python3 - "$template" "$dst" "$plugin_dir" "$sill_py" <<'PY'
import json, pathlib, shlex, sys
template_path, dst_path, plugin, py = sys.argv[1:5]
template = json.loads(pathlib.Path(template_path).read_text())
for entries in template["hooks"].values():
    for entry in entries:
        for hook in entry["hooks"]:
            command = hook["command"]
            prefix = "{{SILL_PYTHON}} {{SILL_PLUGIN_DIR}}/"
            if command.startswith(prefix):
                hook["command"] = shlex.join([py, str(pathlib.Path(plugin) / command[len(prefix):])])
dst = pathlib.Path(dst_path)
data = {}
if dst.exists():
    try:
        data = json.loads(dst.read_text() or "{}")
    except json.JSONDecodeError:
        print(f"install.sh: {dst} isn't valid JSON; leaving it alone.")
        sys.exit(0)
hooks = data.setdefault("hooks", {})
changed = False
for event, entries in template.get("hooks", {}).items():
    existing = hooks.setdefault(event, [])
    # naive dedup: compare exact JSON repr
    existing_repr = {json.dumps(e, sort_keys=True) for e in existing}
    for entry in entries:
        if json.dumps(entry, sort_keys=True) not in existing_repr:
            existing.append(entry)
            changed = True
if changed:
    dst.write_text(json.dumps(data, indent=2) + "\n")
    print(f"  merged sill hooks into {dst}")
else:
    print(f"  {dst} already has sill hooks; nothing to do")
PY
}

# Idempotently append plugin/claude.home.md.template's ambient-instructions
# block to ~/.claude/CLAUDE.md. Never touches anything already in that
# file — it's the operator's own global instructions file, which may
# already carry substantial unrelated content — keyed on a marker comment
# so a second run is a no-op instead of a duplicate append.
_install_home_ambient_file() {
  local plugin_dir="$1"
  local template="$plugin_dir/claude.home.md.template"
  local dst="$HOME/.claude/CLAUDE.md"
  if (( DRY_RUN )); then
    note "DRY: would idempotently append $template -> $dst"
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  python3 - "$template" "$dst" <<'PY'
import pathlib, sys
template_path, dst_path = sys.argv[1:3]
block = pathlib.Path(template_path).read_text().rstrip("\n") + "\n"
marker = "<!-- sill:home-scope-ambient -->"
dst = pathlib.Path(dst_path)
existing = dst.read_text() if dst.exists() else ""
if marker in existing:
    print(f"  {dst} already has the Sill ambient block; nothing to do")
else:
    prefix = existing
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix and not prefix.endswith("\n\n"):
        prefix += "\n"
    dst.write_text(prefix + block)
    print(f"  appended Sill ambient block -> {dst}")
PY
}

# --- step 8: hook wiring, per --scope ------------------------------------------
step_hook_wiring() {
  say "Step 9/10: hook wiring (scope: $SCOPE)"
  local plugin_dir="$SILL_DIR/plugin"
  local template="$SILL_DIR/plugin/codex.hooks.json.template"
  local sill_py
  sill_py="$(sill_python)"

  if [[ "$SCOPE" == "home" ]]; then
    note "home scope: every prompt in every project will pay the recall hook's latency, and any project's work can reach this one store."
    _render_codex_hooks "$template" "$HOME/.codex/hooks.json" "$plugin_dir" "$sill_py"
    _merge_claude_hooks "$template" "$HOME/.claude/settings.json" "$plugin_dir" "$sill_py"
    _install_home_ambient_file "$plugin_dir"
  fi

  if [[ -z "$HOOKS_FOR" ]]; then
    if [[ "$SCOPE" == "project" ]]; then
      note "--hooks-for not provided; skipping"
    fi
    return 0
  fi

  local target
  target="$(cd "$HOOKS_FOR" && pwd)"
  note "project scope target: $target (narrower blast radius, no cross-project mixing)"
  _render_codex_hooks "$template" "$target/.codex/hooks.json" "$plugin_dir" "$sill_py"
  _merge_claude_hooks "$template" "$target/.claude/settings.local.json" "$plugin_dir" "$sill_py"
}

step_verify() {
  say "Step 7/10: verify the runtime and MCP handshake"
  if (( NO_SEED )); then
    sill_run "$SILL_DIR/verify.sh" --no-seed
  else
    sill_run "$SILL_DIR/verify.sh"
  fi
}

# --- step 10: banner ----------------------------------------------------------
step_banner() {
  say "Step 10/10: next steps"
  if (( DRY_RUN )); then return; fi
  cat <<'EOF'
    Sill is installed.

    Next: turn this working install into somebody's Sill.
      Read docs/onboarding/README.md — a phased runbook, walked with the
      person present, ending at a christening: a charter in their own
      words, a name, a first deliberate memory, a chosen cadence.

    Optional follow-ups:
      * Install the episodic-memory marketplace plugin in Claude Code for richer recall.
      * Restart any connected client so it picks up the new MCP entry.
      * Configure SILL_EPISODIC_MEMORY_PATH in your shell if you want the
        spontaneous-recall hook to inject conversation snippets.

    To remove everything: ./uninstall.sh
    To wipe the db and re-seed: ./reset.sh
EOF
}

# --- main ---------------------------------------------------------------------
# Guarded so backend/tests/test_install_scope.py can `source` this file to
# unit-test individual step/helper functions (e.g. _install_home_ambient_file)
# against a tmp HOME without running the full 10-step pipeline. True whenever
# this file is executed directly (./install.sh or bash install.sh, the only
# ways a real operator runs it) and false when sourced.
if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
  trap sill_cleanup_instance EXIT
  step_preflight
  step_install_backend
  step_prepare_instance
  step_docker_up
  step_wait_healthy
  step_import_seed
  step_plugin_symlink
  step_verify
  step_mcp_wiring
  step_hook_wiring
  step_banner
fi
