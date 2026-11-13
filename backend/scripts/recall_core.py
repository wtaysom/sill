"""Shared recall transport and deterministic fusion for packaged hook callers.

The fast floor is actual query cosine, not the historical blended fast score.
Public scores keep their SQL meaning; RRF affects ordering only. No provenance
is inferred. Callers record exposure once, after selecting what they display.
"""
from __future__ import annotations

import asyncio
import json
import inspect
import math
import os
import re
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

from instance_config import InstanceConfigError, instance_dsn, load_instance_env

QUERY_TIMEOUT = 15
FUSION_K = 60
FUSED_POOL = 15
MAX_WORKERS = 4
FUSION_TIMEOUT = 20
_deadlines = threading.local()

class RecallUnavailable(RuntimeError):
    """The requested store could not complete a recall (distinct from no matches)."""

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

# Stop words: copied verbatim from spontaneous-recall.py
_STOP_WORDS = {
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


def extract_keywords(text: str, max_keywords: int = 8) -> list[str]:
    """Extract priority-ordered keywords: proper nouns > acronyms > compounds > regular.

    Mirrors spontaneous-recall.py's extract_keywords exactly.
    """
    proper_nouns = set()
    sentences = re.split(r'[.!?]\s+', text)
    for sent in sentences:
        words_in_sent = sent.split()
        for j, w in enumerate(words_in_sent):
            if j > 0 and w[0:1].isupper() and w.lower() not in _STOP_WORDS:
                clean = re.sub(r'[^a-zA-Z0-9-]', '', w)
                if len(clean) > 1:
                    proper_nouns.add(clean)

    compounds = re.findall(r'\b[a-zA-Z]+-[a-zA-Z]+(?:-[a-zA-Z]+)*\b', text)
    acronyms = re.findall(r'\b[A-Z]{2,}\b', text)

    all_words = re.findall(
        r'\b[a-zA-Z][a-zA-Z0-9-]*[a-zA-Z0-9]\b|\b[a-zA-Z]\b',
        text.lower()
    )
    regular = [w for w in all_words if w not in _STOP_WORDS and len(w) > 2]

    result = []
    seen = set()
    for group in [sorted(proper_nouns), sorted(set(acronyms)), sorted(set(compounds))]:
        for w in group:
            low = w.lower()
            if low not in seen and low not in _STOP_WORDS:
                seen.add(low)
                result.append(w)

    for w in regular:
        if w not in seen:
            seen.add(w)
            result.append(w)

    return result[:max_keywords]


def should_use_hybrid_recall(text: str, keywords: list[str] | None = None) -> bool:
    """Use hybrid recall for operational/config prompts where exact terms matter."""
    terms = set(re.findall(r"[a-z0-9-]+", text.lower()))
    if keywords:
        terms.update(k.lower() for k in keywords)
    return len(terms & OPERATIONAL_TERMS) >= 2


def build_query_text(text: str, keywords: list[str], use_hybrid: bool) -> str:
    """Build a recall query shaped for the selected retrieval mode."""
    if not use_hybrid:
        if keywords:
            return ' '.join(keywords) + '. ' + text[:500]
        return text[:500]

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
    return text[:500]


def fused_subqueries(text: str, previous: str = "", keywords: list[str] | None = None) -> list[str]:
    """The distinct queries fused_recall(text, previous, keywords=keywords) runs.

    The hook's own query, the raw text, the previous turn, and keyword-only
    queries; full-text search catches exact names that vectors blur.
    """
    kw = keywords if keywords is not None else extract_keywords(text)
    use_hybrid = should_use_hybrid_recall(text, kw)
    both = (text + "\n" + previous).strip()
    subqueries = [build_query_text(text, kw, use_hybrid), text[:500], previous[:500],
                  " ".join(kw), " ".join(extract_keywords(both))]
    return list(dict.fromkeys(s.strip() for s in subqueries if s and s.strip()))


def previous_turn(transcript_path: str, max_ai: int = 600, max_user: int = 400) -> str:
    """Tail of the last assistant reply and the previous user message, or ''."""
    try:
        path = Path(transcript_path)
        with path.open("rb") as fh:
            fh.seek(max(0, path.stat().st_size - 400_000))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return ""
    ai_text = user_text = ""
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain") or entry.get("isMeta"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(c.get("text", "") for c in content
                            if isinstance(c, dict) and c.get("type") == "text")
        else:
            text = ""
        text = text.strip()
        if not text or text.startswith("<"):
            continue
        if entry.get("type") == "assistant" and not ai_text:
            ai_text = text[-max_ai:]
        elif entry.get("type") == "user" and ai_text and not user_text:
            user_text = text[:max_user]
            break
    parts = []
    if ai_text:
        parts.append(f"[previous reply] {ai_text}")
    if user_text:
        parts.append(f"[previous message] {user_text}")
    return "\n".join(parts)


def focus_snippet(content: str, terms: list[str], head: int = 120, window: int = 140,
                  max_windows: int = 2) -> str:
    """The memory's opening plus windows around later mentions of `terms`.

    A fixed-length preview can cut off qualifying context. Keep the opening
    and bounded windows near the caller's terms; full_contents remains lossless.
    """
    flat = re.sub(r"\s+", " ", content or "").strip()
    if len(flat) <= head + window * max_windows:
        return flat
    low = flat.lower()
    hits = sorted({m.start() for t in terms if len(t) > 2
                   for m in re.finditer(re.escape(t.lower()), low) if m.start() >= head})
    parts, end = [flat[:head]], head
    for hit in hits:
        if hit < end:
            continue
        start = max(end, hit - window // 3)
        parts.append(flat[start:start + window])
        end = start + window
        if len(parts) > max_windows:
            break
    if len(parts) == 1:
        parts.append(flat[head:head + window])
    return " … ".join(parts)


def _literal(value: str) -> str:
    # Standard strings are explicitly enabled for both transports.
    return "'" + value.replace("'", "''") + "'"


def _ids(ids) -> list[str]:
    out = []
    for value in ids:
        try:
            key = str(UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            continue
        if key not in out:
            out.append(key)
    return out


async def _selected_query(payload: dict) -> str:
    import asyncpg
    conn = await asyncpg.connect(payload['dsn'], timeout=min(5, payload['timeout']),
        command_timeout=payload['timeout'], server_settings={
            'statement_timeout': str(max(1, int(payload['timeout'] * 1000))),
            'standard_conforming_strings': 'on', 'search_path':'public,ag_catalog'})
    try:
        async with conn.transaction():
            # Separate statement before the query's snapshot: M5 cutover barrier.
            await conn.execute('SELECT pg_advisory_xact_lock_shared(714014)')
            rows = await conn.fetch(payload['sql'])
            if payload.get('row_arrays'):
                return '\n'.join(json.dumps(list(row), default=str) for row in rows)
            return '\n'.join(str(row[0]) for row in rows)
    finally:
        await conn.close(timeout=1)


def _selected_main() -> None:
    try:
        print(asyncio.run(_selected_query(json.load(sys.stdin))))
    except Exception:
        raise SystemExit(1) from None


def _run_sql(sql: str, *, timeout: float = QUERY_TIMEOUT, deadline: float | None = None,
             row_arrays: bool = False) -> str:
    """Bounded child transport. Explicit file wins exclusively; errors redact DSNs."""
    lane_deadline = getattr(_deadlines, 'value', None)
    if lane_deadline is not None:
        deadline = min(deadline, lane_deadline) if deadline is not None else lane_deadline
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
    if timeout <= 0:
        raise RecallUnavailable('Recall deadline exceeded')
    selected = os.environ.get('SILL_INSTANCE_ENV_FILE')
    if row_arrays and selected is None:
        raise RecallUnavailable('Selected row transport requires an explicit instance')
    try:
        if selected is not None:
            dsn = instance_dsn(load_instance_env(Path(selected)))
            env = {k:v for k,v in os.environ.items()
                   if not k.startswith(('PG','POSTGRES_','SILL_DB_'))}
            result = subprocess.run([sys.executable, '-c',
                'from scripts.recall_core import _selected_main; _selected_main()'],
                input=json.dumps({'dsn':dsn,'sql':sql,'timeout':timeout,'row_arrays':row_arrays}),
                env=env, capture_output=True, text=True, timeout=timeout,
                cwd=str(Path(__file__).resolve().parents[1]))
        else:
            # psql quiet suppresses BEGIN/SET/COMMIT status lines, leaving JSONL only.
            query = ('BEGIN; SET LOCAL standard_conforming_strings=on; '
                     f'SET LOCAL statement_timeout={max(1,int(timeout*1000))}; '
                     'DO $$ BEGIN PERFORM pg_advisory_xact_lock_shared(714014); END $$; '
                     + sql + '; COMMIT;')
            result = subprocess.run(['docker','exec',os.environ.get('SILL_DB_CONTAINER','sill_db'),
                'timeout','--kill-after=1',str(timeout)+'s','psql','-X','-qAt','-v','ON_ERROR_STOP=1',
                '-U',os.environ.get('SILL_DB_USER','sill'),'-d',os.environ.get('SILL_DB_NAME','sill'),
                '-c',query],capture_output=True,text=True,timeout=timeout)
        if result.returncode:
            raise RecallUnavailable('Recall store request failed')
        return result.stdout
    except InstanceConfigError:
        raise RecallUnavailable('Selected recall instance configuration is unavailable') from None
    except (OSError, subprocess.SubprocessError):
        raise RecallUnavailable('Recall store request could not complete') from None


def _json_rows(output: str) -> list[dict]:
    try:
        rows = [json.loads(line) for line in output.splitlines() if line.strip()]
        if any(not isinstance(row,dict) for row in rows):
            raise ValueError
        return rows
    except (ValueError,TypeError):
        raise RecallUnavailable('Recall store returned invalid JSON rows') from None


def parse_memory_rows(output: str, limit: int = 15) -> list[dict]:
    rows = _json_rows(output)
    for row in rows:
        if (not all(isinstance(row.get(k),str) for k in ('id','type','content'))
                or not isinstance(row.get('score'),(int,float))
                or not math.isfinite(row['score'])):
            raise RecallUnavailable('Recall store returned invalid memory fields')
        row['truncated'] = bool(row.get('truncated')) or len(row['content']) > 500
        row['content'] = row['content'][:500]
    return rows[:limit]


def recall_rows(fn: str, query_text: str, limit: int = 15, *,
                fast_min_similarity: float = .25) -> list[dict]:
    if not query_text.strip() or limit <= 0:
        return []
    if fn not in {'fast','hybrid'}:
        raise ValueError('recall lane must be fast or hybrid')
    if not math.isfinite(fast_min_similarity):
        raise ValueError('fast_min_similarity must be finite')
    query = _literal(query_text[:1000])
    limit = min(int(limit), 100)
    # Evaluate a real cosine independently. fast_recall.score already blends
    # similarity/importance/recency; a second blend or score floor is incorrect.
    cosine = '1 - (m.embedding <=> q.embedding)' if fn == 'fast' else 'NULL::float'
    cte = f"WITH q AS MATERIALIZED (SELECT public.get_embedding({query}, 'query') AS embedding)" if fn == 'fast' else ''
    lateral = ' CROSS JOIN q' if fn == 'fast' else ''
    call = f'public.fast_recall({query}, {limit * 3})' if fn == 'fast' else f'public.hybrid_recall({query}, {limit}, 60)'
    floor = f'WHERE query_similarity >= {float(fast_min_similarity)}' if fn == 'fast' else ''
    sql = f"""{cte}
      SELECT row_to_json(preview) FROM (
        SELECT * FROM (
          SELECT m.id::text AS id, m.type::text AS type, left(m.content,500) AS content,
            round(fr.score::numeric, 4) AS score, {cosine} AS query_similarity,
            m.importance, m.created_at::text, fr.source,
            COALESCE(NULLIF(m.source_attribution->>'ref',''),m.source_attribution->>'path') AS ref,
            m.source_attribution->>'source_store' AS source_store,
            m.force::text, m.speaker, m.production, m.minted_in,
            length(m.content)>500 AS truncated
          FROM {call} fr JOIN public.memories m ON m.id=fr.memory_id{lateral}
          WHERE m.archived_at IS NULL
        ) candidates {floor} ORDER BY score DESC, id LIMIT {limit}
      ) preview"""
    return parse_memory_rows(_run_sql(sql), limit)


def full_contents(ids: list[str], *, deadline: float | None = None) -> dict[str,str]:
    """Lossless display hydration; the displaying caller records exposure once."""
    valid = _ids(ids)
    if not valid:
        return {}
    sql = "SELECT row_to_json(r) FROM (SELECT id::text,content FROM public.memories WHERE id IN (" + ','.join(_literal(i)+'::uuid' for i in valid) + ') AND archived_at IS NULL) r'
    rows = _json_rows(_run_sql(sql, deadline=deadline))
    if any(not isinstance(r.get('id'),str) or not isinstance(r.get('content'),str) for r in rows):
        raise RecallUnavailable('Recall store returned invalid content rows')
    return {r['id']:r['content'] for r in rows}


def record_exposure(ids, timeout: int = 10, *, deadline: float | None = None) -> None:
    """Count unique displayed IDs, without refreshing recency. Failures are typed."""
    valid = _ids(ids)
    if valid:
        _run_sql('UPDATE public.memories SET access_count=COALESCE(access_count,0)+1 WHERE id IN ('
                 + ','.join(_literal(i)+'::uuid' for i in valid)+')', timeout=timeout, deadline=deadline)


def fused_recall(text: str, previous: str = '', fetch=recall_rows,
                 keywords: list[str] | None = None, limit: int = 5,
                 fast_min_similarity: float = .25, *, deadline: float | None = None) -> list[dict]:
    """Fuse distinct lanes with RRF k=60, retaining the selected row's score.

    Production uses at most four bounded child transports and one overall
    deadline. An injected fetch is a synchronous cooperative offline seam:
    callers must return; arbitrary Python callbacks cannot safely be cancelled.
    """
    if not text.strip() or limit <= 0:
        return []
    if deadline is not None and deadline <= time.monotonic():
        raise RecallUnavailable('Recall deadline exceeded')
    jobs = [(fn,q) for q in fused_subqueries(text,previous,keywords)
            for fn in ('fast','hybrid')]
    options = {'limit': FUSED_POOL, 'fast_min_similarity': fast_min_similarity}
    if fetch is not recall_rows:
        try:
            parameters = inspect.signature(fetch).parameters
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
                options = {key:value for key,value in options.items() if key in parameters}
        except (TypeError, ValueError):
            options = {}  # Historical two-positional-argument injection seam.
    def one(job):
        try:
            rows = fetch(*job, **options)
            if not isinstance(rows,list) or any(not isinstance(r,dict) or not r.get('id') for r in rows):
                raise RecallUnavailable('Invalid recall lane')
            return rows
        except Exception:
            return None
    if fetch is recall_rows:
        deadline = min(deadline, time.monotonic() + FUSION_TIMEOUT) if deadline is not None else time.monotonic() + FUSION_TIMEOUT
        def bounded(job):
            _deadlines.value = deadline
            try:
                return one(job)
            finally:
                del _deadlines.value
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            ranked_lists = list(pool.map(bounded, jobs))
    else:
        ranked_lists = [one(job) for job in jobs]
    if all(rows is None for rows in ranked_lists):
        raise RecallUnavailable('All recall lanes failed')
    scores, records = {}, {}
    for rows in ranked_lists:
        seen = set()
        for row in rows or []:
            mid = row['id']
            if mid in seen:
                continue
            seen.add(mid)
            scores[mid] = scores.get(mid,0) + 1/(FUSION_K + len(seen))
            records.setdefault(mid,row)
    return [records[mid] for mid in sorted(scores,key=lambda mid:(-scores[mid],mid))[:limit]]
