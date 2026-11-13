#!/usr/bin/env python3
# Ported from agi-memory .claude/hooks/track-reuse.py (2026-08-04).
"""
Reuse tracking hook: Detect when hydrated memories appear in responses.

Triggers on Stop event. Checks if memory IDs or body phrases from recalled
memories appear in the response text, and calls record_memory_reuse() for
matches that survive the guards below.

This provides lexical-use telemetry, not a value verdict. Each accepted
detection is recorded append-only (migration 006) with its detector
version, evidence, session, and the memory's force/speaker; reuse_count
remains a compatibility aggregate.

Codex transcript records split an MCP call's name into namespace + name
instead of leaving it flat (see get_recalled_memories below); joined via
_harness.join_mcp_name so the reconstructed name carries the "__"
separator a bare f"{namespace}{name}" concatenation dropped (that
concatenation produced "mcp__sillrecall_batch" and only worked at all
because the downstream filter used a substring test for "recall"/
"hydrate"). Fails open — exits 0, no output — if _harness itself cannot
be imported.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path
import _store

try:
    import psycopg2
except ModuleNotFoundError:
    # install.sh wires hook commands to run under the backend's interpreter,
    # which has psycopg2. If this hook is somehow launched under an interpreter
    # that lacks it, degrade to a no-op rather than crashing the agent's Stop
    # event — consistent with how the rest of this hook treats failures as
    # non-fatal.
    psycopg2 = None

LOG_FILE = Path(os.environ.get("SILL_LOG_DIR", "/tmp")) / "reuse-tracking.log"

# Database connection
DB_HOST = os.environ.get("POSTGRES_HOST", "localhost")
DB_PORT = os.environ.get("POSTGRES_PORT", "5432")
DB_NAME = os.environ.get("POSTGRES_DB", "sill")
DB_USER = os.environ.get("POSTGRES_USER", "sill")
DB_PASS = os.environ.get("POSTGRES_PASSWORD", "sill_password")


def _load_harness():
    try:
        spec = importlib.util.spec_from_file_location(
            "_sill_harness", Path(__file__).resolve().parent / "_harness.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


_harness = _load_harness()


def log(message: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"{timestamp} | {message}\n")
    except Exception:
        pass


def get_db_connection():
    if psycopg2 is None:
        log("psycopg2 unavailable; skipping reuse tracking")
        if 'SILL_INSTANCE_ENV_FILE' in os.environ:
            print(json.dumps({'systemMessage':'[sill] Reuse store unavailable; reuse unchecked.'}))
        return None
    try:
        if 'SILL_INSTANCE_ENV_FILE' in os.environ:
            return _store.selected_connection(psycopg2.connect)
        return psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
        )
    except Exception as e:
        log("DB connection unavailable")
        print(json.dumps({"systemMessage":"[sill] Reuse store unavailable; reuse unchecked."}))
        return None


def get_response_text(data: dict) -> str:
    """Extract assistant response text from hook data."""
    if isinstance(data.get("last_assistant_message"), str):
        return data["last_assistant_message"]

    if "transcript_path" in data:
        try:
            transcript_path = Path(data["transcript_path"])
            if transcript_path.exists():
                with open(transcript_path, "r") as f:
                    lines = f.readlines()
                for line in reversed(lines):
                    try:
                        entry = json.loads(line.strip())
                        if entry.get("type") == "assistant":
                            message = entry.get("message", {})
                            content = message.get("content", [])
                            if isinstance(content, str):
                                return content
                            elif isinstance(content, list):
                                return " ".join(
                                    block.get("text", "")
                                    for block in content
                                    if isinstance(block, dict)
                                    and block.get("type") == "text"
                                )
                        payload = entry.get("payload", {})
                        if (
                            entry.get("type") == "response_item"
                            and isinstance(payload, dict)
                            and payload.get("type") == "message"
                            and payload.get("role") == "assistant"
                        ):
                            content = payload.get("content", [])
                            if isinstance(content, list):
                                return " ".join(
                                    block.get("text", "")
                                    for block in content
                                    if isinstance(block, dict) and block.get("type") == "output_text"
                                )
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            log(f"Error reading transcript_path: {e}")
    return ""


def get_recalled_memories(data: dict) -> list[dict]:
    """Extract memories from recall/hydrate tool results in this turn.

    Transcript shape (what Claude Code actually writes):
    - Top-level entries have type "user" or "assistant"
    - Tool USES are content blocks inside assistant messages: type=tool_use, id, name
    - Tool RESULTS are content blocks inside user messages: type=tool_result, tool_use_id, content
    - "Real" user typed messages have content as a string OR a list with text blocks

    We walk backwards collecting tool_use (id->name) and tool_result (use_id, content_str),
    stopping at the previous real user turn. Then match by use_id and extract memories
    from results whose tool name contains "recall" or "hydrate".
    """
    memories = []

    transcript_path = data.get("transcript_path")
    if not transcript_path:
        return memories

    try:
        with open(transcript_path, "r") as f:
            lines = f.readlines()

        tool_uses: dict[str, str] = {}  # tool_use_id -> tool_name
        tool_results: list[tuple[str, str]] = []  # (tool_use_id, content_str)

        for line in reversed(lines):
            try:
                entry = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type", "")
            if entry_type not in ("user", "assistant"):
                continue

            message = entry.get("message", {})
            content = message.get("content", None)

            if entry_type == "user":
                # String content = real typed user input — end of turn going back
                if isinstance(content, str):
                    break
                if isinstance(content, list):
                    has_real_text = False
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type", "")
                        if btype == "tool_result":
                            tool_results.append(
                                (block.get("tool_use_id", ""), block.get("content", ""))
                            )
                        elif btype == "text":
                            # Real user text block (not tool_result) — boundary
                            has_real_text = True
                    if has_real_text:
                        break
            elif entry_type == "assistant":
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_uses[block.get("id", "")] = block.get("name", "")

        # Codex transcript shape: response_item/function_call and
        # response_item/function_call_output keyed by call_id.
        for line in reversed(lines):
            try:
                entry = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            payload = entry.get("payload", {})
            if not isinstance(payload, dict) or entry.get("type") != "response_item":
                continue

            if (
                payload.get("type") == "message"
                and payload.get("role") == "user"
                and payload.get("content")
            ):
                break

            if payload.get("type") == "function_call":
                tool_uses[payload.get("call_id", "")] = _harness.join_mcp_name(payload) or ""
            elif payload.get("type") == "function_call_output":
                tool_results.append((payload.get("call_id", ""), payload.get("output", "")))

        # Match results to uses; extract from recall/hydrate results
        for tool_use_id, result_content in tool_results:
            tool_name = tool_uses.get(tool_use_id, "")
            if not ("recall" in tool_name or "hydrate" in tool_name):
                continue

            # Result content can be a JSON string or a list of content blocks
            parsed = parse_tool_result(result_content)
            if parsed is None:
                continue

            # Memories may be under "memories", "previews", or be top-level list
            mem_list = []
            if isinstance(parsed, dict):
                mem_list = parsed.get("memories") or parsed.get("previews") or []
            elif isinstance(parsed, list):
                mem_list = parsed

            for mem in mem_list:
                if isinstance(mem, dict) and "id" in mem:
                    text = mem.get("content") or mem.get("preview") or ""
                    memories.append({"id": mem["id"], "content": text[:200]})

    except Exception as e:
        log(f"Error extracting recalled memories: {e}")

    return memories


def parse_tool_result(result_content):
    """Parse Claude or Codex tool-result payloads into JSON when possible."""
    raw = result_content
    if isinstance(raw, list):
        raw = " ".join(
            b.get("text", "")
            for b in raw
            if isinstance(b, dict) and b.get("type") == "text"
        )
    if not isinstance(raw, str):
        return None

    candidates = [raw]
    if "\nOutput:\n" in raw:
        candidates.append(raw.rsplit("\nOutput:\n", 1)[1].strip())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            text = parsed[0].get("text")
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return parsed
        return parsed

    return None


# Three guards against false-positive reuse detection. A detector that samples
# phrases from the content HEAD is exactly what a citation, or a near-twin
# memory's shared framing, reproduces — so merely quoting a memory (rather
# than reusing it) would stamp it "reused," and near-twins would co-fire on
# text they share by construction, not by either one being used.
HEAD_SKIP_WORDS = 6   # the head of a memory is what a citation reproduces;
                      # sample evidence from the body instead.
BURST_LIMIT = 3       # more than this many stamps in one Stop event is a
                      # citation sweep, not that many genuine reuse events —
                      # stamp nothing.
DETECTOR_VERSION = "track-reuse/body-v2+guards/events-v1"


def find_reuse_phrase(memory: dict, response: str):
    """Return evidence that a memory was reused: "__ID__" for an id mention, a
    matched BODY phrase, or None. Guard 1: phrases are sampled from beyond the
    first HEAD_SKIP_WORDS words only — a memory too short to have a body yields
    no phrase evidence (id mention still counts)."""
    mem_id = memory.get("id", "")
    content = memory.get("content", "")

    if mem_id and mem_id in response:
        return "__ID__"

    if content:
        words = content.split()
        start = HEAD_SKIP_WORDS
        if len(words) - start >= 3:
            response_lower = response.lower()
            for i in range(start, min(len(words) - 2, start + 20)):
                phrase = " ".join(words[i : i + 3])
                # Skip very common/short phrases
                if len(phrase) > 15 and phrase.lower() in response_lower:
                    return phrase

    return None


def detect_reuse(memories: list[dict], response_lower: str) -> list[tuple[str, str, str]]:
    """Pure guard pipeline over an already-recalled memory list and an
    already-lowercased response: sample evidence per memory (guard 1, inside
    find_reuse_phrase), reject phrases that are a title rather than evidence
    (guard 2), then zero the whole batch if it looks like a citation sweep
    (guard 3). Returns (memory_id, evidence, channel) tuples for reuse that
    survived every guard.
    """
    candidates = []
    for mem in memories:
        evidence = find_reuse_phrase(mem, response_lower)
        if evidence:
            candidates.append((mem, evidence))

    def _phrase_owner_count(phrase: str) -> int:
        # A phrase shared by two memories is a title, not evidence — a
        # near-twin's boilerplate framing must not co-fire as "used."
        p = phrase.lower()
        return sum(1 for m in memories if p in (m.get("content") or "").lower())

    reused = []
    for mem, evidence in candidates:
        if evidence != "__ID__" and _phrase_owner_count(evidence) >= 2:
            log(f"Guard-2 reject: phrase {evidence!r} shared by >=2 recalled memories ({str(mem.get('id', ''))[:8]})")
            continue
        reused.append((mem["id"], evidence, mem.get("channel") or "mcp-tool-result"))

    if len(reused) > BURST_LIMIT:
        log(f"Guard-3 burst: {len(reused)} memories flagged in one Stop event — citation sweep, stamping nothing")
        reused = []

    return reused


def _evidence_kind(evidence: str) -> str:
    return "memory_id" if evidence == "__ID__" else "body_phrase"


def touch_memory_reuse(conn, memory_id: str, evidence: str, session_id: str,
                        channel: str = "mcp-tool-result") -> bool:
    """Record one guard-approved lexical reuse observation.

    channel = how the memory arrived in context (spontaneous-recall,
    mcp-tool-result, or any other sidecar-reported source), persisted into
    metadata so usage telemetry can be partitioned by arrival channel."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT record_memory_reuse(%s::uuid, %s, %s, %s, %s, %s::jsonb)",
                (
                    memory_id,
                    DETECTOR_VERSION,
                    _evidence_kind(evidence),
                    None if evidence == "__ID__" else evidence,
                    session_id or None,
                    json.dumps({"channel": channel}),
                ),
            )
            conn.commit()
            return True
    except Exception as e:
        log("Reuse accounting unavailable")
        conn.rollback()
        return False


def read_recall_sidecars(session_id: str, recent_window_minutes: int = 5) -> list[dict]:
    """Read this session's bounded public recall journal, time-gated to five minutes.

    Never use an unscoped recent bucket: exposure in another session is not
    evidence of context supplied to this assistant.
    """
    out = []
    seen_ids = set()
    try:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        window = timedelta(minutes=recent_window_minutes)
    except Exception:
        return out

    log_dir = Path(os.environ.get("SILL_LOG_DIR", "/tmp"))
    candidate_paths = []
    sid = _harness.hook_session_path_key(session_id)
    if sid:
        candidate_paths.append((log_dir / f"recall-sidecar-{sid}.jsonl", True))

    for path, time_gated in candidate_paths:
        if not path.exists():
            continue
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if time_gated:
                        try:
                            ts = datetime.fromisoformat(entry.get("ts", "").replace("Z", "+00:00"))
                            if now - ts > window:
                                continue
                        except Exception:
                            continue
                    for m in entry.get("memories", []) or []:
                        mid = m.get("id", "")
                        if not mid or mid in seen_ids:
                            continue
                        seen_ids.add(mid)
                        # Carry the arrival channel through to the stamp — the
                        # sidecar knows its own source; losing it here would
                        # leave every reuse event channel-less.
                        out.append({"id": mid, "content": m.get("content", ""),
                                    "channel": entry.get("source") or "sidecar"})
        except Exception:
            continue
    return out


def main():
    if _harness is None:
        sys.exit(0)
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)
    if not isinstance(input_data, dict):
        # Valid JSON that isn't an object (e.g. a bare array) is exactly as
        # unusable as invalid JSON — the log() call two lines down does
        # input_data.get(...), which raises AttributeError on a list.
        sys.exit(0)

    # Unconditional fire-trace so we can confirm the hook runs at all in production.
    log(f"hook invoked, transcript_path={input_data.get('transcript_path', 'MISSING')}")

    response_text = get_response_text(input_data)
    recalled_memories = get_recalled_memories(input_data)
    session_id = _harness.hook_session_id(input_data, env=os.environ)

    # Merge in sidecar-surfaced memories (recall paths with no MCP tool call
    # for this hook to scan) without duplicating anything already found via
    # MCP tool_results.
    sidecar_memories = read_recall_sidecars(session_id)
    if sidecar_memories:
        log(f"Sidecar surfaced {len(sidecar_memories)} memories (beyond MCP tool_results)")
        existing_ids = {m.get("id", "") for m in recalled_memories}
        for m in sidecar_memories:
            if m["id"] not in existing_ids:
                recalled_memories.append(m)

    log(f"response_text len={len(response_text)}, recalled_memories n={len(recalled_memories)}")

    if not response_text or not recalled_memories:
        sys.exit(0)

    log(f"Checking {len(recalled_memories)} recalled memories for reuse")

    reused = detect_reuse(recalled_memories, response_text.lower())

    if reused:
        log(f"Found {len(reused)} reused memories")

        # Update database. No docker fallback: if psycopg2 is unavailable or
        # the connection fails, get_db_connection() has already logged why,
        # and reuse tracking is skipped for this Stop event rather than
        # crashing it.
        conn = get_db_connection()
        if conn:
            for mem_id, evidence, channel in reused:
                if touch_memory_reuse(conn, mem_id, evidence, session_id, channel):
                    log(f"Recorded reuse: {mem_id} ({_evidence_kind(evidence)}, via {channel})")
            conn.close()
    else:
        log("No memories reused in response")

    sys.exit(0)


if __name__ == "__main__":
    main()
