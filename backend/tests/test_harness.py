"""Harness normalization. Pure functions over payload dicts; no I/O."""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "plugin" / "hooks"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
spec = importlib.util.spec_from_file_location("_harness", HOOKS / "_harness.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


CLAUDE_BASH = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
CODEX_EXEC = {"tool_name": "exec", "tool_input": {"command": "echo hi"}}
CODEX_EXEC_CMD = {"tool_name": "exec_command", "tool_input": {"command": "echo hi"}}
CLAUDE_WRITE = {"tool_name": "Write",
                "tool_input": {"file_path": "a.md", "content": "x"}}
CODEX_PATCH = {"tool_name": "apply_patch",
               "tool_input": {"input": "*** Update File: a.md\n+x\n"}}
CLAUDE_EDIT = {"tool_name": "Edit",
               "tool_input": {"file_path": "a.md", "old_string": "foo", "new_string": "bar"}}
CLAUDE_MULTI_EDIT = {"tool_name": "MultiEdit",
                      "tool_input": {"file_path": "a.md", "edits": [
                          {"old_string": "foo", "new_string": "bar"},
                          {"old_string": "baz", "new_string": "qux"},
                      ]}}
CODEX_MULTI_FILE_PATCH = {"tool_name": "apply_patch",
                           "tool_input": {"input": "*** Update File: a.md\n+x\n"
                                                    "*** Update File: b.md\n+y\n"}}


def _hook_events(host):
    with (FIXTURES / f"{host}-hook-events.jsonl").open(encoding="utf-8") as fixture:
        return {event["hook_event_name"]: event for event in map(json.loads, fixture)}


@pytest.mark.parametrize("payload", [CLAUDE_BASH, CODEX_EXEC, CODEX_EXEC_CMD])
def test_shell_kind_across_both_harnesses(payload):
    assert h.tool_kind(payload) == "shell"
    assert h.shell_command(payload) == "echo hi"


# --- Codex shell shapes, ground-truthed against real rollouts --------------
#
# Everything above was written against hand-derived schemas. These cases come
# from a census of 309 real Codex rollout transcripts under ~/.codex/sessions
# (2026-08-06), which contradicted three of those derivations:
#
#   exec_command   1246/1246 carry the command under "cmd", never "command",
#                  always a plain string.
#   shell           314/314 carry "command" as an ARGV LIST (['bash','-lc',…]).
#   shell_command   355/355 carry "command" as a string.
#
# So `shell` and `shell_command` were not in _SHELL_NAMES at all (669 real
# calls classified as "other"), and exec_command's command was unreadable
# (1246 real calls yielding None) — on the harness the guards are documented
# to support. Content is synthesized here; only the key/value SHAPES are
# taken from the census.

CODEX_EXEC_CMD_REAL = {"tool_name": "exec_command",
                       "tool_input": {"cmd": "echo hi", "workdir": "/tmp",
                                      "yield_time_ms": 250, "max_output_tokens": 4096}}
CODEX_SHELL_REAL = {"tool_name": "shell",
                    "tool_input": {"command": ["bash", "-lc", "echo hi"],
                                   "workdir": "/tmp"}}
CODEX_SHELL_COMMAND_REAL = {"tool_name": "shell_command",
                            "tool_input": {"command": "echo hi", "workdir": "/tmp"}}


@pytest.mark.parametrize("payload", [
    CODEX_EXEC_CMD_REAL, CODEX_SHELL_REAL, CODEX_SHELL_COMMAND_REAL,
], ids=["exec_command-cmd", "shell-argv-list", "shell_command-string"])
def test_real_codex_shell_shapes_classify_and_yield_their_command(payload):
    assert h.tool_kind(payload) == "shell"
    got = h.shell_command(payload)
    assert got is not None, "a real Codex shell call yielded no command text"
    assert "echo hi" in got


def test_argv_list_command_is_rendered_for_scanning_not_dropped():
    """`shell`'s argv list is rendered one element per LINE — every guard
    downstream does substring/regex work on this string, so a list must not
    read as "no command"."""
    assert h.shell_command(CODEX_SHELL_REAL) == "bash\n-lc\necho hi"


def test_argv_rendering_keeps_the_script_element_in_command_position():
    """Space-joining an argv fabricates a shell line nobody ran: 'bash -lc'
    + 'echo === && ls' reads as `bash -lc echo === && ls`, where `echo` is
    no longer in command position and shell-idiom-guard's trap regex —
    which anchors on command position, correctly — stops seeing a real
    trap. One element per line keeps the script element at a line start.
    (test_guards_on_both_harnesses.py::…[codex-shell] is the end-to-end
    half of this; this pins the rendering that makes it work.)"""
    payload = {"tool_name": "shell",
               "tool_input": {"command": ["bash", "-lc", "echo === && ls"]}}
    rendered = h.shell_command(payload)
    trap = re.compile(r"(?:^|[;&|`(]\s*)echo\s+=\S", re.MULTILINE)
    assert trap.search(rendered), rendered


def test_command_key_precedence_and_junk_values():
    """"command" wins when both keys are present; a value of a type neither
    string nor list yields None rather than a stringified dict."""
    assert h.shell_command({"tool_name": "exec_command",
                            "tool_input": {"command": "first", "cmd": "second"}}) == "first"
    assert h.shell_command({"tool_name": "shell",
                            "tool_input": {"command": {"not": "a command"}}}) is None
    assert h.shell_command({"tool_name": "shell",
                            "tool_input": {"command": []}}) is None


@pytest.mark.parametrize("payload", [CLAUDE_WRITE, CODEX_PATCH])
def test_write_kind_across_both_harnesses(payload):
    assert h.tool_kind(payload) in ("write", "edit")
    assert h.written_path(payload) == "a.md"


@pytest.mark.parametrize("event_name", ["PreToolUse", "PostToolUse"])
def test_current_codex_apply_patch_command_and_legacy_input_normalize_identically(event_name):
    event = _hook_events("codex")[event_name]
    legacy_event = {**event, "tool_input": {"input": event["tool_input"]["command"]}}
    expected_path = "/tmp/sill-hook-fixture/outputs/rehearsal.md"
    expected_text = (
        "The synthetic blue compass rests beside the sample notebook. "
        "This file verifies the scheduled worker output contract."
    )
    for payload in (event, legacy_event, _hook_events("claude")[event_name]):
        assert h.tool_kind(payload) == "write"
        assert h.written_path(payload) == expected_path
        assert h.written_text(payload) == expected_text
        assert h.written_files(payload) == [(expected_path, expected_text)]


def test_codex_patch_command_takes_precedence_over_legacy_input():
    payload = {
        "tool_name": "apply_patch",
        "tool_input": {
            "command": "*** Add File: current.md\n+current\n",
            "input": "*** Add File: legacy.md\n+legacy\n",
        },
    }
    assert h.written_path(payload) == "current.md"
    assert h.written_text(payload) == "current"
    assert h.written_files(payload) == [("current.md", "current")]
    # A nonblank string body wins even when its contents are unparseable.
    payload["tool_input"]["command"] = "missing a patch header"
    assert h.written_path(payload) is None
    assert h.written_text(payload) is None
    assert h.written_files(payload) == []


@pytest.mark.parametrize("command", [None, ["patch"], {"patch": "text"}, 42, "", " \n"])
def test_codex_patch_uses_legacy_input_if_command_has_no_string_body(command):
    payload = {
        "tool_name": "apply_patch",
        "tool_input": {"command": command, "input": "*** Add File: legacy.md\n+legacy\n"},
    }
    assert h.written_path(payload) == "legacy.md"
    assert h.written_text(payload) == "legacy"
    assert h.written_files(payload) == [("legacy.md", "legacy")]


def test_malformed_patch_fields_do_not_normalize_as_text():
    payload = {
        "tool_name": "apply_patch",
        "tool_input": {"command": ["*** Add File: a.md", "+text"], "input": 42},
    }
    assert h.written_path(payload) is None
    assert h.written_text(payload) is None
    assert h.written_files(payload) == []


def test_hook_events_and_rollout_records_use_separate_parsers(tmp_path):
    rollout = {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": "apply_patch",
            "call_id": "call_fixture",
            "input": "*** Add File: a.md\n+text\n",
        },
    }
    assert h.written_files(rollout) == []
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps(rollout), encoding="utf-8")
    assert list(h.iter_transcript_tool_uses(transcript)) == [
        {"id": "call_fixture", "name": "apply_patch", "input": "*** Add File: a.md\n+text\n"},
    ]
    assert list(h.iter_transcript_tool_uses(FIXTURES / "codex-hook-events.jsonl")) == []


def test_written_text_reads_flat_edit_new_string():
    assert h.written_text(CLAUDE_EDIT) == "bar"


def test_written_text_reads_multi_edit_nested_new_strings():
    """MultiEdit nests its edits under an "edits" list instead of Edit's
    flat top-level new_string, but tool_kind classifies both as "edit" —
    so the natural `if tool_kind(p) == "edit": written_text(p)` caller
    must not silently see nothing for every MultiEdit call."""
    text = h.written_text(CLAUDE_MULTI_EDIT)
    assert text is not None
    assert "bar" in text
    assert "qux" in text


def test_written_text_multi_edit_skips_malformed_entries_without_raising():
    payload = {"tool_name": "MultiEdit",
               "tool_input": {"file_path": "a.md", "edits": [
                   "not a dict",
                   {"old_string": "foo"},  # missing new_string
                   {"old_string": "baz", "new_string": "qux"},
               ]}}
    assert h.written_text(payload) == "qux"


def test_multi_file_patch_text_is_scoped_to_the_path_it_reports():
    """A patch touching two files must not let file B's content answer
    for file A's path: written_path reports the first file only, and
    written_text must be scoped to that same file, not the whole patch
    body — otherwise a caller checking 'is this path in scope, and does
    its text contain X' can judge file A's scope against file B's
    content."""
    assert h.written_path(CODEX_MULTI_FILE_PATCH) == "a.md"
    assert h.written_text(CODEX_MULTI_FILE_PATCH) == "x"


# --- written_files: the plural, multi-file-safe view -----------------------
#
# written_path/written_text only ever answer for the FIRST file header in an
# apply_patch body. A caller that gates scope on written_path() alone before
# reading written_text() answers "in scope?" for the whole call using only
# that first file's path — so a patch whose first file is irrelevant and
# whose second file is in scope is never inspected at all: written_path
# reports the irrelevant path, the scope check fails, and the caller never
# even looks at the second file's text. This is the bypass
# test_guards_on_both_harnesses.py's multi-file tests close at the guard
# level; these are the harness-level tests for the API those guards now use.

def test_written_files_single_file_matches_written_path_and_text():
    """For any single-file call, written_files is exactly the one-element
    [(written_path, written_text)] pair — the plural form must not silently
    diverge from the singular one it replaces for scope-gating."""
    for payload in (CLAUDE_WRITE, CODEX_PATCH, CLAUDE_EDIT, CLAUDE_MULTI_EDIT):
        assert h.written_files(payload) == [(h.written_path(payload), h.written_text(payload))]


def test_written_files_scopes_each_files_text_to_itself_on_a_multi_file_patch():
    """The core property: every file gets its OWN pair, text scoped to only
    that file's '+' lines — b.md's pair must not include a.md's 'alpha' and
    vice versa. Bumped to two added lines on the second file to also prove
    a file's own multi-line text is joined correctly, not just truncated to
    one line."""
    payload = {"tool_name": "apply_patch",
               "tool_input": {"input": "*** Update File: a.md\n+alpha\n"
                                        "*** Update File: b.md\n+bravo\n+charlie\n"}}
    assert h.written_files(payload) == [("a.md", "alpha"), ("b.md", "bravo\ncharlie")]


def test_written_files_skips_a_header_with_no_path_but_keeps_later_files():
    """Mirrors _parse_patch_path's 'blank-tail header' case (Task 1), but
    per-file: a header whose path strips to empty contributes no pair for
    that section — same 'don't guess a path' contract — without that
    dropping the file that follows it."""
    payload = {"tool_name": "apply_patch",
               "tool_input": {"input": "*** Update File:    \n+dropped\n"
                                        "*** Update File: b.md\n+kept\n"}}
    assert h.written_files(payload) == [("b.md", "kept")]


def test_written_files_empty_when_nothing_parseable():
    assert h.written_files({"tool_name": "apply_patch",
                             "tool_input": {"input": "no header in this body\n"}}) == []
    assert h.written_files({"tool_name": "Write", "tool_input": {"content": "x"}}) == []  # no file_path
    assert h.written_files(CLAUDE_BASH) == []  # wrong kind entirely
    assert h.written_files({}) == []


def test_written_files_never_raises_on_garbage():
    for junk in [None, [], "string", {"tool_input": None}, {"tool_name": None}]:
        assert h.written_files(junk) == []


def test_unknown_tool_is_other_not_a_crash():
    assert h.tool_kind({"tool_name": "image_gen.imagegen", "tool_input": {}}) == "other"
    assert h.tool_kind({}) == "other"
    assert h.shell_command({"tool_name": "view_image", "tool_input": {}}) is None


def test_mcp_name_is_flat_on_the_hook_surface():
    p = {"tool_name": "mcp__sill__remember", "tool_input": {}}
    assert h.tool_kind(p) == "mcp"
    assert h.mcp_tool_name(p) == "mcp__sill__remember"


def test_mcp_name_is_joined_correctly_from_a_split_transcript_record():
    """Codex transcripts split namespace and name; joining without the
    separator yields 'mcp__sillrecall_batch', which only worked by accident
    under a substring filter."""
    rec = {"namespace": "mcp__sill", "name": "recall_batch"}
    assert h.join_mcp_name(rec) == "mcp__sill__recall_batch"
    assert h.join_mcp_name({"name": "exec"}) == "exec"


def test_mcp_join_does_not_double_a_separator_the_namespace_already_carries():
    """Real rollouts spell the namespace both ways: "mcp__sill" (23 calls)
    and "mcp__episodic_memory__"/"mcp__agi_memory__" (45 calls) — the latter
    already ends in the separator, so appending another produced four
    underscores (mcp__episodic_memory____search). Current consumers filter
    by substring so it never surfaced, but any exact-name match would miss."""
    assert h.join_mcp_name(
        {"namespace": "mcp__episodic_memory__", "name": "search"}
    ) == "mcp__episodic_memory__search"
    assert h.join_mcp_name(
        {"namespace": "mcp__agi_memory__", "name": "remember"}
    ) == "mcp__agi_memory__remember"


def test_assistant_text_prefers_the_direct_field():
    assert h.assistant_text({"last_assistant_message": "hello"}) == "hello"
    assert h.assistant_text({}) is None


def test_detect_identifies_codex_without_guessing_claude():
    assert h.detect(_hook_events("codex")["UserPromptSubmit"]) == "codex"
    assert h.detect(_hook_events("claude")["UserPromptSubmit"]) == "unknown"
    for turn_id in (None, False, [], "", " \t"):
        assert h.detect({"turn_id": turn_id}) == "unknown"


@pytest.mark.parametrize("event_name", ["UserPromptSubmit", "PreToolUse", "PostToolUse"])
def test_hook_session_identity_uses_payload_for_codex_and_keeps_claude_override(event_name):
    codex = _hook_events("codex")[event_name]
    claude = _hook_events("claude")[event_name]
    stable = "00000000-0000-4000-8000-000000000004"
    env = {"CLAUDE_CODE_SESSION_ID": stable}
    assert h.hook_session_id(codex, env=env) == "00000000-0000-4000-8000-000000000002"
    assert h.hook_session_id(claude, env=env) == stable
    assert h.hook_session_id(claude, env={}) == "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("session_id", [None, True, 42, {}, [], "", " \t"])
def test_hook_session_identity_rejects_invalid_session_ids(session_id):
    env = {"CLAUDE_CODE_SESSION_ID": "inherited-claude-id"}
    assert h.hook_session_id({"turn_id": "t", "session_id": session_id}, env=env) is None
    assert h.hook_session_id({"session_id": session_id}, env={}) is None


def test_hook_session_identity_falls_back_on_invalid_override():
    payload = {"session_id": " synthetic session "}
    for override in (None, 42, " \n"):
        assert h.hook_session_id(payload, env={"CLAUDE_CODE_SESSION_ID": override}) == (
            " synthetic session "
        )


def test_hook_session_identity_rejects_non_payloads():
    env = {"CLAUDE_CODE_SESSION_ID": "inherited-claude-id"}
    for payload in (None, [], "text"):
        assert h.hook_session_id(payload, env=env) is None


def test_hook_session_path_key_preserves_canonical_uuid():
    native_id = "deadbeef-1111-2222-3333-444455556666"
    assert h.hook_session_path_key(native_id) == native_id
    assert h.hook_session_path_key(native_id.upper()) != native_id


def test_hook_session_path_key_keeps_arbitrary_session_ids_in_distinct_safe_paths():
    session_ids = [
        "../shared", ".._shared", "/absolute/path", "a\\b", "a/b", "recent",
        "case", "CASE", "résumé", "re\u0301sume\u0301", " a ", "a",
        "a" * 512, "a\0", "\ud800",
    ]
    keys = [h.hook_session_path_key(session_id) for session_id in session_ids]
    assert all(key and re.fullmatch(r"[a-z0-9-]{1,128}", key) for key in keys)
    assert len(set(keys)) == len(session_ids)
    assert h.hook_session_path_key("recent") == (
        "sha256-034a7e52c5c9534b709dc1dba403868399b0949f7c1933a67325c22077ffc221"
    )
    assert h.hook_session_path_key(session_ids[0]) == keys[0]
    assert h.hook_session_path_key(keys[0]) != keys[0]


def test_hook_session_path_key_rejects_invalid_session_ids():
    for junk in (None, True, 42, [], {}, "", " \t"):
        assert h.hook_session_path_key(junk) is None


def test_never_raises_on_garbage():
    for junk in [None, [], "string", {"tool_input": None}, {"tool_name": None}]:
        assert h.tool_kind(junk) == "other"
        assert h.shell_command(junk) is None
        assert h.written_path(junk) is None


def test_iter_transcript_tool_uses_returns_empty_for_non_path_types():
    """open() treats an int as a raw OS file descriptor, not a filename
    — isinstance-reject before it ever reaches open(), for every
    non-path shape, not just int."""
    for junk in [1, True, False, None, [], {}, 3.14, ["a.jsonl"]]:
        assert list(h.iter_transcript_tool_uses(junk)) == []


def test_iter_transcript_tool_uses_rejects_int_path_without_closing_stdout():
    """True == 1 == stdout's fd. open(True, ...) used to open that live
    fd (open() treats an int argument as a raw fd, not a filename), and
    the `with` block closed it on the way out — silently killing the
    process's stdout with no exception ever reaching the broad
    `except Exception` that wraps the call, since closing a fd doesn't
    raise. Run in a subprocess so the regression's actual symptom
    (stdout dies, a later print() vanishes, the interpreter exits
    nonzero at shutdown trying to flush the dead fd) is caught
    behaviorally — an isinstance assertion alone wouldn't catch a
    reintroduced call path that still reaches open() with an int.
    """
    harness_path = HOOKS / "_harness.py"
    code = (
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('_harness', {str(harness_path)!r})\n"
        "h = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(h)\n"
        "list(h.iter_transcript_tool_uses(True))\n"
        "print('SENTINEL_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr!r}"
    assert "SENTINEL_OK" in proc.stdout


# ---------------------------------------------------------------------------
# typed_text: which prompts a person typed, and what they typed once a
# front-end's wrapper is taken off. spontaneous-recall.py asks it of the
# prompt (a task notice gets a header that says nobody typed it, and no
# long-message recall); _session_clock.py asks it of each transcript entry.
# ---------------------------------------------------------------------------

TYPED_BODY = ("The harbour board stopped paying for the ledgers in 1911.\n\n"
              "Who voted against, and does the minute book say why?")


@pytest.mark.parametrize("text", [
    "<task-notification>\n<task-id>w317</task-id>\n<summary>Workflow finished</summary>\n</task-notification>",
    "<system-reminder>\nA background session has ended.\n</system-reminder>",
    "<environment_context>\n<cwd>/x</cwd>\n</environment_context>",
    "System: you are one of three voices on a stage",
    "This is an app-generated request for a suggested next user message. Reply with it alone.",
    "<pasted_content></pasted_content>",
    "", "   \n\t", None, 42, [], {"prompt": "Who voted against?"},
])
def test_typed_text_is_none_for_what_nobody_typed(text):
    assert h.typed_text(text) is None


def test_typed_text_returns_plain_prose_without_its_surrounding_space():
    assert h.typed_text("  Who voted against?\n") == "Who voted against?"
    assert h.typed_text(TYPED_BODY) == TYPED_BODY


@pytest.mark.parametrize("wrapped", [
    pytest.param('<pasted_content id="a1">\n' + TYPED_BODY + "\n</pasted_content>", id="desktop-paste"),
    pytest.param('<pasted_content id="a1">\n' + TYPED_BODY, id="desktop-paste-never-closed"),
    pytest.param("<timestamp>Tuesday, November 25, 2026, 2:00 PM</timestamp>\n<user_query>\n"
                 + TYPED_BODY + "\n</user_query>", id="cursor"),
    pytest.param("# Files mentioned by the user:\n\n## notes.md: /home/x/notes.md\n\n"
                 "## My request for Codex:\n" + TYPED_BODY, id="codex-files"),
    pytest.param("# Files pasted by the user:\n\n## a.png: /tmp/a.png\n\n## My request for Codex:\n"
                 + TYPED_BODY + "\n<image name=[Image #1]>\n</image>", id="codex-files-with-an-image"),
])
def test_typed_text_takes_what_was_typed_out_of_a_front_ends_wrapper(wrapped):
    assert h.typed_text(wrapped) == TYPED_BODY


def test_typed_text_reads_a_wrapped_paste_with_long_runs_of_spaces_in_linear_time():
    """A pattern with a lazy body and a `\\s*` tail tries every split of every
    run of whitespace: 200,000 characters in runs of 1,000 spaces took four
    seconds, and a megabyte twenty-one. This runs on every prompt."""
    import time
    body = ("word" + " " * 1000) * 200
    for wrapped in ("<pasted_content>" + body,
                    "<timestamp>Tuesday</timestamp>\n<user_query>" + body + "</user_query>"):
        started = time.monotonic()
        assert h.typed_text(wrapped) == body.strip()
        assert time.monotonic() - started < 1.0


# ---------------------------------------------------------------------------
# own_words: the Claude desktop app puts notes of its own in front of what a
# person typed (a session started from a card, the list of linked sessions),
# in the transcript entry and in the prompt a hook is handed alike.
# ---------------------------------------------------------------------------

APP_NOTES = ("<system-reminder>\nThe user started your proposal task_0a1b2c3d (\"Mend the harbour ledger index\") as "
             "session local_0000. It runs on its own; a note reaches you when it ends a turn.\n</system-reminder>\n"
             "<system-reminder>\nLinked sessions now (status, not an instruction): mend-the-ledger = local_0000 "
             "(working).\n</system-reminder>\n")


def test_own_words_takes_the_apps_notes_off_the_front_and_nothing_else():
    assert h.own_words(APP_NOTES + "Yes, try it") == "Yes, try it"
    assert h.own_words(APP_NOTES) == APP_NOTES                  # nothing after them: nobody's message
    assert h.own_words(TYPED_BODY) == TYPED_BODY
    inside = "See <system-reminder>this</system-reminder> tag?"
    assert h.own_words(inside) == inside
    unclosed = "<system-reminder>\nnever closed, so nothing after it is trusted"
    assert h.own_words(unclosed) == unclosed


def test_a_message_behind_the_apps_notes_is_typed_once_they_are_off():
    assert h.typed_text(APP_NOTES + "Yes, try it") is None      # the rule for a prompt as it arrives
    assert h.typed_text(h.own_words(APP_NOTES + "Yes, try it")) == "Yes, try it"
    assert h.typed_text(h.own_words(APP_NOTES)) is None


@pytest.mark.parametrize("junk", [None, 42, [], {"prompt": "x"}, b"<system-reminder>x</system-reminder>y"])
def test_own_words_hands_back_what_is_not_text(junk):
    assert h.own_words(junk) is junk


def test_own_words_reads_megabytes_behind_a_note_in_linear_time():
    import time
    for text in (APP_NOTES + "word " * 1_000_000, "word " * 1_000_000,
                 "<system-reminder>" + "x" * 2_000_000, APP_NOTES * 20_000 + "Yes, try it"):
        started = time.monotonic()
        h.own_words(text)
        assert time.monotonic() - started < 1.0
