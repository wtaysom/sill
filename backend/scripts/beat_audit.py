"""Shared evidence and result contracts for portable beat audits."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections import Counter
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, TypeGuard


CHECK_NAMES = ("receipt", "clock", "links", "quotes")
MARKDOWN_SUFFIXES = {".md", ".markdown"}
TERMINAL_STATUSES = (
    "succeeded", "skipped", "failed", "timeout", "interrupted",
    "launch_error", "no_record", "state_error",
)
# Match the worker's isoformat output and the equivalent UTC Z spelling.
RECEIPT_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?P<fraction>\.[0-9]{1,6})?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9](?:\.[0-9]{1,6})?)?)"
)


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    code: str
    detail: str
    origin: Literal["new", "preexisting", "unknown"]
    resolved_target: str | None = None


@dataclass(frozen=True)
class AuditResult:
    status: Literal["checked", "unchecked", "error"]
    findings: list[Finding]
    reason: str | None = None

    def to_payload(self) -> dict:
        payload = asdict(self)
        for finding in payload["findings"]:
            if finding["resolved_target"] is None:
                del finding["resolved_target"]
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_payload())

    @classmethod
    def from_payload(cls, payload: object) -> AuditResult:
        if not isinstance(payload, dict) or payload.keys() != {"status", "findings", "reason"}:
            raise ValueError("Invalid audit result fields")
        status, items, reason = payload["status"], payload["findings"], payload["reason"]
        if (status not in ("checked", "unchecked", "error") or not isinstance(items, list)
                or (reason is not None and not isinstance(reason, str))
                or (status != "checked" and not reason)):
            raise ValueError("Invalid audit result values")
        findings = []
        required = {"file", "line", "code", "detail", "origin"}
        for item in items:
            if (not isinstance(item, dict) or not required <= item.keys() <= required | {"resolved_target"}
                    or not all(isinstance(item[key], str) for key in ("file", "code", "detail"))
                    or type(item["line"]) is not int or item["line"] < 1
                    or item["origin"] not in ("new", "preexisting", "unknown")
                    or ("resolved_target" in item and not isinstance(item["resolved_target"], str))):
                raise ValueError("Invalid audit finding")
            findings.append(Finding(**item))
        return cls(status, findings, reason)

    @property
    def exit_code(self) -> int:
        if self.status == "checked":
            return int(any(finding.origin == "new" for finding in self.findings))
        return {"error": 2, "unchecked": 3}[self.status]


@dataclass(frozen=True)
class AuditConfig:
    root: Path
    scan_roots: list[Path]
    link_roots: list[Path]
    quote_sources: dict[str, list[Path]]
    receipt_only: bool = False

    def to_payload(self) -> dict:
        return {
            "root": str(self.root),
            "scan_roots": [str(path) for path in self.scan_roots],
            "link_roots": [str(path) for path in self.link_roots],
            "quote_sources": {target: [str(path) for path in paths] for target, paths in self.quote_sources.items()},
            "receipt_only": self.receipt_only,
        }

    @classmethod
    def from_payload(cls, payload: object) -> AuditConfig:
        if not isinstance(payload, dict) or payload.keys() != {
            "root", "scan_roots", "link_roots", "quote_sources", "receipt_only",
        }:
            raise ValueError("Invalid audit configuration fields")
        root, scan_roots, link_roots = payload["root"], payload["scan_roots"], payload["link_roots"]
        quote_sources, receipt_only = payload["quote_sources"], payload["receipt_only"]
        if (not _is_path_list([root]) or not Path(root).is_absolute()
                or not _is_path_list(scan_roots) or not _is_path_list(link_roots)
                or not isinstance(quote_sources, dict)
                or not all(isinstance(target, str) and _is_path_list(paths)
                           for target, paths in quote_sources.items())
                or not isinstance(receipt_only, bool) or (receipt_only and scan_roots)):
            raise ValueError("Invalid audit configuration values")
        return cls(
            Path(root), list(map(Path, scan_roots)), list(map(Path, link_roots)),
            {target: list(map(Path, paths)) for target, paths in quote_sources.items()}, receipt_only,
        )


@dataclass(frozen=True)
class AuditBaseline:
    markdown: dict[Path, str] | None
    links: AuditResult | None

    def to_payload(self) -> dict:
        return {
            "markdown": None if self.markdown is None else {str(path): text for path, text in self.markdown.items()},
            "links": None if self.links is None else self.links.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: object) -> AuditBaseline:
        if not isinstance(payload, dict) or payload.keys() != {"markdown", "links"}:
            raise ValueError("Invalid audit baseline fields")
        markdown, links = payload["markdown"], payload["links"]
        if markdown is not None and (
            not isinstance(markdown, dict)
            or not all(_is_path_list([path]) and Path(path).is_absolute() and isinstance(text, str)
                       for path, text in markdown.items())
        ):
            raise ValueError("Invalid audit baseline Markdown")
        return cls(
            None if markdown is None else {Path(path): text for path, text in markdown.items()},
            None if links is None else AuditResult.from_payload(links),
        )


def _read_receipt(path: Path) -> Mapping[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _offset_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    match = RECEIPT_TIMESTAMP.fullmatch(value)
    if match is None:
        return None
    pattern = "%Y-%m-%dT%H:%M:%S"
    if match["fraction"] is not None:
        pattern += ".%f"
    try:
        return datetime.strptime(value, pattern + "%z")
    except ValueError:
        return None


def window_from_receipt(receipt: Mapping[str, object] | None) -> Window | None:
    """Return the recorded bounds of a completed schema-version-1 attempt."""
    if receipt is None:
        return None
    version = receipt.get("schema_version")
    if type(version) is not int or version != 1 or receipt.get("status") not in TERMINAL_STATUSES:
        return None
    start = _offset_time(receipt.get("spawned_at"))
    end = _offset_time(receipt.get("completed_at"))
    if start is None or end is None or start > end:
        return None
    return Window(start, end)


def load_window(receipt_path: Path) -> Window | None:
    return window_from_receipt(_read_receipt(receipt_path))


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except RuntimeError as exc:
        # Python 3.12 and earlier raise RuntimeError for a symlink loop.
        raise ValueError(f"Cannot resolve path {path}: {exc}") from exc


def _is_path_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) and item and "\0" not in item for item in value)


def _config_paths(root: Path, value: object, field: str) -> list[Path]:
    if not _is_path_list(value):
        raise ValueError(f"{field} must be a list of paths")
    return [_resolve(root / item) for item in value]


def load_audit_config(path: Path, root: Path) -> AuditConfig:
    """Load the optional audits object from the beat configuration JSON."""
    root = _resolve(root)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Beat configuration must be an object")
    audits = payload.get("audits", {})
    if not isinstance(audits, dict):
        raise ValueError("audits must be an object")

    scan_roots = _config_paths(root, audits.get("scan_roots", []), "audits.scan_roots")
    if any(not path.is_relative_to(root) for path in scan_roots):
        raise ValueError("audits.scan_roots must resolve within the project")
    link_roots = _config_paths(root, audits.get("link_roots", []), "audits.link_roots")
    sources = audits.get("quote_sources", {})
    if not isinstance(sources, dict):
        raise ValueError("audits.quote_sources must map citation targets to lists of paths")
    quote_sources = {target: _config_paths(root, paths, "audits.quote_sources") for target, paths in sources.items()}
    return AuditConfig(root, scan_roots, link_roots, quote_sources, receipt_only="scan_roots" not in audits)


def _markdown(path: Path) -> bool:
    return path.suffix.lower() in MARKDOWN_SUFFIXES


def _raise_walk_error(error: OSError) -> None:
    raise error


def _markdown_under(directory: Path) -> Iterator[Path]:
    for base, _, names in os.walk(directory, onerror=_raise_walk_error):
        for name in names:
            candidate = Path(base) / name
            if not _markdown(candidate):
                continue
            path = _resolve(candidate)
            if path.is_relative_to(directory) and _markdown(path) and stat.S_ISREG(path.stat().st_mode):
                yield path


def snapshot_markdown(root: Path, scan_roots: list[Path]) -> dict[Path, str]:
    """Read canonical Markdown files inside the project and selected roots."""
    root = _resolve(root)
    snapshot: dict[Path, str] = {}
    for scan_root in dict.fromkeys(_resolve(root / path) for path in scan_roots):
        if not scan_root.is_relative_to(root):
            raise ValueError(f"Scan root is outside the project: {scan_root}")
        mode = scan_root.stat().st_mode
        if stat.S_ISDIR(mode):
            paths = _markdown_under(scan_root)
        elif stat.S_ISREG(mode):
            paths = iter((scan_root,)) if _markdown(scan_root) else iter(())
        else:
            raise ValueError(f"Scan root must be a directory or a regular file: {scan_root}")
        for path in paths:
            if path not in snapshot:
                snapshot[path] = path.read_bytes().decode("utf-8")
    return dict(sorted(snapshot.items()))


def _receipt_markdown(root: Path, receipt: Mapping[str, object] | None) -> list[Path]:
    if receipt is None or window_from_receipt(receipt) is None:
        raise ValueError("A completed attempt receipt with a valid time window is required")
    outputs = receipt.get("new_outputs")
    if not _is_path_list(outputs):
        raise ValueError("Attempt receipt new_outputs must be a list of paths")

    paths = []
    for output in outputs:
        relative = Path(output)
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Attempt receipt output must be project-relative: {output}")
        if not _markdown(relative):
            continue
        path = root / relative
        if _resolve(path) != path or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError(f"Attempt receipt output must be a regular file at the listed path: {output}")
        paths.append(path)
    return paths


def audit_snapshot(
    config: AuditConfig, receipt: Path | Mapping[str, object] | None,
) -> tuple[dict[Path, str], str | None]:
    """Return current Markdown and an unchecked reason if its scope is unavailable."""
    try:
        scan_roots = config.scan_roots
        if config.receipt_only:
            if isinstance(receipt, Path):
                receipt = _read_receipt(receipt)
            scan_roots = _receipt_markdown(_resolve(config.root), receipt)
        files = snapshot_markdown(config.root, scan_roots)
    except (OSError, ValueError) as exc:
        return {}, str(exc)
    return files, None if files else "No Markdown files in the audit scope"


def classify_findings(
    current: Iterable[tuple[Hashable, Finding]],
    baseline: Iterable[tuple[Hashable, Finding]] | None,
) -> list[Finding]:
    """Assign origin using per-file, per-code counts of check-specific fingerprints."""
    if baseline is None:
        return [replace(finding, origin="unknown") for _, finding in current]

    remaining = Counter((finding.file, finding.code, key) for key, finding in baseline)
    findings = []
    for key, finding in current:
        occurrence = (finding.file, finding.code, key)
        if remaining[occurrence] > 0:
            findings.append(replace(finding, origin="preexisting"))
            remaining[occurrence] -= 1
        else:
            findings.append(replace(finding, origin="new"))
    return findings


def _check(operation: Callable[[], AuditResult]) -> AuditResult:
    try:
        return operation()
    except Exception as exc:
        return AuditResult("error", [], f"Audit check failed ({type(exc).__name__})")


def capture_baseline(config: AuditConfig) -> AuditBaseline:
    """Capture the configured Markdown and observe link existence before the beat."""
    from . import link_check

    if config.receipt_only:
        # The completed receipt will identify which paths were actually new.
        return AuditBaseline(None, None)
    try:
        markdown = snapshot_markdown(config.root, config.scan_roots)
    except (OSError, ValueError):
        return AuditBaseline(None, None)
    links = _check(lambda: link_check.scan(config.root, markdown, config.link_roots)) if config.link_roots else None
    return AuditBaseline(markdown, links)


def _mark_unchecked(result: AuditResult, reason: str) -> AuditResult:
    if result.status == "error":
        return result
    if result.reason:
        reason = f"{result.reason}. {reason}"
    return replace(result, status="unchecked", reason=reason)


def run_checks(config: AuditConfig, baseline: AuditBaseline, receipt: dict) -> dict[str, AuditResult]:
    """Run independent read-only checks against the finished attempt's evidence."""
    from . import dateline_audit, link_check, quote_check, receipt_audit

    after, reason = audit_snapshot(config, receipt)
    if reason:
        return {name: AuditResult("unchecked", [], reason) for name in CHECK_NAMES}

    window = window_from_receipt(receipt)
    # audit_snapshot has validated these receipt-only paths and the window.
    before = {} if config.receipt_only else baseline.markdown

    def links() -> AuditResult:
        current = link_check.scan(config.root, after, config.link_roots)
        earlier = baseline.links
        if config.receipt_only:
            earlier = link_check.scan(config.root, {}, config.link_roots)
        result = link_check.compare(current, earlier)
        if config.link_roots and (earlier is None or earlier.status == "error"
                                  or any(f.code == link_check.BROKEN and f.origin == "unknown"
                                         for f in result.findings)):
            return _mark_unchecked(result, "Link baseline evidence is unavailable")
        return result

    def quotes() -> AuditResult:
        result = quote_check.audit(before, after, config.quote_sources)
        if before is None and config.quote_sources:
            return _mark_unchecked(result, "Markdown baseline is unavailable")
        return result

    return {
        "receipt": _check(lambda: receipt_audit.audit(before, after, window)),
        "clock": _check(lambda: dateline_audit.audit(before, after, window)),
        "links": _check(links),
        "quotes": _check(quotes),
    }


def main(argv: list[str] | None = None) -> int:
    """Serve a single worker audit request over a private input file and stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)
    request = _read_receipt(args.request)
    try:
        if request is None:
            raise ValueError
        config = AuditConfig.from_payload(request.get("config"))
        mode = request.get("mode")
        if mode == "before" and request.keys() == {"mode", "config"}:
            payload = capture_baseline(config).to_payload()
        elif mode == "after" and request.keys() == {"mode", "config", "baseline", "receipt"}:
            baseline = AuditBaseline.from_payload(request["baseline"])
            receipt = request["receipt"]
            if not isinstance(receipt, dict):
                raise ValueError
            payload = {name: result.to_payload() for name, result in run_checks(config, baseline, receipt).items()}
        else:
            raise ValueError
    except ValueError:
        parser.error("Invalid audit request")
    print(json.dumps(payload))
    return 0


def run_audit_cli(
    audit: Callable[..., AuditResult], description: str, argv: list[str] | None = None,
) -> int:
    """Audit explicit file scope, with old files supplied as historical evidence."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--receipt", type=Path, help="completed attempt receipt for the positional files")
    parser.add_argument("--old", type=Path, action="append", default=[], help="historical file (repeatable)")
    parser.add_argument("--grace", type=float, default=90, metavar="SECONDS")
    parser.add_argument("--json", action="store_true", help="emit the audit result as JSON")
    parser.add_argument("files", type=Path, nargs="*", help="files attributed to the supplied attempt")
    args = parser.parse_args(argv)

    if not args.files and not args.old:
        parser.error("supply at least one file")
    try:
        timedelta(seconds=args.grace)
        if args.grace < 0:
            raise ValueError
    except (OverflowError, ValueError):
        parser.error("--grace must be a finite nonnegative duration")

    try:
        old = {_resolve(path) for path in args.old}
        files = dict.fromkeys(_resolve(path) for path in [*args.files, *args.old])
        after = {path: path.read_text(encoding="utf-8") for path in files}
    except (OSError, ValueError) as exc:
        result = AuditResult("error", [], str(exc))
    else:
        window = load_window(args.receipt) if args.receipt is not None else None
        before = {path: text for path, text in after.items() if path in old}
        result = audit(before, after, window, grace_seconds=args.grace)
        if window is None:
            findings = [
                finding if Path(finding.file) in old else replace(finding, origin="unknown")
                for finding in result.findings
            ]
            result = replace(result, findings=findings)
    if args.json:
        print(result.to_json())
    else:
        print(result.status + (f" - {result.reason}" if result.reason else ""))
        for finding in result.findings:
            print(f"{finding.file}:{finding.line}: {finding.code} ({finding.origin}): {finding.detail}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
