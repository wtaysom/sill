"""Portable audit contracts exercised against synthetic receipts and files."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import beat_audit as audit


START = "2026-11-13T12:00:00+08:00"
END = "2026-11-13T04:02:00Z"


def write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_receipt(root, **fields):
    return write_json(root / "attempt.json", {
        "schema_version": 1,
        "status": "succeeded",
        "spawned_at": START,
        "completed_at": END,
        "new_outputs": [],
        **fields,
    })


def test_audit_scope_is_only_configured_markdown_and_deduplicates_overlaps(tmp_path):
    root = tmp_path / "project"
    notes = root / "notes"
    nested = notes / "nested"
    nested.mkdir(parents=True)
    first = notes / "first.md"
    second = nested / "second.MARKDOWN"
    first.write_bytes("Crème\r\n".encode("utf-8"))
    second.write_text("Second\n", encoding="utf-8")
    (notes / "invalid.txt").write_bytes(b"\xff")
    outside = tmp_path / "other"
    outside.mkdir()
    (outside / "external.md").write_bytes(b"\xff")
    sibling = root / "sibling.md"
    sibling.write_bytes(b"\xff")
    (notes / "sibling-link.md").symlink_to(sibling)
    (notes / "external-link.md").symlink_to(outside / "external.md")
    (notes / "external-dir").symlink_to(outside, target_is_directory=True)
    (root / "notes-alias").symlink_to(notes, target_is_directory=True)

    assert audit.snapshot_markdown(root, [Path("notes"), nested, first, Path("notes-alias")]) == {
        first.resolve(): "Crème\r\n",
        second.resolve(): "Second\n",
    }
    with pytest.raises(ValueError):
        audit.snapshot_markdown(root, [notes / "external-dir"])


def test_older_defect_fingerprint_survives_added_lines(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("Heading\n[missing]\n", encoding="utf-8")
    before = audit.snapshot_markdown(tmp_path, [note])
    note.write_text("New heading\n\nHeading\n[missing]\n[missing]\n", encoding="utf-8")
    after = audit.snapshot_markdown(tmp_path, [note])

    def findings(snapshot):
        return [
            (line, audit.Finding(path.name, number, "example", "Missing reference", "unknown"))
            for path, text in snapshot.items()
            for number, line in enumerate(text.splitlines(), start=1)
            if line == "[missing]"
        ]

    current = findings(after)
    assert [(item.line, item.origin) for item in audit.classify_findings(current, findings(before))] == [
        (4, "preexisting"), (5, "new"),
    ]
    assert [item.origin for item in audit.classify_findings(current, None)] == ["unknown", "unknown"]
    assert [item.origin for item in audit.classify_findings(current, findings({}))] == ["new", "new"]


def test_fingerprints_are_scoped_to_file_and_check():
    baseline = [("target", audit.Finding("note.md", 2, "first", "Detail", "unknown"))]
    current = [
        ("target", audit.Finding("other.md", 2, "first", "Detail", "unknown")),
        ("target", audit.Finding("note.md", 2, "second", "Detail", "unknown")),
        ("target", audit.Finding("note.md", 4, "first", "Updated detail", "unknown")),
    ]
    assert [item.origin for item in audit.classify_findings(current, baseline)] == ["new", "new", "preexisting"]


@pytest.mark.parametrize("overrides", [
    {"spawned_at": None},
    {"completed_at": None},
    {"spawned_at": "2026-11-13T12:00:00"},
    {"completed_at": "2026-11-13T03:59:00+00:00"},
    {"completed_at": "yesterday"},
    {"completed_at": 123},
    {"schema_version": 2},
    {"schema_version": True},
    {"status": "running"},
    {"status": "done"},
    {"status": None},
])
def test_invalid_or_incomplete_window_is_unchecked(tmp_path, overrides):
    receipt = write_receipt(tmp_path, **overrides)
    config = audit.load_audit_config(write_json(tmp_path / "beats.json", {}), tmp_path)

    assert audit.load_window(receipt) is None
    files, reason = audit.audit_snapshot(config, receipt)
    assert files == {}
    assert reason


@pytest.mark.parametrize("payload", [None, b"{broken", b"\xff", b"[]", b"{}"])
def test_missing_or_malformed_receipt_has_no_window(tmp_path, payload):
    receipt = tmp_path / "attempt.json"
    if payload is not None:
        receipt.write_bytes(payload)

    assert audit.load_window(receipt) is None


def test_window_uses_offset_aware_receipt_times(tmp_path):
    receipt = write_receipt(tmp_path)
    assert audit.load_window(receipt) == audit.Window(
        datetime(2026, 11, 13, 4, tzinfo=timezone.utc),
        datetime(2026, 11, 13, 4, 2, tzinfo=timezone.utc),
    )


def test_terminal_failure_can_supply_a_zero_duration_window(tmp_path):
    receipt = write_receipt(tmp_path, status="timeout", completed_at=START)
    window = audit.load_window(receipt)
    assert window is not None
    assert window.start == window.end


@pytest.mark.parametrize("failure", ["empty", "missing", "unreadable"])
def test_empty_or_unreadable_scan_root_is_unchecked(tmp_path, failure):
    notes = tmp_path / "notes"
    if failure != "missing":
        notes.mkdir()
    if failure == "unreadable":
        (notes / "note.md").write_bytes(b"\xff")
    config = audit.load_audit_config(write_json(tmp_path / "beats.json", {
        "audits": {"scan_roots": ["notes"]},
    }), tmp_path)
    fresh = tmp_path / "new.md"
    fresh.write_text("Receipt output\n", encoding="utf-8")

    files, reason = audit.audit_snapshot(config, write_receipt(tmp_path, new_outputs=["new.md"]))
    assert files == {}
    assert reason
    if failure == "empty":
        assert audit.snapshot_markdown(tmp_path, [notes]) == {}
    else:
        with pytest.raises((OSError, UnicodeError)):
            audit.snapshot_markdown(tmp_path, [notes])


def test_config_resolves_paths_from_the_project_and_preserves_citation_targets(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    config = write_json(tmp_path / "beats.json", {"audits": {
        "scan_roots": ["notes", "notes/selected.md"],
        "link_roots": ["references", str(external)],
        "quote_sources": {"./sources/one.md#section": ["references/one.vtt", str(external / "one.srt")]},
    }})

    parsed = audit.load_audit_config(config, root)
    assert parsed.root == root.resolve()
    assert parsed.scan_roots == [root.resolve() / "notes", root.resolve() / "notes/selected.md"]
    assert parsed.link_roots == [root.resolve() / "references", external.resolve()]
    assert parsed.quote_sources == {"./sources/one.md#section": [
        root.resolve() / "references/one.vtt", external.resolve() / "one.srt",
    ]}


@pytest.mark.parametrize("settings", [
    None, [], {"scan_roots": "notes"}, {"link_roots": [1]},
    {"quote_sources": []}, {"quote_sources": {"ref.md": "sources/ref.srt"}},
])
def test_invalid_audit_config_raises(tmp_path, settings):
    config = write_json(tmp_path / "beats.json", {"audits": settings})
    with pytest.raises(ValueError):
        audit.load_audit_config(config, tmp_path)


def test_unconfigured_scope_uses_only_receipt_markdown_outputs(tmp_path):
    (tmp_path / "existing.md").write_bytes(b"\xff")
    (tmp_path / "new.txt").write_bytes(b"\xff")
    new = tmp_path / "new.md"
    new.write_text("A new note\n", encoding="utf-8")
    receipt = write_receipt(tmp_path, new_outputs=["new.txt", "new.md"])
    config_path = tmp_path / "beats.json"
    omitted = audit.load_audit_config(write_json(config_path, {"audits": {}}), tmp_path)
    explicit = audit.load_audit_config(write_json(config_path, {"audits": {"scan_roots": []}}), tmp_path)

    assert audit.audit_snapshot(omitted, receipt) == ({new.resolve(): "A new note\n"}, None)
    assert audit.audit_snapshot(explicit, receipt)[1]
    assert audit.audit_snapshot(omitted, None)[1]


def test_explicit_scope_can_supply_independent_evidence_without_a_receipt(tmp_path):
    (tmp_path / "note.md").write_text("Note", encoding="utf-8")
    config = audit.load_audit_config(write_json(tmp_path / "beats.json", {
        "audits": {"scan_roots": ["note.md"]},
    }), tmp_path)

    assert audit.audit_snapshot(config, None) == ({tmp_path.resolve() / "note.md": "Note"}, None)


@pytest.mark.parametrize("outputs", [
    None, "new.md", [123], ["missing.md"], ["escape.md"], ["../outside.md"], ["directory.md"], ["alias.md"],
])
def test_unavailable_receipt_outputs_leave_scope_unchecked(tmp_path, outputs):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("Outside\n", encoding="utf-8")
    (root / "escape.md").symlink_to(outside)
    existing = root / "existing.md"
    existing.write_text("Existing file\n", encoding="utf-8")
    (root / "alias.md").symlink_to(existing)
    (root / "directory.md").mkdir()
    (root / "directory.md" / "nested.md").write_text("Nested", encoding="utf-8")
    config = audit.load_audit_config(write_json(root / "beats.json", {}), root)
    files, reason = audit.audit_snapshot(config, write_receipt(root, new_outputs=outputs))

    assert files == {}
    assert reason


@pytest.mark.parametrize(("status", "origin", "exit_code"), [
    ("checked", "preexisting", 0), ("checked", "unknown", 0), ("checked", "new", 1),
    ("error", "unknown", 2), ("unchecked", "new", 3),
])
def test_result_json_and_exit_codes(status, origin, exit_code):
    result = audit.AuditResult(status, [audit.Finding("note.md", 4, "example", "Missing reference", origin)])

    assert result.exit_code == exit_code
    assert json.loads(result.to_json()) == {
        "status": status,
        "findings": [{
            "file": "note.md", "line": 4, "code": "example", "detail": "Missing reference", "origin": origin,
        }],
        "reason": None,
    }
