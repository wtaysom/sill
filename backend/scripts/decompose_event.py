# Ported from agi-memory scripts/decompose_event.py (2026-07-09).
"""Decompose a memorable event into speech-act records and batch-insert them.

The speech-act memory model says the unit of memory is the *event*, which
decomposes into per-force, per-speaker records. This helper does the second half —
the tagged batch insert — leaving the decomposition (deciding which acts an event
contains) to the caller (a beat, moth, or a person).

Key property: all acts of one event are inserted in ONE transaction, so they share
an identical `created_at` (Postgres CURRENT_TIMESTAMP = transaction-start time,
verified at porting). That shared timestamp IS the bundle key — no bundle_id column
needed. Recompose an event later with:  WHERE created_at = '<bundle ts>'.

Input: a JSON spec (file arg, or stdin), e.g.
{
  "event": "Test event on speech-act memory model",
  "speaker": "Ada",                         # default speaker for acts
  "acts": [
    {"force": "directive",  "content": "Build the speech-act migration.", "type": "procedural"},
    {"force": "assertive",  "content": "created_at is transaction-start time.", "importance": 0.6},
    {"force": "commissive", "content": "We will give proper sense of time after.", "speaker": "Ada"}
  ]
}

Usage:
    python3 -m scripts.decompose_event spec.json          # insert
    python3 -m scripts.decompose_event spec.json --dry-run # print the plan only
    echo '{...}' | python3 -m scripts.decompose_event -
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

VALID_FORCES = {"assertive", "directive", "commissive", "expressive", "declaration"}
VALID_TYPES = {"episodic", "semantic", "procedural", "strategic"}
DQ = "$sa$"  # dollar-quote tag for content (guarded below)


def _dq(s: str) -> str:
    if DQ in s:
        raise ValueError(f"content contains the dollar-quote guard {DQ!r}; pick another")
    return f"{DQ}{s}{DQ}"


def build_sql(spec: dict) -> str:
    default_speaker = spec.get("speaker")
    acts = spec.get("acts") or []
    if not acts:
        raise ValueError("spec has no acts")
    lines = ["BEGIN;", "DO $$", "DECLARE ids uuid[] := '{}'; v_id uuid;", "BEGIN"]
    for a in acts:
        force = a.get("force")
        if force is not None and force not in VALID_FORCES:
            raise ValueError(f"bad force {force!r}")
        mtype = a.get("type", "semantic")
        if mtype not in VALID_TYPES:
            raise ValueError(f"bad type {mtype!r}")
        imp = float(a.get("importance", 0.6))
        speaker = a.get("speaker", default_speaker)
        content = a["content"]
        speaker_arg = _dq(speaker) if speaker is not None else "NULL"
        force_arg = f"'{force}'" if force is not None else "NULL"
        lines.append(f"  SELECT memory_id INTO v_id FROM public.mint_memory('{mtype}'::memory_type, {_dq(content)}, {imp}, "
                     f"p_speaker => {speaker_arg}, p_force => {force_arg});")
        lines.append("  ids := array_append(ids, v_id);")
    lines.append("  RAISE NOTICE 'BUNDLE_IDS %', ids;")
    lines.append("END $$;")
    lines.append("COMMIT;")
    return "\n".join(lines)


def run(sql: str) -> str:
    container = os.getenv("SILL_DB_CONTAINER", "sill_db")
    user = os.getenv("SILL_DB_USER", "sill")
    dbname = os.getenv("SILL_DB_NAME", "sill")
    r = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-v", "ON_ERROR_STOP=1",
         "-U", user, "-d", dbname],
        input=sql, text=True, capture_output=True,
    )
    if r.returncode != 0:
        print(r.stdout); print(r.stderr, file=sys.stderr)
        sys.exit(1)
    return r.stdout + r.stderr


def main() -> None:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    src = args[0] if args else "-"
    raw = sys.stdin.read() if src == "-" else open(src).read()
    spec = json.loads(raw)

    sql = build_sql(spec)
    print(f"# event: {spec.get('event','(unnamed)')}  — {len(spec['acts'])} acts")
    if dry:
        print(sql)
        return

    out = run(sql)
    m = re.search(r"BUNDLE_IDS \{([^}]*)\}", out)
    ids = [x.strip() for x in m.group(1).split(",")] if m and m.group(1) else []
    if not ids:
        print(out); return
    # report the bundle: shared created_at + the tagged acts
    idlist = ",".join(f"'{i}'::uuid" for i in ids)
    report = run(
        f"SELECT force, speaker, created_at, LEFT(content,50) "
        f"FROM memories WHERE id IN ({idlist}) ORDER BY force;"
    )
    print(f"inserted {len(ids)} acts as one bundle (shared created_at = the bundle key):")
    print(report)


if __name__ == "__main__":
    main()
