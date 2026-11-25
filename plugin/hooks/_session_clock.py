"""This session's own clock: the messages a person typed, read from its transcript.

The [TIME] header used to read one database row per project (session_activity)
that every prompt overwrote: the person's own messages, background task
notices, and prompts from any other session working in the same project. A
workflow notice arriving between two typed messages shortened "since your last
message"; another session opened in the same project reset the gap and the
count together. The transcript is the one record that belongs to this session
alone, so the gap and the count are read from it. The row still answers "when
was this project last worked in" for a session's first message, and only a
typed prompt advances it.

Two transcript schemas are read: Claude Code's (user entries, and the
queued_command attachments it writes for messages sent while the assistant was
working) and Codex's rollout (response_item user messages). A harness that
hands the hook no transcript path gets the row, as before.

Everything here fails soft: None means "the transcript cannot say", never an
exception.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from _harness import own_words, typed_text

INTERRUPT_OPENING = "[Request interrupted by user"   # the harness's record of an Esc, not a message
SAME_PROMPT_SECONDS = 15     # an entry this close to now with the prompt's text is the prompt itself
MAX_BYTES = 256 * 1024 * 1024
# User-role messages a harness writes in the person's place and marks with no flag. (Flagged
# ones, isMeta and isCompactSummary among them, are dropped before this is asked.)
HARNESS_OPENINGS = (
    "# AGENTS.md instructions",                                      # Codex: the project's AGENTS.md
    "The following is the Codex agent history",                      # Codex: approval reviewer
    "This session is being continued from a previous conversation",  # compaction summary
    "Base directory for this skill:",                                # skill body
    "[Workflow harness",                                             # workflow subagent task
    "Another Claude session sent a message",                         # cross-session relay
    "The coordinator sent a message while you were working",
)
HARNESS_EXACT = {"Warmup"}
# Claude Code and Codex write compact JSON ("type":"assistant"); a transcript written with a
# space after each colon must read the same, so the screen below is a search, not a substring.
_ASSISTANT = re.compile(rb'"(?:type|role)"\s*:\s*"assistant"')
_FRACTION = re.compile(r"\.(\d+)")
_SHORT_OFFSET = re.compile(r"(\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?[+-]\d{2})\Z")    # after a time, not a date


def typed(text) -> str | None:
    """What a person typed in a transcript entry or prompt, or None if nobody typed it."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = own_words(text)                  # the app's notes in front of what was typed
    if text.lstrip().startswith(INTERRUPT_OPENING):
        return None
    body = typed_text(text)
    if body is None or body in HARNESS_EXACT or body.startswith(HARNESS_OPENINGS):
        return None
    return body


def _when(raw) -> datetime | None:
    """A transcript timestamp as an aware datetime (UTC when it names no offset).

    Before Python 3.11, fromisoformat takes a fraction of exactly three or six
    digits and an offset with minutes, so both are put in that form first: a
    harness that stamps nanoseconds would otherwise lose every typed message.
    """
    try:
        text = str(raw).strip().replace("Z", "+00:00")
        text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
        text = _SHORT_OFFSET.sub(r"\1:00", text)
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _content_text(content) -> str | None:
    """Text of a message's content, or None when it carries a tool result."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _typed_entry(entry):
    """(text, when, queued, prompt_id) if this transcript entry is a message a person typed, else None.

    Three shapes: a Claude Code user entry; a message sent while the assistant was
    working, which Claude Code records as a queued_command attachment stamped with
    when it was sent; a user message in a Codex rollout.
    """
    kind = entry.get("type")
    queued, prompt_id, stamp = False, "", entry.get("timestamp")
    if kind == "user":
        if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isSidechain"):
            return None
        origin = entry.get("origin")
        if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
            return None                      # the harness says who sent it: a task notice, another session
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        prompt_id = entry.get("promptId") if isinstance(entry.get("promptId"), str) else ""
    elif kind == "attachment":
        attached = entry.get("attachment")
        if not isinstance(attached, dict) or attached.get("type") != "queued_command":
            return None
        origin = attached.get("origin")
        if attached.get("commandMode") != "prompt" or not isinstance(origin, dict) or origin.get("kind") != "human":
            return None
        content, queued, stamp = attached.get("prompt"), True, attached.get("timestamp") or stamp
    elif kind == "response_item":
        payload = entry.get("payload")
        if not (isinstance(payload, dict) and payload.get("type") == "message" and payload.get("role") == "user"):
            return None
        content = payload.get("content")
    else:
        return None
    text, when = typed(_content_text(content)), _when(stamp)
    if text is None or when is None:
        return None
    return text, when, queued, prompt_id


def _entries(transcript_path) -> list[dict] | None:
    """Every typed message in the transcript, oldest first; None when the file cannot be read.

    Ordered by when each was sent, not by position: a compaction can leave earlier
    entries after later ones, and a queued message is written when the queue drains.
    "answered" says an assistant entry follows it in the file.
    """
    try:
        if not transcript_path:
            return None
        path = Path(transcript_path)
        if not path.is_file():
            return None
        with path.open("rb") as fh:
            size = path.stat().st_size
            if size > MAX_BYTES:
                fh.seek(size - MAX_BYTES)
            data = fh.read()
    except Exception:
        return None
    found: dict[tuple[datetime, str], dict] = {}
    last_assistant = -1
    for index, raw in enumerate(data.split(b"\n")):
        # Cheap screens before parsing: tool results are most of a transcript's bytes.
        if b'"toolUseResult"' in raw:
            continue
        if b'"assistant"' in raw and _ASSISTANT.search(raw):
            last_assistant = index
            continue
        if b'"user"' not in raw and b'"queued_command"' not in raw:
            continue
        try:
            entry = json.loads(raw)
            got = _typed_entry(entry) if isinstance(entry, dict) else None
        except Exception:
            continue
        if got is None:
            continue
        text, when, queued, prompt_id = got
        found[(when, text)] = {"when": when, "text": text, "queued": queued,
                               "prompt_id": prompt_id, "index": index}
    entries = [found[key] for key in sorted(found)]
    for entry in entries:
        entry["answered"] = last_assistant > entry["index"]
    return entries


def typed_messages(transcript_path) -> list[tuple[datetime, str]] | None:
    """(when, text) of each message a person typed in this transcript, oldest first."""
    entries = _entries(transcript_path)
    return None if entries is None else [(e["when"], e["text"]) for e in entries]


def _is_this_prompt(entry, current, now, prompt_id) -> bool:
    """Whether a transcript entry is the very prompt now being submitted.

    The harness may or may not have written it before the hook runs. A queued
    message is the prompt if nothing has answered it yet. A turn-starting prompt is
    known by the prompt_id the hook is handed; without one, by having the same
    words within a few seconds. (A prompt_id alone proves nothing: a message sent
    mid-turn arrives with the running turn's id, which is its predecessor's.)
    """
    if entry["text"] != current:
        return False
    if entry["queued"]:
        return not entry["answered"]
    if prompt_id and entry["prompt_id"]:
        # ...and only while nothing has answered it: the same words sent again mid-turn
        # arrive with this id too, and the entry they match was answered long ago.
        return entry["prompt_id"] == prompt_id and not entry["answered"]
    return abs((now - entry["when"]).total_seconds()) <= SAME_PROMPT_SECONDS


def before_this_prompt(transcript_path, prompt, now, prompt_id="") -> tuple | None:
    """(typed messages before this prompt, when the latest was sent, when this one was sent).

    The third is None unless the prompt's own entry is already in the transcript.
    (0, None, ...) when this is the session's first typed message; None when the
    transcript cannot be read.
    """
    try:
        entries = _entries(transcript_path)
        if entries is None:
            return None
        if now.tzinfo is None:
            now = now.astimezone()
        current = typed(prompt) or (prompt or "").strip()
        own = next((k for k in range(len(entries) - 1, -1, -1)
                    if current and _is_this_prompt(entries[k], current, now, prompt_id or "")), None)
        sent_at = entries[own]["when"] if own is not None else None
        before = entries[:own] if own is not None else entries
        if not before:
            return 0, None, sent_at
        return len(before), before[-1]["when"], sent_at
    except Exception:
        return None
