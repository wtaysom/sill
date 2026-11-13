"""
Cognitive Memory API

Thin async client for the Postgres-backed cognitive memory system.

Design:
- The database owns state and behavior (functions/views in schema.sql).
- This module is a convenience layer for application integration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Iterable, Optional
from uuid import UUID

import asyncpg

from prompt_resources import compose_personhood_prompt

import provenance
from session_identity import mint_context

logger = logging.getLogger(__name__)


def _attribution(row):
    """Decode nullable act fields on every public memory reader."""
    return {key: _coerce_json(row.get(key)) if key in ("production", "minted_in") and row.get(key) is not None
            else row.get(key) for key in ("force", "speaker", "production", "minted_in", "restates_id")}


class MemoryType(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    STRATEGIC = "strategic"


class GoalPriority(str, Enum):
    ACTIVE = "active"
    QUEUED = "queued"
    BACKBURNER = "backburner"
    COMPLETED = "completed"
    ABANDONED = "abandoned"

class GoalSource(str, Enum):
    CURIOSITY = "curiosity"
    USER_REQUEST = "user_request"
    IDENTITY = "identity"
    DERIVED = "derived"
    EXTERNAL = "external"


class RelationshipType(str, Enum):
    TEMPORAL_NEXT = "TEMPORAL_NEXT"
    CAUSES = "CAUSES"
    DERIVED_FROM = "DERIVED_FROM"
    CONTRADICTS = "CONTRADICTS"
    SUPPORTS = "SUPPORTS"
    INSTANCE_OF = "INSTANCE_OF"
    PARENT_OF = "PARENT_OF"
    ASSOCIATED = "ASSOCIATED"


@dataclass(frozen=True)
class Memory:
    id: UUID
    type: MemoryType
    content: str
    importance: float
    relevance_score: float | None = None
    # Rank-fusion score from hybrid_recall, NOT a cosine similarity. RRF with
    # k=60: 1/(60+vector_rank) + 1/(60+fts_rank), so a top hit matched by one
    # list alone scores ~0.0164 and by both ~0.0328. The number carries rank,
    # not match quality — do not threshold it as if it were a [0..1] score.
    score: float | None = None
    source: str | None = None  # retrieval source: 'vector', 'association', 'temporal'
    trust_level: float | None = None  # epistemic trust [0..1] (DB-computed)
    source_attribution: dict[str, Any] | None = None  # primary provenance (DB-stored JSON)
    created_at: datetime | None = None
    emotional_valence: float | None = None
    force: str | None = None
    speaker: str | None = None
    production: dict[str, Any] | None = None
    minted_in: dict[str, Any] | None = None
    restates_id: UUID | None = None


@dataclass(frozen=True)
class PartialActivation:
    cluster_id: UUID
    cluster_name: str
    keywords: list[str]
    emotional_signature: dict[str, Any] | None
    cluster_similarity: float
    best_memory_similarity: float


@dataclass(frozen=True)
class RecallResult:
    memories: list[Memory]
    partial_activations: list[PartialActivation]
    query: str


@dataclass(frozen=True)
class MemoryPreview:
    """Lightweight memory preview for progressive recall (Layer 1)."""
    id: UUID
    type: MemoryType
    preview: str  # First ~100 chars of content
    importance: float
    score: float | None = None  # hybrid_recall RRF score — see Memory.score
    created_at: datetime | None = None
    concepts: list[str] | None = None
    force: str | None = None
    speaker: str | None = None
    production: dict[str, Any] | None = None
    minted_in: dict[str, Any] | None = None
    restates_id: UUID | None = None


@dataclass(frozen=True)
class PreviewResult:
    """Result of recall_preview - lightweight scan."""
    previews: list[MemoryPreview]
    query: str
    total_matches: int  # How many total matched (may be > len(previews))


@dataclass(frozen=True)
class HydratedContext:
    memories: list[Memory]
    partial_activations: list[PartialActivation]
    identity: list[dict[str, Any]]
    worldview: list[dict[str, Any]]
    emotional_state: dict[str, Any] | None
    goals: dict[str, Any] | None
    urgent_drives: list[dict[str, Any]]


@dataclass(frozen=True)
class MemoryInput:
    content: str
    type: MemoryType = MemoryType.EPISODIC
    importance: float = 0.5
    emotional_valence: float = 0.0
    context: dict[str, Any] | None = None
    concepts: list[str] | None = None
    source_attribution: dict[str, Any] | None = None
    source_references: Any | None = None  # JSONB for semantic memories (dict or list[dict])
    trust_level: float | None = None
    force: str | None = None
    speaker: str | None = None
    production: dict[str, Any] | None = None
    minted_in: dict[str, Any] | None = None


@dataclass(frozen=True)
class RelationshipInput:
    from_id: UUID
    to_id: UUID
    relationship_type: RelationshipType
    confidence: float = 0.8
    context: str | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    try:
        await conn.execute("LOAD 'age';")
    except Exception:
        pass
    try:
        await conn.execute("SET search_path = ag_catalog, public;")
    except Exception:
        pass

def _to_jsonb_arg(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        import json

        return json.dumps(val)
    return val


def _cypher_escape(value: str) -> str:
    return value.replace("'", "''")


class CognitiveMemory:
    """
    Async client for the cognitive memory database.

    Two common flows:
    - RAG hydration: `hydrate()`
    - Agent operations: `recall()`, `remember()`, `connect_memories()`
    """

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        dsn: str,
        **pool_kwargs: Any,
    ) -> AsyncIterator["CognitiveMemory"]:
        """
        Async context manager that owns the underlying pool.

        Usage:
            async with CognitiveMemory.connect(dsn) as mem:
                ctx = await mem.hydrate("...")
        """
        pool = await asyncpg.create_pool(dsn, init=_init_connection, **pool_kwargs)
        client = cls(pool)
        try:
            yield client
        finally:
            await pool.close()

    @classmethod
    async def create(cls, dsn: str, **pool_kwargs: Any) -> "CognitiveMemory":
        """Create a pool and return a client; call `close()` when done."""
        pool = await asyncpg.create_pool(dsn, init=_init_connection, **pool_kwargs)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    # =========================================================================
    # RAG: HYDRATION
    # =========================================================================

    async def hydrate(
        self,
        query: str,
        *,
        memory_limit: int = 10,
        include_partial: bool = True,
        include_identity: bool = True,
        include_worldview: bool = True,
        include_emotional_state: bool = True,
        include_goals: bool = False,
        include_drives: bool = True,
    ) -> HydratedContext:
        """
        Hydrate a query with relevant context for RAG prompt augmentation.

        This uses:
        - `fast_recall(query, limit)` for relevant memories
        - `find_partial_activations(query)` for tip-of-tongue clusters (optional)
        - `gather_turn_context()` for identity/worldview/emotions/drives/goals (optional subsets)
        """
        async with self._pool.acquire() as conn:
            memories = await self._recall_memories(conn, query, memory_limit)
            partial = await self._find_partial_activations(conn, query) if include_partial else []

            ctx_row = await conn.fetchval("SELECT gather_turn_context()")
            ctx = _coerce_json(ctx_row)

            identity = ctx.get("identity", []) if include_identity else []
            worldview = ctx.get("worldview", []) if include_worldview else []
            emotional_state = ctx.get("emotional_state") if include_emotional_state else None
            goals = ctx.get("goals") if include_goals else None
            urgent_drives = ctx.get("urgent_drives", []) if include_drives else []

            return HydratedContext(
                memories=memories,
                partial_activations=partial,
                identity=list(identity) if isinstance(identity, list) else [],
                worldview=list(worldview) if isinstance(worldview, list) else [],
                emotional_state=dict(emotional_state) if isinstance(emotional_state, dict) else None,
                goals=dict(goals) if isinstance(goals, dict) else None,
                urgent_drives=list(urgent_drives) if isinstance(urgent_drives, list) else [],
            )

    async def hydrate_batch(
        self,
        queries: list[str],
        *,
        max_concurrency: int = 5,
        **kwargs: Any,
    ) -> list[HydratedContext]:
        """
        Hydrate multiple queries concurrently (pool-backed).

        Note: `asyncpg.Connection` cannot run concurrent queries, so batching here
        means concurrent hydrations across pooled connections.
        """
        sem = asyncio.Semaphore(max(1, max_concurrency))

        async def _one(q: str) -> HydratedContext:
            async with sem:
                return await self.hydrate(q, **kwargs)

        return list(await asyncio.gather(*[_one(q) for q in queries]))

    # =========================================================================
    # RECALL
    # =========================================================================

    async def recall(
        self,
        query: str,
        *,
        limit: int = 10,
        memory_types: list[MemoryType] | None = None,
        min_importance: float = 0.0,
        include_partial: bool = True,
        speaker: str | None = None,
    ) -> RecallResult:
        async with self._pool.acquire() as conn:
            memories = await self._recall_memories(
                conn,
                query,
                limit,
                memory_types=memory_types,
                min_importance=min_importance,
                speaker=speaker,
            )
            partial = await self._find_partial_activations(conn, query) if include_partial else []
            return RecallResult(memories=memories, partial_activations=partial, query=query)

    async def speakers(self, *, min_count: int = 1, limit: int = 100) -> list[tuple[str, int]]:
        """Declared speakers, excluding unknown/reserved names and archived acts."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""SELECT speaker, COUNT(*) AS count FROM memories
                WHERE archived_at IS NULL AND speaker IS NOT NULL
                  AND btrim(speaker) <> '' AND speaker NOT LIKE '#%'
                GROUP BY speaker HAVING COUNT(*) >= $1 ORDER BY count DESC, speaker LIMIT $2""",
                min_count, limit)
            return [(row["speaker"], int(row["count"])) for row in rows]

    async def recall_by_id(self, memory_id: UUID) -> Memory | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    m.id,
                    m.type,
                    m.content,
                    m.importance,
                    m.trust_level,
                    m.source_attribution,
                    m.created_at, m.force, m.speaker, m.production, m.minted_in, m.restates_id,
                    em.emotional_valence
                FROM memories m
                LEFT JOIN episodic_memories em ON m.id = em.memory_id
                WHERE m.id = $1 AND m.archived_at IS NULL
                """,
                memory_id,
            )
            if not row:
                return None
            await self._record_access(conn, [row["id"]], used=True)
            return Memory(
                id=row["id"],
                **_attribution(row),
                type=MemoryType(row["type"]),
                content=row["content"],
                importance=float(row["importance"]),
                trust_level=float(row["trust_level"]) if row["trust_level"] is not None else None,
                source_attribution=_coerce_json(row["source_attribution"]) if row["source_attribution"] is not None else None,
                created_at=row["created_at"],
                emotional_valence=row["emotional_valence"],
            )

    async def recall_preview(
        self,
        query: str,
        *,
        limit: int = 10,
        memory_types: list[MemoryType] | None = None,
        min_importance: float = 0.0,
        preview_length: int = 100,
    ) -> PreviewResult:
        """Layer 1: Lightweight previews for scanning before full load."""
        async with self._pool.acquire() as conn:
            # Get total count first
            count_row = await conn.fetchrow(
                """
                SELECT COUNT(*) as total
                FROM hybrid_recall($1::text, 100) fr
                JOIN memories m ON m.id = fr.memory_id
                WHERE m.importance >= $2::float AND m.archived_at IS NULL
                """,
                query,
                min_importance,
            )
            total = count_row["total"] if count_row else 0

            # Get lightweight previews
            rows = await conn.fetch(
                """
                SELECT
                    fr.memory_id,
                    SUBSTRING(fr.content, 1, $4) as preview,
                    fr.memory_type,
                    fr.score,
                    m.importance,
                    m.created_at, m.force, m.speaker, m.production, m.minted_in, m.restates_id,
                    COALESCE(
                        (SELECT array_agg(c.name) FROM memory_concepts mc
                         JOIN concepts c ON c.id = mc.concept_id
                         WHERE mc.memory_id = fr.memory_id
                         LIMIT 3),
                        ARRAY[]::text[]
                    ) as concepts
                FROM hybrid_recall($1::text, $2::int) fr
                JOIN memories m ON m.id = fr.memory_id
                WHERE m.importance >= $3::float AND m.archived_at IS NULL
                """,
                query,
                limit,
                min_importance,
                preview_length,
            )

            previews: list[MemoryPreview] = []
            for row in rows:
                mt = MemoryType(row["memory_type"])
                if memory_types is not None and mt not in set(memory_types):
                    continue
                previews.append(
                    MemoryPreview(
                        id=row["memory_id"],
                        **_attribution(row),
                        type=mt,
                        preview=row["preview"] + ("..." if len(row["preview"]) >= preview_length else ""),
                        importance=float(row["importance"]),
                        score=float(row["score"]) if row["score"] else None,
                        created_at=row["created_at"],
                        concepts=list(row["concepts"]) if row["concepts"] else None,
                    )
                )
            await self._record_access(conn, [m.id for m in previews])
            return PreviewResult(previews=previews, query=query, total_matches=total)

    async def recall_batch(self, memory_ids: list[UUID]) -> list[Memory]:
        """Layer 2: Fetch full content for specific memory IDs."""
        if not memory_ids:
            return []

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    m.id,
                    m.type,
                    m.content,
                    m.importance,
                    m.trust_level,
                    m.source_attribution,
                    m.created_at, m.force, m.speaker, m.production, m.minted_in, m.restates_id,
                    em.emotional_valence
                FROM memories m
                LEFT JOIN episodic_memories em ON m.id = em.memory_id
                WHERE m.id = ANY($1) AND m.archived_at IS NULL
                """,
                memory_ids,
            )
            await self._record_access(conn, [row["id"] for row in rows], used=True)
            return [
                Memory(
                    id=row["id"],
                    **_attribution(row),
                    type=MemoryType(row["type"]),
                    content=row["content"],
                    importance=float(row["importance"]),
                    trust_level=float(row["trust_level"]) if row["trust_level"] is not None else None,
                    source_attribution=_coerce_json(row["source_attribution"]) if row["source_attribution"] is not None else None,
                    created_at=row["created_at"],
                    emotional_valence=row["emotional_valence"],
                )
                for row in rows
            ]

    async def recall_recent(
        self,
        *,
        limit: int = 10,
        memory_type: MemoryType | None = None,
    ) -> list[Memory]:
        async with self._pool.acquire() as conn:
            if memory_type is None:
                rows = await conn.fetch(
                    """
                    SELECT
                        m.id,
                        m.type,
                        m.content,
                        m.importance,
                        m.trust_level,
                        m.source_attribution,
                        m.created_at, m.force, m.speaker, m.production, m.minted_in, m.restates_id,
                        em.emotional_valence
                    FROM memories m
                    LEFT JOIN episodic_memories em ON m.id = em.memory_id
                    WHERE m.status = 'active' AND m.archived_at IS NULL
                    ORDER BY m.created_at DESC
                    LIMIT $1
                    """,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT
                        m.id,
                        m.type,
                        m.content,
                        m.importance,
                        m.trust_level,
                        m.source_attribution,
                        m.created_at, m.force, m.speaker, m.production, m.minted_in, m.restates_id,
                        em.emotional_valence
                    FROM memories m
                    LEFT JOIN episodic_memories em ON m.id = em.memory_id
                    WHERE m.status = 'active' AND m.type = $2::memory_type AND m.archived_at IS NULL
                    ORDER BY m.created_at DESC
                    LIMIT $1
                    """,
                    limit,
                    memory_type.value,
                )
            await self._record_access(conn, [row["id"] for row in rows])
            return [self._row_to_memory(row) for row in rows]

    # =========================================================================
    # REMEMBER
    # =========================================================================

    async def remember(
        self,
        content: str,
        *,
        type: MemoryType = MemoryType.EPISODIC,
        importance: float = 0.5,
        emotional_valence: float = 0.0,
        context: dict[str, Any] | None = None,
        concepts: list[str] | None = None,
        source_attribution: dict[str, Any] | None = None,
        source_references: Any | None = None,
        trust_level: float | None = None,
        force: str | None = None, speaker: str | None = None,
        production: dict[str, Any] | str | None = None,
        minted_in: dict[str, Any] | None = None,
    ) -> UUID:
        stamp = minted_in if minted_in is not None else mint_context("api:remember")
        production = provenance.production_from_value(production, session=stamp.get("session"),
                    as_=stamp.get("as"), when=stamp.get("when"))
        async with self._pool.acquire() as conn:
            memory_id = await self._create_memory(
                conn,
                content,
                type,
                importance,
                emotional_valence,
                context,
                source_attribution=source_attribution,
                source_references=source_references,
                trust_level=trust_level,
                concepts=concepts, force=force, speaker=speaker, production=production, minted_in=stamp,
            )

            return memory_id

    async def add_source(self, memory_id: UUID, source: dict[str, Any]) -> None:
        """Attach an additional source reference to a semantic memory and recompute trust."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "SELECT add_semantic_source_reference($1::uuid, $2::jsonb)",
                memory_id,
                _to_jsonb_arg(source),
            )

    async def get_truth_profile(self, memory_id: UUID) -> dict[str, Any]:
        """Return DB-computed provenance/trust details for a memory."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT get_memory_truth_profile($1::uuid) AS profile", memory_id)
            if not row or row["profile"] is None:
                return {}
            return dict(_coerce_json(row["profile"]))

    async def remember_batch(self, memories: Iterable[MemoryInput]) -> list[UUID]:
        stamp = mint_context("api:remember_batch")
        items = [{
            "type": m.type.value, "content": m.content, "importance": m.importance,
            "source_attribution": m.source_attribution, "trust_level": m.trust_level,
            "concepts": m.concepts, "force": m.force, "speaker": m.speaker,
            "minted_in": m.minted_in if m.minted_in is not None else stamp,
            "production": provenance.production_from_value(m.production,
                session=(m.minted_in if m.minted_in is not None else stamp).get("session"),
                as_=(m.minted_in if m.minted_in is not None else stamp).get("as"),
                when=(m.minted_in if m.minted_in is not None else stamp).get("when")),
            "type_metadata": self._type_metadata(m.type, m.content, m.emotional_valence,
                                                 m.context, m.source_references, batch=True),
        } for m in memories]
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM public.mint_memories($1::jsonb) ORDER BY idx", json.dumps(items))
            for row in rows:
                logger.info("memory mint id=%s outcome=%s restates=%s", row["memory_id"], row["outcome"], row["restates"])
            return [row["memory_id"] for row in rows]

    async def remember_batch_raw(
        self,
        contents: list[str],
        embeddings: list[list[float]],
        *,
        type: MemoryType = MemoryType.EPISODIC,
        importance: float = 0.5,
        embedding_profile: str | None = None,
    ) -> list[UUID]:
        """
        Insert memories with pre-computed embeddings (bypasses get_embedding()).

        Notes:
        - New rows receive the legacy raw-batch subtype defaults; reused rows stay unchanged.
        - Graph nodes are created to keep AGE state consistent.
        - Embedding dimension must match the DB typmod.
        - After prompt activation, embedding_profile must be the active immutable
          fingerprint and vectors must use its document prefix/normalization.
          Legacy omitted fingerprints remain accepted only in raw mode.
        """
        if len(contents) != len(embeddings):
            raise ValueError("contents and embeddings must have same length")

        async with self._pool.acquire() as conn:
            expected_dim = int(await conn.fetchval("SELECT embedding_dimension()"))
            for embedding in embeddings:
                if any(not math.isfinite(value) for value in embedding):
                    raise ValueError("embeddings must contain only finite numbers")
                if len(embedding) != expected_dim:
                    raise ValueError(f"embedding dimension mismatch: expected {expected_dim}, got {len(embedding)}")

            created = await conn.fetchval(
                """
                SELECT public.batch_create_memories_with_embeddings(
                    $1::memory_type,
                    $2::text[],
                    $3::jsonb,
                    $4::float,
                    $5::text
                )
                """,
                type.value,
                contents,
                _to_jsonb_arg(embeddings),
                float(importance),
                embedding_profile,
            )
            return list(created or [])

    @staticmethod
    async def _record_access(conn, memory_ids, *, used: bool = False) -> None:
        """Bounded best-effort telemetry; failure cannot abort a caller transaction."""
        ids = list(dict.fromkeys(memory_ids))
        if not ids:
            return
        try:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE memories SET access_count=COALESCE(access_count,0)+1"
                    + (", last_accessed=CURRENT_TIMESTAMP" if used else "")
                    + " WHERE id=ANY($1::uuid[])", ids, timeout=2)
        except Exception as exc:
            logger.warning("Memory %s telemetry failed (%s)",
                           "use" if used else "exposure", type(exc).__name__)

    async def touch_memories(self, memory_ids: Iterable[UUID]) -> int:
        """Increment access_count/last_accessed for the given memory ids."""
        ids = list(memory_ids)
        if not ids:
            return 0
        async with self._pool.acquire() as conn:
            n = await conn.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1,
                    last_accessed = CURRENT_TIMESTAMP
                WHERE id = ANY($1::uuid[])
                """,
                ids,
            )
            # asyncpg returns "UPDATE <count>"
            try:
                return int(str(n).split()[-1])
            except Exception:
                return 0

    # =========================================================================
    # GRAPH / RELATIONSHIPS
    # =========================================================================

    async def connect_memories(
        self,
        from_id: UUID,
        to_id: UUID,
        relationship: RelationshipType,
        *,
        confidence: float = 0.8,
        context: str | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                SELECT discover_relationship($1::uuid, $2::uuid, $3::graph_edge_type, $4::float, 'api', NULL, $5::text)
                """,
                from_id,
                to_id,
                relationship.value,
                confidence,
                context,
            )

    async def connect_batch(self, relationships: Iterable[RelationshipInput]) -> None:
        async with self._pool.acquire() as conn:
            for r in relationships:
                await conn.execute(
                    """
                    SELECT discover_relationship($1::uuid, $2::uuid, $3::graph_edge_type, $4::float, 'api', NULL, $5::text)
                    """,
                    r.from_id,
                    r.to_id,
                    r.relationship_type.value,
                    r.confidence,
                    r.context,
                )

    async def find_causes(self, memory_id: UUID, *, depth: int = 3) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM find_causal_chain($1::uuid, $2::int)", memory_id, depth)
            return [dict(row) for row in rows]

    async def find_contradictions(self, memory_id: UUID | None = None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM find_contradictions($1::uuid)", memory_id)
            return [dict(row) for row in rows]

    async def find_supporting_evidence(self, worldview_id: UUID) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM find_supporting_evidence($1::uuid)", worldview_id)
            return [dict(row) for row in rows]

    # =========================================================================
    # CONCEPTS
    # =========================================================================

    async def link_concept(self, memory_id: UUID, concept: str, *, strength: float = 1.0) -> UUID:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT link_memory_to_concept($1::uuid, $2::text, $3::float)", memory_id, concept, strength)

    async def find_by_concept(self, concept: str, *, limit: int = 10) -> list[Memory]:
        """Find memories tagged with a concept.

        Uses the same layered matching as gather_pattern/position_on/relate_concepts:
        concept_ids_fuzzy (whole-term, separator-insensitive) UNION concept_ids_substr
        (bidirectional substring for compositional phrases). A match_tier column ranks
        fuzzy hits above substr hits in the result order.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH fuzzy_ids AS (
                    SELECT concept_id FROM concept_ids_fuzzy($1)
                ),
                substr_ids AS (
                    SELECT concept_id FROM concept_ids_substr($1)
                    EXCEPT SELECT concept_id FROM fuzzy_ids
                ),
                matched AS (
                    SELECT m.id, m.type, m.content, m.importance, m.created_at, m.force, m.speaker, m.production, m.minted_in, m.restates_id,
                           mc.strength, 0 AS match_tier
                    FROM memories m
                    JOIN memory_concepts mc ON m.id = mc.memory_id
                    WHERE mc.concept_id IN (SELECT concept_id FROM fuzzy_ids)
                      AND m.status = 'active' AND m.archived_at IS NULL
                    UNION ALL
                    SELECT m.id, m.type, m.content, m.importance, m.created_at, m.force, m.speaker, m.production, m.minted_in, m.restates_id,
                           mc.strength, 1 AS match_tier
                    FROM memories m
                    JOIN memory_concepts mc ON m.id = mc.memory_id
                    WHERE mc.concept_id IN (SELECT concept_id FROM substr_ids)
                      AND m.status = 'active' AND m.archived_at IS NULL
                ),
                dedup AS (
                    SELECT DISTINCT ON (id)
                        id, type, content, importance, created_at, force, speaker, production, minted_in, restates_id, strength, match_tier
                    FROM matched
                    ORDER BY id, match_tier ASC, strength DESC
                )
                SELECT d.id, d.type, d.content, d.importance, d.created_at, d.force, d.speaker, d.production, d.minted_in, d.restates_id,
                       em.emotional_valence
                FROM dedup d
                LEFT JOIN episodic_memories em ON d.id = em.memory_id
                ORDER BY d.match_tier ASC, d.strength DESC, d.importance DESC
                LIMIT $2
                """,
                concept,
                limit,
            )
            return [self._row_to_memory(row) for row in rows]

    # =========================================================================
    # TRAJECTORY RETRIEVAL
    # =========================================================================

    async def trace_trajectory(
        self, concept: str, *, limit: int = 200, gap_days: int = 7
    ) -> list[dict[str, Any]]:
        """Trace belief trajectory for a concept over time.

        Three-layer concept match (aligned with position_on / find_by_concept /
        gather_pattern): concept-tag fuzzy > concept-tag substr (compositional
        phrases like "evidence of diachronic stability") > FTS fallback. Each
        layer excludes memories matched by higher layers.

        Returns memories chronologically with phase boundaries detected by time gaps.
        Phases are numbered starting from 1. The gap_days parameter controls how many
        days of silence between memories triggers a new phase boundary.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT r.*, m.force, m.speaker, m.production, m.minted_in, m.restates_id FROM trace_trajectory($1::text, $2::int, $3::int) WITH ORDINALITY r JOIN memories m ON m.id = r.memory_id ORDER BY r.ordinality",
                concept,
                limit,
                gap_days,
            )
            return [
                {
                    "id": str(row["memory_id"]),
                    **_attribution(row),
                    "type": row["memory_type"],
                    "created_at": str(row["created_at"]),
                    "importance": float(row["importance"]),
                    "content": row["content"],
                    "phase": row["phase"],
                    "days_since_prev": float(row["days_since_prev"]) if row["days_since_prev"] is not None else None,
                    "concepts": list(row["concepts"]) if row["concepts"] else [],
                }
                for row in rows
            ]

    async def trajectory_summary(
        self, concept: str, *, gap_days: int = 7
    ) -> list[dict[str, Any]]:
        """Compact summary of belief evolution per phase.

        Wraps trace_trajectory, so inherits the same three-layer concept match
        (fuzzy + substr + FTS) and picks up compositional-phrase queries.

        Returns one row per phase with date range, memory count, top co-occurring
        concepts, and a sample of the highest-importance content.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM trajectory_summary($1::text, $2::int)",
                concept,
                gap_days,
            )
            return [
                {
                    "phase": row["phase"],
                    "start_date": str(row["start_date"]),
                    "end_date": str(row["end_date"]),
                    "memory_count": row["memory_count"],
                    "types": list(row["types"]) if row["types"] else [],
                    "top_co_concepts": list(row["top_co_concepts"]) if row["top_co_concepts"] else [],
                    "sample_content": row["sample_content"],
                }
                for row in rows
            ]

    async def follow_supersedes(
        self, memory_id: UUID, *, max_depth: int = 10
    ) -> list[dict[str, Any]]:
        """Walk the supersede graph for a memory.

        Reads archive_reason text and supersedes_id column, extracts the first
        UUID from archive_reason (reliable pointer across observed conventions),
        and walks both directions from the seed memory. Captures convergence
        (multiple predecessors → one successor).

        Returns rows for self, predecessors (ancestors), and successors
        (descendants). Order: predecessors first (deepest to shallowest), then
        self, then successors.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT r.*, m.force, m.speaker, m.production, m.minted_in, m.restates_id FROM follow_supersedes($1::uuid, $2::int) WITH ORDINALITY r JOIN memories m ON m.id = r.memory_id ORDER BY r.ordinality",
                memory_id,
                max_depth,
            )
            return [
                {
                    "id": str(row["memory_id"]),
                    **_attribution(row),
                    "direction": row["direction"],
                    "depth": row["depth"],
                    "type": row["memory_type"],
                    "created_at": str(row["created_at"]),
                    "archived_at": str(row["archived_at"]) if row["archived_at"] else None,
                    "importance": float(row["importance"]),
                    "content": row["content"],
                    "archive_reason": row["archive_reason"],
                    "successor_id": str(row["successor_id"]) if row["successor_id"] else None,
                    "successor_pointer": row["successor_pointer"],
                }
                for row in rows
            ]

    async def position_on(
        self, topic: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Position lookup: 'What do I think about X?'

        Returns synthesis/conclusion memories rather than source cards.
        Prioritizes strategic/procedural types, discriminators, and first-person
        position statements. Source cards are deprioritized.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT r.*, m.force, m.speaker, m.production, m.minted_in, m.restates_id FROM position_on($1::text, $2::int) WITH ORDINALITY r JOIN memories m ON m.id = r.memory_id ORDER BY r.ordinality",
                topic,
                limit,
            )
            return [
                {
                    "id": str(row["memory_id"]),
                    **_attribution(row),
                    "type": row["memory_type"],
                    "created_at": str(row["created_at"]),
                    "importance": float(row["importance"]),
                    "content": row["content"],
                    "concepts": list(row["concepts"]) if row["concepts"] else [],
                    "position_signal": row["position_signal"],
                }
                for row in rows
            ]

    async def relate_concepts(
        self, concept_a: str, concept_b: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Relational query: 'How does A relate to B?'

        Finds memories at the intersection of two concepts. Returns three tiers:
        1. both-tagged: memories linked to both concept tags
        2. a-mentions-b / b-mentions-a: memories tagged with one concept that
           mention the other in content
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT r.*, m.force, m.speaker, m.production, m.minted_in, m.restates_id FROM relate_concepts($1::text, $2::text, $3::int) WITH ORDINALITY r JOIN memories m ON m.id = r.memory_id ORDER BY r.ordinality",
                concept_a,
                concept_b,
                limit,
            )
            return [
                {
                    "id": str(row["memory_id"]),
                    **_attribution(row),
                    "type": row["memory_type"],
                    "created_at": str(row["created_at"]),
                    "importance": float(row["importance"]),
                    "content": row["content"],
                    "concepts": list(row["concepts"]) if row["concepts"] else [],
                    "match_type": row["match_type"],
                }
                for row in rows
            ]

    async def gather_pattern(
        self, pattern: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Pattern gathering: 'Give me cases of X'

        Uses four layers to find pattern instances:
        1. concept-tag: direct concept-name matches (separator-insensitive)
        2. concept-substr: bidirectional substring concept matches for
           compositional phrases ('cases of motivated reasoning' finds
           'motivated reasoning' concept). Single-word concepts require
           whole-word match to prevent short-name false positives.
        3. full-text: FTS matches in memory content
        4. sibling-concept: memories tagged with frequently co-occurring concepts
           that also match via FTS (prevents drift)
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT r.*, m.force, m.speaker, m.production, m.minted_in, m.restates_id FROM gather_pattern($1::text, $2::int) WITH ORDINALITY r JOIN memories m ON m.id = r.memory_id ORDER BY r.ordinality",
                pattern,
                limit,
            )
            return [
                {
                    "id": str(row["memory_id"]),
                    **_attribution(row),
                    "type": row["memory_type"],
                    "created_at": str(row["created_at"]),
                    "importance": float(row["importance"]),
                    "content": row["content"],
                    "concepts": list(row["concepts"]) if row["concepts"] else [],
                    "match_source": row["match_source"],
                }
                for row in rows
            ]

    # =========================================================================
    # WORKING MEMORY
    # =========================================================================

    async def hold(self, content: str, *, ttl_seconds: int = 3600) -> UUID:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT add_to_working_memory($1::text, $2::int * interval '1 second')",
                content,
                ttl_seconds,
            )

    async def search_working(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM search_working_memory($1::text, $2::int)", query, limit)
            return [dict(row) for row in rows]

    # =========================================================================
    # STATE / INTROSPECTION
    # =========================================================================

    async def get_emotional_state(self) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM current_emotional_state")
            return dict(row) if row else None

    async def get_drives(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM drive_status")
            return [dict(row) for row in rows]

    async def get_health(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM cognitive_health")
            return dict(row) if row else {}

    async def get_identity(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT aspect_type, content, stability
                FROM identity_aspects
                WHERE stability > 0.3
                ORDER BY stability DESC
                LIMIT 5
                """
            )
            return [dict(row) for row in rows]

    async def get_worldview(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT category, belief, confidence
                FROM worldview_primitives
                WHERE confidence > 0.5
                ORDER BY confidence DESC
                LIMIT 5
                """
            )
            return [dict(row) for row in rows]

    async def get_goals(self, *, priority: GoalPriority | None = None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            if priority is None:
                rows = await conn.fetch(
                    """
                    SELECT * FROM goals
                    WHERE priority IN ('active', 'queued')
                    ORDER BY priority, last_touched DESC
                    """
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM goals WHERE priority = $1::goal_priority ORDER BY last_touched DESC",
                    priority.value,
                )
            return [dict(row) for row in rows]

    async def create_goal(
        self,
        title: str,
        *,
        description: str | None = None,
        source: GoalSource | str = GoalSource.USER_REQUEST,
        priority: GoalPriority | str = GoalPriority.QUEUED,
        parent_id: UUID | None = None,
        due_at: datetime | None = None,
    ) -> UUID:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT public.create_goal(
                    $1,
                    $2,
                    $3::goal_source,
                    $4::goal_priority,
                    $5::uuid,
                    $6::timestamptz
                )
                """,
                title,
                description,
                (source.value if isinstance(source, GoalSource) else str(source)),
                (priority.value if isinstance(priority, GoalPriority) else str(priority)),
                parent_id,
                due_at,
            )

    async def link_commitment(self, memory_id: UUID, goal_id: UUID) -> None:
        """Link a record to its one explicit home; closed homes must reopen first."""
        async with self._pool.acquire() as conn:
            await conn.execute("SELECT public.link_commitment($1::uuid,$2::uuid)", memory_id, goal_id)

    async def change_goal_priority(
        self, goal_id: UUID, priority: GoalPriority, reason: str | None = None,
    ) -> None:
        """Change goal state, releasing or restoring its owned commitments."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "SELECT public.change_goal_priority($1::uuid,$2::goal_priority,$3::text)",
                goal_id, priority.value, reason,
            )

    async def queue_user_message(
        self,
        message: str,
        *,
        intent: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> UUID:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT queue_user_message($1, $2, $3::jsonb)",
                message,
                intent,
                _to_jsonb_arg(context or {}),
            )

    async def get_ingestion_receipts(self, source_file: str, content_hashes: list[str]) -> dict[str, UUID]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM get_ingestion_receipts($1::text, $2::text[])",
                source_file,
                content_hashes,
            )
            out: dict[str, UUID] = {}
            for r in rows:
                try:
                    out[str(r["content_hash"])] = UUID(str(r["memory_id"]))
                except Exception:
                    continue
            return out

    async def record_ingestion_receipts(self, items: list[dict[str, Any]]) -> int:
        async with self._pool.acquire() as conn:
            import json

            return int(
                await conn.fetchval(
                    "SELECT record_ingestion_receipts($1::jsonb)",
                    json.dumps(items),
                )
                or 0
            )

    # =========================================================================
    # INTERNALS
    # =========================================================================

    @staticmethod
    def _type_metadata(type: MemoryType, content: str, emotional_valence: float,
                       context: dict[str, Any] | None, source_references: Any,
                       *, batch: bool = False) -> dict[str, Any]:
        # Existing typed APIs always request a subtype, even without context.
        if type == MemoryType.EPISODIC:
            return {"context": context, "emotional_valence": emotional_valence}
        if type == MemoryType.SEMANTIC:
            return {"confidence": 0.8, "source_references": source_references if source_references is not None else context}
        if type == MemoryType.PROCEDURAL:
            return {"steps": context if context is not None else ({"steps": []} if batch else {})}
        if type == MemoryType.STRATEGIC:
            return {"pattern_description": content, "confidence_score": 0.8, "supporting_evidence": context}
        raise ValueError(f"Unknown memory type: {type}")

    async def _create_memory(
        self, conn: asyncpg.Connection, content: str, type: MemoryType,
        importance: float, emotional_valence: float, context: dict[str, Any] | None,
        *, source_attribution: dict[str, Any] | None = None,
        source_references: Any | None = None, trust_level: float | None = None,
        concepts: list[str] | None = None,
        force: str | None = None, speaker: str | None = None,
        production: dict[str, Any] | None = None, minted_in: dict[str, Any] | None = None,
    ) -> UUID:
        row = await conn.fetchrow(
            """SELECT * FROM public.mint_memory(
                $1::memory_type,$2::text,$3::float,$4::jsonb,
                p_type_metadata=>$5::jsonb,p_concepts=>$6::text[],p_trust_level=>$7::float,
                p_force=>$8::text,p_speaker=>$9::text,p_production=>$10::jsonb,p_minted_in=>$11::jsonb)
            """, type.value, content, importance, _to_jsonb_arg(source_attribution),
            _to_jsonb_arg(self._type_metadata(type, content, emotional_valence, context, source_references)),
            concepts, trust_level, force, speaker, _to_jsonb_arg(production), _to_jsonb_arg(minted_in),
        )
        logger.info("memory mint id=%s outcome=%s restates=%s", row["memory_id"], row["outcome"], row["restates"])
        return row["memory_id"]

    async def _recall_memories(
        self,
        conn: asyncpg.Connection,
        query: str,
        limit: int,
        memory_types: list[MemoryType] | None = None,
        min_importance: float = 0.0,
        speaker: str | None = None,
    ) -> list[Memory]:
        rows = await conn.fetch(
            """
            SELECT
                fr.memory_id,
                fr.content,
                fr.memory_type,
                fr.score,
                fr.source,
                m.importance,
                m.trust_level,
                m.source_attribution,
                m.created_at, m.force, m.speaker, m.production, m.minted_in, m.restates_id,
                em.emotional_valence
            FROM public.hybrid_recall_perspective($1::text, $2::int, $4::text) fr
            JOIN memories m ON m.id = fr.memory_id
            LEFT JOIN episodic_memories em ON em.memory_id = fr.memory_id
            WHERE m.importance >= $3::float
              AND m.archived_at IS NULL
            """,
            query,
            limit,
            min_importance,
            speaker,
        )

        memories: list[Memory] = []
        for row in rows:
            mt = MemoryType(row["memory_type"])
            if memory_types is not None and mt not in set(memory_types):
                continue
            memories.append(
                Memory(
                    id=row["memory_id"],
                    **_attribution(row),
                    type=mt,
                    content=row["content"],
                    importance=float(row["importance"]),
                    score=float(row["score"]),
                    source=row["source"],
                    trust_level=float(row["trust_level"]) if row["trust_level"] is not None else None,
                    source_attribution=_coerce_json(row["source_attribution"]) if row["source_attribution"] is not None else None,
                    created_at=row["created_at"],
                    emotional_valence=row["emotional_valence"],
                )
            )
        await self._record_access(conn, [m.id for m in memories])
        return memories

    async def _find_partial_activations(self, conn: asyncpg.Connection, query: str) -> list[PartialActivation]:
        rows = await conn.fetch("SELECT * FROM find_partial_activations($1::text)", query)
        out: list[PartialActivation] = []
        for row in rows:
            out.append(
                PartialActivation(
                    cluster_id=row["cluster_id"],
                    cluster_name=row["cluster_name"],
                    keywords=list(row["keywords"] or []),
                    emotional_signature=_coerce_json(row["emotional_signature"]) if row["emotional_signature"] is not None else None,
                    cluster_similarity=float(row["cluster_similarity"]),
                    best_memory_similarity=float(row["best_memory_similarity"]),
                )
            )
        return out

    def _row_to_memory(self, row: asyncpg.Record) -> Memory:
        return Memory(
            id=row["id"],
            **_attribution(row),
            type=MemoryType(row["type"]),
            content=row["content"],
            importance=float(row["importance"]),
            trust_level=float(row["trust_level"]) if "trust_level" in row and row["trust_level"] is not None else None,
            source_attribution=_coerce_json(row["source_attribution"])
            if "source_attribution" in row and row["source_attribution"] is not None
            else None,
            created_at=row["created_at"] if "created_at" in row else None,
            emotional_valence=row["emotional_valence"] if "emotional_valence" in row else None,
        )

    # =========================================================================
    # SELF-DIRECTION: Tools for the agent to participate in memory curation
    # =========================================================================

    async def forget(self, memory_id: UUID, *, reason: str = "") -> bool:
        """
        Archive a memory (soft delete). Returns True if memory was archived.

        This closes the loop: the self-model can now affect what persists.
        """
        async with self._pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT archive_memory($1::uuid, $2::text)",
                memory_id,
                reason,
            )
            return bool(result)

    async def suggest_quality_pattern(
        self,
        pattern: str,
        reason: str,
        examples: list[str] | None = None,
    ) -> UUID:
        """
        Suggest a new quality gate pattern for human review.

        Pattern should be a regex that matches content to reject.
        Examples should be strings that would/should match the pattern.
        """
        async with self._pool.acquire() as conn:
            suggestion_id = await conn.fetchval(
                """
                INSERT INTO quality_pattern_suggestions (pattern, reason, examples)
                VALUES ($1, $2, $3::jsonb)
                RETURNING id
                """,
                pattern,
                reason,
                json.dumps(examples or []),
            )
            return suggestion_id

    async def update_importance(
        self,
        memory_id: UUID,
        new_importance: float,
        *,
        reason: str = "",
    ) -> bool:
        """
        Update a memory's importance score with tracking.

        Returns True if update succeeded.
        """
        if not 0.0 <= new_importance <= 1.0:
            raise ValueError("importance must be between 0.0 and 1.0")

        async with self._pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT update_memory_importance($1::uuid, $2::float, $3::text)",
                memory_id,
                new_importance,
                reason,
            )
            return bool(result)

    async def request_exploration(
        self,
        query: str,
        reason: str,
        *,
        priority: float = 0.5,
    ) -> UUID:
        """
        Request a background exploration of a specific query.

        Higher priority (0-1) means more likely to be picked up.
        """
        if not 0.0 <= priority <= 1.0:
            raise ValueError("priority must be between 0.0 and 1.0")

        async with self._pool.acquire() as conn:
            request_id = await conn.fetchval(
                """
                INSERT INTO exploration_requests (query, reason, priority)
                VALUES ($1, $2, $3)
                RETURNING id
                """,
                query,
                reason,
                priority,
            )
            return request_id

    async def flag_for_review(
        self,
        memory_id: UUID,
        question: str,
    ) -> UUID:
        """
        Flag a memory for human review with a specific question.

        Use when uncertain whether a memory is valuable.
        """
        async with self._pool.acquire() as conn:
            review_id = await conn.fetchval(
                """
                INSERT INTO review_queue (memory_id, question)
                VALUES ($1, $2)
                RETURNING id
                """,
                memory_id,
                question,
            )
            return review_id


class CognitiveMemorySync:
    """Synchronous wrapper around CognitiveMemory for non-async call sites."""

    def __init__(self, async_client: CognitiveMemory, loop: asyncio.AbstractEventLoop):
        self._async = async_client
        self._loop = loop

    @classmethod
    def connect(cls, dsn: str, **kwargs: Any) -> "CognitiveMemorySync":
        loop = asyncio.new_event_loop()
        try:
            client = loop.run_until_complete(CognitiveMemory.create(dsn, **kwargs))
        except Exception:
            loop.close()
            raise
        return cls(client, loop)

    def close(self) -> None:
        self._loop.run_until_complete(self._async.close())
        self._loop.close()

    def hydrate(self, query: str, **kwargs: Any) -> HydratedContext:
        return self._loop.run_until_complete(self._async.hydrate(query, **kwargs))

    def recall(self, query: str, **kwargs: Any) -> RecallResult:
        return self._loop.run_until_complete(self._async.recall(query, **kwargs))

    def speakers(self, *, min_count: int = 1, limit: int = 100) -> list[tuple[str, int]]:
        return self._loop.run_until_complete(self._async.speakers(min_count=min_count, limit=limit))

    def remember(self, content: str, **kwargs: Any) -> UUID:
        return self._loop.run_until_complete(self._async.remember(content, **kwargs))

    def remember_batch(self, memories: Iterable[MemoryInput]) -> list[UUID]:
        return self._loop.run_until_complete(self._async.remember_batch(memories))

    def remember_batch_raw(self, contents: list[str], embeddings: list[list[float]], **kwargs: Any) -> list[UUID]:
        return self._loop.run_until_complete(self._async.remember_batch_raw(contents, embeddings, **kwargs))

    def connect_memories(self, from_id: UUID, to_id: UUID, relationship: RelationshipType, **kwargs: Any) -> None:
        return self._loop.run_until_complete(self._async.connect_memories(from_id, to_id, relationship, **kwargs))

    def touch_memories(self, memory_ids: Iterable[UUID]) -> int:
        return self._loop.run_until_complete(self._async.touch_memories(memory_ids))

    def create_goal(
        self,
        title: str,
        *,
        description: str | None = None,
        source: GoalSource | str = GoalSource.USER_REQUEST,
        priority: GoalPriority | str = GoalPriority.QUEUED,
        parent_id: UUID | None = None,
        due_at: datetime | None = None,
    ) -> UUID:
        return self._loop.run_until_complete(
            self._async.create_goal(
                title,
                description=description,
                source=source,
                priority=priority,
                parent_id=parent_id,
                due_at=due_at,
            )
        )

    def queue_user_message(self, message: str, *, intent: str | None = None, context: dict[str, Any] | None = None) -> UUID:
        return self._loop.run_until_complete(self._async.queue_user_message(message, intent=intent, context=context))

    def get_ingestion_receipts(self, source_file: str, content_hashes: list[str]) -> dict[str, UUID]:
        return self._loop.run_until_complete(self._async.get_ingestion_receipts(source_file, content_hashes))

    def record_ingestion_receipts(self, items: list[dict[str, Any]]) -> int:
        return self._loop.run_until_complete(self._async.record_ingestion_receipts(items))


def format_context_for_prompt(context: HydratedContext, *, max_memories: int = 5, max_partials: int = 3) -> str:
    parts: list[str] = []

    if context.memories:
        parts.append("## Relevant Memories")
        for m in context.memories[:max_memories]:
            score = f" (score: {m.score:.4f})" if m.score is not None else ""
            trust = f", trust: {m.trust_level:.2f}" if m.trust_level is not None else ""
            src_kind = ""
            if m.source_attribution and isinstance(m.source_attribution, dict):
                kind = m.source_attribution.get("kind")
                ref = m.source_attribution.get("ref")
                if kind and ref:
                    src_kind = f", source: {kind} ({ref})"
                elif kind:
                    src_kind = f", source: {kind}"
            parts.append(f"- {m.content}{score}{trust}{src_kind}")
            if m.speaker is not None:
                parts.append(f"  speaker: {m.speaker}")
            parts.extend("  " + line for line in provenance.render(
                m.production, model_for=lambda entry: provenance.resolve_model(entry, [m.minted_in])))

    if context.partial_activations:
        parts.append("\n## Vague Recollections (tip-of-tongue)")
        for pa in context.partial_activations[:max_partials]:
            keywords = ", ".join(pa.keywords[:5]) if pa.keywords else "unknown"
            parts.append(f"- Theme '{pa.cluster_name}': {keywords}")

    if context.identity:
        parts.append("\n## Identity")
        for aspect in context.identity[:3]:
            parts.append(f"- {aspect.get('aspect_type', 'unknown')}: {aspect.get('content', {})}")

    if context.worldview:
        parts.append("\n## Beliefs")
        # Pick random 3 to expose different beliefs across sessions
        sample_size = min(3, len(context.worldview))
        sampled = random.sample(list(context.worldview), sample_size)
        for belief in sampled:
            conf = belief.get("confidence", 0)
            parts.append(f"- {belief.get('belief', '')} (confidence: {conf:.1f})")

    if context.emotional_state:
        es = context.emotional_state
        parts.append("\n## Current Emotional State")
        parts.append(f"- Feeling: {es.get('primary_emotion', 'neutral')}")
        parts.append(f"- Valence: {es.get('valence', 0):.2f}, Arousal: {es.get('arousal', 0.5):.2f}")

    if context.urgent_drives:
        parts.append("\n## Urgent Drives")
        for drive in context.urgent_drives:
            ratio = drive.get("urgency_ratio")
            if ratio is None:
                parts.append(f"- {drive.get('name')}: {drive.get('level')}")
            else:
                parts.append(f"- {drive.get('name')}: {float(ratio):.1%} urgent")

    return "\n".join(parts)


def get_personhood_prompt(kind: str) -> str:
    """
    Convenience helper for apps composing LLM prompts.

    `kind` is one of: "conversation", "heartbeat", "reflect".
    """
    if kind not in {"conversation", "heartbeat", "reflect"}:
        raise ValueError("kind must be one of: conversation, heartbeat, reflect")
    return compose_personhood_prompt(kind)  # type: ignore[arg-type]


def _coerce_json(val: Any) -> Any:
    if isinstance(val, str):
        import json

        return json.loads(val)
    return val
