"""Synthetic launcher results and native session fixtures for beat receipts."""

import json
import os
import re
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest

import beat_worker as bw


THREAD = "12345678-1234-4123-8123-123456789abc"
NEIGHBOR = "abcdefab-abcd-4abc-8abc-abcdefabcdef"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bw, "LOG_PATH", tmp_path / "worker.log")
    monkeypatch.setattr(bw, "WATCHDOG_SOCKET", None)
    monkeypatch.setattr(bw, "POST_HOOK", None)
    monkeypatch.setenv("SILL_BEAT_CONFIG", str(tmp_path / "beats.json"))
    monkeypatch.setenv("SILL_BEAT_STATE_PATH", str(tmp_path / "rotation.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "prompt.md").write_text("Standing prompt.")
    (tmp_path / "notes").mkdir()
    return tmp_path


def configure(root, **overrides):
    voice = {"name": "probe", "prompt": "prompt.md", "transcripts": "logs", **overrides}
    config = root / "beats.json"
    config.write_text(json.dumps({"voices": [voice, {**voice, "name": "next"}]}))
    return bw.load_voices(config)


def read_receipt(root):
    receipts = list((root / "logs").glob("*.json"))
    assert len(receipts) == 1
    return json.loads(receipts[0].read_text())


def jsonl(*events):
    return "\n".join(map(json.dumps, events)) + "\n"


def codex_events(*extra, terminal="turn.completed"):
    events = [
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "First message"}},
        *extra,
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Final answer"}},
    ]
    if terminal:
        events.append({"type": terminal})
    return jsonl(*events)


def codex_rollout(root, thread, *events, day="13"):
    sessions = root / "codex" / "sessions" / "2026" / "11" / day
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"rollout-2026-11-{day}T08-01-00-{thread}.jsonl").write_text(jsonl(*events))


def stub_cli(monkeypatch, stdout, *, returncode=0, stderr="", before_exit=None):
    runner = bw._run_beat_subprocess

    def run(argv, *, on_spawn=None, **kwargs):
        if on_spawn is None:
            return runner(argv, **kwargs)
        on_spawn()
        if before_exit:
            before_exit(argv, kwargs)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(bw, "_run_beat_subprocess", run)


def test_legacy_voice_uses_sill_beat_cli_and_cli_default_model(project, monkeypatch):
    cli = project / "legacy-wrapper"
    cli.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > wrapper-args\necho "Plain final answer"\n')
    cli.chmod(0o700)
    monkeypatch.setenv("SILL_BEAT_CLI", str(cli))
    monkeypatch.setattr(bw, "CLI", str(project / "unused-default"))
    voices = configure(project)

    class SameSecond(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 13, 12, tzinfo=tz)

    monkeypatch.setattr(bw, "datetime", SameSecond)
    transcripts = []
    for _ in range(2):
        success, _, transcript = bw.spawn_beat(voices[0], voices)
        assert success
        transcripts.append(Path(transcript))
    assert len(set(transcripts)) == 2
    assert len(list((project / "logs").glob("*.json"))) == 2
    for transcript in transcripts:
        receipt = json.loads(transcript.with_suffix(".json").read_text())
        assert receipt["attempt_id"] == str(UUID(transcript.stem))
        assert receipt["session_id"] == f"beat:{transcript.stem}"
        assert receipt["requested_host_session_id"] == transcript.stem
        assert receipt["requested_backend"] == "claude"
        assert receipt["requested_model"] is None
        assert receipt["effective_model"] is None
        assert receipt["host_session_id"] is None
        assert receipt["state_advanced"] is False
        assert "Plain final answer" in transcript.read_text()
    assert (project / "wrapper-args").read_text().splitlines()[:7] == [
        "--print", "--session-id", transcripts[-1].stem, "--output-format", "json", "-p",
        "Standing prompt.",
    ]
    assert not (project / "rotation.json").exists()


@pytest.mark.parametrize("launcher", ["claude", "codex"])
def test_launchers_use_host_specific_argv_and_clear_parent_session(project, monkeypatch, launcher):
    for name in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                 "SILL_INTERACTIVE", "SILL_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.setenv(name, "parent-session")
    monkeypatch.setenv("SILL_BEAT_CLI", "unused-legacy-cli")
    monkeypatch.setenv("SILL_DB_CONTAINER", "fixture-db")
    cli = str(project / "explicit-cli")
    voices = configure(project, launcher=launcher, cli=cli, model="asked-model", output_glob="notes/*.md")
    launch = {}

    def record(argv, kwargs):
        launch.update(argv=argv, env=kwargs["env"], cwd=kwargs["cwd"], receipt=read_receipt(project))
        for name in ("z.md", "a.md"):
            (project / "notes" / name).write_text("new output")

    stub_cli(monkeypatch, "Plain answer" if launcher == "claude" else codex_events(), before_exit=record)
    assert bw.spawn_beat(voices[0], voices)[0]
    receipt = read_receipt(project)
    attempt = receipt["attempt_id"]
    prompt = "Standing prompt.\n\n---\n\nBegin.\n"
    if launcher == "claude":
        assert launch["argv"] == [cli, "--print", "--session-id", attempt, "--output-format", "json",
                                  "--model", "asked-model", "-p", prompt]
        assert launch["env"]["CLAUDE_CODE_ENTRYPOINT"] == "sdk-cli"
    else:
        assert launch["argv"] == [cli, "exec", "--json", "--color", "never", "--cd", str(project),
                                  "--skip-git-repo-check", "--model", "asked-model", "--", prompt]
        assert "CLAUDE_CODE_ENTRYPOINT" not in launch["env"]
    assert launch["cwd"] == project
    assert launch["receipt"]["status"] == "running"
    assert launch["receipt"]["completed_at"] is None
    assert launch["receipt"]["schema_version"] == 1
    assert launch["env"]["SILL_DB_CONTAINER"] == "fixture-db"
    assert launch["env"]["SILL_BEAT_JOURNAL_DIRS"] == "notes/:logs/"
    assert launch["env"]["SILL_DETACHED_BEAT"] == "1"
    assert launch["env"]["SILL_ATTEMPT_ID"] == attempt
    assert launch["env"]["SILL_SESSION_ID"] == f"beat:{attempt}"
    assert launch["env"]["SILL_SESSION_ID_SOURCE"] == "worker_attempt"
    assert not {"CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "SILL_INTERACTIVE"} & launch["env"].keys()
    assert receipt["new_outputs"] == ["notes/a.md", "notes/z.md"]
    assert receipt["requested_model"] == "asked-model"
    assert receipt["effective_model"] is None


@pytest.mark.parametrize("setting,value", [
    ("launcher", "unknown"), ("launcher", None), ("cli", " "), ("cli", 1), ("model", ""),
])
def test_invalid_launcher_options_are_rejected_before_launch(project, setting, value):
    with pytest.raises(ValueError, match=setting):
        configure(project, **{setting: value})


@pytest.mark.parametrize("mode,wanted", [
    ("neighbor_only", None), ("exact", "claude-main"), ("usage", "claude-usage"),
    ("ambiguous", None), ("conflicting_session", None), ("contradictory_usage", None),
    ("invalid_usage", None), ("alternate_path", "claude-main"), ("duplicate", None),
    ("wrong_native_id", None), ("legacy_exact", "claude-main"), ("malformed_native", None),
    ("malformed_native_model", None), ("multiple_usage", None), ("null_usage", None),
])
def test_claude_model_is_attributed_only_to_exact_session(project, monkeypatch, mode, wanted):
    voices = configure(project)
    sessions = project / "claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project))
    sessions.mkdir(parents=True)

    def assistant(session, model, *, sidechain=False):
        return {"type": "assistant", "sessionId": session, "isSidechain": sidechain,
                "message": {"model": model}}

    def run(argv, *, on_spawn, **kwargs):
        on_spawn()
        attempt = argv[argv.index("--session-id") + 1]
        if mode not in {"neighbor_only", "usage"}:
            native_id = NEIGHBOR if mode == "wrong_native_id" else attempt
            records = [assistant(native_id, "claude-main"), assistant(NEIGHBOR, "<synthetic>"),
                       assistant(attempt, "claude-sidechain", sidechain=True),
                       {"type": "user", "message": {"model": "misleading-tool-model"}}]
            if mode == "ambiguous":
                records.append(assistant(attempt, "claude-other"))
            if mode == "malformed_native_model":
                records.append(assistant(attempt, None))
            selected_dir = sessions.with_name("-alternate-native-path") if mode == "alternate_path" else sessions
            selected_dir.mkdir(exist_ok=True)
            native = selected_dir / f"{attempt}.jsonl"
            native.write_text("truncated native file" if mode == "malformed_native" else jsonl(*records))
            if mode == "duplicate":
                duplicate = sessions.with_name("-duplicate-path") / native.name
                duplicate.parent.mkdir()
                duplicate.write_text(jsonl(*records))
        (sessions / f"{NEIGHBOR}.jsonl").write_text(jsonl(assistant(NEIGHBOR, "claude-neighbor")))
        usage = {"claude-usage": {"inputTokens": 12}} if mode == "usage" else None
        if mode == "contradictory_usage":
            usage = {"claude-other": {}, "claude-extra": {}}
        if mode == "invalid_usage":
            usage = {"claude-usage": {}, "unknown": "malformed metadata"}
        if mode == "multiple_usage":
            usage = {"claude-main": {}, "claude-other": {}}
        response = {
            "type": "result", "result": 'Done. {"model":"stdout-impostor"}', "is_error": False,
            "session_id": NEIGHBOR if mode == "conflicting_session" else attempt,
        }
        if usage is not None or mode == "null_usage":
            response["modelUsage"] = usage
        if mode in {"wrong_native_id", "legacy_exact", "malformed_native"}:
            return subprocess.CompletedProcess(argv, 0, "Plain wrapper output", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")

    monkeypatch.setattr(bw, "_run_beat_subprocess", run)
    assert bw.spawn_beat(voices[0], voices)[0]
    receipt = read_receipt(project)
    assert receipt["effective_model"] == wanted
    source = "claude_native_session_metadata" if wanted == "claude-main" else None
    assert receipt["model_source"] == ("claude_result_model_usage" if mode == "usage" else source)
    unknown_host = {"conflicting_session", "wrong_native_id", "malformed_native"}
    assert receipt["host_session_id"] == (None if mode in unknown_host else receipt["attempt_id"])


@pytest.mark.parametrize("mode,wanted", [
    ("exact", "gpt-native"), ("missing", None), ("wrong_id", None),
    ("duplicate", None), ("multiple_models", None), ("malformed_meta", None),
    ("malformed_turn_model", None), ("malformed_turn_context", None),
])
def test_codex_receipt_uses_thread_event_and_omits_execution_trace(project, monkeypatch, mode, wanted):
    configure(project, launcher="codex")
    monkeypatch.setenv("SILL_BEAT_CLI", "unused-claude-executable")
    tool_trace = {"type": "item.completed", "item": {
        "type": "command_execution", "command": "private-tool-argv",
        "aggregated_output": f'{{"model":"stdout-impostor","thread_id":"{NEIGHBOR}"}}',
    }}
    reasoning = {"type": "item.completed", "item": {"type": "reasoning", "text": "raw reasoning trace"}}
    metadata = {"type": "session_meta", "payload": {"id": NEIGHBOR if mode == "wrong_id" else THREAD}}
    context = {"type": "turn_context", "payload": {"model": "gpt-native"}}
    if mode != "missing":
        entries = [metadata, context]
        if mode == "multiple_models":
            entries.append({"type": "turn_context", "payload": {"model": "gpt-other"}})
        if mode == "malformed_meta":
            entries.append({"type": "session_meta", "payload": []})
        if mode == "malformed_turn_model":
            entries.append({"type": "turn_context", "payload": {"model": None}})
        if mode == "malformed_turn_context":
            entries.append({"type": "turn_context", "payload": []})
        codex_rollout(project, THREAD, *entries)
    if mode == "duplicate":
        codex_rollout(project, THREAD, metadata, context, day="14")
    codex_rollout(project, NEIGHBOR, {"type": "session_meta", "payload": {"id": NEIGHBOR}},
                  {"type": "turn_context", "payload": {"model": "gpt-neighbor"}})
    captured = []
    stub_cli(monkeypatch, codex_events(tool_trace, reasoning), stderr="raw stderr trace",
             before_exit=lambda argv, _: captured.extend(argv))

    assert bw.run_beat_loop(once=True) == 0
    assert captured[:4] == ["codex", "exec", "--json", "--color"]
    assert "--model" not in captured
    receipt = read_receipt(project)
    assert receipt["host_session_id"] == THREAD
    assert receipt["requested_host_session_id"] is None
    assert receipt["effective_model"] == wanted
    assert receipt["model_source"] == ("codex_native_session_metadata" if wanted else None)
    assert receipt["state_advanced"] is True
    summary = (project / "logs" / f"{receipt['attempt_id']}.txt").read_text()
    assert "Final answer" in summary
    assert not any(trace in summary for trace in (
        "First message", "private-tool-argv", "stdout-impostor", "raw reasoning trace", "raw stderr trace",
    ))


@pytest.mark.parametrize("launcher,terminal", [
    ("claude", None), ("codex", "turn.failed"), ("codex", None),
])
def test_claude_is_error_and_codex_turn_failed_hold_rotation(project, monkeypatch, launcher, terminal):
    configure(project, launcher=launcher, output_glob="notes/*.md")
    output = (json.dumps({"type": "result", "result": "CLI said it failed", "is_error": True})
              if launcher == "claude" else codex_events(terminal=terminal))
    stub_cli(monkeypatch, output,
             before_exit=lambda *_: (project / "notes" / "partial.md").write_text("partial output"))
    assert bw.run_beat_loop(once=True) == 1
    receipt = read_receipt(project)
    assert receipt["status"] == "failed"
    assert receipt["returncode"] == 0
    assert receipt["new_outputs"] == ["notes/partial.md"]
    assert receipt["state_advanced"] is False
    assert not (project / "rotation.json").exists()


def test_legacy_claude_wrapper_keeps_typeless_json_with_nonstring_result(project, monkeypatch):
    voices = configure(project)
    stdout = '{"result":{"records":[1]},"is_error":false}'
    stub_cli(monkeypatch, stdout)

    success, _, transcript = bw.spawn_beat(voices[0], voices)
    assert success
    assert Path(transcript).read_text().endswith(stdout)


@pytest.mark.parametrize("payload", [
    {"type": "result", "result": "Failed"},
    {"type": "result", "result": "Failed", "is_error": "true"},
    {"type": "result", "result": "Failed", "is_error": 0},
    {"result": "Failed", "is_error": "false"},
])
def test_malformed_claude_outcome_holds_rotation_without_output_glob(project, monkeypatch, payload):
    configure(project)
    stub_cli(monkeypatch, json.dumps(payload))

    assert bw.run_beat_loop(once=True) == 1
    receipt = read_receipt(project)
    assert receipt["status"] == "failed"
    assert receipt["returncode"] == 0
    assert receipt["state_advanced"] is False
    assert not (project / "rotation.json").exists()


@pytest.mark.parametrize("case,expected", [
    ("prompt", "launch_error"), ("executable", "launch_error"), ("timeout", "timeout"),
    ("interrupt", "interrupted"), ("old_output", "no_record"), ("login", "skipped"),
])
def test_attempt_receipts_cover_preflight_timeout_interrupt_and_missing_output(project, monkeypatch, case, expected):
    configure(project, output_glob="notes/*.md")
    existing = project / "notes" / "existing.md"
    existing.write_text("previous attempt")

    def run(argv, *, on_spawn, **kwargs):
        if case == "prompt":
            pytest.fail("The worker tried to launch with a missing prompt")
        if case == "executable":
            raise FileNotFoundError("missing launcher")
        on_spawn()
        if case == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if case == "interrupt":
            raise KeyboardInterrupt
        if case == "login":
            return subprocess.CompletedProcess(argv, 1, "Not logged in", "")
        existing.write_text("rewrote the previous file")
        return subprocess.CompletedProcess(argv, 0, "Done", "")

    monkeypatch.setattr(bw, "_run_beat_subprocess", run)
    if case == "prompt":
        (project / "prompt.md").unlink()
    assert bw.run_beat_loop(once=True) == 1
    receipt = read_receipt(project)
    assert receipt["status"] == expected
    assert receipt["state_advanced"] is False
    assert not (project / "rotation.json").exists()
    assert receipt["new_outputs"] == []
    finished = datetime.fromisoformat(receipt["completed_at"])
    assert finished.utcoffset() is not None
    if case in {"prompt", "executable"}:
        assert receipt["spawned_at"] is None
    else:
        started = datetime.fromisoformat(receipt["spawned_at"])
        assert started.utcoffset() is not None
        assert finished >= started


def test_unwritable_receipt_prevents_launch(project, monkeypatch, caplog):
    configure(project)
    original = bw.atomic_json

    def write(path, data):
        if path.parent == project / "logs":
            raise OSError("simulated receipt storage failure")
        original(path, data)

    monkeypatch.setattr(bw, "atomic_json", write)
    monkeypatch.setattr(bw, "_run_beat_subprocess", lambda *_, **kw: pytest.fail("launched without a receipt"))
    assert bw.run_beat_loop(once=True) == 1
    assert "simulated receipt storage failure" in caplog.text
    assert not list((project / "logs").iterdir())
    assert not (project / "rotation.json").exists()


def test_state_commit_failure_is_visible_in_attempt_receipt(project, monkeypatch, caplog):
    configure(project)
    stub_cli(monkeypatch, "Done")
    state = project / "rotation.json"
    state.write_text('{"next_index":0,"last_successful_attempt_id":"previous"}')
    original = bw.atomic_json

    def write(path, data):
        if path == state:
            staged = read_receipt(project)
            assert staged["status"] == "succeeded"
            assert staged["state_advanced"] is None
            assert data == {"next_index": 1, "last_successful_attempt_id": staged["attempt_id"]}
            raise OSError("simulated state storage failure")
        original(path, data)

    monkeypatch.setattr(bw, "atomic_json", write)
    assert bw.run_beat_loop(once=True) == 1
    receipt = read_receipt(project)
    assert receipt["status"] == "state_error"
    assert receipt["state_advanced"] is False
    assert "simulated state storage failure" in caplog.text
    assert json.loads(state.read_text()) == {"next_index": 0, "last_successful_attempt_id": "previous"}


def test_next_run_honors_state_after_final_receipt_write_failure(project, monkeypatch, caplog):
    configure(project)
    stub_cli(monkeypatch, "Done")
    state = project / "rotation.json"
    original = bw.atomic_json

    def write(path, data):
        if path.parent == project / "logs" and data.get("state_advanced") is True:
            assert json.loads(state.read_text())["last_successful_attempt_id"] == data["attempt_id"]
            raise OSError("simulated final receipt failure")
        original(path, data)

    monkeypatch.setattr(bw, "atomic_json", write)
    assert bw.run_beat_loop(once=True) == 0
    staged = read_receipt(project)
    assert staged["state_advanced"] is None
    assert staged["status"] == "succeeded"
    assert json.loads(state.read_text()) == {"next_index": 1, "last_successful_attempt_id": staged["attempt_id"]}
    assert staged["attempt_id"] in caplog.text
    assert "simulated final receipt failure" in caplog.text

    monkeypatch.setattr(bw, "atomic_json", original)
    assert bw.run_beat_loop(once=True) == 0
    receipts = [json.loads(path.read_text()) for path in (project / "logs").glob("*.json")]
    advanced, = [receipt for receipt in receipts if receipt["attempt_id"] != staged["attempt_id"]]
    assert advanced["voice"] == "next"
    assert advanced["state_advanced"] is True
    assert json.loads(state.read_text()) == {"next_index": 0, "last_successful_attempt_id": advanced["attempt_id"]}



def test_sigint_during_initial_receipt_write_finalizes_without_launch(project, monkeypatch):
    configure(project)
    original = bw.atomic_json

    def write(path, data):
        original(path, data)
        if data.get("status") == "running":
            os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(bw, "atomic_json", write)
    monkeypatch.setattr(bw, "_run_beat_subprocess", lambda *_, **kw: pytest.fail("launched after SIGINT"))
    assert bw.run_beat_loop(once=True) == 1
    receipt = read_receipt(project)
    assert receipt["status"] == "interrupted"
    assert receipt["spawned_at"] is None
    assert receipt["completed_at"] is not None
    assert receipt["state_advanced"] is False
    assert not (project / "rotation.json").exists()
