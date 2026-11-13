#!/usr/bin/env python3
"""
ingest_md_memories.py — bridge the file-based memory store into the Sill DB.

Claude Code's file memory (~/.claude/.../memory/*.md) is loaded as orientation
at session start, but the spontaneous-recall hook only queries the Postgres
`memories` table (see plugin/hooks/spontaneous-recall.py). So memories distilled
into .md files are NEVER surfaced mid-session by similarity to the prompt — only
the seed corpus is. This ingests each .md memory into the DB so recall can reach
it.

Uses sill.notice() -> public.mint_memory() SQL, which:
  - generates the 768-d embedding *inside* the db container (the embeddings
    service is only reachable on the docker network, not from the host),
  - writes the type-specific subtype row,
  - dedups on content (so re-running is safe; unchanged files won't duplicate).

The .md taxonomy (user|feedback|project|reference) maps onto sill's
memory_type (episodic|semantic|procedural|strategic):

    feedback              -> procedural   (how to work)
    project/reference/user -> semantic    (facts / knowledge)

Provenance: the file's `name` slug is attached as a concept, giving a crude
searchable link back to the source file. (A fuller provenance/update-sync pass
-- source_attribution + re-ingest on edit -- is a v2; this v1 just gets the
content into the recalled store.)

Usage:
    python ingest_md_memories.py [--dir DIR] [--dry-run]
"""
import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_DIR = Path(
    "/Users/taysom/.claude/projects/-Volumes-src-sill/memory"
)
TYPE_MAP = {
    "feedback": "procedural",
    "project": "semantic",
    "reference": "semantic",
    "user": "semantic",
}


def _yaml_scalar(v: str) -> str:
    """Decode a YAML scalar without pyyaml: unwrap quotes, unescape.

    The sill memory canonicalizer rewrites frontmatter into double-quoted form
    with \\"-escaped inner quotes (e.g. description: "...\\"mother\\"..."). A
    naive parse keeps those backslashes literally, and sill's get_embedding then
    fails ('invalid input syntax for type bytea'). Decode them away here.
    """
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def parse_md(path: Path) -> dict | None:
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        return None
    front, body = m.group(1), m.group(2).strip()
    name_m = re.search(r"^name:\s*(.+)$", front, re.M)
    name = _yaml_scalar(name_m.group(1)) if name_m else path.stem
    desc_m = re.search(r"^description:\s*(.+)$", front, re.M)
    desc = _yaml_scalar(desc_m.group(1)) if desc_m else ""
    # anchor to line start so `node_type: memory` isn't mistaken for the type
    type_m = re.search(r"^\s*type:\s*(user|feedback|project|reference)", front, re.M)
    md_type = type_m.group(1) if type_m else "project"
    return {"name": name, "description": desc, "body": body, "md_type": md_type}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = sorted(p for p in args.dir.glob("*.md") if p.name != "MEMORY.md")
    if not files:
        print(f"no memory files in {args.dir}")
        return 1

    # imported lazily so --dry-run needs no DB / sill deps
    notice = None
    existing = set()
    if not args.dry_run:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from sill import notice, DB_CONTAINER, DB_USER, DB_NAME  # noqa: E402
        # idempotency: hashes of already-stored content, so re-runs don't dup
        r = subprocess.run(
            ["docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME,
             "-t", "-A", "-c", "SELECT md5(content) FROM memories WHERE status='active'"],
            capture_output=True, text=True, timeout=20)
        existing = {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}

    print(f"{'DRY RUN — ' if args.dry_run else ''}ingesting {len(files)} "
          f"file(s) from {args.dir}\n")
    stored = 0
    for path in files:
        parsed = parse_md(path)
        if not parsed:
            print(f"  skip  {path.name}: no frontmatter")
            continue
        sill_type = TYPE_MAP.get(parsed["md_type"], "semantic")
        # lead with the one-line description so the embedding has a strong
        # summary anchor, then the full fact.
        content = (f"{parsed['description']}\n\n{parsed['body']}"
                   if parsed["description"] else parsed["body"])
        head = parsed["description"][:60] or parsed["name"]
        if args.dry_run:
            print(f"  would store [{sill_type:<10}] {parsed['name']}  «{head}…»")
            continue
        # idempotency: skip if this exact content is already stored (re-run safe)
        if hashlib.md5(content.encode()).hexdigest() in existing:
            print(f"  exists  [{sill_type:<10}] {parsed['name']} (already in DB)")
            continue
        mid = notice(content, memory_type=sill_type,
                     concepts=[parsed["name"]], importance=0.7)
        if mid:
            stored += 1
            print(f"  stored  [{sill_type:<10}] {parsed['name']}  id={mid[:8]}")
        else:
            print(f"  FAILED  {parsed['name']} (dedup hit, or db error)")

    if not args.dry_run:
        print(f"\nstored {stored}/{len(files)}. "
              "Run memory_health.py to see the new corpus size.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
