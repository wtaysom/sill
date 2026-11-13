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
The Postgres schema ships a `public.create_memory(type, content, importance,
source_attribution, trust_level)` function that internally calls
`get_embedding(content)`, which POSTs the text to the embedding service via
the pgsql-http extension. We delegate to that flow rather than computing
embeddings client-side. This is the same path used by the MCP server and the
research worker, so seed rows take exactly the same trajectory as a real
`remember()` call. The embeddings service URL lives in `embedding_config` and
is set to `http://embeddings:80/embed` by default — the db container reaches
it on the private Docker network, so the host doesn't need to expose port 80.

For type-specific rows we follow up with the matching subtype helper:
`create_semantic_memory`, `create_episodic_memory`, `create_procedural_memory`,
`create_strategic_memory`. The simplest path is: insert via
`create_memory()`, then INSERT the subtype row directly (because the helpers
re-call create_memory and trigger exact-content dedup; we'd just be looking
up the same UUID).

For the test environment we honor `SILL_FAKE_EMBEDDINGS=1`, which short-
circuits to a deterministic zero-vector INSERT path that skips the http call.
That lets `test_seed_import.py` run without spinning up the embeddings
service. The CLI never sets this flag — it's strictly a test escape hatch.

Idempotency
-----------
Before inserting, we SELECT for an existing active memory whose content
matches and whose `source_attribution->>'author'` matches the incoming row's
author. If found, we skip. (We don't add a UNIQUE constraint — the schema's
own dedup already returns the existing UUID on exact-content match, so a
narrower pre-check is enough and keeps the schema unmodified.)
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
    """Return True if an active memory with this (author, content) already exists."""
    if author is None:
        cur.execute(
            """
            SELECT 1
            FROM memories
            WHERE content = %s
              AND archived_at IS NULL
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
    """Test-only path: insert a memories row with a zero-vector embedding.

    Bypasses get_embedding() / the http extension. We still write the
    appropriate subtype row so the schema invariants hold.
    """
    dim = _fake_embedding_dim(cur)
    zero_vec = "[" + ",".join(["0"] * dim) + "]"
    cur.execute(
        """
        INSERT INTO memories (type, content, embedding, importance, source_attribution, trust_level)
        VALUES (%s, %s, %s::vector, %s, %s::jsonb, %s)
        RETURNING id
        """,
        (
            mem_type,
            content,
            zero_vec,
            importance,
            json.dumps(source_attribution),
            0.7,
        ),
    )
    new_id = cur.fetchone()[0]
    _insert_subtype_row(cur, mem_type, new_id, content)
    return str(new_id)


def _insert_subtype_row(cur, mem_type: str, memory_id: str, content: str) -> None:
    if mem_type == "episodic":
        cur.execute(
            "INSERT INTO episodic_memories (memory_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (memory_id,),
        )
    elif mem_type == "semantic":
        cur.execute(
            """
            INSERT INTO semantic_memories (memory_id, confidence)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            (memory_id, 0.7),
        )
    elif mem_type == "procedural":
        cur.execute(
            """
            INSERT INTO procedural_memories (memory_id, steps)
            VALUES (%s, %s::jsonb)
            ON CONFLICT DO NOTHING
            """,
            (memory_id, json.dumps([{"step": content[:200]}])),
        )
    elif mem_type == "strategic":
        cur.execute(
            """
            INSERT INTO strategic_memories (memory_id, pattern_description, confidence_score)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (memory_id, content[:200], 0.7),
        )


def _insert_via_create_memory(
    cur,
    *,
    mem_type: str,
    content: str,
    importance: float,
    source_attribution: dict[str, Any],
) -> str:
    """Production path: delegate to public.create_memory, then write subtype row."""
    cur.execute(
        "SELECT public.create_memory(%s::memory_type, %s, %s, %s::jsonb, NULL)",
        (mem_type, content, importance, json.dumps(source_attribution)),
    )
    new_id = cur.fetchone()[0]
    _insert_subtype_row(cur, mem_type, new_id, content)
    return str(new_id)


def _connect_explicit(dsn: str):
    # libpq can otherwise inherit service/SSL/target overrides for fields
    # absent from the URI. Keep explicit instance mode isolated.
    inherited = {key: value for key, value in os.environ.items() if key.startswith("PG")}
    for key in inherited:
        del os.environ[key]
    try:
        try:
            conn = psycopg2.connect(dsn)
        except psycopg2.Error:
            raise SystemExit("seed_import: could not connect to the selected instance") from None
    finally:
        os.environ.update(inherited)
    return conn


def main(
    path: str | Path, *, fake_embeddings: bool | None = None, dsn: str | None = None,
) -> ImportSummary:
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

    conn = psycopg2.connect(**_db_kwargs()) if dsn is None else _connect_explicit(dsn)
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
