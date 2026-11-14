"""Portable audit contracts exercised against synthetic receipts and files."""

import json
import os
import signal
import subprocess
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
    pytest.param(
        {"spawned_at": "2026-11-13T04:00:00+00:60", "completed_at": "2026-11-13T04:02:00+01:00"},
        id="offset-minute-overflow",
    ),
    pytest.param({"spawned_at": "2026-11-13T04:00:00+00:00:60"}, id="offset-second-overflow"),
    pytest.param({"spawned_at": "2026-11-13T04+00:00"}, id="hour-only"),
    pytest.param({"spawned_at": "2026-11-13T04:00+00:00"}, id="missing-seconds"),
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
    (tmp_path / "note.md").write_text("New Markdown\n", encoding="utf-8")
    receipt = write_receipt(tmp_path, new_outputs=["note.md"], **overrides)
    config = audit.load_audit_config(write_json(tmp_path / "beats.json", {}), tmp_path)

    window = audit.load_window(receipt)
    files, reason = audit.audit_snapshot(config, receipt)
    assert (window, files) == (None, {})
    assert reason


@pytest.mark.parametrize("payload", [None, b"{broken", b"\xff", b"[]", b"{}"])
def test_missing_or_malformed_receipt_has_no_window(tmp_path, payload):
    receipt = tmp_path / "attempt.json"
    if payload is not None:
        receipt.write_bytes(payload)

    assert audit.load_window(receipt) is None


@pytest.mark.parametrize("entry_point", ["window", "snapshot"])
def test_deeply_nested_receipt_leaves_evidence_unchecked(tmp_path, entry_point):
    receipt = write_receipt(tmp_path, new_outputs=["note.md"])
    (tmp_path / "note.md").write_text("New Markdown\n", encoding="utf-8")
    config = audit.load_audit_config(write_json(tmp_path / "beats.json", {}), tmp_path)
    contents = receipt.read_text(encoding="utf-8")
    nested = "[" * 10_000 + "null" + "]" * 10_000
    receipt.write_text(contents[:-1] + f', "nested": {nested}' + "}", encoding="utf-8")

    if entry_point == "window":
        assert audit.load_window(receipt) is None
    else:
        files, reason = audit.audit_snapshot(config, receipt)
        assert files == {}
        assert reason


def test_window_uses_offset_aware_receipt_times(tmp_path):
    receipt = write_receipt(tmp_path)
    assert audit.load_window(receipt) == audit.Window(
        datetime(2026, 11, 13, 4, tzinfo=timezone.utc),
        datetime(2026, 11, 13, 4, 2, tzinfo=timezone.utc),
    )


def test_window_accepts_fractional_seconds_and_negative_offset(tmp_path):
    receipt = write_receipt(tmp_path, spawned_at="2026-11-12T23:00:00.123456-05:00")
    window = audit.load_window(receipt)
    assert window is not None
    assert window.start == datetime(2026, 11, 13, 4, 0, 0, 123456, tzinfo=timezone.utc)


def test_window_preserves_offset_seconds_and_fraction(tmp_path):
    receipt = write_receipt(tmp_path, spawned_at="2026-11-13T04:00:00+00:00:00.500000")
    window = audit.load_window(receipt)
    assert window is not None
    assert window.start == datetime(2026, 11, 13, 3, 59, 59, 500000, tzinfo=timezone.utc)


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


@pytest.fixture
def worker_project(tmp_path, monkeypatch):
    import beat_worker as worker

    (tmp_path / "notes").mkdir()
    (tmp_path / "prompt.md").write_text("Standing prompt.\n")
    monkeypatch.setattr(worker, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(worker, "LOG_PATH", tmp_path / "worker.log")
    monkeypatch.setattr(worker, "POST_HOOK", None)
    monkeypatch.setattr(worker, "WATCHDOG_SOCKET", None)
    monkeypatch.setenv("SILL_BEAT_CONFIG", str(tmp_path / "beats.json"))
    monkeypatch.setenv("SILL_BEAT_STATE_PATH", str(tmp_path / "rotation.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("SILL_INSTANCE_ENV_FILE", str(tmp_path / "unavailable.env"))

    def configure(source, **config):
        cli = tmp_path / "stub-cli"
        cli.write_text("#!/bin/sh\n" + source, encoding="utf-8")
        cli.chmod(0o700)
        voice = {"name": "probe", "prompt": "prompt.md", "transcripts": "logs",
                 "output_glob": "notes/*.md", "cli": str(cli)}
        write_json(tmp_path / "beats.json", {
            "voices": [voice, {**voice, "name": "next"}],
            **config,
        })

    return worker, tmp_path, configure


def _worker_receipt(root):
    [path] = (root / "logs").glob("*.json")
    return json.loads(path.read_text())


def test_attempt_checks_old_and_new_records_even_on_child_failure(worker_project, monkeypatch, caplog):
    worker, root, configure = worker_project
    (root / "sub").mkdir()
    (root / "asset.txt").write_text("fixture source")
    (root / "article.txt").write_text("The team crossed the bridge.")
    (root / "notes" / "old.md").write_text(
        "# Existing note\n\n[missing](../sub/../missing.txt)\n\nStored: MINT-PENDING\n"
    )
    untouched = "[source](../asset.txt)\n"
    (root / "notes" / "untouched.md").write_text(untouched)
    configure("""cat > notes/old.md <<'EOF'
# Existing note with a new heading

[renamed label](../missing.txt)

Stored: MINT-PENDING
EOF
cat > notes/new.md <<'EOF'
# New record

**Date:** 2000-01-01, 04:00 UTC

Stored: deadbeef

“Another unrelated claim” [Article](../article.txt).

“Source offline” [Other](../unavailable.txt).
EOF
mv asset.txt moved.txt
exit 17
""", audits={
        "scan_roots": ["notes"], "link_roots": ["."],
        "quote_sources": {"../article.txt": ["article.txt"], "../unavailable.txt": ["unavailable.txt"]},
    })
    docker = root / "docker"
    docker.write_text("#!/bin/sh\ntouch docker-called\nexit 1\n")
    docker.chmod(0o700)
    monkeypatch.setenv("PATH", f"{root}:{os.environ['PATH']}")
    monkeypatch.setattr(worker, "POST_HOOK", "/usr/bin/touch post-hook-called")
    caplog.set_level("INFO", logger="beat_worker")

    assert worker.run_beat_loop(once=True) == 1
    receipt = _worker_receipt(root)
    assert receipt["status"] == "failed"
    assert receipt["returncode"] == 17
    assert receipt["new_outputs"] == ["notes/new.md"]
    assert receipt["state_advanced"] is False
    results = receipt["audits"]
    assert results["receipt"]["status"] == "unchecked"
    assert "Selected instance configuration is unavailable" in results["receipt"]["reason"]
    assert [(f["code"], f["origin"]) for f in results["receipt"]["findings"]] == [
        ("mint_pending", "preexisting"),
    ]
    assert results["clock"]["status"] == "checked"
    assert [(f["code"], f["origin"]) for f in results["clock"]["findings"]] == [
        ("outside_window", "new"),
    ]
    assert results["links"]["status"] == "checked"
    links = {Path(f["file"]).name: f for f in results["links"]["findings"]}
    assert links["old.md"]["origin"] == "preexisting"
    assert links["old.md"]["resolved_target"] == str(root.resolve() / "missing.txt")
    assert links["untouched.md"]["origin"] == "new"
    assert links["untouched.md"]["resolved_target"] == str(root.resolve() / "asset.txt")
    assert results["quotes"]["status"] == "unchecked"
    assert [(f["code"], f["origin"]) for f in results["quotes"]["findings"]] == [
        ("text_mismatch", "new"),
    ]
    assert "[audit receipt] unchecked" in caplog.text
    assert "[audit quotes] unchecked" in caplog.text
    assert "clean" not in caplog.text.lower()
    assert not (root / "rotation.json").exists()
    assert not (root / "docker-called").exists()
    assert not (root / "post-hook-called").exists()
    assert not (root / "asset.txt").exists()
    assert (root / "moved.txt").read_text() == "fixture source"
    assert (root / "article.txt").read_text() == "The team crossed the bridge."
    assert (root / "notes" / "untouched.md").read_text() == untouched


@pytest.mark.parametrize("failure", ["timeout", "crash", "invalid-result"])
def test_audit_failure_and_timeout_are_visible_without_rolling_back_rotation(
    worker_project, monkeypatch, caplog, failure,
):
    worker, root, configure = worker_project
    configure("printf 'A note.\\n' > notes/new.md\n", audits={"scan_roots": ["notes"]})
    monkeypatch.setattr(worker, "POST_HOOK", "/usr/bin/touch post-hook-called")
    caplog.set_level("INFO", logger="beat_worker")
    run = worker._run_beat_subprocess
    monotonic = worker.time.monotonic
    elapsed = 0
    requests = []
    start_process = subprocess.Popen

    def check_audit_descriptors(argv, **kwargs):
        if argv[1:3] == ["-m", "scripts.beat_audit"]:
            assert kwargs["pass_fds"] == ()
        elif argv[0] == str(root / "stub-cli"):
            assert len(kwargs["pass_fds"]) == 1
        return start_process(argv, **kwargs)

    def run_with_fault(argv, **kwargs):
        nonlocal elapsed
        if argv[1:3] != ["-m", "scripts.beat_audit"]:
            result = run(argv, **kwargs)
            elapsed += 300
            return result
        assert kwargs.get("lock_fd") is None
        request = Path(argv[-1])
        requests.append(request)
        assert request.stat().st_mode & 0o077 == 0
        mode = json.loads(request.read_text())["mode"]
        if mode == "before":
            assert 119 < kwargs["timeout"] <= 120
            result = run(argv, **kwargs)
            elapsed += 80
            return result
        assert mode == "after"
        assert 38 < kwargs["timeout"] < 41
        receipt = _worker_receipt(root)
        assert receipt["state_advanced"] is True
        assert json.loads((root / "rotation.json").read_text())["last_successful_attempt_id"] == receipt["attempt_id"]
        assert not (root / "post-hook-called").exists()
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if failure == "crash":
            return subprocess.CompletedProcess(argv, 42, "", "raw helper diagnostics")
        return subprocess.CompletedProcess(argv, 0, '{"links":{"status":"checked","findings":[42]}}', "")

    monkeypatch.setattr(subprocess, "Popen", check_audit_descriptors)
    monkeypatch.setattr(worker, "_run_beat_subprocess", run_with_fault)
    monkeypatch.setattr(worker.time, "monotonic", lambda: monotonic() + elapsed)
    assert worker.run_beat_loop(once=True) == 0
    receipt = _worker_receipt(root)
    assert receipt["status"] == "succeeded"
    assert receipt["state_advanced"] is True
    assert json.loads((root / "rotation.json").read_text())["next_index"] == 1
    assert len(requests) == 2
    assert not any(path.exists() for path in requests)
    results = receipt["audits"]
    assert {name: value["status"] for name, value in results.items()} == {
        "receipt": "error", "clock": "error", "links": "error", "quotes": "error",
    }
    expected = {"timeout": "timed out", "crash": "exit 42", "invalid-result": "invalid"}
    assert expected[failure] in results["receipt"]["reason"]
    assert "[audit receipt] error" in caplog.text
    assert "clean" not in caplog.text.lower()
    assert "raw helper diagnostics" not in caplog.text
    assert (root / "post-hook-called").exists()


def test_no_config_runs_only_eligible_receipt_and_clock_checks(worker_project, monkeypatch, caplog):
    worker, root, configure = worker_project
    (root / "notes" / "unreadable.md").write_bytes(b"\xff")
    (root / "notes" / "old.md").write_text("Stored: MINT-PENDING\n")
    configure("""cat > notes/new.md <<'EOF'
# New record

**Date:** 2000-01-01, 04:00 UTC

Stored: MINT-PENDING

“Unconfigured quote” [source](../unavailable.txt).
EOF
""")
    caplog.set_level("INFO", logger="beat_worker")
    post_hook = root / "post-hook"
    post_hook.write_text("#!/bin/sh\nprintf '\\nStored: MINT-PENDING\\n' >> notes/new.md\n")
    post_hook.chmod(0o700)
    monkeypatch.setattr(worker, "POST_HOOK", str(post_hook))

    assert worker.run_beat_loop(once=True) == 0
    results = _worker_receipt(root)["audits"]
    assert results["receipt"]["status"] == results["clock"]["status"] == "checked"
    [pending] = results["receipt"]["findings"]
    assert (Path(pending["file"]).name, pending["code"], pending["origin"]) == (
        "new.md", "mint_pending", "new",
    )
    [clock] = results["clock"]["findings"]
    assert (Path(clock["file"]).name, clock["code"], clock["origin"]) == (
        "new.md", "outside_window", "new",
    )
    assert results["links"]["status"] == results["quotes"]["status"] == "unchecked"
    assert "No link roots" in results["links"]["reason"]
    assert "No quote sources" in results["quotes"]["reason"]
    assert "[audit links] unchecked" in caplog.text
    assert "[audit quotes] unchecked" in caplog.text
    assert "clean" not in caplog.text.lower()
    assert (root / "notes" / "new.md").read_text().count("Stored: MINT-PENDING") == 2
    assert (root / "notes" / "old.md").read_text() == "Stored: MINT-PENDING\n"
    assert (root / "notes" / "unreadable.md").read_bytes() == b"\xff"


def test_unreadable_baseline_keeps_current_findings_with_unknown_origin(worker_project, caplog):
    worker, root, configure = worker_project
    note = root / "notes" / "old.md"
    note.write_bytes(b"\xff")
    configure("""cat > notes/old.md <<'EOF'
# Existing note

Stored: MINT-PENDING

[missing](../missing.txt)
EOF
""", audits={"scan_roots": ["notes"], "link_roots": ["."]})

    assert worker.run_beat_loop(once=True) == 1
    receipt = _worker_receipt(root)
    assert receipt["status"] == "no_record"
    assert receipt["new_outputs"] == []
    for name, code in (("receipt", "mint_pending"), ("links", "broken_link")):
        result = receipt["audits"][name]
        assert result["status"] == "unchecked"
        [finding] = result["findings"]
        assert (finding["code"], finding["origin"]) == (code, "unknown")
    assert "baseline" in caplog.text.lower()


def test_audit_budget_exhausted_before_attempt_still_allows_rotation(worker_project, monkeypatch, caplog):
    worker, root, configure = worker_project
    configure("printf 'A note.\\n' > notes/new.md\n", audits={"scan_roots": ["notes"]})
    run = worker._run_beat_subprocess
    monotonic = worker.time.monotonic
    spent = 0
    modes = []

    def time_out_baseline(argv, **kwargs):
        nonlocal spent
        if argv[1:3] != ["-m", "scripts.beat_audit"]:
            return run(argv, **kwargs)
        modes.append(json.loads(Path(argv[-1]).read_text())["mode"])
        spent += 120
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(worker, "_run_beat_subprocess", time_out_baseline)
    monkeypatch.setattr(worker.time, "monotonic", lambda: monotonic() + spent)
    assert worker.run_beat_loop(once=True) == 0
    assert modes == ["before"]
    receipt = _worker_receipt(root)
    assert receipt["state_advanced"] is True
    assert receipt["audits"]["clock"]["status"] == "error"
    assert "budget" in receipt["audits"]["clock"]["reason"]
    assert "baseline" in caplog.text.lower()
    assert "timed out" in caplog.text


@pytest.mark.parametrize("config", [{"scan_roots": []}, {"scan_roots": "notes"}])
def test_empty_or_invalid_configured_scope_does_not_use_receipt_files(worker_project, caplog, config):
    worker, root, configure = worker_project
    configure("printf 'Stored: MINT-PENDING\\n' > notes/new.md\n", audits=config)

    assert worker.run_beat_loop(once=True) == 0
    results = _worker_receipt(root)["audits"]
    assert all(result["status"] == "unchecked" and not result["findings"] for result in results.values())
    assert "[audit receipt] unchecked" in caplog.text
    assert "clean" not in caplog.text.lower()


@pytest.mark.parametrize("phase", ["cli", "commit", "after"])
def test_interrupted_diagnostics_keep_terminal_receipt(worker_project, monkeypatch, phase):
    worker, root, configure = worker_project
    configure("printf 'Stored: MINT-PENDING\\n' > notes/new.md\n", audits={"scan_roots": ["notes"]})
    run = worker._run_beat_subprocess
    modes = []

    def stop_at_phase(argv, **kwargs):
        if argv[1:3] == ["-m", "scripts.beat_audit"]:
            mode = json.loads(Path(argv[-1]).read_text())["mode"]
            modes.append(mode)
            if mode == phase:
                raise KeyboardInterrupt
            return run(argv, **kwargs)
        result = run(argv, **kwargs)
        if phase == "cli":
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(worker, "_run_beat_subprocess", stop_at_phase)
    if phase == "commit":
        write_state = worker.write_state

        def write_then_signal(*args, **kwargs):
            write_state(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGINT)

        monkeypatch.setattr(worker, "write_state", write_then_signal)
    assert worker.run_beat_loop(once=True) == 1
    receipt = _worker_receipt(root)
    if phase == "cli":
        assert receipt["status"] == "interrupted"
        assert not receipt["state_advanced"]
        assert receipt["new_outputs"] == ["notes/new.md"]
        assert not (root / "rotation.json").exists()
        assert modes == ["before"]
        expected = "unchecked"
    else:
        assert receipt["status"] == "succeeded"
        assert receipt["state_advanced"]
        assert json.loads((root / "rotation.json").read_text())["last_successful_attempt_id"] == receipt["attempt_id"]
        assert modes == (["before", "after"] if phase == "after" else ["before"])
        expected = "error" if phase == "after" else "unchecked"
    assert set(receipt["audits"]) == {"receipt", "clock", "links", "quotes"}
    assert all(value["status"] == expected and "interrupt" in value["reason"].lower()
               for value in receipt["audits"].values())


def test_direct_spawn_without_config_audits_new_markdown(worker_project, monkeypatch):
    worker, root, configure = worker_project
    configure("printf 'Stored: MINT-PENDING\\n' > notes/new.md\n")
    voices = worker.load_voices(root / "beats.json")
    (root / "beats.json").unlink()
    monkeypatch.delenv("SILL_BEAT_CONFIG")

    success, _, _ = worker.spawn_beat(voices[0], voices)

    assert success
    receipt = _worker_receipt(root)
    [finding] = receipt["audits"]["receipt"]["findings"]
    assert (finding["code"], finding["origin"]) == ("mint_pending", "new")
    assert not (root / "rotation.json").exists()


def test_audit_receipt_recovers_after_terminal_staging_error(worker_project, monkeypatch):
    worker, root, configure = worker_project
    configure("printf 'Stored: MINT-PENDING\\n' > notes/new.md\n")
    write = worker.atomic_json
    lost_stage = False

    def fail_staging_once(path, data):
        nonlocal lost_stage
        if not lost_stage and data.get("status") == "succeeded" and data["state_advanced"] is None:
            lost_stage = True
            raise OSError("simulated terminal staging failure")
        write(path, data)

    monkeypatch.setattr(worker, "atomic_json", fail_staging_once)

    assert worker.run_beat_loop(once=True) == 1
    receipt = _worker_receipt(root)
    assert receipt["status"] == "state_error"
    assert receipt["state_advanced"] is False
    assert receipt["audits"]["receipt"]["findings"][0]["code"] == "mint_pending"
    assert not (root / "rotation.json").exists()
