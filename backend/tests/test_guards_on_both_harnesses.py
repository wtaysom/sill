"""Every guard must reach the same verdict on both harnesses."""

import json, os, subprocess, sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "plugin" / "hooks"


def run(hook, payload, env_extra=None):
    env = {**os.environ, "SILL_BEAT_JOURNAL_DIRS": "journal/",
           "SILL_DB_CONTAINER": "sill_absent_container"}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(HOOKS / hook)],
                          input=json.dumps(payload), capture_output=True,
                          text=True, env=env, timeout=30)


TRAP = "echo === && ls"

# The four Codex payloads below carry the tool names and command keys the
# v0.2.1 rollout census found in real traffic (exec_command → "cmd";
# shell → an argv list under "command"; shell_command → a string under
# "command"), not the uniform tool_input.command this file assumed through
# v0.2.0 — a shape Codex never sends for exec_command. Caveat kept honest:
# the census read TRANSCRIPTS, so the tool names and argument keys are
# ground truth while their appearance in a PreToolUse payload's tool_input
# is inference (a captured live Codex PreToolUse payload would settle it).

@pytest.mark.parametrize("payload", [
    {"tool_name": "Bash", "tool_input": {"command": TRAP}},
    {"tool_name": "exec", "tool_input": {"command": TRAP}, "turn_id": "t1"},
    {"tool_name": "exec_command", "tool_input": {"cmd": TRAP, "workdir": "/tmp"},
     "turn_id": "t1"},
    {"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", TRAP]},
     "turn_id": "t1"},
    {"tool_name": "shell_command", "tool_input": {"command": TRAP, "workdir": "/tmp"},
     "turn_id": "t1"},
], ids=["claude-bash", "codex-exec", "codex-exec_command", "codex-shell",
        "codex-shell_command"])
def test_shell_guard_denies_the_trap_on_both_harnesses(payload):
    r = run("shell-idiom-guard.py", payload)
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("payload", [
    {"tool_name": "Write", "tool_input": {"file_path": "journal/r-1.md",
                                          "content": "The receipt arrived by Edit.\n"}},
    {"tool_name": "apply_patch", "turn_id": "t1",
     "tool_input": {"input": "*** Update File: journal/r-1.md\n+The receipt arrived by Edit.\n"}},
])
def test_witness_denies_on_both_harnesses(payload):
    r = run("tool-type-witness.py", payload)
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_no_guard_emits_a_permission_request_shaped_payload():
    """Codex fails CLOSED on PermissionRequest reserved fields — a Claude-shaped
    response there denies the call. Guards must emit PreToolUse decisions only."""
    for hook in ["shell-idiom-guard.py", "tool-type-witness.py", "stored-slot-guard.py"]:
        src = (HOOKS / hook).read_text()
        for reserved in ["updatedInput", "updatedPermissions", '"interrupt"']:
            assert reserved not in src, f"{hook} emits a PermissionRequest field"


def _fake_docker_env(tmp_path, reply="0"):
    """A fake `docker` placed first on PATH that answers like a REACHABLE
    store reporting REPLY matching rows. This file's own run() helper points
    SILL_DB_CONTAINER at a nonexistent container — the fail-OPEN path, right
    for most tests here, but that path can never produce a genuine 'deny'
    from stored-slot-guard. Same technique as test_receipt_guards.py's
    test_guard_denies_when_the_store_genuinely_lacks_the_id."""
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(f"#!/bin/sh\necho {reply}\n")
    fake_docker.chmod(0o755)
    return {"PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"}


# --- Finding 2: parity coverage for the remaining four rewired hooks -------
#
# shell-idiom-guard and tool-type-witness already have parametrized
# Claude/Codex pairs above. stored-slot-guard, attribution-check,
# state-language-check, and track-reuse were verified by hand in the task-2
# report but had no automated tripwire — and state-language-check's
# apply_patch key already broke silently once when only one payload shape
# was handled. The tests below cover those four hooks.
#
# response-patterns.py was rewired later (its two deliberate-mint checks
# walked Claude's transcript shape only) and is the seventh _harness
# consumer; its parity coverage lives in test_response_patterns.py, where
# the checks can be called directly — it's a Stop hook whose verdict is a
# suppressed auto-store, with no stdout decision to compare here.

RECEIPT_TEXT = "Stored: deadbeef-1111-2222-3333-444455556666"


@pytest.mark.parametrize("payload", [
    {"tool_name": "Write", "tool_input": {"file_path": "journal/r-1.md",
                                          "content": RECEIPT_TEXT + "\n"}},
    {"tool_name": "apply_patch", "turn_id": "t1",
     "tool_input": {"input": f"*** Update File: journal/r-1.md\n+{RECEIPT_TEXT}\n"}},
])
def test_stored_slot_guard_denies_on_both_harnesses(payload, tmp_path):
    r = run("stored-slot-guard.py", payload, env_extra=_fake_docker_env(tmp_path))
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "deadbeef" in out["hookSpecificOutput"]["permissionDecisionReason"]


ATTRIBUTION_CONTENT = "You wrote this and I concluded it holds up."


@pytest.mark.parametrize("payload", [
    {"tool_name": "Bash", "tool_input": {"command": f'sill.py notice "{ATTRIBUTION_CONTENT}"'}},
    {"tool_name": "exec_command", "turn_id": "t1",
     "tool_input": {"cmd": f'sill.py notice "{ATTRIBUTION_CONTENT}"', "workdir": "/tmp"}},
    {"tool_name": "shell", "turn_id": "t1",
     "tool_input": {"command": ["bash", "-lc", f'sill.py notice "{ATTRIBUTION_CONTENT}"']}},
], ids=["claude-bash", "codex-exec_command", "codex-shell"])
def test_attribution_check_flags_the_same_claims_on_both_harnesses(payload):
    r = run("attribution-check.py", payload)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    # Exact match, not just "non-empty": the real parity claim is identical
    # output modulo the tool name, not merely "both fired something".
    assert out["systemMessage"] == "[attribution-check] 2 attribution claim(s)"


STATE_CONTENT = "I am feeling tired and need a break after this long session."


@pytest.mark.parametrize("payload", [
    {"tool_name": "Write", "tool_input": {"file_path": "journal/r-1.md",
                                          "content": STATE_CONTENT + "\n"}},
    {"tool_name": "apply_patch", "turn_id": "t1",
     "tool_input": {"input": f"*** Update File: journal/r-1.md\n+{STATE_CONTENT}\n"}},
    # The shell branch too — this hook scans mint commands, and on real
    # Codex traffic that command sits under "cmd" (see the census note above).
    {"tool_name": "Bash", "tool_input": {"command": f'sill.py notice "{STATE_CONTENT}"'}},
    {"tool_name": "exec_command", "turn_id": "t1",
     "tool_input": {"cmd": f'sill.py notice "{STATE_CONTENT}"', "workdir": "/tmp"}},
], ids=["claude-write", "codex-apply_patch", "claude-bash", "codex-exec_command"])
def test_state_language_check_flags_the_same_matches_on_both_harnesses(payload):
    r = run("state-language-check.py", payload)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["systemMessage"] == "[state-language-check] 2 match(es)"


# track-reuse fires on Stop, not PreToolUse — it never emits a stdout
# deny/advisory verdict (Stop hooks don't gate tool calls), so there is no
# permissionDecision/systemMessage to compare here. Its actual verdict is
# the reuse count it would persist via record_memory_reuse(); the one
# observable reachable without a live DB (this suite runs with no DB — see
# test_hook_safety.py's module docstring) is the "Found N reused memories"
# line, logged BEFORE the DB write is attempted. This proves the
# transcript-parsing step — which differs by harness: Claude's flat
# tool_use/tool_result content blocks vs Codex's split
# function_call/function_call_output records joined via
# _harness.join_mcp_name — reaches the same reuse COUNT on both
# harness-shaped transcripts. It does NOT prove the DB write itself
# succeeds or is identical (POSTGRES_HOST/PORT point at an unreachable
# loopback port here, same no-DB-network policy as test_hook_safety.py),
# and it does not distinguish which memory was flagged beyond the count.

REUSE_MEMORY_ID = "11111111-1111-1111-1111-111111111111"
REUSE_MEMORY_CONTENT = "alpha beta gamma delta epsilon zeta distinctive marker phrase trails onward"
REUSE_RESPONSE_TEXT = "As discussed, the distinctive marker phrase applies here directly."

CLAUDE_REUSE_TRANSCRIPT = "\n".join([
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "mcp__sill__recall_batch", "input": {}},
    ]}}),
    json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1",
         "content": json.dumps({"memories": [
             {"id": REUSE_MEMORY_ID, "content": REUSE_MEMORY_CONTENT}]})},
    ]}}),
]) + "\n"

CODEX_REUSE_TRANSCRIPT = "\n".join([
    json.dumps({"type": "response_item", "payload": {
        "type": "function_call", "call_id": "call_1",
        "namespace": "mcp__sill", "name": "recall_batch", "arguments": "{}"}}),
    json.dumps({"type": "response_item", "payload": {
        "type": "function_call_output", "call_id": "call_1",
        "output": json.dumps({"memories": [
            {"id": REUSE_MEMORY_ID, "content": REUSE_MEMORY_CONTENT}]})}}),
]) + "\n"


@pytest.mark.parametrize("transcript_text", [CLAUDE_REUSE_TRANSCRIPT, CODEX_REUSE_TRANSCRIPT],
                         ids=["claude", "codex"])
def test_track_reuse_detects_the_same_reuse_on_both_harnesses(transcript_text, tmp_path):
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text(transcript_text)
    payload = {
        "hook_event_name": "Stop",
        "transcript_path": str(transcript_path),
        "last_assistant_message": REUSE_RESPONSE_TEXT,
        "session_id": "test-parity-session",
    }
    r = run("track-reuse.py", payload, env_extra={
        "SILL_LOG_DIR": str(tmp_path),
        "POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": "1",
    })
    assert r.returncode == 0
    log_text = (tmp_path / "reuse-tracking.log").read_text()
    assert "Found 1 reused memories" in log_text, log_text


# --- Finding 1: multi-file apply_patch scope bypass ------------------------
#
# written_path used to return only the FIRST file header's path, and the
# three guards below gated their in-scope check on that single path BEFORE
# reading any text. A two-file patch whose first file is irrelevant and
# whose second file is an in-scope journal entry was therefore never
# inspected at all: written_path answered "in scope?" for the whole call
# using the irrelevant first path, the scope check failed, and the guard
# exited without ever reading the second file's text. Fixed by switching
# all three to iterate _harness.written_files and judge each (path, text)
# pair on its own path — see _harness.py's written_files docstring.

@pytest.mark.parametrize("patch_key", ["command", "input"], ids=["current", "legacy"])
@pytest.mark.parametrize(
    ("hook", "second_text"),
    [
        pytest.param("stored-slot-guard.py", RECEIPT_TEXT, id="receipt"),
        pytest.param("tool-type-witness.py", "The receipt arrived by Edit.", id="witness"),
        pytest.param("state-language-check.py", STATE_CONTENT, id="state"),
    ],
)
def test_codex_second_file_reaches_existing_guards_with_current_payload(
    hook, second_text, patch_key, tmp_path,
):
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "00000000-0000-4000-8000-000000000002",
        "turn_id": "00000000-0000-4000-8000-000000000003",
        "tool_name": "apply_patch",
        "tool_input": {patch_key: (
            "*** Begin Patch\n"
            "*** Update File: src/unrelated.py\n+irrelevant change\n"
            f"*** Update File: journal/r-1.md\n+{second_text}\n"
            "*** End Patch"
        )},
    }
    env_extra = _fake_docker_env(tmp_path) if hook == "stored-slot-guard.py" else None
    r = run(hook, payload, env_extra=env_extra)
    assert r.returncode == 0
    assert r.stdout.strip(), f"{hook} missed the second file in the {patch_key} patch"
    out = json.loads(r.stdout)
    if hook == "state-language-check.py":
        assert out["systemMessage"] == "[state-language-check] 2 match(es)"
    else:
        decision = out["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        if hook == "stored-slot-guard.py":
            assert "deadbeef" in decision["permissionDecisionReason"]
