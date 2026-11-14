"""
MCP server exposing CognitiveMemory (Postgres-first brain) tools.

This is intentionally thin: the database owns state and logic.

Install with:
  pip install -e .

Run:
  python -m sill_mcp_server
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from uuid import UUID

from dotenv import load_dotenv

from cognitive_memory_api import (
    CognitiveMemory,
    GoalPriority,
    MemoryInput,
    MemoryType,
    RelationshipInput,
    RelationshipType,
)
from instance_config import InstanceConfigError, instance_dsn, load_instance_env, validate_dsn


def _print_err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def _env_dsn() -> str:
    db_host = os.getenv("POSTGRES_HOST", "localhost")
    db_port = os.getenv("POSTGRES_PORT", "5432")
    db_name = os.getenv("POSTGRES_DB", "sill")
    db_user = os.getenv("POSTGRES_USER", "sill")
    db_password = os.getenv("POSTGRES_PASSWORD", "sill_password")
    return f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"


def _jsonable(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (UUID, datetime, date)):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    return str(obj)


def _tool(name: str, description: str, schema: dict[str, Any]):
    # Tool class is provided by MCP; imported lazily in main().
    from mcp.types import Tool

    return Tool(name=name, description=description, inputSchema=schema)


def _require(args: dict[str, Any], key: str, tool: str) -> Any:
    if key not in args:
        raise ValueError(f"Missing required argument '{key}' for tool '{tool}'")
    return args[key]


from session_identity import mint_context


def _stage_callable():
    """One policy for discovery and direct dispatch; no optional providers by default."""
    if os.environ.get("SILL_ENABLE_STAGE") != "1":
        raise ValueError("stage is disabled; set SILL_ENABLE_STAGE=1 to opt into Chorus file writes and external models")
    try:
        from chorus.stage import run_beat
    except ImportError as exc:
        raise ValueError("stage requires optional Chorus and its provider dependencies; install them separately or unset SILL_ENABLE_STAGE") from exc
    if not callable(run_beat):
        raise ValueError("optional Chorus must provide callable chorus.stage.run_beat")
    return run_beat


async def _dispatch_tool(client: CognitiveMemory, name: str, args: dict[str, Any]) -> Any:
    if name == "hydrate":
        query = _require(args, "query", name)
        return await client.hydrate(
            query,
            memory_limit=int(args.get("memory_limit", 10)),
            include_partial=bool(args.get("include_partial", True)),
            include_identity=bool(args.get("include_identity", True)),
            include_worldview=bool(args.get("include_worldview", True)),
            include_emotional_state=bool(args.get("include_emotional_state", True)),
            include_goals=bool(args.get("include_goals", False)),
            include_drives=bool(args.get("include_drives", True)),
        )

    if name == "hydrate_batch":
        queries = _require(args, "queries", name)
        if not isinstance(queries, list):
            raise ValueError("queries must be an array of strings")
        return await client.hydrate_batch(
            queries,
            memory_limit=int(args.get("memory_limit", 10)),
            include_partial=bool(args.get("include_partial", True)),
            include_identity=bool(args.get("include_identity", True)),
            include_worldview=bool(args.get("include_worldview", True)),
            include_emotional_state=bool(args.get("include_emotional_state", True)),
            include_goals=bool(args.get("include_goals", False)),
            include_drives=bool(args.get("include_drives", True)),
        )

    if name == "recall":
        query = _require(args, "query", name)
        limit = int(args.get("limit", 10))
        include_partial = bool(args.get("include_partial", True))

        memory_types = args.get("memory_types")
        parsed_types: list[MemoryType] | None = None
        if memory_types is not None:
            if not isinstance(memory_types, list):
                raise ValueError("memory_types must be an array of strings")
            parsed_types = [MemoryType(t) for t in memory_types]

        return await client.recall(
            query,
            limit=limit,
            memory_types=parsed_types,
            min_importance=float(args.get("min_importance", 0.0)),
            include_partial=include_partial,
            speaker=args.get("speaker"),
        )

    if name == "speakers":
        return await client.speakers(min_count=int(args.get("min_count", 1)), limit=int(args.get("limit", 100)))

    if name == "recall_by_id":
        memory_id = UUID(_require(args, "memory_id", name))
        return await client.recall_by_id(memory_id)

    if name == "recall_recent":
        limit = int(args.get("limit", 10))
        memory_type = args.get("memory_type")
        mt: MemoryType | None = MemoryType(memory_type) if memory_type else None
        return await client.recall_recent(limit=limit, memory_type=mt)

    if name == "recall_preview":
        query = _require(args, "query", name)
        limit = int(args.get("limit", 10))
        preview_length = int(args.get("preview_length", 100))

        memory_types = args.get("memory_types")
        parsed_types: list[MemoryType] | None = None
        if memory_types is not None:
            if not isinstance(memory_types, list):
                raise ValueError("memory_types must be an array of strings")
            parsed_types = [MemoryType(t) for t in memory_types]

        return await client.recall_preview(
            query,
            limit=limit,
            memory_types=parsed_types,
            min_importance=float(args.get("min_importance", 0.0)),
            preview_length=preview_length,
        )

    if name == "recall_batch":
        memory_ids = _require(args, "memory_ids", name)
        if not isinstance(memory_ids, list):
            raise ValueError("memory_ids must be an array of strings")
        return await client.recall_batch([UUID(mid) for mid in memory_ids])

    if name == "remember":
        content = _require(args, "content", name)
        type_raw = args.get("type", MemoryType.EPISODIC.value)
        return await client.remember(
            content,
            type=MemoryType(type_raw),
            importance=float(args.get("importance", 0.5)),
            emotional_valence=float(args.get("emotional_valence", 0.0)),
            context=args.get("context"),
            concepts=args.get("concepts"),
            force=args.get("force"), speaker=args.get("speaker"),
            production=args.get("production"), minted_in=args.get("minted_in") if args.get("minted_in") is not None else mint_context("mcp:remember"),
        )

    if name == "remember_batch":
        items = _require(args, "memories", name)
        if not isinstance(items, list):
            raise ValueError("memories must be an array")
        stamp = mint_context("mcp:remember_batch")
        memories: list[MemoryInput] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("memories items must be objects")
            memories.append(
                MemoryInput(
                    content=str(item.get("content", "")),
                    type=MemoryType(item.get("type", MemoryType.EPISODIC.value)),
                    importance=float(item.get("importance", 0.5)),
                    emotional_valence=float(item.get("emotional_valence", 0.0)),
                    context=item.get("context"),
                    concepts=item.get("concepts"),
                    force=item.get("force"), speaker=item.get("speaker"),
                    production=item.get("production"), minted_in=item.get("minted_in") if item.get("minted_in") is not None else stamp,
                )
            )
        return await client.remember_batch(memories)

    if name == "remember_batch_raw":
        contents = _require(args, "contents", name)
        embeddings = _require(args, "embeddings", name)
        if not isinstance(contents, list) or not isinstance(embeddings, list):
            raise ValueError("contents and embeddings must be arrays")
        type_raw = args.get("type", MemoryType.EPISODIC.value)
        return await client.remember_batch_raw(
            contents,
            embeddings,
            type=MemoryType(type_raw),
            importance=float(args.get("importance", 0.5)),
            embedding_profile=args.get("embedding_profile"),
        )

    if name == "connect":
        from_id = UUID(_require(args, "from_id", name))
        to_id = UUID(_require(args, "to_id", name))
        rel = RelationshipType(_require(args, "relationship", name))
        await client.connect_memories(
            from_id,
            to_id,
            rel,
            confidence=float(args.get("confidence", 0.8)),
            context=args.get("context"),
        )
        return {"ok": True}

    if name == "connect_batch":
        items = _require(args, "relationships", name)
        if not isinstance(items, list):
            raise ValueError("relationships must be an array")
        rels: list[RelationshipInput] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("relationships items must be objects")
            rels.append(
                RelationshipInput(
                    from_id=UUID(str(item["from_id"])),
                    to_id=UUID(str(item["to_id"])),
                    relationship_type=RelationshipType(str(item["relationship_type"])),
                    confidence=float(item.get("confidence", 0.8)),
                    context=item.get("context"),
                )
            )
        await client.connect_batch(rels)
        return {"ok": True}

    if name == "find_causes":
        memory_id = UUID(_require(args, "memory_id", name))
        return await client.find_causes(memory_id, depth=int(args.get("depth", 3)))

    if name == "find_contradictions":
        memory_id = args.get("memory_id")
        mid = UUID(memory_id) if memory_id else None
        return await client.find_contradictions(mid)

    if name == "find_supporting_evidence":
        worldview_id = UUID(_require(args, "worldview_id", name))
        return await client.find_supporting_evidence(worldview_id)

    if name == "link_concept":
        memory_id = UUID(_require(args, "memory_id", name))
        concept = _require(args, "concept", name)
        strength = float(args.get("strength", 1.0))
        return await client.link_concept(memory_id, concept, strength=strength)

    if name == "find_by_concept":
        concept = _require(args, "concept", name)
        return await client.find_by_concept(concept, limit=int(args.get("limit", 10)))

    # =========================================================================
    # TRAJECTORY RETRIEVAL
    # =========================================================================

    if name == "trace_trajectory":
        concept = _require(args, "concept", name)
        return await client.trace_trajectory(
            concept,
            limit=int(args.get("limit", 200)),
            gap_days=int(args.get("gap_days", 7)),
        )

    if name == "trajectory_summary":
        concept = _require(args, "concept", name)
        return await client.trajectory_summary(
            concept,
            gap_days=int(args.get("gap_days", 7)),
        )

    if name == "follow_supersedes":
        memory_id = UUID(_require(args, "memory_id", name))
        return await client.follow_supersedes(
            memory_id,
            max_depth=int(args.get("max_depth", 10)),
        )

    if name == "position_on":
        topic = _require(args, "topic", name)
        return await client.position_on(
            topic,
            limit=int(args.get("limit", 10)),
        )

    if name == "relate_concepts":
        concept_a = _require(args, "concept_a", name)
        concept_b = _require(args, "concept_b", name)
        return await client.relate_concepts(
            concept_a,
            concept_b,
            limit=int(args.get("limit", 20)),
        )

    if name == "gather_pattern":
        pattern = _require(args, "pattern", name)
        return await client.gather_pattern(
            pattern,
            limit=int(args.get("limit", 20)),
        )

    if name == "hold":
        content = _require(args, "content", name)
        return await client.hold(content, ttl_seconds=int(args.get("ttl_seconds", 3600)))

    if name == "search_working":
        query = _require(args, "query", name)
        return await client.search_working(query, limit=int(args.get("limit", 5)))

    if name == "get_health":
        return await client.get_health()

    if name == "get_drives":
        return await client.get_drives()

    if name == "get_identity":
        return await client.get_identity()

    if name == "get_worldview":
        return await client.get_worldview()

    if name == "get_goals":
        pri = args.get("priority")
        gp: GoalPriority | None = GoalPriority(pri) if pri else None
        return await client.get_goals(priority=gp)

    if name == "link_commitment":
        memory_id = UUID(_require(args, "memory_id", name))
        goal_id = UUID(_require(args, "goal_id", name))
        await client.link_commitment(memory_id, goal_id)
        return {"memory_id": str(memory_id), "goal_id": str(goal_id)}

    if name == "change_goal_priority":
        goal_id = UUID(_require(args, "goal_id", name))
        priority = GoalPriority(_require(args, "priority", name))
        await client.change_goal_priority(goal_id, priority, args.get("reason"))
        return {"goal_id": str(goal_id), "priority": priority.value}

    if name == "batch":
        ops = _require(args, "operations", name) or []
        continue_on_error = bool(args.get("continue_on_error", False))
        results: list[dict[str, Any]] = []
        for op in ops:
            op_name = op.get("name") if isinstance(op, dict) else None
            op_args = op.get("arguments") if isinstance(op, dict) else {}
            try:
                if not isinstance(op_name, str):
                    raise ValueError("operation.name must be a string")
                r = await _dispatch_tool(client, op_name, op_args or {})
                results.append({"name": op_name, "result": _jsonable(r)})
            except Exception as exc:
                results.append({"name": op_name or "<unknown>", "error": str(exc)})
                if not continue_on_error:
                    raise
        return results

    # =========================================================================
    # SELF-DIRECTION: Tools for the agent to participate in memory curation
    # =========================================================================

    if name == "forget":
        memory_id = UUID(_require(args, "memory_id", name))
        reason = args.get("reason", "")
        result = await client.forget(memory_id, reason=reason)
        return {"archived": result, "memory_id": str(memory_id)}

    if name == "suggest_quality_pattern":
        pattern = _require(args, "pattern", name)
        reason = _require(args, "reason", name)
        examples = args.get("examples", [])
        suggestion_id = await client.suggest_quality_pattern(pattern, reason, examples)
        return {"suggestion_id": str(suggestion_id)}

    if name == "update_importance":
        memory_id = UUID(_require(args, "memory_id", name))
        new_importance = float(_require(args, "importance", name))
        reason = args.get("reason", "")
        result = await client.update_importance(memory_id, new_importance, reason=reason)
        return {"updated": result, "memory_id": str(memory_id), "new_importance": new_importance}

    if name == "request_exploration":
        query = _require(args, "query", name)
        reason = _require(args, "reason", name)
        priority = float(args.get("priority", 0.5))
        request_id = await client.request_exploration(query, reason, priority=priority)
        return {"request_id": str(request_id)}

    if name == "flag_for_review":
        memory_id = UUID(_require(args, "memory_id", name))
        question = _require(args, "question", name)
        review_id = await client.flag_for_review(memory_id, question)
        return {"review_id": str(review_id)}

    # =========================================================================
    # CHORUS: Multi-actor conversation orchestration
    # =========================================================================

    if name == "stage":
        run_beat = _stage_callable()

        transcript_path = _require(args, "transcript_path", name)
        beat = _require(args, "beat", name)
        if not isinstance(beat, dict):
            raise ValueError("beat must be an object with content and cast")
        return await run_beat(transcript_path, beat)

    raise ValueError(f"Unknown tool '{name}'")


def _tools() -> list[Any]:
    tools = [
        _tool(
            "hydrate",
            "Hydrate a query with relevant memories + (optional) identity/worldview/drives/emotion.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "memory_limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "include_partial": {"type": "boolean", "default": True},
                    "include_identity": {"type": "boolean", "default": True},
                    "include_worldview": {"type": "boolean", "default": True},
                    "include_emotional_state": {"type": "boolean", "default": True},
                    "include_goals": {"type": "boolean", "default": False},
                    "include_drives": {"type": "boolean", "default": True},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "hydrate_batch",
            "Hydrate multiple queries (sequential) for throughput.",
            {
                "type": "object",
                "properties": {
                    "queries": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "memory_limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "include_partial": {"type": "boolean", "default": True},
                    "include_identity": {"type": "boolean", "default": True},
                    "include_worldview": {"type": "boolean", "default": True},
                    "include_emotional_state": {"type": "boolean", "default": True},
                    "include_goals": {"type": "boolean", "default": False},
                    "include_drives": {"type": "boolean", "default": True},
                },
                "required": ["queries"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "recall",
            "Recall relevant memories (hybrid_recall: vector + full-text, fused with RRF). "
            "Results are ordered best-first. The `score` field is a reciprocal-rank-fusion "
            "score, NOT a cosine similarity: it is ~0.0164 for a top hit found by one of the "
            "two searches and ~0.0328 for one found by both, and it encodes rank rather than "
            "match quality. Judge relevance from the content and the ordering; do not read a "
            "small score as a weak match, and do not threshold it as if it were 0..1.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "memory_types": {
                        "type": ["array", "null"],
                        "items": {"type": "string", "enum": [t.value for t in MemoryType]},
                    },
                    "min_importance": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.0},
                    "include_partial": {"type": "boolean", "default": True},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "recall_by_id",
            "Fetch a memory by id.",
            {
                "type": "object",
                "properties": {"memory_id": {"type": "string"}},
                "required": ["memory_id"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "recall_recent",
            "Fetch recent memories.",
            {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 10},
                    "memory_type": {"type": ["string", "null"], "enum": [t.value for t in MemoryType] + [None]},
                },
                "additionalProperties": False,
            },
        ),
        _tool(
            "recall_preview",
            "Layer 1 progressive recall: Get lightweight previews (ID, type, first 100 chars, importance) for scanning. Use this first to see what exists, then recall_batch to load specific memories. Saves tokens when you only need a few of many matches.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "memory_types": {
                        "type": ["array", "null"],
                        "items": {"type": "string", "enum": [t.value for t in MemoryType]},
                    },
                    "min_importance": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.0},
                    "preview_length": {"type": "integer", "minimum": 50, "maximum": 200, "default": 100},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "recall_batch",
            "Layer 2 progressive recall: Fetch full content for specific memory IDs. Use after recall_preview to load only the memories you need.",
            {
                "type": "object",
                "properties": {
                    "memory_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 20,
                    },
                },
                "required": ["memory_ids"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "remember",
            "Store a memory (embedding generated in DB).",
            {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "type": {"type": "string", "enum": [t.value for t in MemoryType], "default": MemoryType.EPISODIC.value},
                    "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.5},
                    "emotional_valence": {"type": "number", "minimum": -1.0, "maximum": 1.0, "default": 0.0},
                    "context": {"type": ["object", "null"]},
                    "concepts": {"type": ["array", "null"], "items": {"type": "string"}},
                },
                "required": ["content"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "remember_batch",
            "Store many memories (single DB connection).",
            {
                "type": "object",
                "properties": {
                    "memories": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "type": {"type": "string", "enum": [t.value for t in MemoryType], "default": MemoryType.EPISODIC.value},
                                "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.5},
                                "emotional_valence": {"type": "number", "minimum": -1.0, "maximum": 1.0, "default": 0.0},
                                "context": {"type": ["object", "null"]},
                                "concepts": {"type": ["array", "null"], "items": {"type": "string"}},
                            },
                            "required": ["content"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["memories"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "remember_batch_raw",
            "Store many memories using precomputed embeddings (bypasses get_embedding). Supply the active embedding_profile fingerprint for prompted profiles; optional in raw mode.",
            {
                "type": "object",
                "properties": {
                    "contents": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "embeddings": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                    "embedding_profile": {"type": "string", "description": "Active embedding profile fingerprint for these vectors; required for prompted profiles."},
                    "type": {"type": "string", "enum": [t.value for t in MemoryType], "default": MemoryType.EPISODIC.value},
                    "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.5},
                },
                "required": ["contents", "embeddings"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "connect",
            "Create a relationship between two memories (graph).",
            {
                "type": "object",
                "properties": {
                    "from_id": {"type": "string"},
                    "to_id": {"type": "string"},
                    "relationship": {"type": "string", "enum": [t.value for t in RelationshipType]},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.8},
                    "context": {"type": ["string", "null"]},
                },
                "required": ["from_id", "to_id", "relationship"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "connect_batch",
            "Create multiple memory relationships (single DB connection).",
            {
                "type": "object",
                "properties": {
                    "relationships": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "from_id": {"type": "string"},
                                "to_id": {"type": "string"},
                                "relationship_type": {"type": "string", "enum": [t.value for t in RelationshipType]},
                                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.8},
                                "context": {"type": ["string", "null"]},
                            },
                            "required": ["from_id", "to_id", "relationship_type"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["relationships"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "find_causes",
            "Find causal chain leading to a memory.",
            {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "depth": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
                },
                "required": ["memory_id"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "find_contradictions",
            "Find contradictions in the graph (optionally scoped to a memory_id).",
            {
                "type": "object",
                "properties": {"memory_id": {"type": ["string", "null"]}},
                "additionalProperties": False,
            },
        ),
        _tool(
            "find_supporting_evidence",
            "Find supporting evidence for a worldview primitive.",
            {
                "type": "object",
                "properties": {"worldview_id": {"type": "string"}},
                "required": ["worldview_id"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "link_concept",
            "Link a memory to a concept.",
            {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "concept": {"type": "string"},
                    "strength": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 1.0},
                },
                "required": ["memory_id", "concept"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "find_by_concept",
            "Retrieve memories linked to a concept. Uses fuzzy whole-term match (separator-insensitive) plus bidirectional substring match for compositional phrases — e.g. 'cases of motivated reasoning' finds memories tagged with 'motivated reasoning'. Fuzzy hits are ranked above substr hits.",
            {
                "type": "object",
                "properties": {"concept": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 10}},
                "required": ["concept"],
                "additionalProperties": False,
            },
        ),
        # Trajectory retrieval tools
        _tool(
            "trace_trajectory",
            "Temporal/meta query: Trace belief trajectory for a concept over time. Returns memories chronologically with phase boundaries detected by time gaps. Use to answer 'what changed in my beliefs about X?' or 'how did my understanding of X evolve?'. Three-layer concept match (aligned with position_on / find_by_concept / gather_pattern): fuzzy tag, compositional-phrase substring (e.g. 'evidence of diachronic stability' picks up memories tagged 'diachronic stability'), FTS fallback.",
            {
                "type": "object",
                "properties": {
                    "concept": {"type": "string", "description": "Concept name or compositional phrase to trace (case-insensitive, separator-insensitive, substring-matched)"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
                    "gap_days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7, "description": "Days of silence that triggers a new phase boundary"},
                },
                "required": ["concept"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "trajectory_summary",
            "Compact summary of belief evolution per phase. Returns one row per phase with date range, memory count, top co-concepts, and sample content. Use as a quick overview before trace_trajectory. Inherits the three-layer match from trace_trajectory, so compositional phrases work.",
            {
                "type": "object",
                "properties": {
                    "concept": {"type": "string", "description": "Concept name to summarize (case-insensitive)"},
                    "gap_days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7},
                },
                "required": ["concept"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "follow_supersedes",
            "Walk the supersede graph for a memory. Reads archive_reason text and supersedes_id column to find predecessors (ancestors) and successors (descendants), including convergence cases (multiple predecessors → one successor). Use to trace how a position evolved: supersede chains encode revisions that embeddings can't recover.",
            {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "UUID of the seed memory"},
                    "max_depth": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10, "description": "Max hops in each direction"},
                },
                "required": ["memory_id"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "position_on",
            "Position lookup: 'What do I think about X?' Returns synthesis/conclusion memories (strategic, procedural, discriminators) rather than source cards. Uses three match layers: fuzzy concept tag (whole-term), compositional-phrase concept tag (bidirectional substring — e.g. 'evidence of diachronic stability' finds 'diachronic stability' tagged memories), and FTS fallback. Fuzzy hits rank above substr which rank above FTS. Use when you need current position, not raw sources.",
            {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Topic to look up position on (case-insensitive, uses ILIKE)"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
                "required": ["topic"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "relate_concepts",
            "Relational query: 'How does A relate to B?' Finds memories at the intersection of two concepts — tagged with both, or tagged with one and mentioning the other. Each concept is resolved via fuzzy (whole-term) UNION substr (bidirectional substring), so compositional phrases on either side — e.g. 'evidence of diachronic stability' ~ 'memory' — match their underlying tags. Returns the relationship, not the union.",
            {
                "type": "object",
                "properties": {
                    "concept_a": {"type": "string", "description": "First concept (case-insensitive)"},
                    "concept_b": {"type": "string", "description": "Second concept (case-insensitive)"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                },
                "required": ["concept_a", "concept_b"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "gather_pattern",
            "Pattern gathering: 'Give me cases of X.' Uses concept tags + substring concept match + full-text search + sibling concepts to find pattern instances even when vocabulary differs. Four layers: concept-tag (exact), concept-substr (for compositional phrases like 'cases of motivated reasoning'), full-text, sibling-concept.",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Pattern to gather cases of"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "hold",
            "Add content to working memory (auto-expires).",
            {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 604800, "default": 3600},
                },
                "required": ["content"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "search_working",
            "Search working memory.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        _tool("get_health", "Get cognitive health metrics.", {"type": "object", "properties": {}, "additionalProperties": False}),
        _tool("get_drives", "Get current drive levels.", {"type": "object", "properties": {}, "additionalProperties": False}),
        _tool("get_identity", "Get identity aspects.", {"type": "object", "properties": {}, "additionalProperties": False}),
        _tool("get_worldview", "Get worldview primitives.", {"type": "object", "properties": {}, "additionalProperties": False}),
        _tool(
            "get_goals",
            "Get goals (optionally filtered by priority).",
            {
                "type": "object",
                "properties": {"priority": {"type": ["string", "null"], "enum": [g.value for g in GoalPriority] + [None]}},
                "additionalProperties": False,
            },
        ),
        _tool(
            "link_commitment",
            "Link a memory to its one explicit home goal. Reopen a closed goal before adding a new link; same-home retries are harmless.",
            {"type": "object", "properties": {
                "memory_id": {"type": "string", "format": "uuid"},
                "goal_id": {"type": "string", "format": "uuid"}},
             "required": ["memory_id", "goal_id"], "additionalProperties": False},
        ),
        _tool(
            "change_goal_priority",
            "Change goal priority. Closing releases its live home commitments; reopening restores only its own unreplaced releases.",
            {"type": "object", "properties": {
                "goal_id": {"type": "string", "format": "uuid"},
                "priority": {"type": "string", "enum": [g.value for g in GoalPriority]},
                "reason": {"type": ["string", "null"]}},
             "required": ["goal_id", "priority"], "additionalProperties": False},
        ),
        _tool(
            "batch",
            "Run multiple tool calls sequentially (use batch_* tools where available).",
            {
                "type": "object",
                "properties": {
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "arguments": {"type": "object", "default": {}},
                            },
                            "required": ["name"],
                            "additionalProperties": False,
                        },
                        "minItems": 1,
                    },
                    "continue_on_error": {"type": "boolean", "default": False},
                },
                "required": ["operations"],
                "additionalProperties": False,
            },
        ),
        # Self-direction tools
        _tool(
            "forget",
            "Archive a memory (soft delete). Returns True if memory was archived.",
            {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "UUID of the memory to archive"},
                    "reason": {"type": "string", "description": "Why this memory is being archived"},
                },
                "required": ["memory_id"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "suggest_quality_pattern",
            "Suggest a regex pattern for the quality gate (for human review).",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to match low-quality content"},
                    "reason": {"type": "string", "description": "Why this pattern indicates low quality"},
                    "examples": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Example strings that should match",
                    },
                },
                "required": ["pattern", "reason"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "update_importance",
            "Update the importance of a memory with tracking.",
            {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "UUID of the memory"},
                    "importance": {"type": "number", "minimum": 0, "maximum": 1, "description": "New importance value (0-1)"},
                    "reason": {"type": "string", "description": "Why the importance is being changed"},
                },
                "required": ["memory_id", "importance"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "request_exploration",
            "Request a background exploration of a specific query.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The query to explore"},
                    "reason": {"type": "string", "description": "Why this exploration is valuable"},
                    "priority": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5, "description": "Priority (0-1)"},
                },
                "required": ["query", "reason"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "flag_for_review",
            "Flag a memory for human review.",
            {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "UUID of the memory to flag"},
                    "question": {"type": "string", "description": "What needs to be reviewed/decided"},
                },
                "required": ["memory_id", "question"],
                "additionalProperties": False,
            },
        ),
        # Chorus tools
        _tool(
            "stage",
            "Orchestrate a multi-actor conversation beat. Routes content to multiple models in parallel, maintains JSONL transcript.",
            {
                "type": "object",
                "properties": {
                    "transcript_path": {
                        "type": "string",
                        "description": "Path to JSONL transcript file (created if doesn't exist)",
                    },
                    "beat": {
                        "type": "object",
                        "description": "Beat configuration",
                        "properties": {
                            "content": {"type": "string", "description": "The prompt/direction for this beat"},
                            "cast": {
                                "type": "object",
                                "description": "Map of actor alias to config: {model, mask?, system?}",
                                "additionalProperties": {
                                    "type": "object",
                                    "properties": {
                                        "model": {"type": "string", "description": "Model name or alias"},
                                        "mask": {"type": "string", "description": "Role name for display"},
                                        "system": {"type": "string", "description": "System prompt"},
                                    },
                                    "required": ["model"],
                                },
                            },
                            "scenario": {"type": "string", "description": "Scenario name"},
                            "act": {"type": "integer", "description": "Act number"},
                        },
                        "required": ["content", "cast"],
                    },
                },
                "required": ["transcript_path", "beat"],
                "additionalProperties": False,
            },
        ),
    ]

    # Stage is outside this wheel. Explicit opt-in and importable support are
    # both required, including for callers who bypass tool discovery.
    try:
        _stage_callable()
    except ValueError:
        tools = [tool for tool in tools if tool.name != "stage"]
    attribution = {
        "force": {"type": ["string", "null"], "description": "Illocutionary force of this act."},
        "speaker": {"type": ["string", "null"], "description": "Whose act this records; no author is inferred."},
        "production": {"type": ["object", "string", "null"], "description": "Optional declared intellectual contributors. Full typed object, or who [as Voice]: work [@ address] [basis]. Compact self requires a supplied session. Null means undeclared."},
        "minted_in": {"type": ["object", "null"], "description": "Explicit mint context; otherwise gathered from supplied runtime evidence, with unknown fields null."},
    }
    for tool in tools:
        if tool.name == "remember":
            tool.inputSchema["properties"].update(attribution)
        elif tool.name == "remember_batch":
            tool.inputSchema["properties"]["memories"]["items"]["properties"].update(attribution)
        elif tool.name == "recall":
            tool.inputSchema["properties"]["speaker"] = {
                "type": ["string", "null"], "description": "Restrict candidates to this speaker before ranking. #Nobody selects NULL speaker; omit for all perspectives."}
    tools.append(_tool("speakers", "Declared speakers and act counts; excludes reserved names and archived rows. Speaker is distinct from production.", {
        "type": "object", "properties": {"min_count": {"type": "integer", "minimum": 1, "default": 1},
        "limit": {"type": "integer", "minimum": 1, "default": 100}}, "additionalProperties": False}))
    return tools


async def _run_server(dsn: str) -> None:
    try:
        from mcp.server import Server
        from mcp.server.models import InitializationOptions
        from mcp.server.stdio import stdio_server
        from mcp.types import ServerCapabilities, TextContent, ToolsCapability
    except Exception as e:
        raise RuntimeError(
            "MCP dependencies not installed. Install with: pip install -e ."
        ) from e

    server = Server("sill")

    client = await CognitiveMemory.create(dsn)

    @server.list_tools()
    async def list_tools():
        return _tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]):
        try:
            result = await _dispatch_tool(client, name, arguments or {})
            text = json.dumps(_jsonable(result), indent=2, sort_keys=True)
        except Exception as exc:
            text = f"Error: {exc}"
        return [TextContent(type="text", text=text)]

    try:
        init_opts = InitializationOptions(
            server_name="sill",
            server_version=_server_version(),
            capabilities=ServerCapabilities(tools=ToolsCapability()),
        )

        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_opts)
    finally:
        await client.close()


def _server_version() -> str:
    """The installed 'sill-memory' distribution's version (what pip/pipx
    recorded at install time), or "dev" for an uninstalled checkout. Single
    source for both the MCP initialize handshake's server_version and the
    --version CLI flag, so the two can never independently drift."""
    try:
        return version("sill-memory")
    except PackageNotFoundError:  # local dev, not pip-installed
        return "dev"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sill-mcp", description="MCP server exposing CognitiveMemory tools over stdio.")
    p.add_argument("--env-file", type=Path, help="Absolute path to the instance dotenv file")
    p.add_argument("--dsn", help="PostgreSQL URI; overrides SILL_DB_DSN and POSTGRES_* settings")
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {_server_version()}",
        help="Print the installed sill-mcp version and exit.",
    )
    return p


@contextmanager
def _without_pg_environment():
    # Asyncpg can read PGPORT even when the URI contains an explicit port.
    # Keep its libpq-style environment from changing explicit instance mode.
    inherited = {key: value for key, value in os.environ.items() if key.startswith("PG")}
    for key in inherited:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(inherited)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.env_file is None:
            load_dotenv()
            dsn = args.dsn or os.getenv("SILL_DB_DSN") or _env_dsn()
            asyncio.run(_run_server(dsn))
        else:
            settings = load_instance_env(args.env_file)
            dsn = instance_dsn(settings) if args.dsn is None else validate_dsn(args.dsn)
            with _without_pg_environment():
                asyncio.run(_run_server(dsn))
        return 0
    except KeyboardInterrupt:
        return 130
    except InstanceConfigError as exc:
        _print_err(f"sill-mcp: {exc}")
        return 1
    except Exception as exc:
        if args.env_file is None:
            _print_err(str(exc))
        else:
            _print_err(
                f"sill-mcp: {args.env_file}: could not start the server ({type(exc).__name__}). "
                "Check the database and selected settings."
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
