"""Beat worker: config, state, classification, and the output-verification
guard. Tests stub the agent CLI runner to inspect spawn arguments without
starting a subprocess, contacting a database, or using the network."""

import json
import subprocess

import pytest

import beat_worker as bw


@pytest.fixture(autouse=True)
def synthetic_host_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))


VOICES_JSON = json.dumps({
    "voices": [
        {
            "name": "analyst",
            "prompt": "prompts/analyst.md",
            "transcripts": "logs/analyst",
            "output_glob": "notes/analyst-*.md",
            "kickoff": "Begin.",
        },
        {
            "name": "reflector",
            "prompt": "prompts/reflector.md",
            "transcripts": "logs/reflector",
            "output_glob": "journal/reflector-*.md",
            "kickoff": "Begin.",
        },
    ],
})


def test_load_voices_reads_config(tmp_path):
    cfg = tmp_path / "beats.json"
    cfg.write_text(VOICES_JSON)
    voices = bw.load_voices(cfg)
    assert [v.name for v in voices] == ["analyst", "reflector"]
    assert voices[0].prompt.endswith("prompts/analyst.md")
    assert voices[1].output_glob == "journal/reflector-*.md"


def test_load_voices_rejects_empty_config(tmp_path):
    cfg = tmp_path / "beats.json"
    cfg.write_text("{}")
    with pytest.raises(ValueError) as e:
        bw.load_voices(cfg)
    assert "no voices" in str(e.value).lower()


def test_state_roundtrip_and_corruption_tolerance(tmp_path):
    p = tmp_path / "state.json"
    bw.write_state(p, 3)
    assert bw.read_state(p) == 3
    p.write_text("{ not json")
    assert bw.read_state(p) == 0          # corrupt state must not crash the loop
    assert bw.read_state(tmp_path / "absent.json") == 0


def test_rotation_write_failure_preserves_previous_json(tmp_path, monkeypatch):
    path = tmp_path / "rotation.json"
    old = b'{ "next_index": 1 }\n'
    path.write_bytes(old)

    def fail_replace(source, destination):
        assert source.parent == path.parent
        assert source != destination == path
        assert json.loads(source.read_bytes()) == {"next_index": 0}
        raise OSError("replacement failed")

    monkeypatch.setattr("os.replace", fail_replace)
    with pytest.raises(OSError, match="replacement failed"):
        bw.write_state(path, 0)

    assert path.read_bytes() == old
    assert list(tmp_path.iterdir()) == [path]


def test_state_path_is_never_tmp():
    """The upstream lesson: rotation state in /tmp was wiped by a reboot and
    silenced the beats for five days."""
    assert "/tmp" not in str(bw.default_state_path())


@pytest.mark.parametrize("text,expected_substring", [
    ("API Error: unable to connect to API", "connect"),
    ("ECONNRESET while streaming", "reset"),
    ("Not logged in", "logged in"),
    ("please run /login", "logged in"),
])
def test_classify_skip_recognizes_transient_failures(text, expected_substring):
    reason = bw.classify_skip(text, duration=12.0)
    assert reason and expected_substring in reason.lower()


def test_classify_skip_returns_none_for_real_failures():
    assert bw.classify_skip("Traceback: ValueError in tool", duration=42.0) is None


def test_rotation_advances_only_on_success(tmp_path):
    p = tmp_path / "state.json"
    bw.write_state(p, 0)
    bw.advance_if(p, index=0, n_voices=2, success=False)
    assert bw.read_state(p) == 0
    bw.advance_if(p, index=0, n_voices=2, success=True)
    assert bw.read_state(p) == 1
    bw.advance_if(p, index=1, n_voices=2, success=True)
    assert bw.read_state(p) == 0          # wraps


def test_produced_output_detects_a_new_file(tmp_path):
    (tmp_path / "journal").mkdir()
    before = bw.snapshot_outputs(tmp_path, "journal/r-*.md")
    assert bw.produced_output(tmp_path, "journal/r-*.md", before) is False
    (tmp_path / "journal" / "r-001.md").write_text("a beat")
    assert bw.produced_output(tmp_path, "journal/r-*.md", before) is True


def test_produced_output_is_true_when_voice_declares_no_glob(tmp_path):
    """A voice with no output_glob cannot be verified; do not claim failure."""
    assert bw.produced_output(tmp_path, None, set()) is True


@pytest.mark.parametrize("mode", ["malformed-bytes", "missing-file"])
def test_spawn_beat_survives_unreadable_prompt(tmp_path, monkeypatch, mode):
    """spawn_beat() must return rather than raise for both failure classes a
    defensive prompt read has to cover: a missing file (OSError) and a file
    that exists but holds bytes invalid for the codec (UnicodeDecodeError —
    a ValueError, not an OSError subclass, so a bare `except OSError` lets
    it straight through uncaught). Calling spawn_beat directly here still
    respects this file's "no subprocess spawning" design: a prompt-read
    failure returns before the function reaches the agent CLI runner."""
    monkeypatch.setattr(bw, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SILL_BEAT_STATE_PATH", str(tmp_path / "state.json"))
    prompt_path = tmp_path / "prompts" / "voice.md"
    prompt_path.parent.mkdir(parents=True)
    if mode == "malformed-bytes":
        prompt_path.write_bytes(b"\xff\xfe not utf8")
    # else "missing-file": leave prompt_path un-created

    voice = bw.Voice(
        name="analyst", prompt="prompts/voice.md",
        transcripts="logs/analyst", output_glob=None, kickoff="Begin.",
    )
    success, duration, transcript_path = bw.spawn_beat(voice, [voice])
    assert success is False
    assert duration == 0.0
    assert transcript_path == ""

    state_path = tmp_path / "state.json"
    bw.write_state(state_path, 0)
    bw.advance_if(state_path, index=0, n_voices=2, success=success)
    assert bw.read_state(state_path) == 0          # rotation must not advance


# ---------------------------------------------------------------------------
# Guard scope derivation (Step 3b — wiring SILL_BEAT_JOURNAL_DIRS so
# stored-slot-guard.py / tool-type-witness.py / state-language-check.py's
# beat-aware fallback stop being opt-in-with-nothing-opting-in).
# ---------------------------------------------------------------------------

TWO_VOICES = [
    bw.Voice(name="analyst", prompt="prompts/analyst.md",
              transcripts="logs/analyst", output_glob="notes/analyst-*.md",
              kickoff="Begin."),
    bw.Voice(name="reflector", prompt="prompts/reflector.md",
              transcripts="logs/reflector", output_glob="journal/reflector-*.md",
              kickoff="Begin."),
]


def test_journal_dirs_for_voices_derives_from_output_glob_and_transcripts():
    """Matches backend/beats.example.json's two voices: each voice
    contributes its output_glob's directory, then its transcripts dir."""
    assert bw.journal_dirs_for_voices(TWO_VOICES) == (
        "notes/:logs/analyst/:journal/:logs/reflector/"
    )


def test_journal_dirs_for_voices_dedupes_shared_fragments():
    voices = [
        bw.Voice(name="a", prompt="p", transcripts="shared/",
                  output_glob="shared/a-*.md", kickoff="Begin."),
        bw.Voice(name="b", prompt="p", transcripts="shared/",
                  output_glob="shared/b-*.md", kickoff="Begin."),
    ]
    assert bw.journal_dirs_for_voices(voices) == "shared/"


def test_journal_dirs_for_voices_skips_a_voice_with_no_output_glob():
    voices = [bw.Voice(name="a", prompt="p", transcripts="logs/a",
                        output_glob=None, kickoff="Begin.")]
    assert bw.journal_dirs_for_voices(voices) == "logs/a/"


def test_journal_dirs_for_voices_empty_for_no_voices():
    """An install with no configured voices exports nothing — the guards
    stay opt-in-and-off, exactly as they were before this wiring."""
    assert bw.journal_dirs_for_voices([]) == ""


# ---------------------------------------------------------------------------
# Recursive glob (** ) in output_glob: os.path.dirname("journals/**/*.md")
# is "journals/**" — a fragment containing a literal wildcard component,
# not a directory. The guards consume this fragment as a plain substring
# (plugin/hooks/stored-slot-guard.py: `any(f in path for f in fragments)`),
# never re-interpreting "**" as a glob, so "journals/**/" as a scope
# fragment can never match a real write path — no real path contains the
# two literal characters "**". That silently makes the two opt-in guards
# (and state-language-check.py's beat-aware fallback) inert for any voice
# configured with a recursive glob, with no error anywhere: load_voices()
# accepts the glob, spawn_beat() exports the useless fragment, and the
# guard hooks just never fire. The shipped beats.example.json's two voices
# don't happen to use "**", so this was live-but-dormant, not visibly
# broken, in the default config.
# ---------------------------------------------------------------------------

def test_journal_dirs_for_voices_handles_recursive_glob():
    """A '**' path segment is itself a wildcard, not a directory name, so
    it cannot be part of the literal scope prefix — the derived fragment
    must stop at the last literal ancestor directory ('journals/', not
    'journals/**/') so it still substring-matches a real nested path."""
    voices = [bw.Voice(name="a", prompt="p", transcripts="logs/a",
                        output_glob="journals/**/*.md", kickoff="Begin.")]
    scope = bw.journal_dirs_for_voices(voices)
    assert "**" not in scope, f"scope fragment still contains a literal wildcard: {scope!r}"
    assert scope == "journals/:logs/a/"

    # The scope fragment must actually do its job: match the same
    # substring check the real guards use (plugin/hooks/stored-slot-
    # guard.py / tool-type-witness.py: `any(f in path for f in fragments)`).
    fragments = [f for f in scope.split(":") if f]
    real_write_path = "journals/reflector-beats/entry-042.md"
    assert any(f in real_write_path for f in fragments), (
        f"derived scope {fragments!r} does not match a real nested write "
        f"path {real_write_path!r} — the recursive-glob bug this test guards"
    )


def test_journal_dirs_for_voices_recursive_glob_at_top_level():
    """A bare '**/*.md' (no literal directory before the wildcard) yields
    no usable literal prefix — same as no output_glob at all, not a
    crash and not a bogus '**' fragment."""
    voices = [bw.Voice(name="a", prompt="p", transcripts="logs/a",
                        output_glob="**/*.md", kickoff="Begin.")]
    assert bw.journal_dirs_for_voices(voices) == "logs/a/"


def test_spawn_beat_exports_journal_dirs_derived_from_full_voice_config(tmp_path, monkeypatch):
    """The wiring this step exists for: spawn_beat() must export
    SILL_BEAT_JOURNAL_DIRS to the child, derived from every voice in the
    config — not just the one voice running this particular beat — so the
    two opt-in guards and state-language-check.py's beat-aware fallback are
    scoped with zero operator configuration. The stub completes the run and exercises
    the full success path, not just an early return."""
    monkeypatch.setattr(bw, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SILL_BEAT_STATE_PATH", str(tmp_path / "state.json"))
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "analyst.md").write_text("Standing prompt.\n")
    (tmp_path / "notes").mkdir()

    voices = [
        bw.Voice(name="analyst", prompt="prompts/analyst.md",
                  transcripts="logs/analyst", output_glob="notes/analyst-*.md",
                  kickoff="Begin."),
        bw.Voice(name="reflector", prompt="prompts/reflector.md",
                  transcripts="logs/reflector", output_glob="journal/reflector-*.md",
                  kickoff="Begin."),
    ]

    captured_env = {}

    runner = bw._run_beat_subprocess

    def fake_run(cmd, *, on_spawn=None, **kwargs):
        if on_spawn is None:
            return runner(cmd, **kwargs)
        on_spawn()
        captured_env.update(kwargs.get("env") or {})
        (tmp_path / "notes" / "analyst-001.md").write_text("beat output")
        return subprocess.CompletedProcess(cmd, 0, stdout="did something", stderr="")

    monkeypatch.setattr(bw, "_run_beat_subprocess", fake_run)

    success, duration, transcript_path = bw.spawn_beat(voices[0], voices)

    assert success is True, "fixture should exercise the verified-success path"
    assert captured_env.get("SILL_BEAT_JOURNAL_DIRS") == (
        "notes/:logs/analyst/:journal/:logs/reflector/"
    )
    # The other export this same env dict carries must still be present —
    # this test should not be the reason a regression there goes unnoticed.
    assert captured_env.get("SILL_DETACHED_BEAT") == "1"


def test_spawn_beat_does_not_restrict_the_child_to_project_settings(tmp_path, monkeypatch):
    """A beat reaches memory through MCP, and MCP is registered at user scope.

    `install.sh` step 7 registers the server with
    `claude mcp add --scope user sill -- sill-mcp`, so the entry lives in the
    operator's user-scope registry. The v0.2.0 rehearsal could not see whether
    a beat child picks that up, because the shim it used to keep the beats out
    of the operator's settings injected `--setting-sources project,local` —
    which also excludes user-scope MCP servers, and the beat duly reported the
    sill tools as not connected. Spawning a real beat through this function
    with no shim answered it: 38 `mcp__sill__*` tools visible to the child,
    `mcp__sill__get_health` called and answered (confirmed in the child's own
    session jsonl, not just its prose).

    That works *because* the spawn passes no `--setting-sources`. Adding one
    here — an easy-looking hardening — would silently cut every beat off from
    memory while leaving exit codes, transcripts, and output files identical.
    Hence this pin."""
    monkeypatch.setattr(bw, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SILL_BEAT_STATE_PATH", str(tmp_path / "state.json"))
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "analyst.md").write_text("Standing prompt.\n")
    (tmp_path / "notes").mkdir()

    voice = bw.Voice(name="analyst", prompt="prompts/analyst.md",
                     transcripts="logs/analyst", output_glob="notes/analyst-*.md",
                     kickoff="Begin.")
    captured_cmd = []

    runner = bw._run_beat_subprocess

    def fake_run(cmd, *, on_spawn=None, **kwargs):
        if on_spawn is None:
            return runner(cmd, **kwargs)
        on_spawn()
        captured_cmd.extend(cmd)
        (tmp_path / "notes" / "analyst-001.md").write_text("beat output")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(bw, "_run_beat_subprocess", fake_run)
    success, _duration, _transcript = bw.spawn_beat(voice, [voice])

    assert success is True
    assert not any("setting-sources" in str(arg) for arg in captured_cmd), (
        "spawn_beat restricted the child's settings sources; user scope is "
        f"where the MCP server is registered — argv was {captured_cmd[:3]}…"
    )
    assert captured_cmd[1] == "--print"


def test_spawn_beat_forwards_the_parent_environment_to_the_child(tmp_path, monkeypatch):
    """Half of how a beat's bare `sill notice` finds the right database.

    `sill.py` reads `SILL_DB_CONTAINER`/`SILL_DB_USER`/`SILL_DB_NAME` from its
    process environment, and a beat child never sets them itself — so this
    wholesale forward is the only route by which a beat's mint reaches a
    non-default container. The rehearsal saw the effect (a bare `sill notice`
    inside a beat hit the rehearsal database, while the same command from the
    operator's shell hit the default name and failed) and could not explain
    it. The other half is `worker.py`'s module-level `load_dotenv()`, which
    puts `backend/.env` into the worker's own environment before this dict is
    built — pinned in test_env_and_mcp_wiring.py."""
    monkeypatch.setattr(bw, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SILL_BEAT_STATE_PATH", str(tmp_path / "state.json"))
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "analyst.md").write_text("Standing prompt.\n")
    (tmp_path / "notes").mkdir()
    monkeypatch.setenv("SILL_DB_CONTAINER", "some_other_db")

    voice = bw.Voice(name="analyst", prompt="prompts/analyst.md",
                     transcripts="logs/analyst", output_glob="notes/analyst-*.md",
                     kickoff="Begin.")
    captured_env = {}

    runner = bw._run_beat_subprocess

    def fake_run(cmd, *, on_spawn=None, **kwargs):
        if on_spawn is None:
            return runner(cmd, **kwargs)
        on_spawn()
        captured_env.update(kwargs.get("env") or {})
        (tmp_path / "notes" / "analyst-001.md").write_text("beat output")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(bw, "_run_beat_subprocess", fake_run)
    bw.spawn_beat(voice, [voice])

    assert captured_env.get("SILL_DB_CONTAINER") == "some_other_db"


def test_spawn_beat_returns_false_when_exit_0_produces_no_output_file(tmp_path, monkeypatch):
    """The output-verification guard's whole point (beat_worker.py's
    produced_output() check inside spawn_beat, ~lines 536-543): a headless
    agent CLI with no tool permissions gets every tool call auto-denied,
    still exits 0, and has done nothing. Unlike
    test_spawn_beat_exports_journal_dirs_derived_from_full_voice_config's
    fake_run (which DOES write the output file, exercising the verified-
    success path), this fake_run deliberately leaves the declared
    output_glob untouched — the exact silent-failure shape docs/beats.md's
    Permissions section describes — so this exercises spawn_beat()'s
    silent-failure branch end-to-end rather than unit-testing
    produced_output() in isolation (already covered by
    test_produced_output_detects_a_new_file above)."""
    monkeypatch.setattr(bw, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SILL_BEAT_STATE_PATH", str(tmp_path / "state.json"))
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "analyst.md").write_text("Standing prompt.\n")
    (tmp_path / "notes").mkdir()

    voices = [
        bw.Voice(name="analyst", prompt="prompts/analyst.md",
                  transcripts="logs/analyst", output_glob="notes/analyst-*.md",
                  kickoff="Begin."),
        bw.Voice(name="reflector", prompt="prompts/reflector.md",
                  transcripts="logs/reflector", output_glob="journal/reflector-*.md",
                  kickoff="Begin."),
    ]

    def fake_run(cmd, *, on_spawn, **kwargs):
        on_spawn()
        # Exit 0 with plausible-looking output, but — unlike the fixture
        # above — no file written under notes/. This is exactly the shape
        # of a tool-permission-denied headless run: the agent CLI notices
        # the denial, gives up, and reports it in prose, having changed
        # nothing.
        return subprocess.CompletedProcess(
            cmd, 0, stdout="I tried to write the note but the tool call was denied.", stderr="")

    monkeypatch.setattr(bw, "_run_beat_subprocess", fake_run)

    success, duration, transcript_path = bw.spawn_beat(voices[0], voices)

    assert success is False, "exit 0 with no matching output file must not count as success"
    # A transcript IS written on this path — spawn_beat reached and ran the
    # subprocess; only the post-hoc output check failed it. That
    # distinguishes this branch from the earlier prompt-read/spawn failures
    # above, which return "" for transcript_path instead.
    assert transcript_path != ""

    # And the consequence that actually matters operationally: rotation
    # must not advance on this "success", so the same voice retries next
    # interval instead of the rotation silently moving on.
    state_path = tmp_path / "state.json"
    bw.write_state(state_path, 0)
    bw.advance_if(state_path, index=0, n_voices=len(voices), success=success)
    assert bw.read_state(state_path) == 0


def test_spawn_beat_sets_no_journal_dirs_var_when_derivation_is_empty(tmp_path, monkeypatch):
    """A voice with neither a usable output_glob directory nor a transcripts
    value (degenerate, but not something spawn_beat should crash over) must
    not export an empty SILL_BEAT_JOURNAL_DIRS — an empty string would still
    be "set" from os.environ.get's point of view and change the guards'
    unset-means-no-scope contract."""
    monkeypatch.setattr(bw, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SILL_BEAT_STATE_PATH", str(tmp_path / "state.json"))
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "voice.md").write_text("Standing prompt.\n")

    voice = bw.Voice(name="bare", prompt="prompts/voice.md",
                      transcripts="", output_glob=None, kickoff="Begin.")

    captured_env = {}

    def fake_run(cmd, *, on_spawn, **kwargs):
        on_spawn()
        captured_env.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0, stdout="did something", stderr="")

    monkeypatch.setattr(bw, "_run_beat_subprocess", fake_run)

    success, _duration, _transcript = bw.spawn_beat(voice, [voice])

    assert success is True  # no output_glob declared -> produced_output() defaults True
    assert "SILL_BEAT_JOURNAL_DIRS" not in captured_env


# ---------------------------------------------------------------------------
# run_beat_loop(): the top-level `while True:` scheduler. Its pieces
# (spawn_beat, advance_if, read_state, journal_dirs_for_voices) all have
# direct coverage above; the loop function itself — which voice it reads,
# how it calls spawn_beat/advance_if, whether it reaches the sleep call —
# had none (flagged in review, P3 Task 2: "spawn_beat/run_beat_loop have
# no permanent unit coverage of internal wiring"). It runs forever by
# design, so driving it in a test needs a way to stop after exactly one
# real iteration without adding a test-only seam to the production
# function; patching time.sleep to raise a sentinel does that with zero
# source changes, mirroring test_spawn_beat_survives_unreadable_prompt's
# "call the real thing, control only its I/O boundary" style above.
# ---------------------------------------------------------------------------

class _StopAfterOneIteration(Exception):
    """Raised by the patched time.sleep to end run_beat_loop's `while True:`
    after exactly one iteration, once it reaches the sleep it would
    otherwise block on."""


def test_run_beat_loop_drives_one_full_iteration(tmp_path, monkeypatch):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "analyst.md").write_text("Standing prompt.\n")
    (tmp_path / "prompts" / "reflector.md").write_text("Standing prompt.\n")
    cfg = tmp_path / "beats.json"
    cfg.write_text(VOICES_JSON)
    state_path = tmp_path / "state.json"

    monkeypatch.setattr(bw, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SILL_BEAT_CONFIG", str(cfg))
    monkeypatch.setenv("SILL_BEAT_STATE_PATH", str(state_path))
    monkeypatch.setattr(bw, "POST_HOOK", None)
    # A real INTERVAL_SECONDS (7200 default) would make a genuinely-reached
    # sleep_time large and positive; fixing it at 0 keeps
    # max(0, INTERVAL_SECONDS - beat_total) reliably > 0 isn't required —
    # sleep() is patched to stop the loop regardless of the exact value,
    # so what matters is only that the call is reached at all.
    monkeypatch.setattr(bw, "INTERVAL_SECONDS", 300)

    runner = bw._run_beat_subprocess

    def fake_run(cmd, *, on_spawn=None, **kwargs):
        if on_spawn is None:
            return runner(cmd, **kwargs)
        on_spawn()
        (tmp_path / "notes").mkdir()
        (tmp_path / "notes" / "analyst-one.md").write_text("beat output")
        return subprocess.CompletedProcess(cmd, 0, stdout="Done", stderr="")

    monkeypatch.setattr(bw, "_run_beat_subprocess", fake_run)

    def fake_sleep(seconds):
        raise _StopAfterOneIteration(seconds)

    monkeypatch.setattr(bw.time, "sleep", fake_sleep)

    with pytest.raises(_StopAfterOneIteration) as exc_info:
        bw.run_beat_loop()

    # Reached the sleep with a sane, positive duration — proves beat_total
    # was computed and subtracted from INTERVAL_SECONDS, not skipped.
    assert exc_info.value.args[0] > 0

    # The rotation actually advanced: voice 0 (analyst) ran and succeeded,
    # so the state file must now point at voice 1 (reflector).
    assert bw.read_state(state_path) == 1


def test_run_beat_loop_does_not_advance_rotation_on_a_failed_beat(tmp_path, monkeypatch):
    """Same drive-one-iteration harness as above, but spawn_beat reports
    failure — the loop must still reach sleep (it always does, success or
    not) while leaving rotation exactly where it started."""
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "analyst.md").write_text("Standing prompt.\n")
    (tmp_path / "prompts" / "reflector.md").write_text("Standing prompt.\n")
    cfg = tmp_path / "beats.json"
    cfg.write_text(VOICES_JSON)
    state_path = tmp_path / "state.json"

    monkeypatch.setattr(bw, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SILL_BEAT_CONFIG", str(cfg))
    monkeypatch.setenv("SILL_BEAT_STATE_PATH", str(state_path))
    monkeypatch.setattr(bw, "POST_HOOK", None)
    monkeypatch.setattr(bw, "INTERVAL_SECONDS", 300)
    def fake_run(cmd, *, on_spawn, **kwargs):
        on_spawn()
        return subprocess.CompletedProcess(cmd, 2, stdout="Beat failed", stderr="")

    monkeypatch.setattr(bw, "_run_beat_subprocess", fake_run)

    def fake_sleep(seconds):
        raise _StopAfterOneIteration(seconds)

    monkeypatch.setattr(bw.time, "sleep", fake_sleep)

    with pytest.raises(_StopAfterOneIteration):
        bw.run_beat_loop()

    assert bw.read_state(state_path) == 0, "a failed beat must not advance rotation"
