#!/usr/bin/env python3
"""
Factored recall library — extracted from .claude/hooks/spontaneous-recall.py.

Lets the spontaneous-recall logic be called against arbitrary text (e.g.,
transcript messages from past conversations), not just stdin in a hook context.

Use case: R-cycle retrieval-testing. Given a sill memory M, we want to ask
"would M get retrieved when this past conversation message was the prompt?"
That requires running the same recall function against past message text.

Two functions exposed:
- extract_keywords(text) → list of priority-ordered keywords
- query_memories(text, keywords=None, limit=5, min_similarity=None) → list of memory dicts

Container note: the database container is named by $SILL_DB_CONTAINER (default "sill_db").
"""
import os
import re
import subprocess
from typing import Optional

CONTAINER = os.environ.get("SILL_DB_CONTAINER", "sill_db")
DB_USER = os.environ.get("SILL_DB_USER", "sill")
DB_NAME = os.environ.get("SILL_DB_NAME", "sill")
DEFAULT_LIMIT = 5
FAST_MIN_SIMILARITY = 0.25
HYBRID_MIN_SIMILARITY = 0.0
# The historical helper names remain importable; algorithms live in one core.
try:
    from scripts.recall_core import extract_keywords, should_use_hybrid_recall, build_query_text
except ModuleNotFoundError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.recall_core import extract_keywords, should_use_hybrid_recall, build_query_text


def query_memories(
    text: str,
    keywords: Optional[list[str]] = None,
    limit: int = DEFAULT_LIMIT,
    min_similarity: Optional[float] = None,
    *, raise_on_error: bool = False,
) -> list[dict]:
    """Run recall against `text` and return the top ranked memories.

    If keywords is None, extract them automatically from text.
    Returns list of dicts with: id, type, similarity, importance, created_at, source, content.
    Empty list if query fails or nothing matches, unless raise_on_error=True.
    min_similarity is the fast lane's actual query cosine floor; it never
    thresholds a blended score or a hybrid/RRF score. similarity is a legacy
    dictionary alias of score, not cosine evidence.
    """
    try:
        from scripts import recall_core
    except ModuleNotFoundError:  # direct historical script invocation
        import recall_core
    try:
        rows = recall_core.fused_recall(text, keywords=keywords, limit=limit,
            fast_min_similarity=FAST_MIN_SIMILARITY if min_similarity is None else min_similarity)
        return [dict(row, similarity=row['score']) for row in rows]
    except recall_core.RecallUnavailable:
        if raise_on_error:
            raise
        return []


def memory_in_recall(memory_id: str, text: str, limit: int = DEFAULT_LIMIT) -> tuple[bool, Optional[int], list[dict]]:
    """Convenience: check if memory_id appears in top-`limit` recall results for `text`.

    Returns (found, rank_1_indexed, all_results).
    rank_1_indexed is None if not found.
    """
    results = query_memories(text, limit=limit)
    for i, m in enumerate(results, 1):
        if m['id'] == memory_id:
            return True, i, results
    return False, None, results


if __name__ == "__main__":
    # CLI entrypoint for direct testing.
    # Usage:
    #   python recall_lib.py "some text to query"
    #   python recall_lib.py --memory-id <id> "transcript message text"
    import sys

    args = sys.argv[1:]
    if not args:
        print("Usage:")
        print("  python recall_lib.py 'query text'")
        print("  python recall_lib.py --memory-id <id> 'transcript text'")
        sys.exit(1)

    if args[0] == "--memory-id":
        if len(args) < 3:
            print("Need memory id and text")
            sys.exit(1)
        mid = args[1]
        text = args[2]
        found, rank, results = memory_in_recall(mid, text)
        print(f"Memory {mid[:8]} found: {found}, rank: {rank}")
        print(f"Top {len(results)} results:")
        for i, m in enumerate(results, 1):
            print(f"  {i}. {m['id'][:8]} sim={m['similarity']:.3f} imp={m['importance']:.2f} | {m['content'][:100]}")
    else:
        text = ' '.join(args)
        keywords = extract_keywords(text)
        print(f"Keywords ({len(keywords)}): {keywords}")
        results = query_memories(text, keywords=keywords)
        print(f"Top {len(results)} results:")
        for i, m in enumerate(results, 1):
            print(f"  {i}. {m['id'][:8]} sim={m['similarity']:.3f} imp={m['importance']:.2f} | {m['content'][:100]}")
