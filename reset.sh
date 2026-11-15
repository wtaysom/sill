#!/usr/bin/env bash
# reset.sh — drop the db volume and re-seed.
#
# Flags:
#   --yes / -y    Skip the confirmation prompt.
#   --no-seed     Skip re-import (just down -v and back up).
#   --help / -h   Show this help.

set -euo pipefail

ASSUME_YES=0
NO_SEED=0

usage() { sed -n '2,9p' "$0"; }

while (( $# )); do
  case "$1" in
    --yes|-y) ASSUME_YES=1; shift ;;
    --no-seed) NO_SEED=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "reset.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# shellcheck source=scripts/install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/scripts/install_common.sh"
trap sill_cleanup_instance EXIT

if (( ! ASSUME_YES )); then
  printf 'This will DROP the sill postgres volume and re-seed (DATA LOSS).\nContinue? [y/N] '
  read -r reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) echo "aborted"; exit 1 ;;
  esac
fi

say() { printf '\n=== %s ===\n' "$*"; }
note() { printf '  %s\n' "$*"; }

sill_select_embeddings
sill_prepare_instance

say "1/4: remove containers and volumes for the selected project"
sill_compose down -v --remove-orphans

say "2/4: start db, embeddings, rabbitmq"
sill_start_runtime

say "3/4: wait for the schema and start the maintenance worker"
sill_finish_runtime "${SILL_RESET_WAIT_HEALTHY_S:-${SILL_INSTALL_WAIT_HEALTHY_S:-$SILL_WAIT_HEALTHY_DEFAULT}}"

say "4/4: re-import seed"
if (( NO_SEED )); then
  note "--no-seed: skipping"
else
  sill_import_seed
fi

printf '\nReset complete.\n'
