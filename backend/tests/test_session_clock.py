"""The [TIME] header counts the messages a person typed in this session, and nothing else.

Before this the header read one database row per project (session_activity) that
every prompt overwrote: a background task notice, and any other session working in
the same project, reset "since your last message" and the message count. Where the
fault was found, 22 of 96 gaps shown on typed messages were off by more than a
minute and 106 of 122 counts were wrong. The gap and the count are now read from
the session's own transcript (plugin/hooks/_session_clock.py), and only a typed
prompt advances the row.

Every message below is invented. No database and no subprocess: see the
prompt_hook fixture in conftest.py.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOTICE = ("<task-notification>\n<task-id>w317</task-id>\n<summary>The survey workflow finished "
          "and twelve readers reported on the two chapters</summary>")
TYPED_NOW = "What did the readers make of the lighthouse keeper in the first chapter?"


def clock():
    import _session_clock
    return _session_clock


def utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def user(ts, text, **extra):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}, **extra}


def write(path, *entries, separators=(",", ":")):
    """A transcript as the harnesses write one: a JSON object per line, no spaces."""
    path.write_text("".join(json.dumps(e, separators=separators) + "\n" for e in entries))
    return str(path)


def queued(ts, text, kind="human", mode="prompt"):
    """A message that arrived while the assistant was working, as Claude Code records it:
    an attachment written when the queue drains, stamped with when it was sent."""
    return {"type": "attachment", "timestamp": ts,
            "attachment": {"type": "queued_command", "commandMode": mode, "origin": {"kind": kind},
                           "timestamp": ts, "humanTurn": kind == "human", "prompt": text}}


def assistant(ts, text="Working on it."):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def evening(tmp_path):
    """A typed message, a reply, a second typed message, a workflow notice, a tool result."""
    return write(
        tmp_path / "session.jsonl",
        user("2026-11-24T10:12:54.415Z", "I have sent the vicar's letter."),
        assistant("2026-11-24T10:13:30.000Z", "Noted."),
        user("2026-11-24T10:47:49.784Z", "The readers ranked the mill chapter last and you call it interesting."),
        user("2026-11-24T10:58:34.855Z", NOTICE),
        user("2026-11-24T11:00:00.000Z", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
             toolUseResult={"stdout": "ok"}),
    )


# --- which transcript entries are messages a person typed --------------------------

def test_typed_messages_are_the_ones_a_person_typed(tmp_path):
    path = write(
        tmp_path / "s.jsonl",
        user("2026-11-24T10:12:54.415Z", "I have sent the vicar's letter."),
        user("2026-11-24T10:20:00.000Z", NOTICE),
        user("2026-11-24T10:21:00.000Z", "Base directory for this skill: /x/skills/gardener", isMeta=True),
        user("2026-11-24T10:22:00.000Z", "This session is being continued from a previous conversation "
             "that ran out of context.", isCompactSummary=True),
        user("2026-11-24T10:23:00.000Z", "An agent's own prompt, inside a subagent.", isSidechain=True),
        user("2026-11-24T10:24:00.000Z", [{"type": "text", "text": "[Request interrupted by user]"}]),
        user("2026-11-24T10:25:00.000Z", [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]),
        user("2026-11-24T10:47:49.784Z", [{"type": "text", "text": "The readers ranked it last."}]),
    )
    assert clock().typed_messages(path) == [
        (utc("2026-11-24T10:12:54.415Z"), "I have sent the vicar's letter."),
        (utc("2026-11-24T10:47:49.784Z"), "The readers ranked it last."),
    ]


def test_typed_messages_are_ordered_by_time_not_by_position(tmp_path):
    """A compaction can leave earlier entries after later ones in the file."""
    path = write(tmp_path / "s.jsonl",
                 user("2026-11-24T03:09:50.992Z", "Second message."),
                 user("2026-11-23T07:09:04.106Z", "First message."))
    assert [text for _, text in clock().typed_messages(path)] == ["First message.", "Second message."]


def codex_item(ts, text):
    return {"timestamp": ts, "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}}


def test_a_codex_rollout_counts_its_typed_messages_too(tmp_path):
    path = write(
        tmp_path / "rollout.jsonl",
        {"timestamp": "2026-11-25T04:20:51.496Z", "type": "session_meta",
         "payload": {"originator": "Codex Desktop", "cwd": "/x"}},
        codex_item("2026-11-25T04:20:52.000Z", "# AGENTS.md instructions for /x\n\nBe careful."),
        codex_item("2026-11-25T04:20:53.000Z", "<environment_context>\n<cwd>/x</cwd>\n</environment_context>"),
        codex_item("2026-11-25T04:20:54.169Z", "Who kept the tide tables after the keeper died?"),
    )
    assert clock().typed_messages(path) == [(utc("2026-11-25T04:20:54.169Z"),
                                             "Who kept the tide tables after the keeper died?")]


@pytest.mark.parametrize("opening", [
    "# AGENTS.md instructions for /x\n\nBe careful.",
    "The following is the Codex agent history of an approval request.",
    "This session is being continued from a previous conversation that ran out of context.",
    "Base directory for this skill: /x/skills/gardener\n\n# Gardener",
    "[Workflow harness] You are one of six reviewers.",
    "Another Claude session sent a message while you were working: the tests pass.",
    "The coordinator sent a message while you were working: carry on.",
    "Warmup",
])
def test_what_a_harness_writes_in_the_users_place_is_not_typed(tmp_path, opening):
    """Each of these reaches a transcript as a user message with no flag to say who wrote it."""
    path = write(tmp_path / "s.jsonl",
                 user("2026-11-24T10:00:00.000Z", "A person typed this."),
                 user("2026-11-24T10:01:00.000Z", opening),
                 codex_item("2026-11-24T10:02:00.000Z", opening))
    assert [text for _, text in clock().typed_messages(path)] == ["A person typed this."]
    assert clock().typed(opening) is None


@pytest.mark.parametrize("make", [
    lambda tmp: "",
    lambda tmp: str(tmp / "no-such-file.jsonl"),
    lambda tmp: str(tmp),
    lambda tmp: None,
    lambda tmp: 7,
])
def test_an_unreadable_transcript_gives_none_not_an_error(tmp_path, make):
    assert clock().typed_messages(make(tmp_path)) is None
    assert clock().before_this_prompt(make(tmp_path), "anything", utc("2026-11-24T15:20:58Z")) is None


def test_rubbish_lines_are_skipped(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('not json\n{"type":"user"}\n{"type":"user","timestamp":"never","message":{"content":"x y z"}}\n'
                    + json.dumps(user("2026-11-24T10:12:54.415Z", "A real message."), separators=(",", ":")) + "\n")
    assert [text for _, text in clock().typed_messages(str(path))] == ["A real message."]


def test_a_message_sent_while_the_assistant_was_working_counts(tmp_path):
    """Claude Code records such a message as a queued_command attachment, not a user entry."""
    path = write(
        tmp_path / "s.jsonl",
        user("2026-11-24T04:37:10.100Z", "There is a ledger for it in the vestry."),
        queued("2026-11-24T04:38:11.267Z", "And while you are there, look at the other one."),
        queued("2026-11-24T04:39:00.000Z", "<task-notification>done</task-notification>",
               kind="task-notification", mode="task-notification"),
        queued("2026-11-24T04:39:30.000Z", "A message another session sent.", kind="peer"),
    )
    assert clock().typed_messages(path) == [
        (utc("2026-11-24T04:37:10.100Z"), "There is a ledger for it in the vestry."),
        (utc("2026-11-24T04:38:11.267Z"), "And while you are there, look at the other one."),
    ]


def test_an_entry_the_harness_marks_as_not_from_a_person_is_not_typed(tmp_path):
    path = write(
        tmp_path / "s.jsonl",
        user("2026-11-24T10:00:00.000Z", "Plain prose, but the harness sent it.", origin={"kind": "task-notification"}),
        user("2026-11-24T10:01:00.000Z", "A person typed this.", origin={"kind": "human"}),
    )
    assert [text for _, text in clock().typed_messages(path)] == ["A person typed this."]


REMINDER = "<system-reminder>\nLinked sessions now: the survey room is working.\n</system-reminder>"


def test_a_typed_message_the_harness_prefixed_with_a_reminder_still_counts(tmp_path):
    """The harness sometimes puts a note of its own in front of what was typed. The prompt
    the hook is handed does not carry it; the transcript entry does."""
    path = write(
        tmp_path / "s.jsonl",
        user("2026-11-13T06:18:29.000Z", "First message."),
        user("2026-11-13T06:56:25.000Z", [{"type": "text", "text": REMINDER},
                                          {"type": "text", "text": "Second message, typed."}],
             origin={"kind": "human"}),
        user("2026-11-13T06:58:00.000Z", REMINDER + "\n\nThird, with the reminder in the same string."),
        user("2026-11-13T07:00:00.000Z", [{"type": "text", "text": REMINDER}]),
        user("2026-11-13T07:01:00.000Z", "<system-reminder>\nnever closed, so nothing after it is trusted"),
    )
    assert clock().typed_messages(path) == [
        (utc("2026-11-13T06:18:29.000Z"), "First message."),
        (utc("2026-11-13T06:56:25.000Z"), "Second message, typed."),
        (utc("2026-11-13T06:58:00.000Z"), "Third, with the reminder in the same string."),
    ]


@pytest.mark.parametrize("stamp", [
    "2026-11-24T10:12:54Z", "2026-11-24T10:12:54.4Z", "2026-11-24T10:12:54.41Z", "2026-11-24T10:12:54.415Z",
    "2026-11-24T10:12:54.4150Z", "2026-11-24T10:12:54.415000Z", "2026-11-24T10:12:54.415000000Z",
    "2026-11-24T10:12:54.415+00:00", "2026-11-24T18:12:54.415+08:00", "2026-11-24T10:12:54.415+00",
])
def test_a_timestamp_is_read_whatever_its_precision(tmp_path, stamp):
    """Before Python 3.11, fromisoformat takes a fraction of exactly three or six digits and
    an offset with minutes. A harness that stamps nanoseconds would lose every typed message,
    and the header would quietly fall back to the row."""
    path = write(tmp_path / "s.jsonl", user(stamp, "A person typed this."))
    [(when, _)] = clock().typed_messages(path)
    assert abs((when - utc("2026-11-24T10:12:54.415Z")).total_seconds()) < 1


def test_only_a_time_is_given_an_offsets_minutes():
    """An offset written without minutes is completed. A date with no time ends the same way
    ("-24") and is not an offset."""
    when = clock()._when
    assert when("2026-11-24T10:12:54-08") == utc("2026-11-24T18:12:54Z")
    assert when("2026-11-24") == utc("2026-11-24T00:00:00Z")
    assert when("never") is None and when(None) is None and when("") is None


def test_this_repositorys_own_transcript_fixtures_are_read_on_both_harnesses():
    asked = "Recall anything relevant to the adapter contract, please."
    for name in ("claude-transcript.jsonl", "codex-rollout.jsonl"):
        assert clock().typed_messages(str(FIXTURES / name)) == [(utc("2026-08-05T10:00:00Z"), asked)], name


# --- the gap and the count for the prompt being submitted ---------------------------

def test_the_gap_runs_from_the_last_typed_message_past_a_notice(tmp_path):
    got = clock().before_this_prompt(evening(tmp_path), TYPED_NOW, utc("2026-11-24T15:20:58.176Z"))
    assert got == (2, utc("2026-11-24T10:47:49.784Z"), None)


def test_the_prompts_own_entry_is_not_its_own_predecessor(tmp_path):
    """If the harness has already written the prompt to the transcript, skip that entry."""
    path = evening(tmp_path)
    with open(path, "a") as fh:
        fh.write(json.dumps(user("2026-11-24T15:20:58.176Z", TYPED_NOW), separators=(",", ":")) + "\n")
    got = clock().before_this_prompt(path, TYPED_NOW, utc("2026-11-24T15:20:59.000Z"))
    assert got == (2, utc("2026-11-24T10:47:49.784Z"), utc("2026-11-24T15:20:58.176Z"))


def test_the_same_words_sent_minutes_ago_are_a_predecessor(tmp_path):
    path = write(tmp_path / "s.jsonl", user("2026-11-24T15:00:00.000Z", "Keep going."))
    got = clock().before_this_prompt(path, "Keep going.", utc("2026-11-24T15:20:58.000Z"))
    assert got == (1, utc("2026-11-24T15:00:00.000Z"), None)


def test_the_prompts_own_entry_is_known_by_its_prompt_id_however_late_the_hook_runs(tmp_path):
    """Claude Code hands the hook a prompt_id; the transcript entry of a turn-starting prompt carries it."""
    path = evening(tmp_path)
    with open(path, "a") as fh:
        fh.write(json.dumps(user("2026-11-24T15:20:58.176Z", TYPED_NOW, promptId="p-9"),
                            separators=(",", ":")) + "\n")
    got = clock().before_this_prompt(path, TYPED_NOW, utc("2026-11-24T15:21:40.000Z"), prompt_id="p-9")
    assert got == (2, utc("2026-11-24T10:47:49.784Z"), utc("2026-11-24T15:20:58.176Z"))


def test_the_same_words_under_another_prompt_id_are_a_predecessor(tmp_path):
    path = write(tmp_path / "s.jsonl", user("2026-11-24T15:20:50.000Z", "Keep going.", promptId="p-1"))
    got = clock().before_this_prompt(path, "Keep going.", utc("2026-11-24T15:20:58.000Z"), prompt_id="p-2")
    assert got == (1, utc("2026-11-24T15:20:50.000Z"), None)


def test_a_prompt_id_alone_does_not_make_an_entry_the_prompt(tmp_path):
    """A message sent mid-turn arrives with the running turn's prompt_id, which is its predecessor's."""
    path = write(tmp_path / "s.jsonl", user("2026-11-24T15:00:00.000Z", "Start on the ledgers.", promptId="p-1"))
    got = clock().before_this_prompt(path, "And the orchard too.", utc("2026-11-24T15:10:00.000Z"), prompt_id="p-1")
    assert got == (1, utc("2026-11-24T15:00:00.000Z"), None)


def test_a_prompt_id_match_counts_only_while_nothing_has_answered_the_entry(tmp_path):
    """The same words sent again mid-turn arrive with the running turn's id, and the entry
    they match was answered long ago."""
    path = write(
        tmp_path / "s.jsonl",
        user("2026-11-24T14:00:00.000Z", "Look at the ledgers first.", promptId="p-0"),
        assistant("2026-11-24T14:01:00.000Z"),
        user("2026-11-24T15:00:00.000Z", "Keep going.", promptId="p-1"),
        assistant("2026-11-24T15:00:30.000Z"),
    )
    got = clock().before_this_prompt(path, "Keep going.", utc("2026-11-24T15:05:00.000Z"), prompt_id="p-1")
    assert got == (2, utc("2026-11-24T15:00:00.000Z"), None)


def test_a_first_message_has_no_predecessor(tmp_path):
    path = write(tmp_path / "s.jsonl", user("2026-11-24T10:58:34.855Z", NOTICE))
    assert clock().before_this_prompt(path, TYPED_NOW, utc("2026-11-24T15:20:58Z")) == (0, None, None)


QUEUED_TEXT = "And while you are there, look at the other one."


def queued_session(tmp_path, *after, separators=(",", ":")):
    """Typed at 04:38:11 while the assistant worked; the hook runs at 04:40:09 when the queue drains."""
    return write(
        tmp_path / "queued.jsonl",
        user("2026-11-24T04:37:10.100Z", "There is a ledger for it in the vestry."),
        assistant("2026-11-24T04:37:40.000Z"),
        queued("2026-11-24T04:38:11.267Z", QUEUED_TEXT),
        *after,
        separators=separators,
    )


def test_a_queued_prompt_is_measured_from_when_it_was_sent(tmp_path):
    got = clock().before_this_prompt(queued_session(tmp_path), QUEUED_TEXT, utc("2026-11-24T04:40:09.401Z"))
    assert got == (1, utc("2026-11-24T04:37:10.100Z"), utc("2026-11-24T04:38:11.267Z"))


def test_a_queued_message_already_answered_is_a_predecessor_of_the_same_words(tmp_path):
    path = queued_session(tmp_path, assistant("2026-11-24T04:41:00.000Z", "Looked at the other one."))
    got = clock().before_this_prompt(path, QUEUED_TEXT, utc("2026-11-24T05:00:00.000Z"))
    assert got == (2, utc("2026-11-24T04:38:11.267Z"), None)


def test_a_transcript_written_with_spaces_after_its_separators_reads_the_same(tmp_path):
    """Claude Code and Codex write compact JSON. This repository's own fixtures, and anything
    that writes a transcript with Python's defaults, put a space after each colon; whether an
    entry has been answered must not depend on that."""
    path = queued_session(tmp_path, assistant("2026-11-24T04:41:00.000Z", "Looked at the other one."),
                          separators=(", ", ": "))
    got = clock().before_this_prompt(path, QUEUED_TEXT, utc("2026-11-24T05:00:00.000Z"))
    assert got == (2, utc("2026-11-24T04:38:11.267Z"), None)


# --- the header ----------------------------------------------------------------------

def stub_row(module, row):
    """Stand in for the database: record each advance, answer with `row`."""
    advances = []
    module.track_session_time = lambda sid, project: advances.append((sid, project)) or row
    return advances


NOW = utc("2026-11-24T15:20:58.176Z")
STOMPED = {"prev_session": "another-session", "gap_seconds": 4, "count": 1}


def test_a_typed_message_reads_its_gap_from_its_own_transcript(prompt_hook, tmp_path):
    """Another session has just overwritten the project's row; this session's header does not care."""
    hook = prompt_hook.head()
    advances = stub_row(hook, STOMPED)
    header = hook.build_time_header("this-session", "/x/garden", typed=True, prompt=TYPED_NOW,
                                    transcript_path=evening(tmp_path), now=NOW)
    assert "msg #3 this session · 4h 33m since your last message." in header
    assert "new session" not in header
    assert advances == [("this-session", "/x/garden")]


def test_a_notice_gets_a_header_and_does_not_advance_the_clock(prompt_hook, tmp_path):
    hook = prompt_hook.head()
    advances = stub_row(hook, STOMPED)
    header = hook.build_time_header("this-session", "/x/garden", typed=False, prompt=NOTICE,
                                    transcript_path=evening(tmp_path), now=utc("2026-11-24T10:58:35Z"))
    assert advances == []
    assert header.startswith("[TIME] ")
    assert "not a typed message" in header
    assert "10m 45s since the last typed message (#2 this session)" in header
    assert "since your last message" not in header


def test_a_queued_message_says_when_it_was_sent(prompt_hook, tmp_path):
    hook = prompt_hook.head()
    stub_row(hook, STOMPED)
    header = hook.build_time_header("this-session", "/x/garden", typed=True, prompt=QUEUED_TEXT,
                                    transcript_path=queued_session(tmp_path),
                                    now=utc("2026-11-24T04:40:09.401Z"))
    assert "msg #2 this session · sent 1m 58s ago, 1m 1s after your last message." in header


def test_a_notice_with_no_transcript_still_gets_the_wall_clock(prompt_hook):
    hook = prompt_hook.head()
    advances = stub_row(hook, STOMPED)
    header = hook.build_time_header("this-session", "/x/garden", typed=False, prompt=NOTICE, now=NOW)
    assert advances == [] and header.startswith("[TIME] ") and "not a typed message" in header


def test_the_first_message_of_a_session_reports_the_gap_across_sessions(prompt_hook, tmp_path):
    hook = prompt_hook.head()
    stub_row(hook, {"prev_session": "an-older-session", "gap_seconds": 3 * 86400, "count": 1})
    path = write(tmp_path / "new.jsonl")
    header = hook.build_time_header("this-session", "/x/garden", typed=True, prompt=TYPED_NOW,
                                    transcript_path=path, now=NOW)
    assert "new session (msg #1) · last worked in garden 3d 0h ago" in header


def test_without_a_readable_transcript_the_row_answers_as_before(prompt_hook):
    hook = prompt_hook.head()
    stub_row(hook, {"prev_session": "this-session", "gap_seconds": 125, "count": 7})
    header = hook.build_time_header("this-session", "/x/garden", typed=True, prompt=TYPED_NOW, now=NOW)
    assert "msg #7 this session · 2m 5s since your last message." in header


def test_without_the_clock_module_the_row_answers(prompt_hook, tmp_path):
    hook = prompt_hook.head()
    hook.session_clock = None
    stub_row(hook, {"prev_session": "this-session", "gap_seconds": 125, "count": 7})
    header = hook.build_time_header("this-session", "/x/garden", typed=True, prompt=TYPED_NOW,
                                    transcript_path=evening(tmp_path), now=NOW)
    assert "msg #7 this session · 2m 5s since your last message." in header


def test_the_old_two_argument_call_still_works(prompt_hook):
    hook = prompt_hook.head()
    stub_row(hook, {"prev_session": "this-session", "gap_seconds": 125, "count": 7})
    assert "msg #7 this session" in hook.build_time_header("this-session", "/x/garden")


def test_whether_a_prompt_was_typed_does_not_depend_on_the_optional_modules(prompt_hook):
    """The clock and the long-message path both ask. Neither answer may turn false for
    every prompt because one of the two optional modules failed to load."""
    hook = prompt_hook.head()
    hook.session_clock = None
    hook.recall_passages = None
    assert hook.typed_by_a_person(TYPED_NOW) is True
    assert hook.typed_by_a_person(NOTICE) is False
    assert hook.typed_by_a_person("") is False
    behind_a_note = REMINDER + "\n" + TYPED_NOW
    assert hook.own_words(behind_a_note) == TYPED_NOW
    assert hook.typed_by_a_person(hook.own_words(behind_a_note)) is True


# --- the whole hook -------------------------------------------------------------------

def run_hook(prompt_hook, tmp_path, prompt, transcript_path="", extra=None):
    hook = prompt_hook.head()
    advances = stub_row(hook, STOMPED)
    hook.query_memories_vector = lambda prompt, keywords=None: []
    hook.search_episodic_memory = lambda query: []
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "cwd": str(tmp_path),
               "session_id": "clock-test-session", "transcript_path": transcript_path, **(extra or {})}
    return prompt_hook.run(hook, payload)["hookSpecificOutput"]["additionalContext"], advances


def test_hook_does_not_advance_the_clock_for_a_task_notice(prompt_hook, tmp_path):
    context, advances = run_hook(prompt_hook, tmp_path, NOTICE, evening(tmp_path))
    assert advances == []
    assert context.startswith("[TIME] ") and "not a typed message" in context


def test_hook_advances_the_clock_for_a_typed_message(prompt_hook, tmp_path):
    context, advances = run_hook(prompt_hook, tmp_path, TYPED_NOW, evening(tmp_path))
    assert len(advances) == 1
    assert "msg #3 this session" in context and "since your last message." in context


def test_hook_hands_the_payloads_prompt_id_to_the_clock(prompt_hook, tmp_path):
    """The prompt's own entry, written 40 seconds before the hook ran, is not its predecessor."""
    now = datetime.now(timezone.utc)
    stamp = lambda delta: (now - delta).isoformat().replace("+00:00", "Z")
    path = write(tmp_path / "late.jsonl",
                 user(stamp(timedelta(hours=1)), "An earlier message."),
                 user(stamp(timedelta(seconds=40)), TYPED_NOW, promptId="p-9"))
    context, _ = run_hook(prompt_hook, tmp_path, TYPED_NOW, path, extra={"prompt_id": "p-9"})
    assert "msg #2 this session · 59m" in context


@pytest.mark.parametrize("field", ["transcript_path", "prompt_id"])
@pytest.mark.parametrize("junk", [None, 7, ["x"], {"a": 1}])
def test_hook_takes_a_payload_whose_clock_fields_are_not_strings(prompt_hook, tmp_path, field, junk):
    context, advances = run_hook(prompt_hook, tmp_path, TYPED_NOW, evening(tmp_path), extra={field: junk})
    assert context.startswith("[TIME] ") and len(advances) == 1


# --- a typed message behind the app's own notes --------------------------------------------
#
# Found live where this was first built: a click on a reply button reached the hook behind
# two of the desktop app's notes, and the header called it "not a typed message". The
# transcript side already set such notes aside; the check of the prompt in hand did not.

APP_NOTES = ("<system-reminder>\nThe user started your proposal task_0a1b2c3d (\"Mend the harbour ledger index\") as "
             "session local_0000. It runs on its own; a note reaches you when it ends a turn.\n</system-reminder>\n"
             "<system-reminder>\nLinked sessions now (status, not an instruction): mend-the-ledger = local_0000 "
             "(working).\n</system-reminder>\n")


def test_hook_counts_a_message_the_app_put_its_own_notes_in_front_of(prompt_hook, tmp_path):
    context, advances = run_hook(prompt_hook, tmp_path, APP_NOTES + "Yes, try it", evening(tmp_path))
    assert len(advances) == 1
    assert "msg #3 this session" in context and "not a typed message" not in context


def test_hook_does_not_count_the_apps_notes_alone(prompt_hook, tmp_path):
    context, advances = run_hook(prompt_hook, tmp_path, APP_NOTES, evening(tmp_path))
    assert advances == []
    assert "not a typed message" in context


def test_the_prompts_own_entry_is_found_behind_the_apps_notes(tmp_path):
    """The transcript entry carries the notes too. It is still this prompt, not its predecessor."""
    path = evening(tmp_path)
    with open(path, "a") as fh:
        fh.write(json.dumps(user("2026-11-24T15:20:58.176Z", APP_NOTES + "Yes, try it"), separators=(",", ":")) + "\n")
    got = clock().before_this_prompt(path, APP_NOTES + "Yes, try it", utc("2026-11-24T15:20:59.000Z"))
    assert got == (2, utc("2026-11-24T10:47:49.784Z"), utc("2026-11-24T15:20:58.176Z"))
