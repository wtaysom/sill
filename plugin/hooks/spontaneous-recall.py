#!/usr/bin/env python3
# Ported from agi-memory .claude/hooks/spontaneous-recall.py (2026-08-04).
"""
Hook: Spontaneous memory recall on UserPromptSubmit.
Queries sill (Postgres + pgvector) for relevant context before the agent responds.

Protocol: Think -> Recall -> Think more -> Remember
This hook handles the "Recall" step by injecting relevant memories.

Queries two sources:
1. sill (hybrid_recall / fast_recall) - semantic memories via vector + full-text search.
   A typed message over 2,000 characters is recalled for passage by passage
   (_recall_passages.py); anything shorter through recall_core.fused_recall().
2. episodic-memory (optional) - conversation history via keyword search
   Enabled only when SILL_EPISODIC_MEMORY_PATH is set and the CLI exists.

Every prompt also gets a one-line [TIME] header. Its gap and count are the
session's own, read from its transcript (_session_clock.py).
"""
import json
import sys
import re
import subprocess
import os
import time
from datetime import datetime
from pathlib import Path
from _store import core
import _store
from _harness import hook_session_id, hook_session_path_key, own_words, typed_text
from _recall_state import record_shown

try:  # this session's own clock, read from its transcript (the module must never break a prompt)
    import _session_clock as session_clock
except Exception:
    session_clock = None

try:  # recall for a long message, passage by passage (same rule)
    import _recall_passages as recall_passages
except Exception:
    recall_passages = None

# The shipped hook command has a 45-second host timeout. Reserve five
# seconds for process startup and cleanup, result formatting and delivery.
_SUBPROCESS_DEADLINE = time.monotonic() + 40


def _run_with_budget(args, *, timeout: float, **kwargs):
    remaining = _SUBPROCESS_DEADLINE - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(args, 0)
    return subprocess.run(args, timeout=min(timeout, remaining), **kwargs)


_SILL_LOG_DIR = Path(os.environ.get("SILL_LOG_DIR", "/tmp"))
LOG_FILE = _SILL_LOG_DIR / "spontaneous-recall.log"


def _env_int(name: str, default: int, maximum: int) -> int:
    """Read a bounded integer setting, using the default for invalid values."""
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if 0 <= value <= maximum else default


MAX_MEMORIES = _env_int("SILL_RECALL_MAX_MEMORIES", 5, 50)
MAX_CONVERSATIONS = _env_int("SILL_RECALL_MAX_CONVERSATIONS", 2, 50)
MIN_QUERY_LENGTH = _env_int("SILL_RECALL_MIN_QUERY_LENGTH", 20, 10_000)
FAST_MIN_SCORE = _store.recall_min_similarity()

# Quiet mode hides the visible result list. The time header, failure warning,
# and model context still appear.
QUIET_TUI = os.environ.get("SILL_RECALL_QUIET", "").strip().lower() in ("1", "true", "yes")

MIN_TOPICAL_KEYWORDS = 3  # Lever 1: skip process-only messages
LONG_MESSAGE_CHARS = 2000  # past this a typed message is recalled for passage by passage
# Claude Code replaces a hook's additionalContext over 10,000 characters with a file path
# and a 2,000-character preview, and does not ask the model to read the file. Stay under.
CONTEXT_CAP = 9500
NOTICE_ROOM = 3000  # the most a pattern check may take of it
# Episodic memory archive (optional integration)
EPISODIC_CLI_ENV = os.environ.get("SILL_EPISODIC_MEMORY_PATH", "").strip()
EPISODIC_CLI = Path(EPISODIC_CLI_ENV) if EPISODIC_CLI_ENV else None
SUPERPOWERS_DIR = Path(
    os.environ.get(
        "PERSONAL_SUPERPOWERS_DIR",
        Path.home() / ".config" / "superpowers",
    )
)


def log(message: str):
    """Append a timestamped entry to the log file.

    Never raises, and keeps a line whose text will not encode (a prompt can
    carry a lone surrogate). The log holds the opening of each prompt recalled
    for, so it is its owner's alone: created that way, and narrowed when an
    earlier version of this hook created it readable by everyone.
    """
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", errors="backslashreplace") as f:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            f.write(f"{timestamp} | {message}\n")
    except Exception:
        pass


def typed_body(prompt: str) -> str:
    """What the person typed, out of any wrapper a front-end put round it (the Claude
    desktop app's <pasted_content>, Cursor's <timestamp>/<user_query>, Codex's
    "# Files mentioned by the user")."""
    return typed_text(prompt) or prompt


def squeeze(text: str, room: int = NOTICE_ROOM) -> str:
    """A one-time notice cut to `room` characters, so it cannot push the context over the cap."""
    if len(text) <= room:
        return text
    return text[:room - 40].rstrip() + " … [cut to fit the hook's context]"


def _units(text: str) -> int:
    """Length as Claude Code is likely to count it: UTF-16 units (an emoji is two)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _passage_rows(prompt, deadline):
    """Memories for a long message, every call bounded by `deadline`.

    [] if the passage module itself breaks, so the opening of the message is still
    asked. A store that could not answer raises, as fused_recall() does.
    """
    def fetch(fn, query_text):
        return core.recall_rows(fn, query_text, core.FUSED_POOL,
                                fast_min_similarity=FAST_MIN_SCORE, deadline=deadline)
    try:
        return recall_passages.passage_recall(typed_body(prompt), fetch=fetch, limit=MAX_MEMORIES, log=log)
    except core.RecallUnavailable:
        raise
    except Exception as exc:
        log(f"Passage recall error: {type(exc).__name__}")
        return []


def _snippet_terms(row, keywords):
    """The words a memory is excerpted around: those of the passage it was recalled
    for, or else the message's own keywords."""
    passage = row.get('passage_text')
    return core.extract_keywords(passage) if passage else (keywords or [])


# Retrieval and ranking belong to the shared core; hooks select and display.
def query_memories_vector(prompt, keywords=None):
    """Recall for the prompt: passage by passage when it is long, else fused_recall().

    fused_recall() reads the first 500 characters of a message and eight keywords,
    which is most of a short message and little of a long one. If the passage
    module is missing or finds nothing, the opening of the message is still asked.
    None means the store could not answer.
    """
    if MAX_MEMORIES <= 0:
        return []
    try:
        # One deadline for every retrieval call, however the message is asked. With the
        # store hung each call waits out its time: passage by passage and then the
        # opening would be two waits.
        deadline = min(_SUBPROCESS_DEADLINE-7, time.monotonic()+15)
        rows = []
        if len(prompt) > LONG_MESSAGE_CHARS and recall_passages is not None:
            rows = _passage_rows(prompt, deadline)
            if not rows and deadline - time.monotonic() < 1:
                # Nothing came back because the calls ran out of time, not because
                # nothing matched. Asking again would wait as long again.
                log('Passage recall ran out of time; the message is not asked a second time')
                return []
            if not rows:
                log('Passage recall found nothing; asking with the opening of the message')
        if not rows:
            rows = core.fused_recall(prompt, core.previous_turn(input_data.get('transcript_path', '')),
                keywords=keywords, limit=MAX_MEMORIES, fast_min_similarity=FAST_MIN_SCORE,
                deadline=deadline)[:MAX_MEMORIES]
        contents = core.full_contents([r['id'] for r in rows], deadline=_SUBPROCESS_DEADLINE-4)
        return [dict(row, content=core.focus_snippet(contents.get(row['id'],row['content']), _snippet_terms(row, keywords))) for row in rows]
    except core.RecallUnavailable:
        log('Recall store unavailable')
        return None


extract_keywords = core.extract_keywords


def search_episodic_memory(query: str) -> list[dict]:
    """Search conversation archive using episodic-memory CLI (semantic search with indexing).

    Skip the search when disabled or the episodic CLI is unavailable.
    """
    if MAX_CONVERSATIONS == 0 or EPISODIC_CLI is None or not EPISODIC_CLI.exists():
        return []

    try:
        env = os.environ.copy()
        env["EPISODIC_MEMORY_CONFIG_DIR"] = str(SUPERPOWERS_DIR)
        env["EPISODIC_MEMORY_READONLY"] = "1"

        result = _run_with_budget(
            [str(EPISODIC_CLI), 'search', query, '--limit', str(MAX_CONVERSATIONS)],
            capture_output=True,
            text=True,
            timeout=30,  # Longer timeout for semantic search
            env=env,
        )

        if result.returncode != 0:
            # Not its stderr: a search may echo the query it was given.
            log(f"Episodic search failed (exit {result.returncode})")
            return []

        # Parse CLI output - format is "N. [project, date] - match%\n   snippet\n   Lines..."
        matches = []
        lines = result.stdout.strip().split('\n')
        i = 0
        while i < len(lines) and len(matches) < MAX_CONVERSATIONS:
            line = lines[i]
            # Look for numbered result lines like "1. [-Users-..., 2026-01-08] - -15% match"
            if re.match(r'^\d+\.\s+\[', line):
                # Extract date from the line
                date_match = re.search(r'(\d{4}-\d{2}-\d{2})', line)
                date = date_match.group(1) if date_match else "unknown"

                # Next line should be the snippet (indented)
                if i + 1 < len(lines) and lines[i + 1].startswith('   '):
                    snippet = lines[i + 1].strip().strip('"')
                    if len(snippet) > 30:
                        matches.append({
                            'content': snippet[:300],
                            'date': date
                        })
                    i += 2
                    continue
            i += 1

        # Deduplicate by content similarity
        seen_prefixes = set()
        unique = []
        for m in matches:
            prefix = m['content'][:50]
            if prefix not in seen_prefixes:
                seen_prefixes.add(prefix)
                unique.append(m)

        return unique[:MAX_CONVERSATIONS]

    except Exception as e:
        # By kind only. The exception's own text can hold the command (a timeout's
        # does), and the command holds the query: the person's message.
        log(f"Episodic search error: {type(e).__name__}")
        return []


def _passage_hint(text: str, width: int = 60) -> str:
    """The opening of the passage a memory was recalled for, fit for one line."""
    flat = re.sub(r"\s+", " ", text or "").strip().replace('"', "'")
    return flat[:width] + ("…" if len(flat) > width else "")


def format_results(memories: list[dict], conversations: list[dict], *, log_ids: bool = True) -> str:
    """Format all results for injection as context."""
    lines = []

    if memories:
        # Log recalled memory IDs for importance feedback (Lever 3)
        ids = [mem.get('id', '?')[:8] for mem in memories]
        if log_ids:     # fit_to_cap() formats to measure, and must not log what is not yet shown
            log(f"Recalled IDs: {ids}")

        by_passage = any(mem.get('passage_text') for mem in memories)
        lines.append("[SPONTANEOUS RECALL] Relevant memories"
                     + (" (long message; on= is the part each was recalled for):" if by_passage else ":"))
        for i, mem in enumerate(memories, 1):
            content = mem['content'].replace('\n', ' ')
            sim = f"{mem['score']:.4f}" if isinstance(mem.get('score'), (int,float)) else '?'
            imp = mem.get('importance', '?')
            mid = mem.get('id', '')[:8]
            # The address, when the memory has one: a recalled paraphrase can
            # drift from its source with every retelling, and re-reading the
            # source is the only correction. Advice to open the file is
            # worthless without the file, so hand it over here, ahead of the
            # content.
            ref = (mem.get('ref') or '').strip()
            src = f" src={ref}" if ref else ""
            src += "".join(f" {key}={mem[key]}" for key in ("speaker", "production", "source_store", "force") if mem.get(key))
            if mem.get('passage_text'):
                src += f' on="{_passage_hint(mem["passage_text"])}"'
            lines.append(f"  {i}. [{mem['type']}, score={sim}, imp={imp}, id={mid}{src}] {content}")

    if conversations:
        lines.append("[CONVERSATION HISTORY] Related past discussions:")
        for i, conv in enumerate(conversations, 1):
            content = conv['content'].replace('\n', ' ')
            lines.append(f"  {i}. [past conversation] {content}")

    if lines:
        lines.append("[Consider how these connect to the current conversation.]")
        return '\n'.join(lines)

    return ""


def fit_to_cap(memories: list[dict], conversations: list[dict], overhead: int = 0,
               cap: int | None = None) -> tuple[list[dict], list[dict]]:
    """The leading memories (then conversations) whose printed form fits under the cap.

    `overhead` is what else goes into the same context: the time header and a
    pattern check. Memories go from the end first: they are in rank order.
    """
    cap = CONTEXT_CAP if cap is None else cap
    memories, conversations = list(memories), list(conversations)
    while (memories or conversations) and _units(
            format_results(memories, conversations, log_ids=False)) + overhead > cap:
        if memories:
            memories.pop()
        else:
            conversations.pop()
    return memories, conversations

# ---------------------------------------------------------------------------
# Time awareness (migration 002, session_activity table).
# This is not continuity — regular context preservation handles that. It just
# reports how much wall-clock has passed since the project was last worked on,
# so a large gap can hint that something upstream may have changed since (a
# dependency shipped a new release, a spec got revised, etc). Injected as
# reference data to cite, not a cue to perform circadian affect.
# ---------------------------------------------------------------------------
def parse_db_timestamp(raw: str):
    """Parse a Postgres timestamptz into an aware datetime.

    psql renders these as `2026-08-06 18:25:21.164779+00` — a *two-digit*
    offset, which `datetime.fromisoformat` did not accept until 3.11, below
    this project's 3.10 floor. So normalize the offset to ±HH:MM before
    parsing rather than relying on the interpreter's leniency.

    A timestamp with no offset at all is read as UTC: that is what the store
    holds, and guessing local time would silently reintroduce the very skew
    `memory_age_days` exists to prevent.
    """
    from datetime import datetime as dt, timezone

    text = (raw or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    text = text.replace("Z", "+00:00")
    # ...+00  ->  ...+00:00   (leave ±HH:MM and offset-less strings alone)
    m = re.match(r"^(?P<body>.*?)(?P<sign>[+-])(?P<hh>\d{2})$", text)
    if m:
        text = f"{m.group('body')}{m.group('sign')}{m.group('hh')}:00"
    parsed = dt.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def memory_age_days(created) -> int:
    """Whole days between `created` and now, compared as instants.

    The bug this replaces: the offset was split off the timestamp, leaving a
    naive datetime that still held the *UTC* wall clock, and that was
    subtracted from a naive *local* `datetime.now()`. Anywhere west of UTC the
    difference went negative for anything recent — 18:25Z minus 11:25 PDT is
    -7h, whose `.days` is -1 — so a memory stored seconds ago was labelled
    "-1d ago" in the header the model reads on every prompt.

    Clamped at zero so clock skew or a future-dated row reads as "today"
    rather than going negative again by another route.
    """
    from datetime import datetime as dt, timezone

    return max(0, (dt.now(timezone.utc) - created).days)


def _humanize_gap(seconds: int) -> str:
    """Precise, compact duration — better than '6d ago'."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def track_session_time(session_id: str, project: str) -> dict | None:
    """Upsert this exchange into session_activity; return the PRIOR row's timing.

    One psql round-trip. The `prev` CTE reads the pre-upsert snapshot (Postgres CTEs
    share one snapshot), so it yields the previous prompt's time and session before
    `up` overwrites them. Returns None on any failure — never breaks the prompt.
    """
    if not session_id or not project:
        return None
    sid = session_id.replace("'", "''")
    proj = project.replace("'", "''")
    q = f"""
    WITH prev AS (
        SELECT session_id AS ps, last_prompt_at AS pt
        FROM session_activity WHERE project = '{proj}'
    ),
    up AS (
        INSERT INTO session_activity (project, session_id, last_prompt_at, session_started_at, prompt_count)
        VALUES ('{proj}', '{sid}', now(), now(), 1)
        ON CONFLICT (project) DO UPDATE SET
            session_id = '{sid}',
            last_prompt_at = now(),
            session_started_at = CASE WHEN session_activity.session_id = '{sid}'
                                      THEN session_activity.session_started_at ELSE now() END,
            prompt_count = CASE WHEN session_activity.session_id = '{sid}'
                                THEN session_activity.prompt_count + 1 ELSE 1 END
        RETURNING prompt_count
    )
    SELECT row_to_json(timing) FROM (SELECT COALESCE((SELECT ps FROM prev), '') AS previous_session,
           COALESCE(EXTRACT(EPOCH FROM (now() - (SELECT pt FROM prev)))::bigint, -1) AS gap_seconds,
           (SELECT prompt_count FROM up) AS count) timing;
    """
    try:
        rows = _store.query(q, timeout=5, deadline=_SUBPROCESS_DEADLINE, json_output=True)
        if not rows:
            return None
        parts = list(rows[0].values())
        return {"prev_session": parts[0], "gap_seconds": int(parts[1]), "count": int(parts[2])}
    except Exception:
        return None


def typed_by_a_person(prompt) -> bool:
    """Whether a person typed this prompt (rule: _harness.typed_text). The clock asks,
    and so does the long-message path: a task notice is not a message to count."""
    return typed_text(prompt) is not None


def build_time_header(session_id: str, project: str, *, typed: bool = True,
                      transcript_path: str = "", prompt: str = "", prompt_id: str = "",
                      now=None) -> str:
    """A compact 1-line temporal header: wall-clock (for day/night legibility)
    plus sequence/gap since the last typed message.

    The gap and the count are this session's own, read from its transcript
    (_session_clock). The session_activity row is one per project, and every
    prompt in the project used to overwrite it, so a task notice or another
    session working in the same project reset "since your last message". Only
    a typed prompt advances the row now; it still answers "when was this
    project last worked in" for a session's first message, and stands in when
    the transcript cannot be read. A message sent while the assistant was
    working reaches this hook when the queue drains; its gap is measured from
    when it was sent, and the header says how long ago that was.
    """
    now = (now or datetime.now()).astimezone()
    clock = now.strftime("%a %Y-%m-%d %H:%M %Z")
    own = None
    if session_clock is not None and transcript_path:
        try:
            own = session_clock.before_this_prompt(transcript_path, prompt, now, prompt_id=prompt_id)
        except Exception:
            own = None
    own_count, own_gap, sent_ago = 0, None, None
    if own and own[1] is not None:
        sent = own[2] or now
        own_count, own_gap = own[0], max(0, int((sent - own[1]).total_seconds()))
        sent_ago = max(0, int((now - sent).total_seconds()))
    if not typed:
        # A notice or another machine-written prompt: say so, and leave the clock alone.
        if own_gap is not None:
            return (f"[TIME] {clock} · not a typed message · {_humanize_gap(own_gap)} since the last "
                    f"typed message (#{own_count} this session).")
        return f"[TIME] {clock} · not a typed message."
    info = track_session_time(session_id, project)
    if own_gap is not None:
        if sent_ago is not None and sent_ago >= 60:   # typed while the assistant was working
            return (f"[TIME] {clock} · msg #{own_count + 1} this session · sent {_humanize_gap(sent_ago)} "
                    f"ago, {_humanize_gap(own_gap)} after your last message.")
        return (f"[TIME] {clock} · msg #{own_count + 1} this session · "
                f"{_humanize_gap(own_gap)} since your last message.")
    projname = os.path.basename(project.rstrip('/')) if project else "?"
    if not info:
        return f"[TIME] {clock}"
    prev, gap, count = info["prev_session"], info["gap_seconds"], info["count"]
    if not prev or gap < 0:
        return f"[TIME] {clock} · first exchange recorded for {projname}."
    if prev == session_id:
        return f"[TIME] {clock} · msg #{count} this session · {_humanize_gap(gap)} since your last message."
    # session_id changed → a new session (a "waking"): report the across-gap.
    nudge = " — consider what may have changed upstream since." if gap >= 7200 else ""
    return f"[TIME] {clock} · new session (msg #{count}) · last worked in {projname} {_humanize_gap(gap)} ago{nudge}"


# Main execution
try:
    input_data = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)
if not isinstance(input_data, dict):
    # Valid JSON that isn't an object (e.g. a bare array) is exactly as
    # unusable as invalid JSON — every input_data.get(...) below raises
    # AttributeError on a list.
    sys.exit(0)

if input_data.get('hook_event_name', 'UserPromptSubmit') != 'UserPromptSubmit':
    sys.exit(0)

# `claude --print` (SDK/headless) invocations are non-conversational — no human
# on the other end. They must not get recall injected into their prompt, nor
# advance the interactive activity clock. Every `--print` fire — a scheduled
# job, a spawned subagent, a batch script — reports an entrypoint in the "sdk"
# family (e.g. "sdk-cli").
#
# So gate on the HEADLESS signal, not on a whitelist of interactive strings. A
# whitelist ("== cli") is the wrong shape: it goes silently dark on every
# front-end string it did not anticipate — including front-ends that don't
# exist yet. Default to interactive; exclude only the known-headless family
# (blacklist, not whitelist) — an unknown entrypoint is a human until proven
# otherwise. Any new front-end (cli, desktop app, web, IDE extension, …) then
# works automatically, with no list to update.
#
# The mirror case is a front-end that drives `--print` on behalf of a person
# who really is there. It looks headless from here (--print, sdk-cli), so it
# sets SILL_INTERACTIVE=1 to say "there is a human in this loop": it gets the
# header and advances the clock like any interactive session. SILL_HEADLESS_TOOL
# is the opposite override — an explicit "be quiet" that always wins.
#
# SILL_DETACHED_BEAT=1 is an authoritative headless flag a scheduler can set on
# every child session it spawns, for exactly this classification — a stronger
# signal than the entrypoint string, which a wrapping front-end could leave
# unchanged. The entrypoint test stays as a second signal, but is never the only
# lock — nor was the 2,000-character recall length-gate that used to sit below
# ever one: a shortened standing prompt would have switched injection on silently.
_entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli")
_interactive = bool(os.environ.get("SILL_INTERACTIVE"))
_headless_entrypoint = _entrypoint.startswith("sdk")
_detached_beat = bool(os.environ.get("SILL_DETACHED_BEAT"))
if _detached_beat or os.environ.get("SILL_HEADLESS_TOOL") or (
        not _interactive and (_headless_entrypoint or _detached_beat)):
    sys.exit(0)

prompt = input_data.get("prompt", "")
if not isinstance(prompt, str) or not prompt.strip():
    sys.exit(0)

# A "System:"-tagged prompt is not a person talking — no header, no recall.
if prompt.lstrip().startswith("System:"):
    sys.exit(0)

# Time header: every prompt that gets this far gets one, including short ones
# like "yes" or "ok" (a short message hitting the recall length-gate below
# should not also drop the header). Only a prompt a person typed advances the
# activity clock; a task notice or another machine-written prompt gets a header
# that says so (see build_time_header). RECALL is gated separately below.
# Identity uses CLAUDE_CODE_SESSION_ID (stable across the conversation,
# compaction included), not the payload session_id (random for headless
# --print fires).
def _stable_project(raw: str) -> str:
    """Normalize to the git toplevel so project identity survives cwd drift.
    The payload cwd follows the invoking shell (a `cd` into a subdirectory
    mid-session can mint a phantom project row keyed on that subdirectory),
    so prefer CLAUDE_PROJECT_DIR (the launch dir, immune to cd) and fold any
    path inside a repo to the repo root."""
    try:
        r = _run_with_budget(["git", "-C", raw, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=3)
        top = r.stdout.strip()
        if r.returncode == 0 and top:
            return top
    except Exception:
        pass
    return raw


_project = _stable_project(os.environ.get("CLAUDE_PROJECT_DIR")
                           or input_data.get("cwd") or os.getcwd())
_sid = hook_session_id(input_data, env=os.environ)
# The Claude desktop app puts notes of its own in front of what a person typed
# (<system-reminder> blocks: a session started from a card, the list of linked sessions).
# Whether the prompt was typed is decided on what follows them: otherwise a click on a
# reply button is stamped "not a typed message", and a long message behind such a note
# gets no recall.
_own = own_words(prompt)
_typed_prompt = typed_by_a_person(_own)
_transcript = input_data.get("transcript_path")
_prompt_id = input_data.get("prompt_id")
time_header = build_time_header(_sid, _project, typed=_typed_prompt, prompt=prompt,
                                transcript_path=_transcript if isinstance(_transcript, str) else "",
                                prompt_id=_prompt_id if isinstance(_prompt_id, str) else "")
# Recall is asked with what the person typed. An answer too short to ask with ("Yes, try
# it") leaves the whole prompt to be asked as before, the app's note included: that note
# is news of what the person just did.
_asks_own = _typed_prompt and len(_own.strip()) >= MIN_QUERY_LENGTH
if _asks_own:
    prompt = _own
try:
    _store.selected_dsn()
except Exception:
    time_header += ' · [sill] Selected store unavailable; timing unchecked.'


def _read_pattern_carry_forward(session_id: str) -> str:
    """Read (and consume) last turn's response-pattern warnings for this session.

    response-patterns.py is a Stop hook: it fires after the reply is already on
    screen, so it cannot prevent anything. It stashes its warnings here; this
    hook surfaces them at the top of the next turn, before any token is
    generated. Deleted on read so a warning is delivered exactly once.
    """
    sid = hook_session_path_key(session_id)
    if not sid:
        return ""
    path = _SILL_LOG_DIR / f"response-patterns-last-{sid}.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text())
        path.unlink(missing_ok=True)
    except Exception:
        return ""
    warnings = data.get("warnings") or []
    if not warnings:
        return ""
    body = "\n\n".join(warnings)
    return (
        "[PATTERN CHECK — your PREVIOUS reply tripped these. Apply now, before "
        "writing. Do not apologize for the earlier slip or perform a correction; "
        "just do not repeat it in this reply.]\n" + body
    )


# Read once, here: this is the only point both exit paths pass through, and the
# read consumes the sidecar. A short prompt takes _emit_header_and_exit() and
# would otherwise drop the flag — which is precisely the prose-only turn where no
# PreToolUse/PostToolUse hook fires and nothing else can reach the agent in time.
_pattern_flag = squeeze(_read_pattern_carry_forward(_sid))


def _emit_header_and_exit():
    """Show the time header even when memory recall is skipped (short/greeting/thin)."""
    parts = [p for p in (_pattern_flag, time_header) if p]
    if parts:
        print(json.dumps({
            "systemMessage": time_header,
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": "\n\n".join(parts)},
        }))
    sys.exit(0)


# --- Recall gates: from here down only decide whether to ALSO do memory recall;
#     the time header has already fired, so a skip still shows it. ---

# Too short to query. Anything over 2,000 characters used to be skipped as well ("so
# long the embedding would time out"): a query is cut to 1,000 characters before it
# is embedded, so that guard protected nothing and cost a person recall on their
# longest messages. A long prompt nobody typed (a workflow report runs to 10,000
# characters) is still left with the header alone.
if len(prompt) < MIN_QUERY_LENGTH or (len(prompt) > LONG_MESSAGE_CHARS and not _asks_own):
    _emit_header_and_exit()

# A bare greeting/acknowledgement (whole message, not just prefix).
simple_patterns = [
    r'^(hi|hello|hey|good morning|good night|thanks|bye|ok|okay)[!.,]?$',
    r'^(yes|no|sure|right|got it)[!.,]?$',
    r'^\s*$',
]
if any(re.match(p, prompt.lower().strip()) for p in simple_patterns):
    _emit_header_and_exit()

# Extract keywords and check topical threshold (Lever 1)
keywords = extract_keywords(prompt)
log(f"Keywords ({len(keywords)}): {keywords}")

if len(keywords) < MIN_TOPICAL_KEYWORDS:
    log(f"Skipping recall: only {len(keywords)} topical keywords (need {MIN_TOPICAL_KEYWORDS})")
    _emit_header_and_exit()

# Query both sources
log(f"Query for: {prompt[:100]}...")

# 1. sill vector search (Lever 2: keywords steer the embedding)
store_memories = query_memories_vector(prompt, keywords=keywords)
memories = store_memories or []
log(f"Found {len(memories)} memories")

# 2. episodic-memory semantic search (optional). Its query goes on a command line, so a
#    long message is asked with the opening of what was typed: 2,000 characters, the
#    most this search was ever given.
conversations = search_episodic_memory(
    typed_body(prompt)[:LONG_MESSAGE_CHARS] if len(prompt) > LONG_MESSAGE_CHARS else prompt)
log(f"Found {len(conversations)} conversations")

# Keep what is printed under the harness's cap before anything is counted as shown.
memories, conversations = fit_to_cap(
    memories, conversations,
    overhead=sum(_units(part) + 2 for part in (_pattern_flag, time_header) if part))

# Output if anything found (or we at least have a time header to inject)
if memories or conversations or time_header:
    context = format_results(memories, conversations)
    # Prepend the temporal header so it frames the recalled memories.
    if time_header:
        context = f"{time_header}\n\n{context}" if context else time_header

    # Build detailed summary for TUI
    tui_lines = [time_header] if time_header else []
    if store_memories is None:
        tui_lines.append(f"[sill] Store recall failed. See {LOG_FILE} for details.")
    if (memories or conversations) and not QUIET_TUI:
        tui_lines.append("[sill] Recalled:")

    if memories and not QUIET_TUI:
        for mem in memories:
            # First sentence as summary (split on . ! ? or newline)
            content = mem.get('content', '')
            first_sent = re.split(r'[.!?\n]', content, maxsplit=1)[0].strip()
            if len(first_sent) > 100:
                first_sent = first_sent[:97] + "..."

            sim = f"{mem['score']:.4f}" if isinstance(mem.get('score'), (int,float)) else '?'
            mtype = mem.get('type', '?')[:4]  # sema, epis, proc, stra
            mid = mem.get('id', '')[:8]

            # Age: how old is this memory?
            created = mem.get('created_at', '')
            age = ""
            if created:
                try:
                    from datetime import datetime as dt, timezone
                    created_date = parse_db_timestamp(created)
                    days = memory_age_days(created_date)
                    if days == 0:
                        age = "today"
                    elif days == 1:
                        age = "yesterday"
                    elif days < 30:
                        age = f"{days}d ago"
                    elif days < 365:
                        age = f"{days//30}mo ago"
                    else:
                        age = f"{days//365}y ago"
                except Exception:
                    age = ""

            age_str = f", {age}" if age else ""
            tui_lines.append(f"  {mtype} ({sim}{age_str}) {first_sent}")

    if conversations and not QUIET_TUI:
        for conv in conversations:
            snippet = conv.get('content', '')[:80].replace('\n', ' ')
            date = conv.get('date', '?')
            tui_lines.append(f"  conv ({date}) {snippet}")

    system_msg = "\n".join(tui_lines)

    # Carry forward last turn's response-pattern warnings (read+consumed above,
    # so both exit paths deliver it exactly once).
    if _pattern_flag:
        context = _pattern_flag + "\n\n" + context

    # JSON output: systemMessage for TUI, additionalContext for the agent
    output = {
        "systemMessage": system_msg,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context
        }
    }
    emitted = False
    def emit(rows):
        global emitted
        if not emitted:
            print(json.dumps(output), flush=True)
            emitted = True
    try:
        record_shown(_sid, memories, 'spontaneous-recall', emit=emit)
    except (OSError, ValueError, TimeoutError):
        emit(memories)
    try:
        core.record_exposure([row['id'] for row in memories], deadline=_SUBPROCESS_DEADLINE)
    except core.RecallUnavailable:
        log('Recall exposure accounting unavailable')

sys.exit(0)
