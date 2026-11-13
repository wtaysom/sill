"""Canned-event tests for spontaneous-recall. No DB: docker calls fail fast in
the test env (SILL_DB_CONTAINER points at a nonexistent container), which is a
supported degrade path — header-only output."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / "plugin" / "hooks" / "spontaneous-recall.py"


def run_hook(payload, env_extra=None, cwd=None):
    env = {**os.environ, "SILL_DB_CONTAINER": "sill_test_absent",
           "SILL_LOG_DIR": os.environ.get("PYTEST_SILL_LOG", "/tmp")}
    env.pop("SILL_DETACHED_BEAT", None)
    env.pop("SILL_INTERACTIVE", None)
    env.pop("SILL_HEADLESS_TOOL", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    # This suite's own dev loop runs inside a real Claude Code session, so
    # CLAUDE_CODE_SESSION_ID is live in the ambient environment — pop it
    # like the other harness-identity vars above, so a test's payload
    # session_id (the thing tests actually control) isn't silently
    # shadowed by whatever session happens to be running pytest. A test
    # that specifically wants CLAUDE_CODE_SESSION_ID set can still do so
    # via env_extra below.
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, cwd=cwd, timeout=60)


def test_detached_beat_gate_silences_everything():
    r = run_hook({"prompt": "a long enough genuine question about the system"},
                 {"SILL_DETACHED_BEAT": "1"})
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_headless_tool_wins_over_interactive():
    r = run_hook({"prompt": "another long enough genuine question here"},
                 {"SILL_HEADLESS_TOOL": "1", "SILL_INTERACTIVE": "1"})
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_interactive_overrides_sdk_entrypoint():
    r = run_hook({"prompt": "short"},
                 {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "SILL_INTERACTIVE": "1"})
    assert r.returncode == 0 and "[TIME]" in r.stdout


def test_desktop_entrypoint_not_gated():
    r = run_hook({"prompt": "short"}, {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"})
    assert r.returncode == 0 and "[TIME]" in r.stdout


def test_short_prompt_db_down_emits_clock_only_header():
    r = run_hook({"prompt": "hi there"})
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert re.match(r"^\[TIME\] \w{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2}",
                    out["systemMessage"])
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_empty_prompt_fully_silent():
    r = run_hook({"prompt": "   "})
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_no_sidecar_written_when_no_memories(tmp_path):
    env = {"PYTEST_SILL_LOG": str(tmp_path), "SILL_LOG_DIR": str(tmp_path)}
    run_hook({"prompt": "hello", "session_id": "tsx1"}, env)
    assert not list(tmp_path.glob("recall-sidecar-*"))


# --- Pattern carry-forward: the read side of response-patterns.py's Stop-
#     to-next-turn sidecar (see test_response_patterns.py's carry_forward
#     tests for the write side; this is the roundtrip through the real
#     reader, _read_pattern_carry_forward, via this hook's actual process
#     boundary, matching this file's own subprocess-driven style).
# ---------------------------------------------------------------------------

def test_pattern_carry_forward_surfaces_and_consumes_the_sidecar(tmp_path):
    sidecar = tmp_path / "response-patterns-last-sha256-83d9078614f3d1f7f2aeef8ad78c0a327998f2de637fc167cf501168f106dce9.json"
    sidecar.write_text(json.dumps({
        "timestamp": "2026-08-05T00:00:00",
        "warnings": ["you agreed without verifying last turn"],
    }))

    # _sid resolution prefers CLAUDE_CODE_SESSION_ID over the payload's
    # session_id (matching carry_forward()'s own write-side precedence —
    # see test_response_patterns.py's carry-forward tests) — set it
    # explicitly rather than relying on it being absent from the ambient
    # environment, which it is NOT when this suite runs inside a real
    # Claude Code session (this repo's own dev loop, notably).
    env = {"PYTEST_SILL_LOG": str(tmp_path), "SILL_LOG_DIR": str(tmp_path),
           "CLAUDE_CODE_SESSION_ID": "carryfwd-sess"}
    r = run_hook({"prompt": "short", "session_id": "carryfwd-sess"}, env)

    assert r.returncode == 0
    out = json.loads(r.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "[PATTERN CHECK" in ctx
    assert "you agreed without verifying last turn" in ctx

    # Delivered exactly once: the sidecar is consumed on read.
    assert not sidecar.exists()


def test_pattern_carry_forward_absent_is_a_quiet_no_op(tmp_path):
    """No sidecar file at all (the common case — most turns trip no
    response-pattern warning) must not add anything to the output, just
    the ordinary time header."""
    env = {"PYTEST_SILL_LOG": str(tmp_path), "SILL_LOG_DIR": str(tmp_path),
           "CLAUDE_CODE_SESSION_ID": "no-sidecar-sess"}
    r = run_hook({"prompt": "short", "session_id": "no-sidecar-sess"}, env)

    assert r.returncode == 0
    out = json.loads(r.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "[PATTERN CHECK" not in ctx

# Shared callback runner uses only synthetic rows and the real hook program.
from test_write_recall import callback, ROWS


def test_prompt_recall_current_claude_and_codex_event_injects_expected_context(callback, monkeypatch, tmp_path):
    run, calls, exposed = callback
    import _store
    monkeypatch.setattr(_store, 'query', lambda *a, **kw: [])
    transcript = tmp_path / 'session.jsonl'
    transcript.write_text(json.dumps({'type':'assistant','message':{'content':'Previous cedar survey'}})+'\n')
    for fields in ({}, {'turn_id':'turn-1'}):
        out = run(dict(prompt='Cedar meadow survey records and water retention', session_id='s',
                       hook_event_name='UserPromptSubmit', transcript_path=str(transcript), **fields),
                  'spontaneous-recall.py')
        assert 'Synthetic cedar finding' in out['hookSpecificOutput']['additionalContext']
        assert 'score=0.0164' in out['hookSpecificOutput']['additionalContext']
        assert out['hookSpecificOutput']['hookEventName'] == 'UserPromptSubmit'
    assert len(exposed) == 10
    assert 'Previous cedar survey' in calls[0][1]
    assert calls[0][2]['limit'] == 5


def test_prompt_quiet_keeps_model_context_and_store_outage_visible(callback, monkeypatch):
    run, calls, exposed = callback
    import _store
    from scripts import recall_core
    monkeypatch.setattr(_store, 'query', lambda *a, **kw: [])
    monkeypatch.setenv('SILL_RECALL_QUIET','1')
    payload = dict(prompt='Cedar meadow survey records and water retention')
    out = run(payload,'spontaneous-recall.py')
    assert 'Synthetic cedar' in out['hookSpecificOutput']['additionalContext']
    assert 'Synthetic cedar' not in out['systemMessage']
    monkeypatch.setattr(recall_core,'fused_recall',lambda *a, **kw: [])
    out = run(payload,'spontaneous-recall.py')
    assert 'failed' not in out['systemMessage']
    def failed(*a, **kw):
        raise recall_core.RecallUnavailable('unavailable')
    monkeypatch.setattr(recall_core,'fused_recall',failed)
    out = run(payload,'spontaneous-recall.py')
    assert 'failed' in out['systemMessage']


def test_short_prompt_invalid_explicit_instance_reports_unchecked(tmp_path):
    result=run_hook({'prompt':'yes','session_id':'s'}, {
        'SILL_INSTANCE_ENV_FILE':str(tmp_path/'missing.env'), 'SILL_LOG_DIR':str(tmp_path)})
    assert result.returncode == 0
    output=json.loads(result.stdout)
    assert '[TIME]' in output['systemMessage']
    assert 'unavailable' in output['systemMessage'].lower()
