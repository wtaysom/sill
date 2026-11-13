import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import link_check
from scripts.beat_audit import AuditResult


def test_link_checker_uses_explicit_roots_and_skips_remote_and_fenced_links(tmp_path):
    root = tmp_path / "project"
    notes = root / "notes"
    notes.mkdir(parents=True)
    sources = tmp_path / "sources"
    sources.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.md").touch()
    (sources / "escape").symlink_to(outside, target_is_directory=True)
    (notes / "not-allowed.md").touch()
    (sources / "paper [a]*#?.txt").touch()
    (sources / "other.txt").touch()
    (sources / "lines.txt").touch()
    lines_uri = (sources / "lines.txt").as_uri()
    text = (
        '[citation](../../sources/paper%20%5Ba%5D%2A%23%3F.txt?view=1#section "title")\n'
        f'[absolute](<{sources}/other.txt>) ![image](<{sources}/other.txt>)\n'
        f'[file URI]({lines_uri}:2–4?view=1#here) [local URI]({lines_uri.replace("file:///", "file://localhost/")})\n'
        '[https](https://example.invalid/a) [remote](//example.invalid/a) [anchor](#a)\n'
        '[mail](mailto:test@example.invalid) [remote file](file://host.example/private.md)\n'
        '```markdown\n[fenced](missing.md)\n```\n'
        '`[code span](missing.md)` J[m,n](r,t)\n'
        'This paragraph continues\n[0, 1]: not-a-reference.md\n\n'
        '[reference]: ../../sources/missing.txt\n'
        '[glob](../../sources/*.txt)\n'
        '[project not allowed](not-allowed.md)\n'
        '[escaped](../../sources/escape/private.md)\n'
        '[escaped with line](../../sources/escape/private.md:1)\n'
        f'[outside absolute](<{outside}/private.md>)\n'
    )

    result = link_check.scan(root, {Path("notes/note.md"): text}, [sources])

    assert result.status == "unchecked"
    assert result.reason
    assert result.exit_code == 3
    assert [(finding.line, finding.code) for finding in result.findings] == [
        (13, "broken_link"), (14, "broken_link"),
        (15, "link_unchecked"), (16, "link_unchecked"),
        (17, "link_unchecked"), (18, "link_unchecked"),
    ]


def test_link_target_move_is_new_break_even_in_untouched_note(tmp_path):
    asset = tmp_path / "asset.txt"
    asset.touch()
    files = {tmp_path / "note.md": "[asset](asset.txt)"}
    before = link_check.scan(tmp_path, files, [Path(".")])
    assert before.status == "checked"
    assert before.findings == []

    asset.rename(tmp_path / "moved.txt")
    current = link_check.scan(tmp_path, files, [Path(".")])
    result = link_check.compare(current, before)

    assert result.exit_code == 1
    [finding] = result.findings
    assert finding.file == str((tmp_path / "note.md").resolve())
    assert (finding.code, finding.origin) == ("broken_link", "new")


def test_rewritten_old_note_retains_only_the_baseline_number_of_occurrences(tmp_path):
    note = tmp_path / "old.md"
    before = link_check.scan(tmp_path, {note: "[source](old%20file.txt:7#heading)"}, [tmp_path])
    current = link_check.scan(tmp_path, {note: (
        '# Rewritten heading\n\n'
        '[updated label](<old file.txt?mode=preview>)\n'
        '[extra occurrence](old%20file.txt)\n'
        '[another target](new.txt)\n'
    )}, [tmp_path])

    result = link_check.compare(current, before)

    assert [(finding.line, finding.origin) for finding in result.findings] == [
        (3, "preexisting"), (4, "new"), (5, "new"),
    ]


def test_link_without_baseline_has_unknown_origin(tmp_path):
    files = {tmp_path / "note.md": "[gone](nowhere.md)"}
    current = link_check.scan(tmp_path, files, [tmp_path])

    assert current.status == "checked"
    assert current.exit_code == 0
    [finding] = current.findings
    assert (finding.code, finding.origin) == ("broken_link", "unknown")
    assert link_check.compare(current, None).findings == [finding]
    assert link_check.compare(current, AuditResult("error", [], "failed")).findings == [finding]

    empty = link_check.scan(tmp_path, {}, [tmp_path])
    assert empty.status == "unchecked"
    assert link_check.compare(current, empty).findings[0].origin == "new"


def test_previously_unchecked_occurrences_do_not_prove_new_breaks(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "target").symlink_to(outside, target_is_directory=True)
    note = root / "note.md"
    prior_text = "[unchecked](target/missing.md)\n[old break](old.md)"
    before = link_check.scan(root, {note: prior_text}, [root])
    assert before.status == "unchecked"

    (root / "target").unlink()
    (root / "target").mkdir()
    after_text = prior_text + "\n[extra occurrence](target/missing.md)\n[another](new.md)"
    result = link_check.compare(link_check.scan(root, {note: after_text}, [root]), before)

    assert [(finding.line, finding.origin) for finding in result.findings] == [
        (1, "unknown"), (2, "preexisting"), (3, "new"), (4, "new"),
    ]


def test_missing_configured_root_is_unavailable_unless_an_ancestor_is_allowed(tmp_path):
    missing = tmp_path / "missing"
    files = {tmp_path / "note.md": "[asset](missing/asset.md)"}

    unavailable = link_check.scan(tmp_path, files, [missing])
    assert unavailable.status == "unchecked"
    assert [finding.code for finding in unavailable.findings] == ["link_unchecked"]

    current = link_check.scan(tmp_path, files, [tmp_path, missing])
    assert current.status == "checked"
    assert [finding.code for finding in current.findings] == ["broken_link"]
    assert link_check.compare(current, unavailable).findings[0].origin == "unknown"

    no_roots = link_check.scan(tmp_path, {}, [])
    assert no_roots.status == "unchecked"
    assert link_check.compare(current, no_roots).findings[0].origin == "unknown"


def test_a_symlink_loop_does_not_hide_an_independent_broken_link(tmp_path):
    (tmp_path / "loop").symlink_to("loop")
    files = {tmp_path / "note.md": "[loop](loop/file.md)\n[gone](gone.md)"}

    result = link_check.scan(tmp_path, files, [tmp_path])

    assert result.status == "unchecked"
    assert [(finding.line, finding.code) for finding in result.findings] == [
        (1, "link_unchecked"), (2, "broken_link"),
    ]


def test_fence_marker_with_trailing_text_does_not_end_the_code_block(tmp_path):
    files = {tmp_path / "note.md": (
        "````markdown\n```\n````still code\n[inside](inside.md)\n````\n[outside](outside.md)"
    )}

    result = link_check.scan(tmp_path, files, [tmp_path])

    assert result.status == "checked"
    assert [(finding.line, finding.code) for finding in result.findings] == [(6, "broken_link")]


def test_symlinks_are_resolved_before_interpreting_parent_components(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    (outside / "subdirectory").mkdir(parents=True)
    (root / "alias").symlink_to(outside / "subdirectory", target_is_directory=True)
    (root / "target.md").touch()
    (outside / "target.md").touch()
    files = {root / "note.md": "[target](alias/../target.md)"}

    result = link_check.scan(root, files, [root])

    assert result.status == "unchecked"
    assert [finding.code for finding in result.findings] == ["link_unchecked"]


def test_unreadable_configured_root_leaves_its_targets_unchecked(tmp_path):
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o111)
    if os.access(protected, os.R_OK):
        pytest.skip("current process can read directories without the read bit")
    files = {tmp_path / "note.md": "[asset](protected/asset.md)"}

    result = link_check.scan(tmp_path, files, [protected])

    assert result.status == "unchecked"
    assert [finding.code for finding in result.findings] == ["link_unchecked"]


def test_link_cli_scans_explicit_files_and_reports_unknown_origin(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("[gone](nowhere.md)\n", encoding="utf-8")
    command = [
        sys.executable, "-m", "scripts.link_check", "--root", str(tmp_path),
        "--link-root", ".", "--link-root", str(tmp_path / "other"), "--json",
    ]

    result = subprocess.run([*command, "note.md"], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "checked"
    [finding] = payload["findings"]
    assert (finding["line"], finding["code"], finding["origin"]) == (1, "broken_link", "unknown")


def test_link_cli_rejects_a_note_symlink_outside_the_project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("[private](outside.md)\n", encoding="utf-8")
    (root / "escape.md").symlink_to(outside)

    result = subprocess.run([
        sys.executable, "-m", "scripts.link_check", "--root", str(root),
        "--link-root", str(tmp_path), "--json", "escape.md",
    ], capture_output=True, text=True, check=False)

    assert result.returncode == 2, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["findings"] == []
