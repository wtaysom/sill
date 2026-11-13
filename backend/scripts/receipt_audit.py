"""Check Stored receipts against the configured memory store and attempt bounds."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from instance_config import InstanceConfigError, instance_dsn, load_instance_env
from scripts.beat_audit import AuditResult, Finding, Window, run_audit_cli


# Match the whole token so an invented suffix cannot be accepted as a prefix.
LIST_MARKER = r"(?:[-+*]|\d{1,9}[.)])[ \t]+"
SLOT_RE = re.compile(
    rf"^[ \t]*(?:{LIST_MARKER})?(?:#{{1,6}}[ \t]+)?[*_]*Stored(?:[*_]{{2}})?"
    r"\s*:(?:[*_]{2})?\s*",
    re.IGNORECASE,
)
TOKEN = re.compile(r"[0-9a-f]{8}[A-Za-z0-9_-]*", re.IGNORECASE)
KEY_START = re.compile(r"[0-9a-f]{8}", re.IGNORECASE)
SUPERSEDES_RE = re.compile(r"\bsupersed\w*\s+", re.IGNORECASE)
PENDING_RE = re.compile(r"MINT-PENDING\b", re.IGNORECASE)
FENCE_RE = re.compile(rf"^[ \t]*(?:{LIST_MARKER})?(`{{3,}}|~{{3,}})(.*)$")
BACKTICKS_RE = re.compile(r"`+")
NEW_BLOCK_RE = re.compile(rf"^[ \t]*(?:{LIST_MARKER}|#{{1,6}}[ \t]+|>)")
HEADING_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|>)")
UUID_SEGMENTS = (8, 4, 4, 4, 12)
QUERY_TIMEOUT = 20
MAX_KEYS = 1024
QUERY = """
SELECT key, matched.id::text, matched.created_at
FROM unnest({keys}) AS prefixes(key)
CROSS JOIN LATERAL (
    SELECT id, created_at FROM public.memories
    WHERE id BETWEEN rpad(replace(key, '-', ''), 32, '0')::uuid
                 AND rpad(replace(key, '-', ''), 32, 'f')::uuid
    ORDER BY id LIMIT 2
) AS matched
"""

Rows = dict[str, list[tuple[str, datetime]]]


class StoreUnreachable(RuntimeError):
    """The configured store could not complete a receipt lookup."""


def _valid_key(key: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{8}[0-9a-f-]{0,28}", key):
        return False
    segments = key.split("-")
    # The complete segments must align with canonical UUID hyphens. An
    # empty final segment is a prefix ending just after a separator.
    return (
        len(segments) <= len(UUID_SEGMENTS)
        and all(len(part) == expected for part, expected in zip(segments[:-1], UUID_SEGMENTS))
        and len(segments[-1]) <= UUID_SEGMENTS[len(segments) - 1]
    )


@dataclass(frozen=True)
class Reference:
    line: int
    kind: str
    key: str
    parent: str = ""

    @property
    def fingerprint(self) -> tuple[str, str, str]:
        return self.kind, self.key, self.parent


def _code_spans(text: str, start: int = 0) -> Iterator[tuple[re.Match, re.Match]]:
    """Pair backtick runs of the same length within a Markdown block."""
    markers = list(BACKTICKS_RE.finditer(text, start))
    next_by_length = {}
    closes = {}
    for index in range(len(markers) - 1, -1, -1):
        length = len(markers[index][0])
        if length in next_by_length:
            closes[index] = next_by_length[length]
        next_by_length[length] = index

    index = 0
    while index < len(markers):
        opening = markers[index]
        before = opening.start()
        while before and text[before - 1] == "\\":
            before -= 1
        if (opening.start() - before) % 2 == 0 and index in closes:
            index = closes[index]
            yield opening, markers[index]
        index += 1


def _slot_value(text: str, start: int) -> tuple[str, int] | None:
    if opening := BACKTICKS_RE.match(text, start):
        for closing in BACKTICKS_RE.finditer(text, opening.end()):
            if closing[0] == opening[0]:
                return text[opening.end():closing.start()].strip().lower(), closing.end()
        return None
    if match := TOKEN.match(text, start) or PENDING_RE.match(text, start):
        return match[0].lower(), match.end()
    return None


def _supersedes(text: str, start: int) -> Iterator[str]:
    code_spans = _code_spans(text, start)
    for opening, closing in code_spans:
        yield from _supersedes_in_text(text, start, opening.start())
        start = closing.end()
    yield from _supersedes_in_text(text, start, len(text))


def _supersedes_in_text(text: str, start: int, end: int) -> Iterator[str]:
    for marker in SUPERSEDES_RE.finditer(text, start, end):
        target = _slot_value(text, marker.end())
        if target and KEY_START.match(target[0]):
            yield target[0]


def _blocks(text: str) -> Iterator[tuple[int, list[str]]]:
    """Group adjacent inline lines without crossing block boundaries or fences."""
    lines: list[str] = []
    start = 1
    fence = ""
    for number, line in enumerate(text.splitlines(), 1):
        marker = FENCE_RE.match(line)
        if fence:
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = ""
            continue
        if lines and (marker or not line.strip() or NEW_BLOCK_RE.match(line)):
            yield start, lines
            lines = []
        if marker:
            fence = marker[1]
        elif HEADING_RE.match(line):
            yield number, [line]
        elif line.strip():
            if not lines:
                start = number
            lines.append(line)
    if lines:
        yield start, lines


def _references(text: str) -> Iterator[Reference]:
    for start, lines in _blocks(text):
        spans = _code_spans("\n".join(lines))
        active = next(spans, None)
        offset = 0
        for number, line in enumerate(lines, start):
            while active and active[1].end() <= offset:
                active = next(spans, None)
            inside_code = active is not None and active[0].start() < offset
            offset += len(line) + 1
            slot = SLOT_RE.match(line)
            if inside_code or slot is None:
                continue
            value = _slot_value(line, slot.end())
            if value is None:
                continue
            key, end = value
            if key == "mint-pending":
                yield Reference(number, "pending", "")
            elif KEY_START.match(key):
                yield Reference(number, "receipt", key)
                for superseded in _supersedes(line, end):
                    yield Reference(number, "supersedes", superseded, key)


async def _read_selected(payload: dict) -> list[list[str]]:
    import asyncpg

    connection = await asyncpg.connect(
        payload["dsn"], timeout=5, command_timeout=10,
        server_settings={"statement_timeout": "10000"},
    )
    try:
        async with connection.transaction(readonly=True):
            rows = await connection.fetch(QUERY.format(keys="$1::text[]"), payload["keys"])
            return [[key, memory_id, created_at.isoformat()] for key, memory_id, created_at in rows]
    finally:
        await connection.close(timeout=2)


def _selected_rows() -> None:
    """Run the isolated direct query; the parent reports only redacted errors."""
    import asyncio

    try:
        rows = asyncio.run(_read_selected(json.load(sys.stdin)))
    except Exception:
        raise SystemExit(1) from None
    print(json.dumps(rows))


def lookup_rows(keys: set[str]) -> Rows:
    """Read up to two rows per UUID or prefix, enough to detect ambiguity.

    Canonical UUID prefixes map to contiguous intervals in UUID sort order,
    so the database can use the primary key index.
    """
    keys = {key for key in keys if _valid_key(key)}
    if not keys:
        return {}
    if len(keys) > MAX_KEYS:
        raise StoreUnreachable(f"Receipt lookup exceeds {MAX_KEYS} distinct keys")
    selected = os.environ.get("SILL_INSTANCE_ENV_FILE")
    try:
        if selected is not None:
            dsn = instance_dsn(load_instance_env(Path(selected)))
            env = {
                key: value for key, value in os.environ.items()
                if not key.startswith(("PG", "POSTGRES_", "SILL_DB_"))
            }
            result = subprocess.run(
                [sys.executable, "-E", "-c", "from scripts.receipt_audit import _selected_rows; _selected_rows()"],
                input=json.dumps({"dsn": dsn, "keys": sorted(keys)}), env=env,
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, timeout=QUERY_TIMEOUT,
            )
        else:
            # Every interpolated value passed _valid_key; only ASCII hex and hyphens remain.
            array = ", ".join(f"'{key}'" for key in sorted(keys))
            result = subprocess.run([
                "docker", "exec",
                "-e", "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=10000"
                " -c TimeZone=UTC -c DateStyle=ISO,YMD",
                os.environ.get("SILL_DB_CONTAINER", "sill_db"),
                "psql", "-U", os.environ.get("SILL_DB_USER", "sill"),
                "-d", os.environ.get("SILL_DB_NAME", "sill"),
                "-X", "-q", "-t", "--csv", "-v", "ON_ERROR_STOP=1",
                "-c", QUERY.format(keys=f"ARRAY[{array}]"),
            ], capture_output=True, text=True, timeout=QUERY_TIMEOUT)
        if result.returncode != 0:
            target = "selected instance" if selected is not None else "Docker store"
            raise StoreUnreachable(f"Receipt lookup failed in the {target}")
        wire = (json.loads(result.stdout) if selected is not None else
                csv.reader(io.StringIO(result.stdout, newline=""), strict=True))
        rows: Rows = {key: [] for key in keys}
        for key, memory_id, created in wire:
            if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", memory_id):
                raise ValueError
            timestamp = datetime.fromisoformat(created)
            if timestamp.utcoffset() is None or not memory_id.startswith(key):
                raise ValueError
            rows[key].append((memory_id, timestamp))
        return rows
    except InstanceConfigError as exc:
        raise StoreUnreachable(f"Selected instance configuration is unavailable: {exc}") from None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, csv.Error):
        raise StoreUnreachable("Receipt lookup could not complete or returned invalid rows") from None


def audit(
    before: dict[Path, str] | None,
    after: dict[Path, str],
    window: Window | None,
    *,
    lookup: Callable[[set[str]], Rows] = lookup_rows,
    grace_seconds: float = 90,
) -> AuditResult:
    """Check current IDs for existence and time-check only new receipt occurrences."""
    current = {path: list(_references(text)) for path, text in after.items()}
    keys = {
        ref.key for references in current.values() for ref in references if _valid_key(ref.key)
    }
    reasons = []
    if window is None or before is None:
        reasons.append("Receipt timing requires a completed attempt and a baseline")
    try:
        rows = lookup(keys) if keys else {}
    except StoreUnreachable as exc:
        rows = None
        reasons.append(str(exc))

    grace = timedelta(seconds=grace_seconds)
    findings = []
    for path, references in current.items():
        previous = Counter(ref.fingerprint for ref in _references((before or {}).get(path, "")))
        for ref in references:
            seen = previous[ref.fingerprint] > 0
            previous[ref.fingerprint] -= 1
            origin = "unknown" if before is None else "preexisting" if seen else "new"
            verdict = _check_reference(ref, rows, window if origin == "new" else None, grace)
            if verdict is not None:
                code, detail = verdict
                findings.append(Finding(str(path), ref.line, code, detail, origin))
    return AuditResult("unchecked" if reasons else "checked", findings, ". ".join(reasons) or None)


def _check_reference(
    ref: Reference, rows: Rows | None, window: Window | None, grace: timedelta,
) -> tuple[str, str] | None:
    if ref.kind == "pending":
        return "mint_pending", "MINT-PENDING remains in the Stored slot"
    if not _valid_key(ref.key):
        return f"invalid_{ref.kind}", f"{ref.key} is not a UUID or UUID prefix"
    if rows is None:
        return None
    matches = rows.get(ref.key, [])
    if not matches:
        return f"missing_{ref.kind}", f"{ref.key} has no matching memory"
    if len(matches) > 1:
        return f"ambiguous_{ref.kind}", f"{ref.key} matches multiple memories"
    if ref.kind == "receipt" and window is not None:
        _, created = matches[0]
        if window.start - created > grace or created - window.end > grace:
            return "outside_window", (
                f"{ref.key} was minted at {created.isoformat()}, outside the attempt "
                f"({window.start.isoformat()} to {window.end.isoformat()})"
            )
    return None


def main(argv: list[str] | None = None) -> int:
    return run_audit_cli(audit, __doc__, argv)


if __name__ == "__main__":
    raise SystemExit(main())
