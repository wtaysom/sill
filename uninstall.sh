#!/usr/bin/env bash
# uninstall.sh — tear down a Sill install.
#
# Flags:
#   --keep-data   Leave the postgres / embeddings_cache / rabbitmq volumes intact.
#   --yes         Skip the confirmation prompt.
#   --help        Show this help.
#
# By default this drops Docker volumes, removes the backend install,
# removes the plugin symlink. Client MCP registrations are left for
# deliberate removal. Remove the Sill entry for each client you connected.
#   Claude Code      mcpServers.sill in ~/.claude.json
#   Codex            mcp_servers.sill in ${CODEX_HOME:-$HOME/.codex}/config.toml
#   Claude Desktop   mcpServers.sill in ~/Library/Application Support/Claude/claude_desktop_config.json

set -euo pipefail

KEEP_DATA=0
ASSUME_YES=0

usage() { sed -n '2,14p' "$0"; }

while (( $# )); do
  case "$1" in
    --keep-data) KEEP_DATA=1; shift ;;
    --yes|-y)    ASSUME_YES=1; shift ;;
    --help|-h)   usage; exit 0 ;;
    *) echo "uninstall.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# shellcheck source=scripts/install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/scripts/install_common.sh"

if (( ! ASSUME_YES )); then
  if (( KEEP_DATA )); then
    msg="This will stop sill containers and remove the backend + plugin symlink. Volumes are kept."
  else
    msg="This will stop sill containers, drop volumes (DATA LOSS), and remove the backend + plugin symlink."
  fi
  printf '%s\nContinue? [y/N] ' "$msg"
  read -r reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) echo "aborted"; exit 1 ;;
  esac
fi

say() { printf '\n=== %s ===\n' "$*"; }
note() { printf '  %s\n' "$*"; }

# --- step 1: docker compose down ---------------------------------------------
say "1/4: docker compose down"
if (( KEEP_DATA )); then
  sill_compose down --remove-orphans
else
  sill_compose down -v --remove-orphans
fi

# --- step 2: uninstall backend -----------------------------------------------
say "2/4: uninstall backend"
if command -v pipx >/dev/null 2>&1 &&
   pipx_venvs="$(sill_pipx_venvs)" && [[ -x "$pipx_venvs/sill-memory/bin/sill" ]]; then
  pipx uninstall sill-memory
  note "removed pipx package sill-memory"
fi
venv="$HOME/.local/share/sill-venv"
if [[ -d "$venv" ]]; then
  rm -rf "$venv"
  note "removed venv at $venv"
fi
for bin in sill sill-mcp sill-worker; do
  link="$HOME/.local/bin/$bin"
  if [[ -L "$link" && "$(readlink "$link")" == "$venv/bin/$bin" ]]; then
    rm -f "$link"
    note "removed symlink $link"
  fi
done

# --- step 3: plugin symlink --------------------------------------------------
say "3/4: plugin symlink"
plugin_link="$HOME/.claude/plugins/local/sill-plugin"
if [[ -L "$plugin_link" || -e "$plugin_link" ]]; then
  rm -f "$plugin_link"
  note "removed $plugin_link"
else
  note "$plugin_link not present"
fi

# --- step 4: MCP entry warning -----------------------------------------------
say "4/4: MCP entries — manual edit"
cat <<'EOF'
  To finish uninstalling, remove the Sill entry for each client you connected.

    Claude Code      mcpServers.sill in ~/.claude.json
    Codex            mcp_servers.sill in ${CODEX_HOME:-$HOME/.codex}/config.toml
    Claude Desktop   mcpServers.sill in ~/Library/Application Support/Claude/claude_desktop_config.json
EOF
