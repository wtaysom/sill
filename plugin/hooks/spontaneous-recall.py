#!/usr/bin/env python3
# Ported from agi-memory .claude/hooks/spontaneous-recall.py (2026-08-04).
"""
Hook: Spontaneous memory recall on UserPromptSubmit.
Queries sill (Postgres + pgvector) for relevant context before the agent responds.

Protocol: Think -> Recall -> Think more -> Remember
This hook handles the "Recall" step by injecting relevant memories.

Queries two sources:
1. sill (hybrid_recall / fast_recall) - semantic memories via vector + full-text search
2. episodic-memory (optional) - conversation history via keyword search
   Enabled only when SILL_EPISODIC_MEMORY_PATH is set and the CLI exists.
"""
import json
import sys
import re
import subprocess
import os
from datetime import datetime
from pathlib import Path

_SILL_LOG_DIR = Path(os.environ.get("SILL_LOG_DIR", "/tmp"))
LOG_FILE = _SILL_LOG_DIR / "spontaneous-recall.log"
def _env_int(name: str, default: int) -> int:
    """Read an int tunable from the environment, ignoring unusable values.

    A bad value must not take recall down: this hook runs on every prompt, and
    a traceback here costs the operator their whole turn. Fall back silently.
    """
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


# All five were hardcoded. They are the only levers an operator has when
# recall is technically working and practically noise — which is the normal
# early state, since a young store answers every query out of whatever it
# happens to contain. Defaults are unchanged.
MAX_MEMORIES = _env_int("SILL_RECALL_MAX_MEMORIES", 5)
MAX_CONVERSATIONS = _env_int("SILL_RECALL_MAX_CONVERSATIONS", 2)
MIN_QUERY_LENGTH = _env_int("SILL_RECALL_MIN_QUERY_LENGTH", 20)
FAST_MIN_SIMILARITY = _env_float("SILL_RECALL_MIN_SIMILARITY", 0.25)
HYBRID_MIN_SIMILARITY = 0.0  # hybrid_recall returns small RRF scores, not cosine similarity

# Suppress the recalled-memory list from the TUI systemMessage without
# touching what reaches the model. The two are separate payloads (see the
# output block at the end of this file), so an operator who finds the display
# distracting does not have to weaken recall itself to stop seeing it. The
# [TIME] header is kept — it is one line and it is the only visible evidence
# that the hook ran at all.
QUIET_TUI = os.environ.get("SILL_RECALL_QUIET", "").strip().lower() in ("1", "true", "yes")
MIN_TOPICAL_KEYWORDS = 3  # Lever 1: skip process-only messages
OPERATIONAL_TERMS = {
    "cli",
    "codex",
    "config",
    "episodic-memory",
    "hook",
    "hooks",
    "json",
    "mcp",
    "pretooluse",
    "server",
    "spontaneous",
    "sync",
    "toml",
    "userpromptsubmit",
}

# DB connection (overridable via env vars)
DB_CONTAINER = os.environ.get("SILL_DB_CONTAINER", "sill_db")
DB_USER = os.environ.get("SILL_DB_USER", "sill")
DB_NAME = os.environ.get("SILL_DB_NAME", "sill")

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
    """Append timestamped entry to log file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"{timestamp} | {message}\n")
    except Exception:
        pass

def should_use_hybrid_recall(prompt: str, keywords: list[str] | None = None) -> bool:
    """Use hybrid recall for operational/config prompts where exact terms matter."""
    terms = set(re.findall(r"[a-z0-9-]+", prompt.lower()))
    if keywords:
        terms.update(k.lower() for k in keywords)
    return len(terms & OPERATIONAL_TERMS) >= 2

def build_query_text(prompt: str, keywords: list[str], use_hybrid: bool) -> str:
    """Build a recall query shaped for the selected retrieval mode."""
    if not use_hybrid:
        if keywords:
            return ' '.join(keywords) + '. ' + prompt[:500]
        return prompt[:500]

    focus = []
    seen = set()
    for kw in keywords:
        low = kw.lower()
        if low in OPERATIONAL_TERMS and low not in seen:
            seen.add(low)
            focus.append(kw)

    expansions = []
    if "codex" in seen and ("hook" in seen or "hooks" in seen):
        expansions.extend(["hook", "parity", "setup", "UserPromptSubmit", "Stop", "PreToolUse", "config"])
    if "episodic-memory" in seen:
        expansions.extend(["MCP", "sync"])

    for term in expansions:
        low = term.lower()
        if low not in seen:
            seen.add(low)
            focus.append(term)

    if focus:
        return ' '.join(focus)
    return prompt[:500]

def query_memories_vector(prompt: str, keywords: list[str] | None = None) -> list[dict]:
    """Query Postgres using the recall path best suited to the prompt.

    If keywords are provided, prepends them to the prompt to steer
    recall toward topical content rather than surface tone.
    """
    use_hybrid = should_use_hybrid_recall(prompt, keywords)
    query_text = build_query_text(prompt, keywords or [], use_hybrid)
    log(f"Recall mode: {'hybrid' if use_hybrid else 'fast'}, query: {query_text[:120]}")

    # Escape single quotes for SQL
    escaped = query_text.replace("'", "''")

    # content LAST so delimiter collisions only mangle content, not metadata
    if use_hybrid:
        # hybrid_recall is helpful for operational/config prompts with exact
        # tool names, but its RRF score is not a cosine similarity.
        query = f"""
        SELECT memory_id::text, memory_type::text,
               round(score::numeric, 4) as similarity,
               round(importance::numeric, 2) as imp,
               created_at::text,
               source,
               COALESCE(ref, ''),
               LEFT(content, 500)
        FROM (
            SELECT hr.memory_id, hr.content, hr.memory_type,
                   hr.score, hr.source, m.importance, m.created_at,
                   m.source_attribution->>'ref' AS ref
            FROM hybrid_recall('{escaped}', {MAX_MEMORIES}, 60) hr
            JOIN memories m ON hr.memory_id = m.id
        ) sub
        WHERE score >= {HYBRID_MIN_SIMILARITY}
        ORDER BY score DESC
        LIMIT {MAX_MEMORIES};
        """
    else:
        query = f"""
        SELECT memory_id::text, memory_type::text,
               round((score * 0.7 + importance * 0.3)::numeric, 3) as similarity,
               round(importance::numeric, 2) as imp,
               created_at::text,
               source,
               COALESCE(ref, ''),
               LEFT(content, 500)
        FROM (
            SELECT fr.memory_id, m.content, m.type as memory_type,
                   fr.score, fr.source, m.importance, m.created_at,
                   m.source_attribution->>'ref' AS ref
            FROM fast_recall('{escaped}', {MAX_MEMORIES * 3}) fr
            JOIN memories m ON fr.memory_id = m.id
            WHERE fr.score >= {FAST_MIN_SIMILARITY}
        ) sub
        ORDER BY (score * 0.7 + importance * 0.3) DESC
        LIMIT {MAX_MEMORIES};
        """

    try:
        result = subprocess.run(
            ['docker', 'exec', DB_CONTAINER, 'psql', '-U', DB_USER, '-d', DB_NAME,
             '-t', '-A', '-F', '|||', '-c', query],
            capture_output=True, text=True, timeout=15  # Longer timeout for embedding
        )

        if result.returncode != 0:
            log(f"Query failed: {result.stderr}")
            return []

        memories = []
        for line in result.stdout.strip().split('\n'):
            if line and '|||' in line:
                parts = line.split('|||')
                if len(parts) >= 8:
                    # content is last — rejoin in case it contained |||
                    memories.append({
                        'id': parts[0],
                        'type': parts[1],
                        'similarity': parts[2],
                        'importance': parts[3],
                        'created_at': parts[4],
                        'source': parts[5],
                        'ref': parts[6],
                        'content': '|||'.join(parts[7:])[:500],
                    })

        return memories

    except subprocess.TimeoutExpired:
        log("Query timed out")
        return []
    except Exception as e:
        log(f"Error querying memories: {e}")
        return []

def extract_keywords(prompt: str) -> list[str]:
    """Extract meaningful keywords from prompt for memory search.

    Prioritizes: proper nouns > technical terms > hyphenated compounds > other.
    This matters because 'Aaron' (5 chars) is more useful for recall than
    'regularities' (13 chars).
    """
    stop_words = {
        'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'dare',
        'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from', 'as',
        'into', 'through', 'during', 'before', 'after', 'above', 'below',
        'between', 'under', 'again', 'further', 'then', 'once', 'here',
        'there', 'when', 'where', 'why', 'how', 'all', 'each', 'few',
        'more', 'most', 'other', 'some', 'such', 'no', 'nor', 'not',
        'only', 'own', 'same', 'so', 'than', 'too', 'very', 'just',
        'and', 'but', 'if', 'or', 'because', 'until', 'while', 'about',
        'what', 'which', 'who', 'whom', 'this', 'that', 'these', 'those',
        'am', 'it', 'its', 'they', 'them', 'their', 'we', 'us', 'our',
        'you', 'your', 'he', 'him', 'his', 'she', 'her', 'i', 'me', 'my',
        'also', 'like', 'think', 'know', 'want', 'see', 'look', 'make',
        'get', 'go', 'come', 'take', 'use', 'find', 'give', 'tell', 'say',
        'try', 'let', 'keep', 'put', 'seem', 'help', 'show', 'hear', 'play',
        'run', 'move', 'live', 'believe', 'hold', 'bring', 'happen', 'write',
        'provide', 'sit', 'stand', 'lose', 'pay', 'meet', 'include', 'continue',
        'set', 'learn', 'change', 'lead', 'understand', 'watch', 'follow',
        'really', 'actually', 'something', 'anything', 'everything', 'nothing',
        'already', 'still', 'even', 'much', 'well', 'back', 'now', 'going',
        'right', 'sure', 'yeah', 'okay', 'good', 'great', 'first', 'last',
        'things', 'stuff', 'way', 'kind', 'sort', 'bit', 'lot', 'part',
        'thing', 'done', 'start', 'work', 'working', 'check', 'commit',
        'fix', 'update', 'add', 'remove', 'test', 'debug', 'push', 'pull',
        'read', 'don', 'doesn', 'didn', 'won', 'isn', 'aren', 'wasn',
        'haven', 'hasn', 'hadn', 'wouldn', 'couldn', 'shouldn',
        'break', 'breaks', 'broke', 'restart', 'stop', 'file', 'files',
        'better', 'worse', 'maybe', 'probably', 'definitely', 'basically',
        'whole', 'hog', 'order', 'instead', 'rather', 'simply', 'exactly',
        'making', 'says', 'said', 'called', 'need', 'needs', 'needed',
        'looks', 'looking', 'getting', 'doing', 'having', 'being',
        'using', 'trying', 'running', 'going', 'coming', 'taking',
    }

    # Find proper nouns (capitalized words not at sentence start)
    proper_nouns = set()
    sentences = re.split(r'[.!?]\s+', prompt)
    for sent in sentences:
        words_in_sent = sent.split()
        for j, w in enumerate(words_in_sent):
            if j > 0 and w[0:1].isupper() and w.lower() not in stop_words:
                clean = re.sub(r'[^a-zA-Z0-9-]', '', w)
                if len(clean) > 1:
                    proper_nouns.add(clean)

    # Find hyphenated compounds and acronyms (high signal)
    compounds = re.findall(r'\b[a-zA-Z]+-[a-zA-Z]+(?:-[a-zA-Z]+)*\b', prompt)
    acronyms = re.findall(r'\b[A-Z]{2,}\b', prompt)

    # Extract all words for regular keyword filtering
    all_words = re.findall(r'\b[a-zA-Z][a-zA-Z0-9-]*[a-zA-Z0-9]\b|\b[a-zA-Z]\b', prompt.lower())
    regular = [w for w in all_words if w not in stop_words and len(w) > 2]

    # Build priority-ordered list: proper nouns first, then compounds/acronyms, then rest
    result = []
    seen = set()

    for group in [proper_nouns, set(acronyms), set(compounds)]:
        for w in group:
            low = w.lower()
            if low not in seen and low not in stop_words:
                seen.add(low)
                result.append(w)

    for w in regular:
        if w not in seen:
            seen.add(w)
            result.append(w)

    return result[:8]  # More keywords for richer queries

def search_episodic_memory(query: str) -> list[dict]:
    """Search conversation archive using episodic-memory CLI (semantic search with indexing).

    No-op when SILL_EPISODIC_MEMORY_PATH is unset or points to a missing file.
    """
    if EPISODIC_CLI is None or not EPISODIC_CLI.exists():
        return []

    try:
        env = os.environ.copy()
        env["EPISODIC_MEMORY_CONFIG_DIR"] = str(SUPERPOWERS_DIR)
        env["EPISODIC_MEMORY_READONLY"] = "1"

        result = subprocess.run(
            [str(EPISODIC_CLI), 'search', query, '--limit', str(MAX_CONVERSATIONS)],
            capture_output=True,
            text=True,
            timeout=30,  # Longer timeout for semantic search
            env=env,
        )

        if result.returncode != 0:
            log(f"Episodic search failed: {result.stderr}")
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
        log(f"Episodic search error: {e}")
        return []

def format_results(memories: list[dict], conversations: list[dict]) -> str:
    """Format all results for injection as context."""
    lines = []

    if memories:
        # Log recalled memory IDs for importance feedback (Lever 3)
        ids = [mem.get('id', '?')[:8] for mem in memories]
        log(f"Recalled IDs: {ids}")

        lines.append("[SPONTANEOUS RECALL] Relevant memories:")
        for i, mem in enumerate(memories, 1):
            content = mem['content'].replace('\n', ' ')
            sim = mem.get('similarity', '?')
            imp = mem.get('importance', '?')
            mid = mem.get('id', '')[:8]
            # The address, when the memory has one: a recalled paraphrase can
            # drift from its source with every retelling, and re-reading the
            # source is the only correction. Advice to open the file is
            # worthless without the file, so hand it over here, ahead of the
            # content.
            ref = (mem.get('ref') or '').strip()
            src = f" src={ref}" if ref else ""
            lines.append(f"  {i}. [{mem['type']}, sim={sim}, imp={imp}, id={mid}{src}] {content}")

    if conversations:
        lines.append("[CONVERSATION HISTORY] Related past discussions:")
        for i, conv in enumerate(conversations, 1):
            content = conv['content'].replace('\n', ' ')
            lines.append(f"  {i}. [past conversation] {content}")

    if lines:
        lines.append("[Consider how these connect to the current conversation.]")
        return '\n'.join(lines)

    return ""

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
    SELECT COALESCE((SELECT ps FROM prev), ''),
           COALESCE(EXTRACT(EPOCH FROM (now() - (SELECT pt FROM prev)))::bigint, -1),
           (SELECT prompt_count FROM up);
    """
    try:
        r = subprocess.run(
            ['docker', 'exec', DB_CONTAINER, 'psql', '-U', DB_USER, '-d', DB_NAME,
             '-t', '-A', '-F', '|||', '-c', q],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode != 0:
            return None
        line = r.stdout.strip().split('\n')[0]
        parts = line.split('|||')
        if len(parts) < 3:
            return None
        return {"prev_session": parts[0], "gap_seconds": int(parts[1]), "count": int(parts[2])}
    except Exception:
        return None


def build_time_header(session_id: str, project: str) -> str:
    """A compact 1-line temporal header: wall-clock (for day/night legibility)
    plus sequence/gap since the last exchange."""
    now = datetime.now().astimezone()
    clock = now.strftime("%a %Y-%m-%d %H:%M %Z")
    info = track_session_time(session_id, project)
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
# lock — nor is the 2000-char recall length-gate below a lock at all: a short
# prompt could otherwise switch injection on silently.
_entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli")
_interactive = bool(os.environ.get("SILL_INTERACTIVE"))
_headless_entrypoint = _entrypoint.startswith("sdk")
_detached_beat = bool(os.environ.get("SILL_DETACHED_BEAT"))
if os.environ.get("SILL_HEADLESS_TOOL") or (
        not _interactive and (_headless_entrypoint or _detached_beat)):
    sys.exit(0)

prompt = input_data.get("prompt", "")
if not prompt.strip():
    sys.exit(0)

# A "System:"-tagged prompt is not a person talking — no header, no recall.
if prompt.lstrip().startswith("System:"):
    sys.exit(0)

# Time header: advance the activity clock + compute deltas for EVERY genuine
# prompt — including short ones like "yes" or "ok". The clock must not drift,
# and the header should show even when there's nothing to recall (a short
# message hitting the recall length-gate below should not also drop the
# header). RECALL is gated separately below; the clock is not. Identity uses
# CLAUDE_CODE_SESSION_ID (stable across the conversation, compaction
# included), not the payload session_id (random for headless --print fires).
def _stable_project(raw: str) -> str:
    """Normalize to the git toplevel so project identity survives cwd drift.
    The payload cwd follows the invoking shell (a `cd` into a subdirectory
    mid-session can mint a phantom project row keyed on that subdirectory),
    so prefer CLAUDE_PROJECT_DIR (the launch dir, immune to cd) and fold any
    path inside a repo to the repo root."""
    try:
        r = subprocess.run(["git", "-C", raw, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=3)
        top = r.stdout.strip()
        if r.returncode == 0 and top:
            return top
    except Exception:
        pass
    return raw


_project = _stable_project(os.environ.get("CLAUDE_PROJECT_DIR")
                           or input_data.get("cwd") or os.getcwd())
_sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or input_data.get("session_id", "")
time_header = build_time_header(_sid, _project)


def _read_pattern_carry_forward(session_id: str) -> str:
    """Read (and consume) last turn's response-pattern warnings for this session.

    response-patterns.py is a Stop hook: it fires after the reply is already on
    screen, so it cannot prevent anything. It stashes its warnings here; this
    hook surfaces them at the top of the next turn, before any token is
    generated. Deleted on read so a warning is delivered exactly once.
    """
    sid = (session_id or os.environ.get("CLAUDE_SESSION_ID", "")).strip()
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
_pattern_flag = _read_pattern_carry_forward(_sid)


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

# Too short to query, or so long the embedding would time out (e.g. a big paste).
if len(prompt) < MIN_QUERY_LENGTH or len(prompt) > 2000:
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
memories = query_memories_vector(prompt, keywords=keywords)
log(f"Found {len(memories)} memories")

# 2. episodic-memory semantic search (optional)
conversations = search_episodic_memory(prompt)
log(f"Found {len(conversations)} conversations")

# Write surfaced-memory-IDs to the reuse-tracking sidecar so a Stop hook can
# detect reuse on this recall path (which produces no MCP tool call to scan).
# Session-keyed when CLAUDE_SESSION_ID env is set; falls back to a 'recent' file.
def _write_recall_sidecar(memories_list, session_id):
    if not memories_list:
        return
    try:
        from datetime import datetime, timezone
        sid = (session_id or os.environ.get("CLAUDE_SESSION_ID", "")).strip()
        path = _SILL_LOG_DIR / (f"recall-sidecar-{sid}.jsonl" if sid else "recall-sidecar-recent.jsonl")
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": "spontaneous-recall",
            "memories": [
                {"id": str(m.get("id", "")), "content": (m.get("content") or "")[:400]}
                for m in memories_list if m.get("id")
            ],
        }
        if not entry["memories"]:
            return
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _touch_access(memories_list):
    """Record only memories emitted into this prompt's model-visible context."""
    ids = list(dict.fromkeys(
        str(memory.get("id", ""))
        for memory in memories_list
        if re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            str(memory.get("id", "")),
        )
    ))
    if not ids:
        return
    idlist = ",".join(f"'{memory_id}'::uuid" for memory_id in ids)
    query = (
        "UPDATE memories "
        "SET access_count = access_count + 1, last_accessed = CURRENT_TIMESTAMP "
        f"WHERE id IN ({idlist});"
    )
    try:
        result = subprocess.run(
            ["docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME,
             "-q", "-c", query],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log(f"Access tracking failed: {result.stderr.strip()}")
    except Exception as exc:
        log(f"Access tracking failed: {type(exc).__name__}: {exc}")


_touch_access(memories)
_write_recall_sidecar(memories, input_data.get("session_id", ""))

# Output if anything found (or we at least have a time header to inject)
if memories or conversations or time_header:
    context = format_results(memories, conversations)
    # Prepend the temporal header so it frames the recalled memories.
    if time_header:
        context = f"{time_header}\n\n{context}" if context else time_header

    # Build detailed summary for TUI
    tui_lines = [time_header] if time_header else []
    if (memories or conversations) and not QUIET_TUI:
        tui_lines.append("[sill] Recalled:")

    if memories and not QUIET_TUI:
        for mem in memories:
            # First sentence as summary (split on . ! ? or newline)
            content = mem.get('content', '')
            first_sent = re.split(r'[.!?\n]', content, maxsplit=1)[0].strip()
            if len(first_sent) > 100:
                first_sent = first_sent[:97] + "..."

            sim = mem.get('similarity', '?')
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
    print(json.dumps(output))

sys.exit(0)
