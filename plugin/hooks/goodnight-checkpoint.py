#!/usr/bin/env python3
"""
Goodnight checkpoint hook.

Fires on UserPromptSubmit. If the user's message matches a configured
goodnight trigger, writes a daily-checkpoint markdown summarizing today's
memory activity and optionally updates a 'focus' drive for tomorrow.

Environment:
  SILL_DB_CONTAINER         Postgres container name (default: sill_db)
  SILL_DB_USER              Postgres user (default: sill)
  SILL_DB_NAME              Postgres database (default: sill)
  SILL_PROJECT_ROOT         Where to write daily logs (default: cwd)
  SILL_PLUGIN_DIR           Where config files live (default: parent of this file's parent)
  SILL_GOODNIGHT_FOCUS_DRIVE  Name of drives row to update (default: none — skip)

Config files:
  {SILL_PLUGIN_DIR}/config/goodnight-triggers.txt
    One phrase per line. Substrings; case-insensitive. If missing, uses a builtin default.

Daily logs land at:
  {SILL_PROJECT_ROOT}/logs/daily-checkpoints/YYYY-MM-DD.md
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
import _store

DB_CONTAINER = os.environ.get("SILL_DB_CONTAINER", "sill_db")
DB_USER = os.environ.get("SILL_DB_USER", "sill")
DB_NAME = os.environ.get("SILL_DB_NAME", "sill")
PROJECT_ROOT = Path(os.environ.get("SILL_PROJECT_ROOT", os.getcwd()))
PLUGIN_DIR = Path(os.environ.get("SILL_PLUGIN_DIR", Path(__file__).parent.parent))
FOCUS_DRIVE = os.environ.get("SILL_GOODNIGHT_FOCUS_DRIVE", "")

LOGS_DIR = PROJECT_ROOT / "logs" / "daily-checkpoints"
TRIGGERS_FILE = PLUGIN_DIR / "config" / "goodnight-triggers.txt"

DEFAULT_TRIGGERS = [
    "goodnight",
    "good night",
    "going to sleep",
    "going to bed",
    "off to bed",
    "calling it a night",
    "signing off",
    "see you tomorrow",
]


def load_triggers() -> list[str]:
    if TRIGGERS_FILE.is_file():
        lines = [line.strip().lower() for line in TRIGGERS_FILE.read_text().splitlines()]
        return [line for line in lines if line and not line.startswith("#")]
    return DEFAULT_TRIGGERS


def is_goodnight_message(message: str, triggers: list[str]) -> bool:
    text = message.lower()
    return any(re.search(rf"\b{re.escape(t)}\b", text) for t in triggers)


def query_postgres(sql: str) -> list[list[str]]:
    if 'SILL_INSTANCE_ENV_FILE' in os.environ:
        return _store.selected_legacy_rows(sql, unavailable_message=
            '[sill] Selected store unavailable; checkpoint query unchecked.')
    cmd = [
        "docker", "exec", DB_CONTAINER,
        "psql", "-U", DB_USER, "-d", DB_NAME,
        "-t", "-A", "-F", "|",
        "-c", sql,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return []
        return [line.split("|") for line in result.stdout.strip().splitlines() if line.strip()]
    except Exception:
        return []


def get_todays_memories(limit: int = 10) -> list[dict]:
    sql = f"""
    SELECT content, type, importance
    FROM memories
    WHERE archived_at IS NULL
      AND created_at::date = CURRENT_DATE
    ORDER BY importance DESC, created_at DESC
    LIMIT {limit};
    """
    rows = query_postgres(sql)
    return [{"content": r[0][:150], "type": r[1], "importance": r[2]} for r in rows if len(r) >= 3]


def get_todays_memory_counts() -> dict[str, int]:
    rows = query_postgres(
        "SELECT type, COUNT(*) FROM memories WHERE created_at::date = CURRENT_DATE GROUP BY type;"
    )
    return {r[0]: int(r[1]) for r in rows if len(r) >= 2}


def write_daily_log(counts: dict[str, int], memories: list[dict]) -> str:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = LOGS_DIR / f"{date_str}.md"

    lines = [f"# Daily Checkpoint: {date_str}", ""]

    if counts:
        lines += ["## Memories created today", ""]
        for mtype, count in counts.items():
            lines.append(f"- {mtype}: {count}")
        lines.append("")

    if memories:
        lines += ["## Highlights", ""]
        for m in memories:
            lines.append(f"- [{m['type']}, importance {m['importance']}] {m['content']}...")
        lines.append("")

    if not counts and not memories:
        lines += ["_No memory activity today._", ""]

    log_file.write_text("\n".join(lines))
    return str(log_file)


def update_focus(focus_text: str) -> None:
    if not FOCUS_DRIVE:
        return
    sql = (
        "UPDATE drives "
        f"SET current_focus = '{focus_text.replace(chr(39), chr(39) * 2)}', focus_updated_at = NOW() "
        f"WHERE name = '{FOCUS_DRIVE}';"
    )
    query_postgres(sql)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        # Valid JSON that isn't an object (e.g. a bare array) is exactly as
        # unusable as invalid JSON — payload.get(...) below raises
        # AttributeError on a list.
        payload = {}
    message = payload.get("prompt") or payload.get("message") or ""

    triggers = load_triggers()
    if not is_goodnight_message(message, triggers):
        sys.exit(0)

    counts = get_todays_memory_counts()
    memories = get_todays_memories()
    log_path = write_daily_log(counts, memories)

    total = sum(counts.values())
    summary = (
        f"Last checkpoint: {datetime.now().strftime('%Y-%m-%d')}. "
        f"Created {total} memories today."
    )
    update_focus(summary)

    result = {
        "systemMessage": f"[sill] Goodnight checkpoint written → {log_path}",
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
