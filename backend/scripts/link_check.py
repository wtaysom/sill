"""Audit Markdown links against explicitly configured local directories."""

from __future__ import annotations

import argparse
import os
import re
import stat
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .beat_audit import MARKDOWN_SUFFIXES, AuditResult, Finding, classify_findings


FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
CODE_SPAN_RE = re.compile(r"(`+)(?:(?!\1).)+?\1")
# Keep the house parser's one level of nested brackets and target parentheses.
# A '[' attached to a word is notation such as J[m,n](r,t).
INLINE_RE = re.compile(
    r"(?<![\w])!?\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]\(\s*"
    r"(?:<(?P<angle>[^>\n]*)>|(?P<bare>(?:[^()\s]|\([^()\s]*\))+))"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?\s*\)"
)
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")
REFERENCE_RE = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*(?:<(?P<angle>[^>\n]*)>|(?P<bare>\S+))")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
LINE_SUFFIX_RE = re.compile(r":[0-9]+(?:[-–][0-9]+)?$")
# A bare filename with an extension and a numeric suffix can look like a URI.
FILE_LINE_RE = re.compile(r"^[^:/?#]*\.[A-Za-z0-9]+:[0-9]+(?:[-–][0-9]+)?$")

BROKEN = "broken_link"
UNCHECKED = "link_unchecked"
NO_FILES = "No Markdown files in the link audit scope"
NO_ROOTS = "No link roots configured"


def _iter_links(text: str) -> Iterator[tuple[int, str]]:
    fence = None
    in_paragraph = False
    for number, line in enumerate(text.splitlines(), 1):
        marker = FENCE_RE.match(line)
        if marker:
            run, rest = marker.groups()
            if fence:
                if run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
                    fence = None
                continue
            if run[0] == "~" or "`" not in rest:
                fence = run
                in_paragraph = False
                continue
        if fence:
            continue

        visible = CODE_SPAN_RE.sub(lambda match: " " * len(match.group(0)), line)
        reference = None if in_paragraph else REFERENCE_RE.match(visible)
        if reference:
            yield number, reference["angle"] if reference["angle"] is not None else reference["bare"]
            continue
        in_paragraph = bool(line.strip()) and not HEADING_RE.match(line)
        for match in INLINE_RE.finditer(visible):
            yield number, match["angle"] if match["angle"] is not None else match["bare"]


def _local_target(target: str) -> str | None:
    target = target.strip().split("#", 1)[0].split("?", 1)[0]
    if not target or target.startswith("//"):
        return None
    if target.lower().startswith("file:"):
        try:
            url = urlsplit(target)
        except ValueError:
            return None
        if url.netloc.lower() not in {"", "localhost"} or not url.path.startswith("/"):
            return None
        target = url.path
    elif SCHEME_RE.match(target) and not FILE_LINE_RE.fullmatch(target):
        return None
    # Decode afterwards so an escaped '?' or ':' can be part of a filename.
    return unquote(LINE_SUFFIX_RE.sub("", target)) or None


def _available_roots(root: Path, link_roots: list[Path]) -> list[Path]:
    available = []
    for configured in dict.fromkeys(link_roots):
        try:
            directory = (root / configured).resolve(strict=True)
            if directory.is_dir() and os.access(directory, os.R_OK | os.X_OK):
                available.append(directory)
        except (OSError, RuntimeError, ValueError):
            continue
    return available


def _missing_leaf(target: Path) -> bool:
    """Require a traversable parent and no directory entry for the leaf."""
    try:
        parent_mode = target.parent.stat().st_mode
    except OSError:
        return False
    if not stat.S_ISDIR(parent_mode):
        return False
    try:
        target.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _check_link(target: Path, roots: list[Path]) -> tuple[str | None, str | None]:
    if not roots:
        return UNCHECKED, None
    try:
        # realpath resolves symlinks without Path.resolve's extra final stat.
        # Test existence only after the resolved target passes containment.
        resolved = Path(os.path.realpath(target))
    except (OSError, RecursionError, ValueError):
        return UNCHECKED, None
    if not any(resolved.is_relative_to(root) for root in roots):
        return UNCHECKED, None
    try:
        mode = target.stat().st_mode
    except FileNotFoundError:
        # Non-strict realpath can remove a failed traversal such as
        # missing-dir/.., or one within a dangling final symlink's payload.
        return BROKEN, str(resolved) if _missing_leaf(target) else None
    except NotADirectoryError:
        return BROKEN, None
    except (OSError, ValueError):
        return UNCHECKED, None
    if stat.S_ISREG(mode) or stat.S_ISDIR(mode):
        return None, None
    return BROKEN, str(resolved)


def scan(root: Path, files: dict[Path, str], link_roots: list[Path]) -> AuditResult:
    """Observe the current filesystem for the supplied Markdown snapshot.

    Finding details hold the written absolute path, decoded and without a
    query, fragment, or line suffix. When traversal establishes a resolved
    in-scope target, broken findings also carry that identity across scans.
    """
    if not link_roots:
        return AuditResult("unchecked", [], NO_ROOTS)
    if not files:
        return AuditResult("unchecked", [], NO_FILES)
    try:
        root = root.resolve(strict=True)
        roots = _available_roots(root, link_roots)
        notes = {(root / path).resolve(): text for path, text in files.items()}
        if any(not path.is_relative_to(root) for path in notes):
            return AuditResult("error", [], "Markdown files must resolve within the project")
    except (OSError, RuntimeError, ValueError) as exc:
        return AuditResult("error", [], str(exc))

    findings = []
    for path, text in sorted(notes.items()):
        for line, link in _iter_links(text):
            target = _local_target(link)
            if target is None:
                continue
            candidate = path.parent / target
            code, resolved = _check_link(candidate, roots)
            if code is not None:
                findings.append(Finding(str(path), line, code, str(candidate), "unknown", resolved_target=resolved))

    if not roots:
        return AuditResult("unchecked", findings, "No configured link roots are available")
    if any(finding.code == UNCHECKED for finding in findings):
        return AuditResult("unchecked", findings, "Some targets are outside available link roots or cannot be checked")
    return AuditResult("checked", findings)


def compare(current: AuditResult, baseline: AuditResult | None) -> AuditResult:
    """Classify broken links against an earlier scan, consuming occurrence counts.

    An observed empty snapshot is usable evidence. An unavailable baseline
    leaves origins unknown. An unchecked occurrence supplies no evidence that
    its target was valid at the time of the earlier scan.
    """
    earlier = None
    unknown = Counter()
    if baseline is not None and (
        baseline.status == "checked" or baseline.findings or baseline.reason == NO_FILES
    ) and baseline.status != "error":
        earlier = [
            (finding.resolved_target or finding.detail, finding)
            for finding in baseline.findings if finding.code == BROKEN
        ]
        unknown.update((finding.file, finding.detail) for finding in baseline.findings if finding.code == UNCHECKED)
    findings = current.findings.copy()
    breaks = []
    for index, finding in enumerate(findings):
        if finding.code != BROKEN:
            continue
        # Reserve unchecked occurrences by spelling before consuming known
        # breaks. An alias newly in scope can now share a known broken target.
        key = (finding.file, finding.detail)
        if unknown[key] > 0:
            findings[index] = replace(finding, origin="unknown")
            unknown[key] -= 1
        else:
            breaks.append((index, finding))
    classified = classify_findings(
        ((finding.resolved_target or finding.detail, finding) for _, finding in breaks), earlier,
    )
    for (index, _), finding in zip(breaks, classified):
        findings[index] = finding
    return replace(current, findings=findings)


def _read_notes(root: Path, files: list[Path]) -> dict[Path, str]:
    notes = {}
    for file in files:
        # Resolve before opening a file, including symlink inputs to the CLI.
        path = Path(os.path.realpath(root / file))
        if not path.is_relative_to(root):
            raise ValueError(f"Markdown file is outside the project: {file}")
        if path.suffix.lower() not in MARKDOWN_SUFFIXES or not path.is_file():
            raise ValueError(f"Expected a Markdown file: {file}")
        if path not in notes:
            notes[path] = path.read_text(encoding="utf-8")
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="project directory")
    parser.add_argument("--link-root", type=Path, action="append", required=True, help="allowed target directory")
    parser.add_argument("--json", action="store_true", help="emit the audit result as JSON")
    parser.add_argument("files", type=Path, nargs="+", help="Markdown files to scan")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        result = scan(root, _read_notes(root, args.files), args.link_root)
    except (OSError, RecursionError, RuntimeError, ValueError) as exc:
        result = AuditResult("error", [], str(exc))
    if args.json:
        print(result.to_json())
    else:
        print(result.status + (f" - {result.reason}" if result.reason else ""))
        for finding in result.findings:
            print(f"{finding.file}:{finding.line}: {finding.code} ({finding.origin}): {finding.detail}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
