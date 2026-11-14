"""Four-slot adapter conformance: inject / mint / capture / track, each
proven against BOTH harnesses' real payload/transcript shapes.

This is the executable half of docs/adapters.md's contract — that doc
defines "supporting a new harness" as implementing the four slots and
passing this file (see verify.sh's conformance check, which runs this file
plus the mint-slot test in test_notice.py — see slot 2 below for why that
split exists rather than duplicating assertions).

Slot -> where its proof lives:

  1. inject  — here (test_inject_*): spontaneous-recall.py, run as a real
     subprocess against a UserPromptSubmit payload shaped like each harness.
  2. mint    — split across two files, deliberately not duplicated:
       - test_notice.py (extended by this task) proves the REAL argv
         plugin/hooks/response-patterns.py's auto_store_insight() builds
         parses against sill.py's own parser (backend/sill.py:build_parser),
         for both calling shapes source_project() branches on.
       - here (test_mint_*) proves the layer test_notice.py does NOT touch:
         that same argv, run through the FULL 'sill' console-script chain
         (sill_cli.build_parser() -> its notice passthrough shim ->
         sill.main()) — the actual command SILL_CLI subprocess.run()s.
  3. capture — here (test_capture_*): _harness.iter_transcript_tool_uses
     against backend/tests/fixtures/{claude-transcript,codex-rollout}.jsonl.
  4. track   — here (test_track_*): track-reuse.py's get_recalled_memories +
     detect_reuse against the same two fixtures.

Honesty note: transcript fixtures below are HAND-WRITTEN to match schemas ground-truthed
against one Codex build, not captured from a live Codex session. This file
proves the CONTRACT (both harnesses' fixture shapes normalize/behave
identically to each other); it does not prove either fixture is byte-
accurate to what a real session of that harness would produce. The separate
hook-event fixtures now contain sanitized projections captured from isolated
Claude Code 2.1.296 / Codex CLI 0.162.1 sessions. Their recorder omitted prompt
text: synthetic prompt injection tests do not establish native delivery.

custom_tool_call's field shape, carried as unverified from Task 1's report
through v0.2.0, was CONFIRMED by the v0.2.1 census of 309 real rollouts
(1,314 records; call_id/name/input, input always a string). That census
also falsified three shapes the hand-derivation got wrong — see
backend/tests/test_harness.py's Codex shell-shape section — which is the
standing argument for censusing real transcripts rather than extending
these fixtures by inference.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import sill
import sill_cli

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "plugin" / "hooks"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


h = _load("_harness_for_conformance", HOOKS / "_harness.py")
track_reuse = _load("track_reuse_for_conformance", HOOKS / "track-reuse.py")
response_patterns = _load("response_patterns_for_conformance", HOOKS / "response-patterns.py")

RECALLED_MEMORY_ID = "11111111-1111-1111-1111-111111111111"
RECALLED_MEMORY_CONTENT = (
    "Alpha beta gamma delta epsilon zeta the distinctive body phrase "
    "continues here uniquely across both harnesses."
)
REUSED_RESPONSE_TEXT = (
    "Sure -- the distinctive body phrase continues to matter across both "
    "harnesses' fixtures, so here is the synthesis you asked for."
)


# ---------------------------------------------------------------------------
# Slot 1: inject
# ---------------------------------------------------------------------------

RECALL_HOOK = HOOKS / "spontaneous-recall.py"

CLAUDE_USER_PROMPT_PAYLOAD = {
    "hook_event_name": "UserPromptSubmit",
    "prompt": "What did we conclude about the four-slot adapter contract's tests?",
    "session_id": "claude-fixture-session",
    "cwd": "/tmp/sill-conformance-fixture",
    "transcript_path": "/tmp/sill-conformance-fixture/.claude/nonexistent.jsonl",
}

# Codex's real UserPromptSubmit payload shape is NOT independently verified
# anywhere in this repo (see docs/adapters.md's version-fragility section).
# turn_id is the one confirmed Codex-only marker (_harness.detect keys on
# it); the rest mirrors the Claude shape, which is this codebase's own
# standing assumption — plugin/codex.hooks.json.template wires this hook to
# Codex's UserPromptSubmit unconditionally, with no Codex-specific field
# handling anywhere in spontaneous-recall.py.
CODEX_USER_PROMPT_PAYLOAD = {
    "hook_event_name": "UserPromptSubmit",
    "turn_id": "turn-fixture-1",
    "prompt": "What did we conclude about the four-slot adapter contract's tests?",
    "session_id": "codex-fixture-session",
    "cwd": "/tmp/sill-conformance-fixture",
}


def _run_recall_hook(payload: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, "SILL_DB_CONTAINER": "sill_test_absent_conformance",
           "SILL_LOG_DIR": "/tmp"}
    # Mirrors test_spontaneous_recall.py's defensive popping: this suite may
    # itself be running inside an ambient session with these already set —
    # strip them so the hook sees a plain interactive invocation regardless
    # of what's running the test.
    for var in ("SILL_DETACHED_BEAT", "SILL_INTERACTIVE", "SILL_HEADLESS_TOOL",
                "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(var, None)
    return subprocess.run([sys.executable, str(RECALL_HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=60)


@pytest.mark.parametrize("payload", [CLAUDE_USER_PROMPT_PAYLOAD, CODEX_USER_PROMPT_PAYLOAD],
                        ids=["claude", "codex"])
def test_inject_slot_returns_additional_context_on_both_harnesses(payload):
    """inject: spontaneous-recall.py must return hookSpecificOutput.additionalContext
    for a genuine UserPromptSubmit prompt, regardless of harness. No DB in
    this test env (SILL_DB_CONTAINER points at a nonexistent container) —
    memory recall itself degrades to empty, but the [TIME] header alone is
    enough to prove the slot's wiring: the hook parses the payload, emits
    the right hookSpecificOutput shape, and exits 0 either way."""
    r = _run_recall_hook(payload)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert isinstance(ctx, str) and ctx.strip()


def test_inject_slot_never_emits_a_permission_request_shaped_payload():
    """Codex fails CLOSED on PermissionRequest reserved fields (docs/adapters.md)
    — a Claude-shaped response there denies the call. spontaneous-recall.py
    only ever fires on UserPromptSubmit, never PreToolUse/PermissionRequest,
    but the invariant is cheap to pin directly against the source, the same
    way test_guards_on_both_harnesses.py pins it for the three guards."""
    src = RECALL_HOOK.read_text()
    for reserved in ("updatedInput", "updatedPermissions", '"interrupt"'):
        assert reserved not in src


# ---------------------------------------------------------------------------
# Slot 2: mint — see the module docstring for why most of this slot's proof
# lives in test_notice.py instead of here.
# ---------------------------------------------------------------------------

def _captured_auto_store_argv() -> list:
    """Run the REAL auto_store_insight() (not a hand-typed stand-in) with
    subprocess.run mocked to capture argv instead of shelling out."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Stored: 11111111-2222-3333-4444-555555555555 [1 tags]", stderr="")

    with mock.patch("subprocess.run", side_effect=fake_run):
        response_patterns.auto_store_insight(
            REUSED_RESPONSE_TEXT, "a conformance-test summary", ["adapter-contract"],
            cwd="/tmp/sill-conformance-fixture", transcript_path=None)
    return captured["cmd"]


def test_mint_slot_argv_parses_through_the_full_cli_chain(tmp_path, monkeypatch):
    """mint: the argv auto_store_insight() builds is what SILL_CLI actually
    runs as a subprocess — 'sill notice ...', i.e. the sill_cli.py console
    script, NOT backend/sill.py directly. test_notice.py's (extended)
    contract test proves the argv parses against sill.py's own parser one
    layer down; this proves the layer that test doesn't touch: the 'sill'
    console script's own parser (sill_cli.build_parser(), whose 'notice'
    subcommand is a bare passthrough shim with add_help=False) forwards it
    correctly and sill_cli.main() completes with the receipt sill.py
    printed — the actual chain the auto-store path depends on, end to end."""
    monkeypatch.setattr(response_patterns, "AUTO_STORE_LOG", tmp_path / "auto-store.jsonl")
    monkeypatch.delenv("SILL_HOME_PROJECT", raising=False)
    cmd = _captured_auto_store_argv()
    assert cmd[0] == response_patterns.SILL_CLI

    def fake_query_db(sql, timeout=60):
        if "mint_memory" in sql:
            return [["11111111-2222-3333-4444-555555555555", "inserted", None]]
        return [["ok"]]

    with mock.patch.object(sill, "_query_db", side_effect=fake_query_db):
        rc = sill_cli.main(cmd[1:])
    assert rc == 0


# ---------------------------------------------------------------------------
# Slot 3: capture
# ---------------------------------------------------------------------------

CLAUDE_TRANSCRIPT = FIXTURES / "claude-transcript.jsonl"
CODEX_TRANSCRIPT = FIXTURES / "codex-rollout.jsonl"


@pytest.mark.parametrize("fixture", [CLAUDE_TRANSCRIPT, CODEX_TRANSCRIPT],
                        ids=["claude", "codex"])
def test_capture_slot_normalizes_the_shared_recall_call_identically(fixture):
    """capture: _harness.iter_transcript_tool_uses must yield the SAME
    normalized {id, name, input} shape for the SAME logical recall_batch
    call on both harnesses' transcript schemas — same name, same input,
    even though the two fixtures encode it completely differently (a flat
    tool_use block vs. a split namespace/name function_call whose arguments
    arrive as a JSON-encoded string). Call ids are intentionally NOT
    compared: they're harness-native call identifiers (Claude's toolu_...
    vs. Codex's call_...), not something the contract normalizes away."""
    records = list(h.iter_transcript_tool_uses(fixture))
    recall_records = [r for r in records if r.get("name") == "mcp__sill__recall_batch"]
    assert len(recall_records) == 1
    record = recall_records[0]
    assert record["id"]
    assert record["input"] == {"ids": [RECALLED_MEMORY_ID]}


def test_capture_slot_covers_both_codex_record_shapes():
    """The brief's own caveat: Codex emits function_call (exec_command, MCP
    calls) AND custom_tool_call (exec) with a different field shape.
    Covered here: custom_tool_call's {call_id, name, input} — confirmed
    against 1,314 real records by the v0.2.1 census (see the module
    docstring). function_call is covered by the recall_batch call the
    parametrized test above already exercises."""
    records = list(h.iter_transcript_tool_uses(CODEX_TRANSCRIPT))
    exec_records = [r for r in records if r.get("name") == "exec"]
    assert exec_records == [{"id": "call_fixture_00", "name": "exec", "input": "ls -la"}]


def test_capture_slot_record_counts_are_not_cross_contaminated():
    """Each fixture's own record count must be exactly what its file
    contains — not inflated by the other schema's branch silently
    double-matching (iter_transcript_tool_uses handles both shapes in one
    pass over the same file by design, so this is a real risk to pin, not
    a redundant check)."""
    assert len(list(h.iter_transcript_tool_uses(CLAUDE_TRANSCRIPT))) == 1
    assert len(list(h.iter_transcript_tool_uses(CODEX_TRANSCRIPT))) == 2


# ---------------------------------------------------------------------------
# Slot 4: track
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fixture", [CLAUDE_TRANSCRIPT, CODEX_TRANSCRIPT],
                        ids=["claude", "codex"])
def test_track_slot_reaches_the_same_verdict_on_both_harnesses(fixture):
    """track: track-reuse.py's own detector — get_recalled_memories() (the
    harness-shape-dependent half, walking the raw transcript) followed by
    the harness-agnostic detect_reuse() — must reach the identical verdict
    from both fixtures: the same memory id, flagged reused via a body
    phrase (not the id, and not the citation-shaped head)."""
    data = {"transcript_path": str(fixture)}
    response_text = track_reuse.get_response_text(data)
    assert response_text == REUSED_RESPONSE_TEXT

    memories = track_reuse.get_recalled_memories(data)
    assert memories == [{"id": RECALLED_MEMORY_ID, "content": RECALLED_MEMORY_CONTENT}]

    reused = track_reuse.detect_reuse(memories, response_text.lower())
    assert len(reused) == 1
    mem_id, evidence, channel = reused[0]
    assert mem_id == RECALLED_MEMORY_ID
    assert evidence != "__ID__"       # reused via a body phrase, not the id
    assert channel == "mcp-tool-result"


def test_track_slot_never_emits_a_permission_request_shaped_payload():
    """Same invariant as the guards (docs/adapters.md) — track-reuse.py only
    ever fires on Stop, never PreToolUse/PermissionRequest, but pinning it
    directly against the source costs nothing."""
    src = (HOOKS / "track-reuse.py").read_text()
    for reserved in ("updatedInput", "updatedPermissions", '"interrupt"'):
        assert reserved not in src
