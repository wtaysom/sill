"""Gate logic via subprocess canned Stop events; no DB, no model
(SILL_INSIGHT_DETECT unset ⇒ off; gate tests enable it but die before ollama
via missing transcript / home project)."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "plugin" / "hooks" / "response-patterns.py"
spec = importlib.util.spec_from_file_location("response_patterns", HOOK)
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


def test_source_project_root_cwd_is_unknown():
    assert rp.source_project("/", None) == "unknown"


def test_source_project_home_match(tmp_path, monkeypatch):
    home = tmp_path / "myproj"
    home.mkdir()
    monkeypatch.setenv("SILL_HOME_PROJECT", str(home))
    assert rp.source_project(str(home), None) == rp.HOME_PROJECT_NAME
    assert rp.source_project(str(home / "sub"), None) == rp.HOME_PROJECT_NAME


def test_empty_home_env_treats_everything_as_home(monkeypatch):
    monkeypatch.setenv("SILL_HOME_PROJECT", "")
    assert rp.source_project("/anywhere/else", None) == rp.HOME_PROJECT_NAME


def test_munged_transcript_fallback_is_fail_closed_when_home_unset(monkeypatch):
    monkeypatch.delenv("SILL_HOME_PROJECT", raising=False)
    got = rp.source_project(None, "/x/.claude/projects/-Users-x-code-someproj/abc.jsonl")
    assert got == rp.HOME_PROJECT_NAME


def test_munged_transcript_fallback_still_resolves_short_name_when_home_set(monkeypatch):
    # Guard against the fix regressing the branch's actual purpose: with a
    # home configured and the munged dir NOT matching it, a real project
    # short name must still come through (this is what the branch exists for).
    monkeypatch.setenv("SILL_HOME_PROJECT", "/Users/x/code/home-project")
    got = rp.source_project(None, "/x/.claude/projects/-Users-x-code-someproj/abc.jsonl")
    assert got == "someproj"


def test_munged_transcript_fallback_matches_home(monkeypatch):
    monkeypatch.setenv("SILL_HOME_PROJECT", "/Users/x/code/someproj")
    got = rp.source_project(None, "/x/.claude/projects/-Users-x-code-someproj/abc.jsonl")
    assert got == rp.HOME_PROJECT_NAME


def test_deliberate_mint_detector_sees_bash_notice():
    block = {"type": "tool_use", "name": "Bash",
             "input": {"command": "sill notice 'a fact' --speaker Ada"}}
    assert rp._block_is_deliberate_store(block)
    block2 = {"type": "tool_use", "name": "mcp__sill__remember", "input": {}}
    assert rp._block_is_deliberate_store(block2)
    block3 = {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
    assert not rp._block_is_deliberate_store(block3)


# --- Echo suppression must see a Codex session's mints too ------------------
#
# The two checks below read the session transcript, whose schema differs by
# harness, and this hook is registered on Stop with no matcher — the same file
# fires on Claude AND Codex (plugin/codex.hooks.json.template, merged into both
# settings files by install.sh). Before _harness was wired in, both functions
# walked only Claude's assistant/tool_use shape and recognized only the tool
# name "Bash", so on Codex they returned False unconditionally: every
# Codex-side deliberate mint was invisible and the echo suppression silently
# never engaged. Parity here is the regression that catches that.

MINT_CMD = "sill notice 'a deliberately minted fact' --force assertive --speaker instance"

CLAUDE_MINT_TRANSCRIPT = "\n".join([
    json.dumps({"type": "user", "message": {"content": "please store that"}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash",
         "input": {"command": MINT_CMD}},
    ]}}),
]) + "\n"

# Codex spells the same call exec_command: a response_item/function_call whose
# arguments arrive as a JSON-encoded string, keyed "cmd" — not "command", the
# key this fixture asserted until the rollout census (309 real transcripts,
# 2026-08-06) showed 1246/1246 real calls using "cmd". The old shape passed
# while real Codex sessions went undetected.
CODEX_MINT_TRANSCRIPT = "\n".join([
    json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "please store that"}]}}),
    json.dumps({"type": "response_item", "payload": {
        "type": "function_call", "call_id": "call_1", "name": "exec_command",
        "arguments": json.dumps({"cmd": MINT_CMD, "workdir": "/tmp",
                                 "yield_time_ms": 250})}}),
]) + "\n"

# Codex's `shell`: same logical call, argv list under "command".
CODEX_SHELL_MINT_TRANSCRIPT = "\n".join([
    json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "please store that"}]}}),
    json.dumps({"type": "response_item", "payload": {
        "type": "function_call", "call_id": "call_1", "name": "shell",
        "arguments": json.dumps({"command": ["bash", "-lc", MINT_CMD],
                                 "workdir": "/tmp"})}}),
]) + "\n"

# Codex's other shape for a shell call: exec arrives as a custom_tool_call
# whose input is a bare freeform string, not a dict with a "command" key.
CODEX_EXEC_MINT_TRANSCRIPT = "\n".join([
    json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "please store that"}]}}),
    json.dumps({"type": "response_item", "payload": {
        "type": "custom_tool_call", "call_id": "call_1", "name": "exec",
        "input": MINT_CMD}}),
]) + "\n"

CODEX_MCP_MINT_TRANSCRIPT = "\n".join([
    json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "please store that"}]}}),
    json.dumps({"type": "response_item", "payload": {
        "type": "function_call", "call_id": "call_1",
        "namespace": "mcp__sill", "name": "remember",
        "arguments": json.dumps({"content": "a deliberately minted fact"})}}),
]) + "\n"

# Same Codex shapes, no mint: a plain shell call and a plain MCP read. These
# must stay False, or "recognizes Codex" would just be "returns True on Codex".
CODEX_NO_MINT_TRANSCRIPT = "\n".join([
    json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "what changed?"}]}}),
    json.dumps({"type": "response_item", "payload": {
        "type": "function_call", "call_id": "call_1", "name": "exec_command",
        "arguments": json.dumps({"command": "git status"})}}),
    json.dumps({"type": "response_item", "payload": {
        "type": "custom_tool_call", "call_id": "call_2", "name": "exec",
        "input": "ls -la"}}),
    json.dumps({"type": "response_item", "payload": {
        "type": "function_call", "call_id": "call_3",
        "namespace": "mcp__sill", "name": "recall_batch", "arguments": "{}"}}),
]) + "\n"


def _transcript(tmp_path, text):
    path = tmp_path / "transcript.jsonl"
    path.write_text(text)
    return {"transcript_path": str(path)}


@pytest.mark.parametrize("transcript", [
    CLAUDE_MINT_TRANSCRIPT,
    CODEX_MINT_TRANSCRIPT,
    CODEX_SHELL_MINT_TRANSCRIPT,
    CODEX_EXEC_MINT_TRANSCRIPT,
    CODEX_MCP_MINT_TRANSCRIPT,
], ids=["claude-bash", "codex-exec_command", "codex-shell", "codex-exec",
        "codex-mcp-remember"])
def test_deliberate_mint_is_seen_on_both_harnesses(transcript, tmp_path):
    data = _transcript(tmp_path, transcript)
    assert rp.has_remember_call(data), "turn-scoped check missed the mint"
    assert rp.session_has_deliberate_mint(data), "session-scoped check missed the mint"


@pytest.mark.parametrize("check", [rp.has_remember_call, rp.session_has_deliberate_mint],
                         ids=["turn", "session"])
def test_codex_session_without_a_mint_is_not_treated_as_minted(check, tmp_path):
    assert not check(_transcript(tmp_path, CODEX_NO_MINT_TRANSCRIPT))


def test_codex_mint_in_an_earlier_turn_is_session_visible_but_not_turn_visible(tmp_path):
    """The two checks answer different questions, and the Codex walk must
    keep them different: a mint before the last user message is a session
    mint (echo suppression's actual trigger) but not a current-turn one."""
    data = _transcript(
        tmp_path,
        CODEX_MINT_TRANSCRIPT + "\n".join([
            json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "now something else"}]}}),
            json.dumps({"type": "response_item", "payload": {
                "type": "function_call", "call_id": "call_2", "name": "exec_command",
                "arguments": json.dumps({"command": "git status"})}}),
        ]) + "\n")
    assert not rp.has_remember_call(data)
    assert rp.session_has_deliberate_mint(data)


# --- The turn boundary is the TYPED prompt, not any "user" entry -----------
#
# Claude writes tool results as "user" entries, so breaking on every "user"
# entry stopped the turn scan at the last tool result: a mint made earlier in
# the same turn, with any tool call after it, read as "no mint" — a false
# noted-without-noting warning, and one fewer brace on the auto-store path.
# Codex never had this ambiguity (its results are function_call_output
# records), so this is the Claude side catching up to the Codex walk, not a
# loosening of it: the pair below pins both directions at once.

def _claude_turn(*blocks) -> str:
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})


CLAUDE_MINT_BEHIND_A_TOOL_RESULT = "\n".join([
    json.dumps({"type": "user", "message": {"content": "store that, then check git"}}),
    _claude_turn({"type": "tool_use", "id": "toolu_1", "name": "Bash",
                  "input": {"command": MINT_CMD}}),
    json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Stored: ..."}]}}),
    _claude_turn({"type": "tool_use", "id": "toolu_2", "name": "Bash",
                  "input": {"command": "git status"}}),
    json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "clean"}]}}),
    _claude_turn({"type": "text", "text": "Stored, and the tree is clean."}),
]) + "\n"


def test_claude_mint_earlier_in_the_same_turn_is_still_turn_visible(tmp_path):
    data = _transcript(tmp_path, CLAUDE_MINT_BEHIND_A_TOOL_RESULT)
    assert rp.has_remember_call(data), (
        "the turn scan stopped at a tool_result 'user' entry instead of the "
        "typed prompt, so it never reached this turn's own mint")
    assert rp.session_has_deliberate_mint(data)


@pytest.mark.parametrize("boundary", [
    json.dumps({"type": "user", "message": {"content": "now something else"}}),
    json.dumps({"type": "user", "message": {"content": [
        {"type": "text", "text": "now something else"}]}}),
], ids=["string-content", "text-block-content"])
def test_claude_mint_in_an_earlier_turn_is_not_turn_visible(boundary, tmp_path):
    """The boundary must still BE a boundary — in both shapes a typed Claude
    prompt takes — or the fix above would just be its deletion."""
    data = _transcript(tmp_path, CLAUDE_MINT_BEHIND_A_TOOL_RESULT + boundary + "\n" +
                       _claude_turn({"type": "tool_use", "id": "toolu_3", "name": "Bash",
                                     "input": {"command": "git status"}}) + "\n")
    assert not rp.has_remember_call(data)
    assert rp.session_has_deliberate_mint(data)


def _run(payload, env_extra=None):
    env = {**os.environ, "SILL_INSIGHT_DETECT": "0"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=60)


def test_canned_stop_clean_sentence_exit0():
    r = _run({"hook_event_name": "Stop",
              "last_assistant_message": "A plain sentence with no flagged patterns."})
    assert r.returncode == 0


def test_missing_transcript_never_reaches_store(tmp_path):
    r = _run({"hook_event_name": "Stop", "cwd": str(tmp_path),
              "last_assistant_message": "x" * 400},
             {"SILL_INSIGHT_DETECT": "1", "SILL_HOME_PROJECT": "/nonexistent-home"})
    assert r.returncode == 0   # payload-integrity guard: no transcript ⇒ no store path


def test_frontmatter_parser_tolerates_comments(tmp_path):
    rule = tmp_path / "r.md"
    rule.write_text("---\nname: t\nseverity: low\npatterns:\n"
                    "  # a comment that must not kill the rule\n"
                    "  - \\bteststring\\b\n---\nbody\n")
    pats, _ = rp.parse_frontmatter(rule.read_text())
    assert any("teststring" in p for p in pats.get("patterns", []))


# --- carry_forward: the write side of the Stop->UserPromptSubmit sidecar ----
#
# response-patterns.py is a Stop hook — it fires after the reply is already
# on screen, so its own additionalContext can't reach the turn that tripped
# it. carry_forward() stashes this turn's warnings in a per-session sidecar
# file that spontaneous-recall.py's _read_pattern_carry_forward() reads (and
# deletes) at the top of the next turn. Neither side had any test coverage
# before this file (flagged in review, P2 Task 5: "carry_forward write-side
# untested" — the read side turned out to be equally untested, not just the
# write side). The roundtrip through the real reader lives in
# test_spontaneous_recall.py, which already has the subprocess harness for
# driving that hook; these test carry_forward() itself in isolation.

def test_carry_forward_writes_the_expected_sidecar_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "_SILL_LOG_DIR", tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    rp.carry_forward(["warning one", "warning two"], "sess-abc")

    sidecar = tmp_path / "response-patterns-last-sha256-33a386c9464a4538527687ee331d081db7d7448137f25db6fd13b1d8d1053a71.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["warnings"] == ["warning one", "warning two"]
    assert "timestamp" in data


def test_carry_forward_prefers_the_env_session_id_over_the_argument(tmp_path, monkeypatch):
    """Matches spontaneous-recall.py's own precedence (env wins there too;
    see _sid's derivation) — a mismatch here would write under one key and
    read under another, a silent miss."""
    monkeypatch.setattr(rp, "_SILL_LOG_DIR", tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-session")

    rp.carry_forward(["w"], "argument-session")

    assert (tmp_path / "response-patterns-last-sha256-958fb03fefc51c3489138fe560ccfbaca36207c1dc587715a98090ff384d1561.json").exists()
    assert not (tmp_path / "response-patterns-last-sha256-b3c61174dfd015ff01e4fb0211914df8a2224a675764c99801d0677123056094.json").exists()


def test_carry_forward_no_ops_without_any_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "_SILL_LOG_DIR", tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    rp.carry_forward(["w"], None)

    assert list(tmp_path.iterdir()) == []
