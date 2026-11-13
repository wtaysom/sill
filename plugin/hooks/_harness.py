"""Harness normalization: one vocabulary for Claude Code and Codex.

Every hook that used to string-match "Bash", "Write", "Edit", "Read" — the
Claude-only tool names — silently never fired on Codex, whose PreToolUse
payloads carry "exec"/"exec_command"/"shell"/"shell_command" (shell),
"apply_patch" (write), and "view_image" (read) instead. This module is the
one place that knows both vocabularies, so every other hook asks it instead
of rediscovering the mapping (or a subset of it) on its own.

The Codex half of that vocabulary was originally derived by hand and got
three things wrong, corrected in v0.2.1 against a census of 309 real Codex
rollout transcripts: "shell"/"shell_command" were missing from the shell
names entirely, exec_command keeps its command under "cmd" rather than
"command", and `shell` passes an argv list rather than a string. Each
constant below carries its own specimen counts; re-derive them by walking
~/.codex/sessions/**/*.jsonl rather than by reasoning about what the tool
"should" send.

Two input shapes, not one:

- Hook payload dicts (`{"tool_name": ..., "tool_input": {...}}`, plus
  Stop-event fields like `last_assistant_message`) — what a
  PreToolUse/PostToolUse/Stop hook receives on stdin. Already flattened
  by the harness itself: an MCP tool name arrives as one flat string
  (`mcp__server__tool`) on both Claude Code and Codex.
- Raw session transcript JSONL records, read from disk by
  `iter_transcript_tool_uses` — the one function here that does I/O.
  Codex transcripts split an MCP call's name into `namespace` + `name`
  instead of leaving it flat; `join_mcp_name` reconstructs the flat form
  WITH the separator that ad hoc call sites were missing (joining
  `mcp__sill` + `recall_batch` with no separator produces
  `mcp__sillrecall_batch`, not `mcp__sill__recall_batch` — it only
  looked like it worked because a downstream filter used a substring
  test).

written_path/written_text answer for ONE file only — the first header in
an apply_patch body. A caller that gates an in-scope check on
written_path() before ever reading written_text() therefore judges a
whole multi-file patch by its first file alone; written_files() is the
per-file-safe alternative — see its docstring.

Every function below that takes a payload/record is total: garbage in
(None, a list, a bare string, a dict with None values, an empty dict)
returns the documented "nothing here" value rather than raising. Hooks
run on every tool call in every session; an exception escaping this
module is a broken harness, not a broken test.
"""
import json
import os
import re
from collections.abc import Mapping
from hashlib import sha256
from typing import Iterator, Literal

Harness = Literal["claude", "codex", "unknown"]
ToolKind = Literal["shell", "write", "edit", "read", "mcp", "other"]

# Tool names as they appear in a normalized hook payload's "tool_name"
# field: Claude Code's own names plus Codex's equivalents (see module
# docstring). "mcp__"-prefixed names are matched by prefix, not
# membership, in tool_kind below.
_SHELL_NAMES = frozenset({"Bash", "exec", "exec_command", "shell", "shell_command"})

# Where a shell tool keeps its command, in the order they're tried.
# Ground-truthed against 309 real Codex rollouts (2026-08-06): exec_command
# uses "cmd" (1246/1246, always a string) while shell/shell_command use
# "command" (a 314/314 argv LIST for `shell`, a 355/355 string for
# `shell_command`) — see _command_value for the list case. Claude's Bash
# uses "command". The earlier single-key "command" read returned None for
# every real exec_command call.
_COMMAND_KEYS = ("command", "cmd")

_WRITE_NAMES = frozenset({"Write", "apply_patch"})
_EDIT_NAMES = frozenset({"Edit", "MultiEdit"})
_READ_NAMES = frozenset({"Read", "view_image"})

_MCP_PREFIX = "mcp__"

# Codex's apply_patch body carries the path in its own header line
# (there is no separate file_path field). written_path/written_text read
# the first "*** Update File: <path>" / "*** Add File: <path>" line; a
# patch with neither (a bare hunk, an empty string, a Delete-only patch)
# is unparseable and yields None rather than a guess. Matched by literal
# prefix line-by-line — same convention as state-language-check.py's
# extract_relevant_patch_additions — not a multiline regex: a regex
# using \s* around the capture would match across the newline (\s
# includes \n) and, on a header line with a blank/whitespace-only tail,
# silently bleed the "path" capture into the next line's content.
_PATCH_HEADERS = ("*** Update File: ", "*** Add File: ")


def _as_dict(value: object) -> dict | None:
    return value if isinstance(value, dict) else None


def _as_str(value: object) -> str | None:
    """A non-empty string, or None — for anything else, including "" """
    return value if isinstance(value, str) and value else None


def _nonblank_string(value: object) -> str | None:
    """Preserve the original string if it contains any non-whitespace."""
    return value if isinstance(value, str) and value.strip() else None


def _tool_name(payload: object) -> str | None:
    d = _as_dict(payload)
    return _as_str(d.get("tool_name")) if d is not None else None


def _tool_input(payload: object) -> dict | None:
    d = _as_dict(payload)
    return _as_dict(d.get("tool_input")) if d is not None else None


def detect(payload: object) -> Harness:
    """Identify which harness produced this hook payload.

    A nonblank "turn_id" marks a Codex event. Events without that marker,
    including the captured Claude Code events, are "unknown".
    """
    try:
        d = _as_dict(payload)
        if d is not None and _nonblank_string(d.get("turn_id")) is not None:
            return "codex"
        return "unknown"
    except Exception:
        return "unknown"


def hook_session_id(payload: object, *, env: Mapping[str, str]) -> str | None:
    """Resolve the ID shared by callbacks that read and write session state.

    Codex uses the payload's session_id. For events without a Codex marker,
    Claude's stable CLAUDE_CODE_SESSION_ID override takes precedence when set.
    Use hook_session_path_key before putting this ID in a filename. Durable
    attribution has its own contract in backend/session_identity.py.
    """
    try:
        data = _as_dict(payload)
        if data is None:
            return None
        session_id = _nonblank_string(data.get("session_id"))
        if detect(data) == "codex":
            return session_id
        return _nonblank_string(env.get("CLAUDE_CODE_SESSION_ID")) or session_id
    except Exception:
        return None


def hook_session_path_key(session_id: object) -> str | None:
    """Return a filesystem-safe session key, preserving native UUID filenames.

    Lowercase canonical UUIDs pass through. Other nonblank strings use a
    separate hash namespace that preserves differences in case and keeps
    path separators and reserved names such as "recent" out of filenames.
    """
    sid = _nonblank_string(session_id)
    if sid is None:
        return None
    if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", sid):
        return sid
    # JSON can carry lone surrogates as escapes. Preserve them in the hash.
    return "sha256-" + sha256(sid.encode("utf-8", errors="surrogatepass")).hexdigest()


def tool_kind(payload: object) -> ToolKind:
    """Classify a hook payload's tool into a harness-independent kind."""
    try:
        name = _tool_name(payload)
        if name is None:
            return "other"
        if name.startswith(_MCP_PREFIX):
            return "mcp"
        if name in _SHELL_NAMES:
            return "shell"
        if name in _WRITE_NAMES:
            return "write"
        if name in _EDIT_NAMES:
            return "edit"
        if name in _READ_NAMES:
            return "read"
        return "other"
    except Exception:
        return "other"


def mcp_tool_name(payload: object) -> str | None:
    """The flat `mcp__server__tool` name, on either harness's hook
    payload (both already flatten it — see join_mcp_name for the
    transcript records that don't)."""
    try:
        name = _tool_name(payload)
        if name is not None and name.startswith(_MCP_PREFIX):
            return name
        return None
    except Exception:
        return None


def join_mcp_name(record: object) -> str | None:
    """Reconstruct a flat `mcp__server__tool` name from a transcript
    record that splits it into `namespace` + `name` (Codex's transcript
    shape — hook payloads are already flat; see mcp_tool_name for those).

    Joins with "__" only when a namespace is present, and only when the
    namespace doesn't already end in one: real rollouts spell it both ways
    ("mcp__sill", but also "mcp__episodic_memory__" and "mcp__agi_memory__"),
    and appending unconditionally produced four underscores
    ("mcp__episodic_memory____search"). A plain, non-MCP call like
    {"name": "exec"} carries no namespace and passes through unchanged
    rather than picking up a spurious separator.

    Not normalized here: the same server appears hyphenated in a flat name
    ("mcp__episodic-memory__search") and underscored in a namespace
    ("mcp__episodic_memory__"), so the two spellings don't reconcile to one
    string. Every current consumer filters by substring, where it makes no
    difference; an exact-match consumer would have to handle both.
    """
    try:
        d = _as_dict(record)
        if d is None:
            return None
        name = _as_str(d.get("name"))
        if name is None:
            return None
        namespace = _as_str(d.get("namespace"))
        if namespace is None:
            return name
        separator = "" if namespace.endswith("__") else "__"
        return f"{namespace}{separator}{name}"
    except Exception:
        return None


def _command_value(value: object) -> str | None:
    """One command key's value as scannable text.

    A string passes through. An argv LIST — Codex's `shell` spells every
    call that way (['bash', '-lc', 'ls'], 314/314 in the rollout census) —
    is joined with NEWLINES, one element per line. Dropping the list would
    read as "no command at all"; joining with spaces would be worse than
    that, because it fabricates a shell line nobody ran: 'bash -lc' +
    'echo === && ls' becomes `bash -lc echo === && ls`, where `echo` is no
    longer in command position, and shell-idiom-guard's trap regex (which
    anchors on command position, correctly) stops seeing a trap that is
    really there. One element per line keeps each element's content at a
    line start — which is where a `-c`/`-lc` script element genuinely
    begins — and every consumer's regexes already span it (`\\s` matches a
    newline). Anything else (a dict, a number, an empty list) yields None
    rather than a stringified guess.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, list):
        parts = [part for part in value if isinstance(part, str) and part]
        return "\n".join(parts) if parts else None
    return None


def shell_command(payload: object) -> str | None:
    """The shell command text, from Bash (Claude) or exec/exec_command/
    shell/shell_command (Codex), whichever key that tool keeps it under —
    see _COMMAND_KEYS for the per-tool ground truth and _command_value for
    the argv-list case.

    Note what the text IS on Codex's freeform `exec`: its input is a
    JavaScript program that calls other tools, not a shell line (real
    specimens read `const r = await Promise.all([tools.exec_command({cmd:
    "…"})…`). Returning it here is still right for every current consumer
    — they scan for a command's text, which appears verbatim inside that
    program — but it is script text, not an argv.
    """
    try:
        if tool_kind(payload) != "shell":
            return None
        ti = _tool_input(payload)
        if ti is None:
            return None
        for key in _COMMAND_KEYS:
            text = _command_value(ti.get(key))
            if text is not None:
                return text
        return None
    except Exception:
        return None


def _patch_body(tool_input: dict) -> str | None:
    """Read the first nonblank string from Codex's command and legacy input fields."""
    return (
        _nonblank_string(tool_input.get("command"))
        or _nonblank_string(tool_input.get("input"))
    )


def _parse_patch_path(patch: str) -> str | None:
    for line in patch.splitlines():
        for header in _PATCH_HEADERS:
            if line.startswith(header):
                path = line[len(header):].strip()
                return path or None
    return None


def written_path(payload: object) -> str | None:
    """The path a Write/Edit/MultiEdit (Claude) or apply_patch (Codex)
    call touches. apply_patch has no file_path field — the path lives in
    the patch body's own header line, so it is parsed out of the body
    rather than looked up; see _parse_patch_path for the unparseable
    case.

    This is the single-file view of written_files: for a multi-file
    apply_patch body it reports only the FIRST file's path. A caller that
    gates scope on this value alone before ever calling written_text is
    exactly the bug written_files exists to fix — see that function's
    docstring — so prefer written_files for any scope-then-read check.
    """
    try:
        kind = tool_kind(payload)
        if kind not in ("write", "edit"):
            return None
        ti = _tool_input(payload)
        if ti is None:
            return None
        if _tool_name(payload) == "apply_patch":
            patch = _patch_body(ti)
            return _parse_patch_path(patch) if patch is not None else None
        return _as_str(ti.get("file_path"))
    except Exception:
        return None


def _patch_added_text(patch: str) -> str | None:
    """Added ("+") lines, scoped to the FIRST file header only — the
    same file written_path returns. A multi-file patch (*** Update
    File: a.md / ... / *** Update File: b.md / ...) would otherwise
    return every file's additions concatenated, so a caller pairing this
    with written_path could judge file A's scope against file B's
    content. Stops collecting at the second header line (if any); a
    patch with 0 or 1 headers is unaffected — both are already
    unambiguous ("no file" or "the one file"), same as before this
    scoping was added.
    """
    lines = []
    headers_seen = 0
    for line in patch.splitlines():
        if any(line.startswith(header) for header in _PATCH_HEADERS):
            headers_seen += 1
            if headers_seen >= 2:
                break  # second file's header — stop; stay scoped to the first
            continue
        if line.startswith("+"):
            lines.append(line[1:])
    return "\n".join(lines) if lines else None


def _multi_edit_text(ti: dict) -> str | None:
    """MultiEdit's new_string values, newline-joined. MultiEdit nests
    its edits under an "edits" list instead of Edit's flat top-level
    new_string: {"file_path": ..., "edits": [{"old_string": ...,
    "new_string": ...}, ...]}. tool_kind classifies both as "edit", but
    only Edit's payload has a top-level new_string — reading that key
    directly for MultiEdit silently returns None for every call.
    A non-list "edits" (missing, wrong type) returns None so the caller
    falls back to the flat Edit shape; a malformed entry (not a dict, or
    a missing/non-string new_string) is skipped rather than raising.
    """
    edits = ti.get("edits")
    if not isinstance(edits, list):
        return None
    parts = [text for text in (
        _as_str(edit.get("new_string")) for edit in edits if isinstance(edit, dict)
    ) if text is not None]
    return "\n".join(parts) if parts else None


def written_text(payload: object) -> str | None:
    """The text a Write/Edit/MultiEdit (Claude) or apply_patch (Codex)
    call introduces: Write's full content, Edit's new_string (what's
    being introduced, not what it replaces), MultiEdit's new_string
    values newline-joined (see _multi_edit_text), or apply_patch's added
    ("+") lines scoped to the first file header (see _patch_added_text).

    This is the single-file view of written_files: for a multi-file
    apply_patch body it reports only the FIRST file's added text, paired
    with written_path's first-file path above. See written_files's
    docstring for the caller-visible bug that pairing causes when a
    caller checks written_path's scope before ever reading this.
    """
    try:
        kind = tool_kind(payload)
        ti = _tool_input(payload)
        if ti is None:
            return None
        if _tool_name(payload) == "apply_patch":
            patch = _patch_body(ti)
            return _patch_added_text(patch) if patch is not None else None
        if kind == "write":
            return _as_str(ti.get("content"))
        if kind == "edit":
            multi = _multi_edit_text(ti)
            return multi if multi is not None else _as_str(ti.get("new_string"))
        return None
    except Exception:
        return None


def _parse_patch_files(patch: str) -> list[tuple[str, str]]:
    """Split an apply_patch body into one (path, added_text) pair PER FILE
    header ("*** Update File: <path>" / "*** Add File: <path>"), each
    added_text scoped to only that file's own "+" lines — the per-file
    generalization of _parse_patch_path/_patch_added_text, which both stop
    at the first header.

    A header line whose path strips to empty contributes no pair for that
    section (same "don't guess a path" contract as _parse_patch_path), but
    does not stop a later, well-formed header from contributing its own
    pair. A file section with no "+" lines still contributes ("path", "")
    rather than being dropped — the file WAS named by this patch; there is
    simply nothing added to scan for it. Content before the first header
    (no path to attribute it to yet) is discarded, matching
    _parse_patch_path's "no header, no path" contract.
    """
    files: list[tuple[str, str]] = []
    path: str | None = None
    lines: list[str] = []

    def flush() -> None:
        if path is not None:
            files.append((path, "\n".join(lines)))

    for line in patch.splitlines():
        header = next((h for h in _PATCH_HEADERS if line.startswith(h)), None)
        if header is not None:
            flush()
            path = line[len(header):].strip() or None
            lines = []
            continue
        if path is not None and line.startswith("+"):
            lines.append(line[1:])
    flush()
    return files


def written_files(payload: object) -> list[tuple[str, str]]:
    """Every (path, added_text) pair a Write/Edit/MultiEdit (Claude) or
    apply_patch (Codex) call touches — the plural, multi-file-safe form of
    written_path/written_text. A single-file call yields a one-element
    list; a multi-file apply_patch body yields one pair per file header,
    each text scoped to that file's own "+" lines only (see
    _parse_patch_files). [] when nothing is parseable at all (wrong tool
    kind, missing tool_input, an apply_patch body with no header, a
    Write/Edit/MultiEdit with no file_path).

    Exists to close a bypass: a caller that checks "is THE path in scope?"
    using written_path() alone, then reads written_text() only if so,
    answers for the WHOLE call using just the first file's path. A patch
    whose first file is irrelevant and whose second file is in scope reads
    as entirely out of scope — the in-scope second file's text is never
    even looked at. A caller that needs correct per-file scope must
    iterate this function instead and judge each pair on its own path; see
    plugin/hooks/state-language-check.py, stored-slot-guard.py, and
    tool-type-witness.py, which all switched to this after the bypass was
    found (task report has the specifics).
    """
    try:
        kind = tool_kind(payload)
        if kind not in ("write", "edit"):
            return []
        ti = _tool_input(payload)
        if ti is None:
            return []
        if _tool_name(payload) == "apply_patch":
            patch = _patch_body(ti)
            return _parse_patch_files(patch) if patch is not None else []
        path = _as_str(ti.get("file_path"))
        if path is None:
            return []
        if kind == "write":
            text = _as_str(ti.get("content")) or ""
        else:
            multi = _multi_edit_text(ti)
            text = multi if multi is not None else (_as_str(ti.get("new_string")) or "")
        return [(path, text)]
    except Exception:
        return []


def assistant_text(payload: object) -> str | None:
    """The assistant's turn text, when the hook payload carries it
    directly (Stop-event payloads on both harnesses expose
    last_assistant_message). No transcript fallback here: this module's
    payload-dict functions do no I/O; iter_transcript_tool_uses is the
    one exception, and it reads tool calls, not assistant prose.
    """
    try:
        d = _as_dict(payload)
        return _as_str(d.get("last_assistant_message")) if d is not None else None
    except Exception:
        return None


def _parse_call_arguments(raw: object) -> object:
    """Codex function_call `arguments` is typically a JSON-encoded
    string; parse it when possible, keep the raw value otherwise (never
    raise on malformed JSON) so a caller still gets something rather
    than nothing."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return raw


def _iter_transcript_records(path: object) -> list:
    out: list = []
    # open() treats an int as a raw OS file descriptor, not a filename —
    # an int path (e.g. True, which is 1, which is stdout) would open
    # that live fd instead of failing, and the `with` block below would
    # then close it on the way out. That closes a fd the process needs
    # with no exception ever reaching the `except` below to report it.
    # Reject anything that isn't an actual path before open() ever sees
    # it, so this failure mode can't reach the open() call at all.
    if not isinstance(path, (str, os.PathLike)):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return out

    for line in lines:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue

        if entry.get("type") == "assistant":
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        out.append({
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "input": block.get("input"),
                        })
            continue

        if entry.get("type") == "response_item":
            item = entry.get("payload")
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "function_call":
                out.append({
                    "id": item.get("call_id"),
                    "name": join_mcp_name(item),
                    "input": _parse_call_arguments(item.get("arguments")),
                })
            elif itype == "custom_tool_call":
                # Codex's freeform-input record type — exec's own shape,
                # distinct from exec_command's function_call. Field names
                # (call_id/name/input) CONFIRMED against 1314 real records
                # in the 2026-08-06 rollout census: names are `exec` (1188)
                # and `apply_patch` (126), `input` is always a string, and
                # some records add `id`/`status` fields this ignores. Note
                # `exec`'s string is a JavaScript program calling other
                # tools, not a shell line — see shell_command.
                out.append({
                    "id": item.get("call_id"),
                    "name": item.get("name"),
                    "input": item.get("input"),
                })

    return out


def iter_transcript_tool_uses(path: object) -> Iterator[dict]:
    """Yield {id, name, input} for every tool call in a session
    transcript, across both JSONL schemas:

    - Claude Code: type=="assistant" entries; message.content blocks
      with type=="tool_use" (id, name, input already flat).
    - Codex: type=="response_item" entries wrapping a payload;
      payload.type=="function_call" (id=call_id, name via join_mcp_name
      since MCP calls split namespace/name here, input from its
      JSON-or-raw `arguments`) or payload.type=="custom_tool_call"
      (id=call_id, name, input taken as-is).

    Total: a non-path argument (including an int, which open() would
    treat as a raw fd rather than a filename — see _iter_transcript_records),
    a missing file, undecodable line, or any other read/parse failure
    yields nothing rather than raising. Built eagerly (not a generator)
    so a failure can never surface mid-iteration either — the returned
    iterator is exhausted-safe from the moment this function returns.
    """
    try:
        return iter(_iter_transcript_records(path))
    except Exception:
        return iter(())
