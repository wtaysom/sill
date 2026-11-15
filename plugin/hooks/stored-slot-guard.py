#!/usr/bin/env python3
# Ported from agi-memory .claude/hooks/stored-slot-guard.py (2026-08-04).
"""stored-slot-guard: Write/Edit-time check for mint receipts.

A receipt line naming a memory id that was never actually minted is a
fabrication, and it becomes permanent the moment the Write/Edit lands. This
guard closes that window: it checks any receipt-shaped id against the store
before the tool call is allowed through.

The canonical pre-mint form is the literal placeholder line exported as
RECEIPT_PLACEHOLDER by the mint module (backend/sill.py) — never an
invented id. Placeholder lines pass this guard by construction: the
placeholder text is not hex-id-shaped, so it never matches the receipt-line
pattern below.

Scope: opt-in via SILL_BEAT_JOURNAL_DIRS (colon-separated path fragments).
Unset or empty means no scope at all, so an install that never turned on
the beat worker pays nothing for this check.

Checked form: receipt lines — `Stored:` at line start with an id following.
Mention exemption (head-only): a line is skipped as specimen material when
its HEAD — the text before "Stored"/"stored", matched case-insensitively —
contains a backtick, or when the line is blockquoted. The exemption is
head-only: an id backticked AFTER a bare line-start `Stored:` is still
checked, so a confession should backtick or blockquote the whole receipt
line, not just the id. The head split is case-insensitive by design and
must stay that way: a case-sensitive split finds no match on a lowercase
receipt and returns the WHOLE LINE as head, so any backtick anywhere on
the line — even an unrelated trailing aside — falsely exempts it (this
guard's own 2026-08-04 bug, closed same-day). Known limit: a forgery
wearing full quote typography around the whole line also passes this guard
— a separate act-history check (tool-type-witness.py) and any downstream
record checks cover that side.

Fails OPEN when the store is unreachable — a down store must not block a
journal write. Each id is checked independently, so one unreachable lookup
skips only that id rather than abandoning the rest of the line's ids.

Fires on both harnesses via _harness.tool_kind/written_files: Claude's
Write/Edit and Codex's apply_patch all normalize to "write" or "edit" kind
(see _harness.py). Scope and text are read per FILE via written_files, not
written_path/written_text (which only ever answer for a patch's first
file) — a multi-file apply_patch is judged file by file, so an
out-of-scope first file can no longer hide a forged receipt in an in-scope
second file (2026-08-05 bypass, closed same-day). Also fails open — exits
0, no output — if _harness itself cannot be imported.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
import _store

DB_CONTAINER = os.environ.get("SILL_DB_CONTAINER", "sill_db")
DB_USER = os.environ.get("SILL_DB_USER", "sill")
DB_NAME = os.environ.get("SILL_DB_NAME", "sill")

RECEIPT_LINE = re.compile(
    r"^[\s*\-#]*(?:\*\*)?Stored(?:\*\*)?\s*:\s*`?([0-9a-f]{8})[0-9a-f\-]*",
    re.IGNORECASE,
)


def _resolve_placeholder() -> str | None:
    """Resolve the mint module's receipt placeholder without retyping it —
    the literal (its dash is U+2014) is a wire contract, and a hand-typed
    copy is exactly how a wire contract drifts silently. Import the mint
    module if it is importable; otherwise read the one assignment line
    straight out of its source with a narrow regex. Returns None if both
    paths fail, so the caller can fall back to describing the slot
    generically instead of quoting a literal it could not confirm.
    """
    try:
        import sill
        return sill.RECEIPT_PLACEHOLDER
    except Exception:
        pass
    try:
        sill_py = Path(__file__).resolve().parents[2] / "backend" / "sill.py"
        text = sill_py.read_text(encoding="utf-8")
        m = re.search(r'^RECEIPT_PLACEHOLDER\s*=\s*"(.*)"\s*$', text, re.MULTILINE)
        return m.group(1) if m else None
    except Exception:
        return None


def _in_scope(path: str) -> bool:
    raw = os.environ.get("SILL_BEAT_JOURNAL_DIRS", "")
    fragments = [f for f in raw.split(":") if f]
    return any(f in path for f in fragments)


def _load_harness():
    try:
        spec = importlib.util.spec_from_file_location(
            "_sill_harness", Path(__file__).resolve().parent / "_harness.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


_harness = _load_harness()


def main() -> None:
    if _harness is None:
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    # A non-object payload (e.g. "[]") has no .get() — every hook exits 0
    # on every path, so this is a scope check, not an exception handler.
    if not isinstance(data, dict):
        sys.exit(0)
    if _harness.tool_kind(data) not in ("write", "edit"):
        sys.exit(0)

    # Iterate every file this call touches, not just the first: a multi-file
    # apply_patch's first file deciding scope for the WHOLE call would let an
    # out-of-scope first file hide a forged receipt in an in-scope second
    # file — see _harness.written_files's docstring. Each file is judged
    # (and, if in scope, scanned) on its own path/text pair.
    ids = []
    for path, text in _harness.written_files(data):
        if not _in_scope(path):
            continue
        if "stored" not in text.lower():
            continue
        for line in text.splitlines():
            head = re.split(r"stored", line, maxsplit=1, flags=re.IGNORECASE)[0]
            if "`" in head or line.lstrip().startswith(">"):
                continue  # quoted/backticked context: specimen material, not a receipt
            m = RECEIPT_LINE.match(line)
            if m:
                ids.append(m.group(1).lower())
    if not ids:
        sys.exit(0)

    missing = []
    unchecked = False
    for h in dict.fromkeys(ids):
        try:
            if 'SILL_INSTANCE_ENV_FILE' in os.environ:
                rows = _store.query(f"SELECT count(*) AS n FROM memories WHERE id::text LIKE '{h}%';", timeout=8)
                count = str(rows[0]['n'])
            else:
                r = subprocess.run(
                    ["docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d",
                     DB_NAME, "-t", "-A", "-c",
                     f"SELECT count(*) FROM memories WHERE id::text LIKE '{h}%';"],
                    capture_output=True, text=True, timeout=8,
                )
                if r.returncode != 0:
                    unchecked = True
                    continue
                count = r.stdout.strip()
            if count == "0":
                missing.append(h)
        except Exception:
            unchecked = True

    if unchecked and not missing:
        print(json.dumps({'systemMessage':'[sill] Receipt store unavailable; receipt unchecked.'}))
    if missing:
        reason = (
            f"stored-slot-guard: receipt-shaped id(s) {missing} have no row "
            "in the store — a receipt line naming an id that was never "
            "minted. "
        )
        placeholder = _resolve_placeholder()
        if placeholder:
            reason += (
                "Until the real mint returns, the slot holds the literal "
                f"placeholder line {placeholder!r}. "
            )
        reason += (
            "Run the mint, then paste its printed receipt line verbatim by "
            "Edit. To quote a forged id as a specimen, backtick it or use "
            "quoted context — bare receipt lines are the checked form."
        )
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))
    sys.exit(0)


main()
