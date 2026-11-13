#!/usr/bin/env bash
# verify.sh — six smoke checks for an installed Sill.
#
# Exits 0 only if all six checks pass. Designed to be safe to re-run.
#
# Checks:
#   1. Database, embeddings and RabbitMQ healthy; maintenance worker running.
#   2. sill-mcp answers a real MCP `initialize` handshake over stdio.
#   3. At least 22 active memories (skip with --no-seed).
#   4. plugin/hooks/response-patterns.py exits 0 on a canned Stop event.
#   5. Report the schema checked during readiness.
#   6. adapter conformance: the four-slot contract (docs/adapters.md) holds
#      on both harnesses' fixture shapes.

set -euo pipefail

# shellcheck source=scripts/install_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/scripts/install_common.sh"

NO_SEED=0
RUNTIME_ONLY=0
while (( $# )); do
  case "$1" in
    --no-seed) NO_SEED=1; shift ;;
    --runtime-only) RUNTIME_ONLY=1; shift ;;
    --help|-h) printf 'Usage: verify.sh [--no-seed]\n'; exit 0 ;;
    *) printf 'verify.sh: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
trap sill_cleanup_instance EXIT
if (( RUNTIME_ONLY )); then export SILL_EXPECTED_VERSION="$(sill_checkout_version)"; fi

fail() {
  printf '  FAIL: %s\n' "$*" >&2
  exit 1
}

pass() {
  printf '  pass: %s\n' "$*"
}

note() {
  printf '    %s\n' "$*"
}

say() {
  printf '\n=== %s ===\n' "$*"
}

# Speak MCP to the installed server and require a real answer.
#
# Check 2 used to run `sill-mcp --help`, which exits before importing the MCP
# SDK at all. During the v0.2.0 clean-machine rehearsal that check reported
# green on a server that could not serve a single tool: an unpinned
# `mcp>=1.0.0` had resolved to 2.x, which removed the `Server.list_tools()`
# API sill_mcp_server.py registers through, and the server died on its first
# handshake with `'Server' object has no attribute 'list_tools'`. The
# dependency is pinned now (backend/pyproject.toml), but the pin only blocks
# that one resolution — `--help` would exit 0 again the next time the SDK
# moves, and a check that stays green over a dead server is worse than no
# check. So: start it, send `initialize`, require a well-formed result naming
# this server, and kill it.
#
# SILL_MCP_CMD overrides the command (used by the test suite to point at
# stubs that fail in specific ways). SILL_VERIFY_MCP_TIMEOUT_S bounds the
# wait — the server opens a database connection before it will answer, so
# this is not instant.
mcp_handshake() {
  SILL_MCP_CMD="${SILL_MCP_CMD:-}" \
  SILL_VERIFY_MCP_TIMEOUT_S="${SILL_VERIFY_MCP_TIMEOUT_S:-20}" \
  python3 - "$@" <<'PY'
import json
import os
import shlex
import subprocess
import sys
import threading

cmd = shlex.split(os.environ["SILL_MCP_CMD"]) if os.environ["SILL_MCP_CMD"] else sys.argv[1:]
budget = float(os.environ["SILL_VERIFY_MCP_TIMEOUT_S"])

request = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "verify.sh", "version": "1"},
    },
}


def die(message, proc=None):
    print(f"    {message}", file=sys.stderr)
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
        err = (proc.stderr.read() or "").strip() if proc.stderr else ""
        if err:
            print("    the server said:", file=sys.stderr)
            for line in err.splitlines()[-8:]:
                print(f"      {line}", file=sys.stderr)
    sys.exit(1)


try:
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
except OSError as exc:
    print(f"    could not start {cmd[0]!r}: {exc}", file=sys.stderr)
    sys.exit(1)

# One reply, or nothing. A reader thread is the stdlib's only way to put a
# deadline on a blocking read: a server that accepts the request and then
# hangs must fail this check, not hang verify.sh.
reply = []


def read_reply():
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue          # tolerate stray non-protocol output on stdout
        if isinstance(obj, dict) and obj.get("id") == 1:
            reply.append(obj)
            return


reader = threading.Thread(target=read_reply, daemon=True)
reader.start()

try:
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
except (BrokenPipeError, OSError):
    die("the server closed its input before the handshake was sent", proc)

reader.join(budget)

if not reply:
    if proc.poll() is not None:
        die(f"the server exited ({proc.returncode}) without answering `initialize`", proc)
    die(f"no answer to `initialize` within {budget:.0f}s", proc)

obj = reply[0]
if "error" in obj:
    die(f"the server refused `initialize`: {obj['error']}", proc)

result = obj.get("result")
if not isinstance(result, dict):
    die(f"malformed handshake reply: {obj}", proc)
info = result.get("serverInfo") or {}
if info.get("name") != "sill":
    die(f"handshake answered by {info.get('name')!r}, expected 'sill'", proc)
expected = os.environ.get("SILL_EXPECTED_VERSION")
if expected and info.get("version") != expected:
    die(f"MCP running version {info.get('version')!r}, expected {expected}", proc)
if "protocolVersion" not in result:
    die(f"handshake reply names no protocolVersion: {result}", proc)

print(f"{info.get('name')} {info.get('version', '?')} "
      f"(MCP {result['protocolVersion']})")

try:
    proc.stdin.close()
    proc.wait(timeout=5)
except Exception:
    proc.kill()
PY
}

# --- check 1 -------------------------------------------------------------------
say "Check 1/6: Compose runtime ready"
sill_prepare_instance
sill_wait_healthy "${SILL_VERIFY_WAIT_HEALTHY_S:-${SILL_INSTALL_WAIT_HEALTHY_S:-$SILL_WAIT_HEALTHY_DEFAULT}}" \
  db embeddings rabbitmq maintenance_worker
sill_check_running_instance

# --- check 2 -------------------------------------------------------------------
say "Check 2/6: sill-mcp answers an MCP initialize handshake"
if [[ -n "${SILL_MCP_CMD:-}" ]]; then
  if ! handshake="$(mcp_handshake)"; then
    fail "MCP test command did not answer the initialize handshake (see above)."
  fi
else
  mcp="$(sill_bin sill-mcp)" || exit 1
  if ! handshake="$(mcp_handshake "$mcp" --env-file "$SILL_RUNTIME_ENV_FILE")"; then
    fail "sill-mcp did not answer the initialize handshake. Check the selected instance and re-run ./install.sh if needed."
  fi
fi
pass "MCP handshake answered by $handshake"

if (( RUNTIME_ONLY )); then
  sill_check_runtime_versions
  exit 0
fi

# --- check 3 -------------------------------------------------------------------
say "Check 3/6: seed loaded (memories count >= 22)"
if (( NO_SEED )); then
  note "--no-seed: skipping"
else
  if ! count="$(sill_db_query 'SELECT count(*) FROM memories WHERE archived_at IS NULL' 2>/dev/null)" ||
     [[ ! "$count" =~ ^[0-9]+$ || "$count" -lt 22 ]]; then
    fail "expected at least 22 active memories; rerun ./install.sh to import the seed."
  fi
  pass "memories count is $count"
fi

# --- check 4 -------------------------------------------------------------------
say "Check 4/6: response-patterns hook parses a canned Stop event"
# The hook reads last_assistant_message (Codex Stop shape) and exits 0 when
# the text contains no flagged patterns. We expect 0 here.
canned='{"hook_event_name":"Stop","last_assistant_message":"Verify smoke check: a plain sentence with no flagged patterns."}'
if printf '%s' "$canned" | python3 "$SILL_DIR/plugin/hooks/response-patterns.py" >/dev/null 2>&1; then
  pass "response-patterns.py exit 0 on canned Stop event"
else
  fail "response-patterns.py rejected a canned Stop event"
fi

# --- check 5 -------------------------------------------------------------------
say "Check 5/6: schema level current"
pass "migration ledger and public.memories.speaker checked in readiness"

# --- check 6 -------------------------------------------------------------------
say "Check 6/6: adapter conformance (inject/mint/capture/track, both harnesses)"
# The full proof (docs/adapters.md) is the backend test suite's own
# conformance file plus the mint-slot test it deliberately doesn't duplicate
# (see that doc's slot-2 section for why the split). pytest is a dev-only
# extra (backend/pyproject.toml's [dev] group), so it may not be present on
# an end-user install — degrade to a direct, dependency-free check of the
# capture slot (the one part cheap to prove with nothing but the stdlib)
# rather than skipping the check silently.
SILL_PY="$(sill_python)"
if "$SILL_PY" -c "import pytest" >/dev/null 2>&1; then
  note "conformance interpreter: $SILL_PY"
  if (cd "$SILL_DIR/backend" && "$SILL_PY" -m pytest \
        tests/test_adapter_conformance.py \
        "tests/test_notice.py::test_auto_store_argv_parses_for_both_harness_calling_shapes" \
        -q); then
    pass "adapter conformance suite green (pytest)"
  else
    fail "adapter conformance suite failed — see pytest output above, and docs/adapters.md"
  fi
else
  note "pytest not importable by $SILL_PY (dev-only extra) — running a direct capture-slot check instead."
  note "For the full four-slot suite, add pytest to that same interpreter — one of:"
  if command -v pipx >/dev/null 2>&1 && [[ "$SILL_PY" == *"/pipx/venvs/sill-memory/"* ]]; then
    note "  pipx inject sill-memory pytest pytest-asyncio"
  fi
  note "  \"$SILL_PY\" -m pip install -e \"$SILL_DIR/backend[dev]\""
  note "Then re-run ./verify.sh. Override the interpreter with SILL_PYTHON=<path> if it guessed wrong."
  if python3 - "$SILL_DIR" <<'PY'
import importlib.util
import sys
from pathlib import Path

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("_harness", root / "plugin" / "hooks" / "_harness.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)

for name in ("claude-transcript.jsonl", "codex-rollout.jsonl"):
    path = root / "backend" / "tests" / "fixtures" / name
    records = list(h.iter_transcript_tool_uses(path))
    recall = [r for r in records if r.get("name") == "mcp__sill__recall_batch"]
    assert len(recall) == 1, f"{name}: expected 1 recall_batch record, got {len(recall)}"
    assert recall[0]["input"] == {"ids": ["11111111-1111-1111-1111-111111111111"]}, \
        f"{name}: normalized input did not match"
print("capture-slot check OK on both harness fixtures")
PY
  then
    pass "adapter capture-slot check green (no-pytest fallback — install pytest for the full four-slot suite)"
  else
    fail "adapter capture-slot check failed — see docs/adapters.md"
  fi
fi

printf '\nAll checks passed.\n'
