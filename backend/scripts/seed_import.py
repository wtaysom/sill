#!/usr/bin/env python3
"""
seed_import — import a JSONL methodology seed pack into the Sill database.

Each row in the JSONL file has shape:

    {
      "type": "procedural" | "semantic" | "episodic" | "strategic",
      "content": "<text>",
      "importance": 0.85,
      "concepts": ["a", "b"],
      "source_attribution": {"author": "sill-seed-v0.1", ...}
    }

Embedding strategy
------------------
The importer uses public.mint_memory with explicit subtype metadata. Base row,
subtype and graph are one transaction. Retrying content in the NULL-speaker
namespace returns the existing UUID without changing it. Source author remains
source metadata; it is never inferred to be a speaker.

SILL_FAKE_EMBEDDINGS=1 supplies a deterministic zero vector through
public.mint_memory_with_embedding for disposable tests. It still follows the
same atomic and uniqueness contracts, and never bypasses embedding profile
validation. The CLI does not set this flag.

The existing source-author precheck avoids unnecessary calls on repeat packs;
the database's speaker/content uniqueness is authoritative under concurrency.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import psycopg2
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "seed_import requires psycopg2-binary. Install the sill backend "
        "(`pipx install -e ./backend`) or `pip install psycopg2-binary`."
    ) from exc


VALID_TYPES = {"episodic", "semantic", "procedural", "strategic"}


@dataclass
class ImportSummary:
    inserted: int = 0
    skipped: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "skipped": self.skipped,
            "errors": self.errors,
        }


def _db_kwargs() -> dict[str, Any]:
    """Return psycopg2.connect kwargs from SILL_* / POSTGRES_* env."""
    return {
        "host": os.environ.get("SILL_DB_HOST", os.environ.get("POSTGRES_HOST", "localhost")),
        "port": int(os.environ.get("SILL_DB_PORT", os.environ.get("POSTGRES_PORT", "5432"))),
        "dbname": os.environ.get("POSTGRES_DB", "sill"),
        "user": os.environ.get("POSTGRES_USER", "sill"),
        "password": os.environ.get("POSTGRES_PASSWORD", "sill_password"),
    }


def _iter_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r") as f:
        for lineno, raw in enumerate(f, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"seed_import: {path}:{lineno}: invalid JSON ({exc.msg})"
                ) from exc
    return rows


def _validate_row(row: dict[str, Any], lineno: int) -> None:
    for key in ("type", "content"):
        if key not in row:
            raise SystemExit(f"seed_import: row {lineno}: missing required key {key!r}")
    if row["type"] not in VALID_TYPES:
        raise SystemExit(
            f"seed_import: row {lineno}: type {row['type']!r} not in {sorted(VALID_TYPES)}"
        )
    if not isinstance(row["content"], str) or not row["content"].strip():
        raise SystemExit(f"seed_import: row {lineno}: content must be a non-empty string")


def _author(row: dict[str, Any]) -> str | None:
    attr = row.get("source_attribution") or {}
    if isinstance(attr, dict):
        author = attr.get("author")
        if isinstance(author, str):
            return author
    return None


def _already_present(cur, content: str, author: str | None) -> bool:
    """Return True only for an active NULL-speaker memory with this author/content."""
    if author is None:
        cur.execute(
            """
            SELECT 1
            FROM memories
            WHERE content = %s
              AND archived_at IS NULL
              AND coalesce(speaker, '') = ''
              AND (source_attribution ? 'author') = FALSE
            LIMIT 1
            """,
            (content,),
        )
    else:
        cur.execute(
            """
            SELECT 1
            FROM memories
            WHERE content = %s
              AND archived_at IS NULL
              AND coalesce(speaker, '') = ''
              AND source_attribution->>'author' = %s
            LIMIT 1
            """,
            (content, author),
        )
    return cur.fetchone() is not None


def _fake_embedding_dim(cur) -> int:
    cur.execute("SELECT embedding_dimension()")
    row = cur.fetchone()
    return int(row[0]) if row and row[0] else 768


def _insert_with_fake_embedding(
    cur,
    *,
    mem_type: str,
    content: str,
    importance: float,
    source_attribution: dict[str, Any],
) -> str:
    """Test-only supplied-vector path; retains the ordinary atomic mint contract."""
    dim = _fake_embedding_dim(cur)
    zero_vec = "[" + ",".join(["0"] * dim) + "]"
    cur.execute(
        """SELECT memory_id FROM public.mint_memory_with_embedding(
            %s::memory_type,%s,%s::vector,%s,%s::jsonb,
            p_type_metadata=>%s::jsonb,p_trust_level=>0.7)""",
        (mem_type, content, zero_vec, importance, json.dumps(source_attribution),
         json.dumps(_seed_metadata(mem_type, content))),
    )
    return str(cur.fetchone()[0])


def _seed_metadata(mem_type: str, content: str) -> dict[str, Any]:
    if mem_type == "semantic":
        return {"confidence": 0.7}
    if mem_type == "procedural":
        return {"steps": [{"step": content[:200]}]}
    if mem_type == "strategic":
        return {"pattern_description": content[:200], "confidence_score": 0.7}
    return {}


def _insert_via_create_memory(
    cur, *, mem_type: str, content: str, importance: float,
    source_attribution: dict[str, Any],
) -> str:
    """Production seed path: one atomic statement, no post-reuse subtype writes."""
    cur.execute(
        """SELECT memory_id FROM public.mint_memory(%s::memory_type,%s,%s,%s::jsonb,
            p_type_metadata=>%s::jsonb)""",
        (mem_type, content, importance, json.dumps(source_attribution),
         json.dumps(_seed_metadata(mem_type, content))),
    )
    return str(cur.fetchone()[0])


def main(path: str | Path, *, fake_embeddings: bool | None = None) -> ImportSummary:
    """Import the JSONL file at *path*. Returns a summary.

    `fake_embeddings`: if None, read SILL_FAKE_EMBEDDINGS env (default False).
    If True, use the zero-vector escape hatch (test-only).
    """
    seed_path = Path(path)
    if not seed_path.is_file():
        raise SystemExit(f"seed_import: {seed_path}: file not found")

    if fake_embeddings is None:
        fake_embeddings = os.environ.get("SILL_FAKE_EMBEDDINGS", "").lower() in {"1", "true", "yes"}

    rows = _iter_rows(seed_path)
    if not rows:
        print(f"seed_import: {seed_path}: no rows", file=sys.stderr)
        return ImportSummary()

    for i, row in enumerate(rows, start=1):
        _validate_row(row, i)

    summary = ImportSummary()
    total = len(rows)

    conn = psycopg2.connect(**_db_kwargs())
    try:
        with conn:
            with conn.cursor() as cur:
                for i, row in enumerate(rows, start=1):
                    content = row["content"]
                    mem_type = row["type"]
                    importance = float(row.get("importance", 0.5))
                    source_attribution = row.get("source_attribution") or {}
                    if not isinstance(source_attribution, dict):
                        source_attribution = {}
                    author = _author(row)

                    try:
                        if _already_present(cur, content, author):
                            summary.skipped += 1
                            print(f"  {i:>3}/{total} skip   ({mem_type}) — already present")
                            continue

                        if fake_embeddings:
                            mem_id = _insert_with_fake_embedding(
                                cur,
                                mem_type=mem_type,
                                content=content,
                                importance=importance,
                                source_attribution=source_attribution,
                            )
                        else:
                            mem_id = _insert_via_create_memory(
                                cur,
                                mem_type=mem_type,
                                content=content,
                                importance=importance,
                                source_attribution=source_attribution,
                            )

                        summary.inserted += 1
                        print(f"  {i:>3}/{total} insert ({mem_type}) {mem_id}")
                    except Exception as exc:  # noqa: BLE001
                        summary.errors += 1
                        print(
                            f"  {i:>3}/{total} ERROR  ({mem_type}): {exc}",
                            file=sys.stderr,
                        )
                        # Re-raise to roll back the whole batch — partial seed is worse than none.
                        raise
    finally:
        conn.close()

    print()
    print(
        f"seed_import: inserted={summary.inserted}, "
        f"skipped={summary.skipped}, errors={summary.errors}"
    )
    return summary


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) != 2:
        print("usage: seed_import.py <path-to-jsonl>", file=sys.stderr)
        sys.exit(2)
    summary = main(sys.argv[1])
    sys.exit(0 if summary.errors == 0 else 1)
