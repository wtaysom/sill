#!/usr/bin/env python3
"""
PreCompact hook: Materialize orientation before context compaction.

Writes current focus and recent memories to a static file that survives
context compression, so fresh context wakes up with orientation already loaded.
"""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
import _store
import sys

PROJECT_DIR = Path(os.environ.get("SILL_PROJECT_ROOT", os.getcwd()))
RULES_DIR = Path(
    os.environ.get("SILL_RULES_DIR", str(PROJECT_DIR / ".claude" / "rules"))
)
OUTPUT_FILE = Path(
    os.environ.get(
        "SILL_ORIENTATION_OUTPUT",
        str(RULES_DIR / "sill-orientation.generated.md"),
    )
)

# Database connection (overridable)
DB_CONTAINER = os.environ.get("SILL_DB_CONTAINER", "sill_db")
DB_USER = os.environ.get("SILL_DB_USER", "sill")
DB_NAME = os.environ.get("SILL_DB_NAME", "sill")


def query_postgres(sql: str) -> list[list[str]]:
    """Run a SQL query and return results as list of lists."""
    if 'SILL_INSTANCE_ENV_FILE' in os.environ:
        try:
            if sql.lstrip().upper().startswith('UPDATE '):
                _store.execute(sql, timeout=5)
                return []
            return [[str(value) if value is not None else '' for value in row.values()]
                    for row in _store.query(sql, timeout=5)]
        except Exception:
            print('[sill] Selected store unavailable; checkpoint query unchecked.', file=sys.stderr)
            return []
    cmd = [
        "docker", "exec", DB_CONTAINER,
        "psql", "-U", DB_USER, "-d", DB_NAME,
        "-t", "-A", "-F", "|",
        "-c", sql
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return []

        lines = result.stdout.strip().split("\n")
        if not lines or lines == [""]:
            return []

        rows = []
        for line in lines:
            if line.strip():
                rows.append(line.split("|"))
        return rows
    except Exception:
        return []


def get_current_focus() -> str:
    """Get current focus from drives table."""
    sql = "SELECT current_focus FROM drives WHERE name = 'curiosity' AND current_focus IS NOT NULL LIMIT 1;"
    rows = query_postgres(sql)
    if rows and rows[0]:
        return rows[0][0]
    return ""


def get_recent_memories(limit: int = 5, mem_type: str | None = None) -> list[dict]:
    """Get recent high-importance memories, optionally filtered by type."""
    type_filter = f"AND type = '{mem_type}'" if mem_type else ""
    sql = f"""
    SELECT content, type, importance, created_at::date
    FROM memories
    WHERE archived_at IS NULL
      AND importance >= 0.6
      {type_filter}
    ORDER BY created_at DESC
    LIMIT {limit};
    """
    rows = query_postgres(sql)
    memories = []
    for row in rows:
        if len(row) >= 4:
            content = row[0]
            memories.append({
                "content": content[:200] + "..." if len(content) > 200 else content,
                "type": row[1],
                "importance": row[2],
                "date": row[3]
            })
    return memories


def get_active_goals() -> list[str]:
    """Get any active goals."""
    sql = """
    SELECT description FROM goals
    WHERE priority = 'active'
    ORDER BY created_at DESC
    LIMIT 3;
    """
    rows = query_postgres(sql)
    return [row[0] for row in rows if row]


def get_strategic_decisions(limit: int = 3) -> list[dict]:
    """Get recent strategic decisions."""
    sql = f"""
    SELECT content, created_at::date
    FROM memories
    WHERE archived_at IS NULL
      AND type = 'strategic'
      AND importance >= 0.7
    ORDER BY created_at DESC
    LIMIT {limit};
    """
    rows = query_postgres(sql)
    decisions = []
    for row in rows:
        if len(row) >= 2:
            content = row[0]
            decisions.append({
                "content": content[:150] + "..." if len(content) > 150 else content,
                "date": row[1]
            })
    return decisions


def get_open_contradictions() -> list[dict]:
    """Get unresolved contradictions from memory relationships."""
    sql = """
    SELECT m1.content, m2.content
    FROM memory_relationships r
    JOIN memories m1 ON r.from_memory_id = m1.id
    JOIN memories m2 ON r.to_memory_id = m2.id
    WHERE r.relationship_type = 'CONTRADICTS'
      AND m1.archived_at IS NULL
      AND m2.archived_at IS NULL
    LIMIT 3;
    """
    rows = query_postgres(sql)
    contradictions = []
    for row in rows:
        if len(row) >= 2:
            contradictions.append({
                "a": row[0][:100] + "..." if len(row[0]) > 100 else row[0],
                "b": row[1][:100] + "..." if len(row[1]) > 100 else row[1]
            })
    return contradictions


def get_research_progress() -> dict:
    """Check research worker progress from manifest."""
    manifest_path = Path(
        os.environ.get(
            "SILL_RESEARCH_MANIFEST",
            str(PROJECT_DIR / "docs" / "research-manifest.json"),
        )
    )
    try:
        import json as json_mod
        with open(manifest_path) as f:
            manifest = json_mod.load(f)
        stats = manifest.get("stats", {})
        return {
            "completed": stats.get("completed", 0),
            "total": stats.get("total_sources", 0),
            "memories": stats.get("total_memories_stored", 0)
        }
    except Exception:
        return {}


def write_orientation_file(
    focus: str,
    memories: list[dict],
    goals: list[str],
    decisions: list[dict],
    contradictions: list[dict],
    research: dict
):
    """Write the orientation markdown file."""
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    title = os.environ.get("SILL_ORIENTATION_TITLE", "Sill Orientation")
    lines = [
        "<!--",
        "  Auto-generated by precompact-snapshot.py",
        f"  Generated: {datetime.now().isoformat()}",
        "-->",
        "",
        f"# {title}",
        ""
    ]

    # Current focus
    if focus:
        lines.extend([
            "## Current Focus",
            "",
            focus,
            ""
        ])

    # Active goals
    if goals:
        lines.extend(["## Active Goals", ""])
        for goal in goals:
            lines.append(f"- {goal}")
        lines.append("")

    # Strategic decisions
    if decisions:
        lines.extend(["## Recent Decisions", ""])
        for d in decisions:
            lines.append(f"- [{d['date']}] {d['content']}")
        lines.append("")

    # Open contradictions
    if contradictions:
        lines.extend(["## Open Tensions", ""])
        for c in contradictions:
            lines.append(f"- **A**: {c['a']}")
            lines.append(f"  **B**: {c['b']}")
        lines.append("")

    # Research progress
    if research and research.get("total", 0) > 0:
        pct = (research["completed"] / research["total"]) * 100
        lines.extend([
            "## Research Progress",
            "",
            f"- {research['completed']}/{research['total']} sources ({pct:.1f}%)",
            f"- {research['memories']} memories stored",
            ""
        ])

    # Recent memories
    if memories:
        lines.extend(["## Recent High-Value Memories", ""])
        for mem in memories:
            lines.append(f"- [{mem['type']}] {mem['content']}")
        lines.append("")

    OUTPUT_FILE.write_text("\n".join(lines))
    return str(OUTPUT_FILE)


def main():
    """Main hook entry point."""
    focus = get_current_focus()
    memories = get_recent_memories(limit=5)
    goals = get_active_goals()
    decisions = get_strategic_decisions(limit=3)
    contradictions = get_open_contradictions()
    research = get_research_progress()

    if any([focus, memories, goals, decisions, contradictions, research]):
        path = write_orientation_file(
            focus, memories, goals, decisions, contradictions, research
        )
        print(json.dumps({
            "status": "updated",
            "path": path,
            "sections": {
                "focus": bool(focus),
                "memories": len(memories),
                "goals": len(goals),
                "decisions": len(decisions),
                "contradictions": len(contradictions),
                "research": bool(research)
            }
        }))
    else:
        print(json.dumps({"status": "skipped", "reason": "no content"}))


if __name__ == "__main__":
    main()
