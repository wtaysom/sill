#!/usr/bin/env python3
"""
The Sill — mint path for durable memory.

  notice(content, type, concepts, importance, force, speaker, source, ...)
      Store something important as a speech act: what it says, and who said
      it with what illocutionary force (assertive/directive/commissive/
      expressive/declaration).

Used by CLI hooks, detached workers, and any future agent. All queries go
through subprocess to docker exec psql.

orient/check/stance are house- and ollama-bound and are deferred to Plan 5 —
this module carries the mint path only.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


import provenance
from uuid import UUID

from session_identity import mint_context


def _load_backend_env() -> None:
    """Read `backend/.env` into the process environment, with Compose's
    precedence: an already-exported shell variable wins, the file fills in the
    rest. Mirrors `load_env_file` in verify.sh/upgrade.sh and `load_env` in
    memory_health.py.

    Why the mint path needs this. These three variables used to come from the
    process environment only, and nothing put them there. Inside a beat they
    were nonetheless right, by a route nobody had traced: `sill-worker`
    imports `worker.py`, whose module-level `load_dotenv()` resolves `.env`
    against **worker.py's own directory** rather than the cwd, so `backend/.env`
    ended up in the worker's environment, and `beat_worker.spawn_beat()` hands
    the child `{**os.environ, …}` wholesale. From the operator's own shell, in
    the same directory, the same bare `sill notice` fell back to `sill_db` and
    failed — reproduced twice during the v0.2.0 acceptance rehearsal, which
    recorded the mechanism as undetermined (docs/RELEASE-REHEARSAL.md §4).

    Two things were wrong with that. The mint path's database coordinates
    should not arrive as an unrelated module's import side effect: a worker
    started as `python -m beat_worker` never imports `worker`, and would mint
    against the defaults — which, on a machine with a second Sill, is another
    install's container. And the asymmetry is itself a trap: a command that
    works inside a beat and fails from the shell that launched it is exactly
    the surprise this file's error reporting exists to prevent.
    """
    env_path = Path(__file__).resolve().parent / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return          # no .env is the normal single-stack case, not an error
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_backend_env()

DB_CONTAINER = os.environ.get("SILL_DB_CONTAINER", "sill_db")
DB_USER = os.environ.get("SILL_DB_USER", "sill")
DB_NAME = os.environ.get("SILL_DB_NAME", "sill")

VALID_TYPES = ("semantic", "episodic", "procedural", "strategic")


def _query_db(sql: str, timeout: int = 10) -> list[list[str]]:
    """Run SQL against the memory database. Returns rows as lists of strings."""
    cmd = [
        "docker", "exec", DB_CONTAINER,
        "psql", "-U", DB_USER, "-d", DB_NAME,
        "-t", "-A", "-F", "|||", "-c", sql
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            # Surface the DB error instead of failing silently — a swallowed
            # stderr here is indistinguishable from a genuine no-op success at
            # the call site, and reports as a bare "Failed to store" with no
            # way to diagnose why.
            if result.stderr.strip():
                print(f"[sill db] {result.stderr.strip()}", file=sys.stderr)
            return []
        rows = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                rows.append(line.split("|||"))
        return rows
    except Exception as exc:
        print(f"[sill db] {type(exc).__name__}: {exc}", file=sys.stderr)
        return []


def _escape(text: str) -> str:
    """Escape single quotes for SQL."""
    return text.replace("'", "''")


def _sidecar_path() -> Path:
    """Where surfaced-memory-IDs from this turn get appended for reuse tracking.

    Session-keyed when CLAUDE_SESSION_ID is in env (interactive callers and
    spawned subprocesses both have it); falls back to a shared 'recent' file
    otherwise so bare CLI runs still produce a signal a Stop hook can scan
    within a time window.
    """
    sid = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    base = Path(os.environ.get("SILL_LOG_DIR", "/tmp"))
    if sid:
        return base / f"recall-sidecar-{sid}.jsonl"
    return base / "recall-sidecar-recent.jsonl"


def _write_sidecar(source: str, memories: list[dict]) -> None:
    """Append surfaced memories to the session sidecar so a reuse-tracking
    hook can detect reuse on recall paths that don't go through an MCP tool.
    Silent on any failure — instrumentation, not load-bearing.
    """
    if not memories:
        return
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "memories": [
                {"id": str(m.get("id", "")), "content": (m.get("content") or "")[:400]}
                for m in memories if m.get("id")
            ],
        }
        if not entry["memories"]:
            return
        with open(_sidecar_path(), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Notice — the mint path
# ---------------------------------------------------------------------------

VALID_FORCES = {"assertive", "directive", "commissive", "expressive", "declaration"}


def _source_json(source: str,
                 source_kind: str | None = None,
                 source_label: str | None = None) -> str:
    """Build the source_attribution jsonb literal for a memory's origin.

    `source` is the address a later reader can actually open — a repo-relative
    path, or a URI. Without it mint_memory() stamps kind='unattributed', and
    the memory becomes unre-readable: nothing in it says which file to reopen.
    """
    kind = source_kind or ("url" if source.startswith(("http://", "https://")) else "file")
    payload: dict[str, str] = {"kind": kind, "ref": source}
    if source_label:
        payload["label"] = source_label
    return _escape(json.dumps(payload))


# This literal is the wire contract with the receipt gate in the instance's
# standing prompts; change it nowhere or everywhere.
RECEIPT_PLACEHOLDER = "Stored: MINT-PENDING — no receipt yet"


def write_receipt_to(path_str: str, receipt_line: str) -> str:
    """Replace the literal placeholder line in a journal with the mint's receipt.

    The store holds the pen: a receipt the hand writes is mention and
    forgeable by construction; a receipt the store writes is the act layer
    extending into the prose layer. Requirements: the target line, stripped,
    must equal RECEIPT_PLACEHOLDER exactly — quoted/backticked occurrences
    (specimens, prompt text) never match. Zero or multiple anchors: write
    nothing, say so. Never raises and never fails the mint; provenance lives
    on the wire (the command and this function's printed status line).
    """
    try:
        p = Path(path_str).expanduser()
        if not p.is_file():
            return (f"receipt-to: {path_str} not found — receipt NOT written; "
                    "paste it by Edit per the gate")
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        idxs = [i for i, ln in enumerate(lines)
                if ln.strip() == RECEIPT_PLACEHOLDER]
        if not idxs:
            if RECEIPT_PLACEHOLDER in text:
                return ("receipt-to: placeholder occurs only inside a longer "
                        "line (quoted specimen?) — receipt NOT written; paste "
                        "it by Edit per the gate")
            return ("receipt-to: no placeholder anchor found — receipt NOT "
                    "written; paste it by Edit per the gate")
        if len(idxs) > 1:
            return (f"receipt-to: {len(idxs)} placeholder anchors — ambiguous, "
                    "receipt NOT written; repair the file, then paste by Edit")
        i = idxs[0]
        eol = "\n" if lines[i].endswith("\n") else ""
        indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
        lines[i] = f"{indent}{receipt_line}{eol}"
        p.write_text("".join(lines), encoding="utf-8")
        return f"Receipt written by the store into {path_str} (line {i + 1})"
    except Exception as e:  # pragma: no cover — belt for the never-fail contract
        return (f"receipt-to: failed ({e.__class__.__name__}: {e}) — receipt "
                "NOT written; paste it by Edit per the gate")


def notice(content: str, memory_type: str = "semantic",
           concepts: list[str] | None = None,
           importance: float = 0.7,
           force: str | None = None,
           speaker: str | None = None,
           source: str | None = None,
           source_kind: str | None = None,
           source_label: str | None = None,
           production: dict | str | None = None,
           minted_in: dict | None = None,
           goal: str | UUID | None = None,
           supersedes: str | UUID | None = None) -> str | None:
    """Store something important. Returns memory ID or None on failure.

    Atomically mints the act and its concepts; retries leave the existing act unchanged.

    Speech-act tags (migration 001):
      force   — illocutionary force: assertive/directive/commissive/expressive/declaration.
                Only 'assertive' is truth-scored; the others succeed by
                complied/kept/sincere/felicitous. Default None = untagged (≈ assertive).
      speaker — whose act this records (perspective axis). e.g. a person's name, the instance's name, or a source author.

    Origin (source_attribution):
      source  — where this came from, as an address a later reader can open:
                the beat file, journal, doc, or URI. Re-reading the source is the
                only known correction for paraphrase drift, so a memory that can't
                name its origin can't be repaired.
    """
    if force is not None and force not in VALID_FORCES:
        raise ValueError(f"force must be one of {sorted(VALID_FORCES)} or None, got {force!r}")
    if memory_type not in {"semantic", "episodic", "procedural", "strategic"}:
        raise ValueError(f"invalid memory type: {memory_type!r}")
    goal = str(UUID(str(goal))) if goal is not None else None
    supersedes = str(UUID(str(supersedes))) if supersedes is not None else None
    stamp = minted_in if minted_in is not None else mint_context("cli:notice")
    production = provenance.production_from_value(production, session=stamp.get("session"),
                     as_=stamp.get("as"), when=stamp.get("when"))
    production_arg = "NULL" if production is None else "'" + _escape(json.dumps(production)) + "'::jsonb"
    stamp_arg = "'" + _escape(json.dumps(stamp)) + "'::jsonb"
    source_arg = (f"'{_source_json(source, source_kind, source_label)}'::jsonb"
                  if source else "NULL")
    speaker_arg = f"'{_escape(speaker)}'" if speaker is not None else "NULL"
    force_arg = f"'{force}'" if force is not None else "NULL"
    concepts_arg = ("ARRAY[" + ",".join(f"'{_escape(c)}'" for c in concepts) + "]::text[]"
                    if concepts is not None else "NULL")
    # One statement owns attribution, subtype, graph, concepts and home. A failed
    # link rolls back the mint (and predecessor archival for supersession).
    if supersedes is not None:
        metadata = {"speaker": speaker, "force": force, "production": production, "minted_in": stamp}
        if concepts is not None:
            metadata["concepts"] = [{"name": c} for c in concepts]
        metadata_arg = "'" + _escape(json.dumps(metadata)) + "'::jsonb"
        if source:
            metadata_arg += f" || jsonb_build_object('source_attribution', {source_arg})"
        mint_sql = f"""SELECT public.supersede_memory(
            '{supersedes}'::uuid,'{_escape(content)}',NULL,
            {metadata_arg}) AS memory_id,
            'inserted'::text AS outcome,NULL::uuid AS restates"""
    else:
        mint_sql = f"""SELECT * FROM public.mint_memory(
            '{memory_type}'::memory_type, '{_escape(content)}', {float(importance)}, {source_arg},
            p_speaker => {speaker_arg}, p_force => {force_arg}, p_concepts => {concepts_arg},
            p_production => {production_arg}, p_minted_in => {stamp_arg}
        )"""
    if goal is not None:
        sql = f"""WITH minted AS MATERIALIZED ({mint_sql}),
            linked AS (
                INSERT INTO goal_memory_links(memory_id,goal_id,link_type)
                SELECT memory_id,'{goal}'::uuid,'home' FROM minted WHERE outcome='inserted'
                RETURNING memory_id
            )
            SELECT m.memory_id::text,m.outcome,m.restates::text
            FROM minted m LEFT JOIN linked l ON l.memory_id=m.memory_id"""
    else:
        sql = f"SELECT memory_id::text,outcome,restates::text FROM ({mint_sql}) minted"
    rows = _query_db(sql, timeout=60)
    if not rows or not rows[0]:
        return None
    memory_id, outcome, restates = rows[0]
    logging.getLogger(__name__).info("memory mint id=%s outcome=%s restates=%s", memory_id, outcome, restates)
    if outcome == "reused":
        print("Reused existing memory; its attribution, concepts and home are unchanged. Use link_commitment explicitly to add a home.", file=sys.stderr)
    elif restates:
        print(f"Restates memory {restates}.", file=sys.stderr)
    return memory_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """The `python sill.py <cmd>` parser.

    Only `notice` (the mint path) lives here for now — orient/check/stance
    are house- and ollama-bound and are deferred to Plan 5.
    """
    parser = argparse.ArgumentParser(
        prog="sill.py",
        description="The Sill — mint path for durable memory.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_notice = sub.add_parser("notice", help="Store something important")
    p_notice.add_argument("content", help="Content to store (use quotes)")
    p_notice.add_argument("type", nargs="?", default="semantic",
                          choices=VALID_TYPES,
                          help="Memory type (default: semantic)")
    p_notice.add_argument("--concepts", action="append", default=None,
                          help="Comma-separated concept tags (flag may repeat; "
                               "repeated flags accumulate)")
    p_notice.add_argument("--importance", type=float, default=0.7,
                          help="Importance 0.0-1.0 (default: 0.7)")
    p_notice.add_argument("--force", default=None, choices=sorted(VALID_FORCES),
                          help="Illocutionary force (speech-act model): assertive/directive/"
                               "commissive/expressive/declaration. Default untagged (≈assertive).")
    p_notice.add_argument("--speaker", required=True,
                          help="Whose act this records (the perspective axis). Required: "
                               "unattributed mints are the store's main hygiene hole.")
    made = p_notice.add_mutually_exclusive_group()
    made.add_argument("--made", help="Declared contributors: who [as Voice]: work [@ address] [basis]; compact self requires a supplied session")
    made.add_argument("--made-json", help="Full typed production JSON object; JSON null leaves production undeclared")
    p_notice.add_argument("--source", default=None,
                          help="Where this came from, as an address a later reader can OPEN: "
                               "the beat file, journal, doc, or URI. Without it the memory is "
                               "stamped 'unattributed' and cannot be re-read against its origin.")
    p_notice.add_argument("--source-kind", default=None,
                          help="Override the inferred kind (file/url). e.g. beat, twitter, book.")
    p_notice.add_argument("--source-label", default=None,
                          help="Human label for the source, e.g. 'journal-042'.")
    p_notice.add_argument("--goal", type=UUID, metavar="UUID",
                          help="Home goal for a new commitment. Reused records require explicit link_commitment.")
    p_notice.add_argument("--supersedes", type=UUID, metavar="UUID",
                          help="Replace this live memory atomically; the successor keeps its predecessor's type.")
    p_notice.add_argument("--receipt-to", default=None, metavar="FILE",
                          help="Journal file whose literal placeholder line "
                               "('Stored: MINT-PENDING — no receipt yet') the "
                               "store replaces with this mint's receipt. The "
                               "store holds the pen: your job becomes verifying "
                               "the slot changed, not writing it. On zero/multiple "
                               "anchors the mint still succeeds and the receipt is "
                               "NOT written — fall back to paste-by-Edit per the gate.")

    return parser


def flatten_concepts(values: list[str] | None) -> list[str] | None:
    """Flatten --concepts (an append-action list, each item itself possibly a
    comma-separated chunk) into one flat list of individual concept tags.
    Returns None, not [], when no flag was given at all."""
    if not values:
        return None
    return [c.strip() for chunk in values for c in chunk.split(",") if c.strip()]


def format_receipt(mid: str, concepts: list[str] | None,
                    speaker: str | None, force: str | None) -> str:
    """Build the wire-format receipt line for a completed mint: 'Stored: <id>
    [...]'. This is the exact string write_receipt_to splices into a waiting
    journal placeholder."""
    tag_note = (f" [{len(concepts)} tags]" if concepts
                else " [WARNING: no concept tags — memory won't surface in concept search]")
    sa_note = f" [{speaker or '?'}/{force or 'untagged'}]" if (speaker or force) else ""
    return f"Stored: {mid}{tag_note}{sa_note}"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "notice":
        concepts = flatten_concepts(args.concepts)
        mid = notice(args.content, memory_type=args.type,
                     concepts=concepts, importance=args.importance,
                     force=args.force, speaker=args.speaker,
                     source=args.source, source_kind=args.source_kind,
                     source_label=args.source_label,
                     goal=args.goal, supersedes=args.supersedes,
                     production=json.loads(args.made_json) if args.made_json is not None else args.made)
        if mid:
            receipt = format_receipt(mid, concepts, args.speaker, args.force)
            print(receipt)
            if args.receipt_to:
                print(write_receipt_to(args.receipt_to, receipt))
            return 0
        print("Failed to store")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
