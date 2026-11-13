"""Portable clock checks against synthetic completed attempts and notes."""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from scripts import dateline_audit as datelines
from scripts.beat_audit import Window


NOTE = Path("record.md")
WINDOW = Window(
    datetime.fromisoformat("2026-11-13T23:56:00+08:00"),
    datetime.fromisoformat("2026-11-13T16:03:00+00:00"),
)


@pytest.mark.parametrize("header", [
    "**Date:** 2026-11-13, 00:01",
    "**Date:** 2026-11-14, 23:57",
    "**Date:** 2026-11-14, 16:01 UTC",
    "**Date:** 2026-11-13, 16:01 GMT",
    "**Date:** 2026-11-14, 00:01 +08:00",
    "**Date:** 2026-11-14, 11:01 -05:00",
    "**Date:** 2026-11-13, 23:57–00:01",
    "`date -Iseconds` returned\n`2026-11-13T11:01:00-05:00`",
    "`date`: Fri Nov 13 16:01:00 GMT 2026",
])
def test_dateline_uses_receipt_offset_and_crosses_midnight(header):
    result = datelines.audit({}, {NOTE: f"# New record\n\n{header}\n"}, WINDOW)
    assert result.status == "checked"
    assert result.findings == []


def test_later_edit_does_not_reassign_old_dateline():
    before = {NOTE: "# Older record\n\n**Date:** 2026-11-10, 12:45\n\nOriginal text."}
    after = {NOTE: (
        "# Older record\n\n**Date:** 2026-11-10, 12:45\n\nRevised prose.\n\n"
        "## Body\nclock read 03:00 CST\n"
    )}
    result = datelines.audit(before, after, WINDOW)
    assert result.status == "checked"
    assert result.findings == []


def test_changed_dateline_date_and_clock_are_attributable():
    before = {NOTE: "# Older record\n\n**Date:** 2026-11-10, 00:01\n"}
    after = {NOTE: "# Older record\n\n**Date:** 2026-11-11, 00:01\n"}
    result = datelines.audit(before, after, WINDOW)
    assert result.status == "checked"
    assert [(item.line, item.code, item.origin) for item in result.findings] == [
        (3, "outside_window", "new"),
    ]
    assert "2026-11-11" in result.findings[0].detail


def test_clock_approximation_and_unsupported_timezone_are_distinct():
    after = {
        Path("estimate.md"): "Date: 2026-11-14, about 00:20",
        Path("rounding.md"): "Date: 2026-11-14, clock read ~00:20",
        Path("offset-estimate.md"): "Date: 2026-11-14, ~00:45 UTC",
        Path("ambiguous.md"): "Date: 2026-11-14, 00:01 CST",
    }
    result = datelines.audit({}, after, WINDOW)
    assert result.status == "unchecked"
    assert [(item.file, item.code, item.origin) for item in result.findings] == [
        ("rounding.md", "outside_window", "new"),
        ("offset-estimate.md", "estimate_outside_window", "new"),
        ("ambiguous.md", "unsupported_timezone", "new"),
    ]
    assert "CST" in result.findings[-1].detail


def test_intrinsically_wide_range_keeps_preexisting_origin():
    before = {NOTE: "Date: 2026-11-13, 23:00–01:00"}
    after = {NOTE: "# Older record\n\nDate: 2026-11-13, 23:00–01:00\n\nNew prose."}
    result = datelines.audit(before, after, WINDOW)
    assert [(item.line, item.code, item.origin) for item in result.findings] == [
        (3, "range_too_wide", "preexisting"),
    ]


def test_explicit_bad_clock_with_no_baseline_is_unchecked():
    result = datelines.audit(None, {NOTE: "Date: 2026-11-11, 00:01"}, WINDOW)
    assert result.status == "unchecked"
    assert result.findings == []


def test_audit_cli_missing_receipt_is_unchecked(tmp_path):
    note = tmp_path / "record.md"
    note.write_text("Date: 2026-11-11, 00:01\n")
    for module, evidence in [
        ("dateline_audit", []),
        ("dateline_audit", ["--receipt", str(tmp_path / "missing.json")]),
        ("receipt_audit", []),
    ]:
        result = subprocess.run(
            [sys.executable, "-m", f"scripts.{module}", *evidence, "--json", str(note)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 3, result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "unchecked" and payload["reason"]
        assert payload["findings"] == []


def test_cli_checks_explicit_new_scope_even_with_historical_claims(tmp_path, capsys):
    new = tmp_path / "new.md"
    old = tmp_path / "old.md"
    new.write_text("Date: 2026-11-11, 00:01\n")
    old.write_text("Date: 2026-11-10, 15:03\n")
    receipt = tmp_path / "attempt.json"
    receipt.write_text(json.dumps({
        "schema_version": 1, "status": "succeeded",
        "spawned_at": WINDOW.start.isoformat(), "completed_at": WINDOW.end.isoformat(),
    }))

    assert datelines.main(["--receipt", str(receipt), "--json", str(new)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "checked"
    assert [item["file"] for item in result["findings"]] == [str(new)]

    assert datelines.main(["--receipt", str(receipt), "--old", str(old), "--json", str(new)]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "unchecked" and "historical" in result["reason"]
    assert [item["file"] for item in result["findings"]] == [str(new)]


@pytest.mark.parametrize("header", [
    "Date: 2026-11-13, 23:57-00:01",
    "Date: 2026-11-14, 11:01 UTC-05:00",
    "Date: 2026-11-14, 11:01 UTC -05:00",
    "`date` returned `2026-11-13T11:01:00.250-05:00`",
    "Date: `date` returned Fri Nov 13 16:01:00 GMT 2026",
    "clock read about 00:10",
])
def test_header_clock_spellings_are_checked(header):
    result = datelines.audit({}, {NOTE: header}, WINDOW)
    assert result.status == "checked"
    assert result.findings == []


@pytest.mark.parametrize("header", [
    "Date: 2026-11-13, 22:50-00:01",
    "Date: 2026-11-13, 22:50–00:01",
])
def test_ranges_enclosing_the_window_have_a_width_limit(header):
    result = datelines.audit({}, {NOTE: header}, WINDOW)
    assert [(item.code, item.origin) for item in result.findings] == [
        ("range_too_wide", "new"),
    ]


def test_nonoverlapping_wide_range_is_outside_window():
    result = datelines.audit({}, {NOTE: "Date: 2026-11-13, 17:00–20:00"}, WINDOW)
    assert [(item.code, item.origin) for item in result.findings] == [
        ("outside_window", "new"),
    ]


def test_unix_and_explicit_cue_dates_cannot_borrow_the_header_day():
    for reading in ("Thu Nov 12 16:01:00 GMT 2026", "2026-11-12 16:01 UTC"):
        result = datelines.audit({}, {NOTE: f"Date: 2026-11-14 (`date` read {reading})"}, WINDOW)
        assert [(item.code, item.origin) for item in result.findings] == [
            ("outside_window", "new"),
        ]


def test_date_referenced_in_title_does_not_create_a_dateline():
    result = datelines.audit({}, {
        NOTE: "# Notes on 2026-11-09 and the 09:00 session\n\nNew prose."
    }, WINDOW)
    assert result.status == "checked"
    assert result.findings == []


def test_numeric_timezone_with_out_of_range_minutes_is_unchecked():
    result = datelines.audit({}, {NOTE: "Date: 2026-11-14, 11:01 -0561"}, WINDOW)
    assert result.status == "unchecked"
    assert [item.code for item in result.findings] == ["unsupported_timezone"]


def test_cue_attributed_estimate_wording_gets_rounding_tolerance():
    result = datelines.audit({}, {NOTE: "clock read about 00:20"}, WINDOW)
    assert [item.code for item in result.findings] == ["outside_window"]


def test_a_clock_cue_uses_a_leading_date_label_even_if_the_label_has_no_time():
    after = {NOTE: "# Older record\n\nDate: 2026-11-09\n\nClock read 00:01\n"}
    result = datelines.audit({}, after, WINDOW)
    assert [item.code for item in result.findings] == ["outside_window"]


def test_narrative_clock_text_following_a_body_paragraph_is_ignored():
    after = {NOTE: "# Record\n\nThis note describes a different session.\n\nClock read 03:00 CST\n"}
    result = datelines.audit({}, after, WINDOW)
    assert result.status == "checked"
    assert result.findings == []


def test_dated_title_clock_uses_the_dateline_date():
    after = {NOTE: "# Reviewing 2026-11-09 — 2026-11-13, 23:57"}
    result = datelines.audit({}, after, WINDOW)
    assert result.status == "checked"
    assert result.findings == []


def test_explicit_iso_fraction_counts_at_the_grace_boundary():
    result = datelines.audit({}, {
        NOTE: "Clock read 2026-11-13T16:04:30.25Z",
    }, WINDOW)
    assert [item.code for item in result.findings] == ["outside_window"]


@pytest.mark.parametrize("header", [
    "Date: 2026-11-13, 16:01 utc",
    "Date: 2026-11-13, 16:01 gMt",
    "Date: 2026-11-13, 11:01 utc-05:00",
    "`date` read Fri Nov 13 16:01:00 gmt 2026",
    "Date: 2026-11-13, 23:57 before the run",
    "Date: 2026-11-13, 23:57 in the log",
    "Date: 2026-11-13, 23:57 UTCish",
])
def test_case_insensitive_zones_have_token_boundaries(header):
    result = datelines.audit({}, {NOTE: header}, WINDOW)
    assert result.status == "checked"
    assert result.findings == []


@pytest.mark.parametrize("header", [
    "Date: 2026-11-13, 23:57 cst",
    "Clock read 2026-11-13T23:57:00cSt",
    "Clock read 2026-11-13T23:57:00unknown",
])
def test_unsupported_lowercase_zones_preserve_the_reading(header):
    result = datelines.audit({}, {NOTE: header}, WINDOW)
    assert result.status == "unchecked"
    assert [item.code for item in result.findings] == ["unsupported_timezone"]


def test_explicit_lowercase_z_timestamp_is_compared():
    result = datelines.audit({}, {NOTE: "Clock read 2026-11-12T16:03:00z"}, WINDOW)
    assert [item.code for item in result.findings] == ["outside_window"]


@pytest.mark.parametrize("day", ["9999-12-31", "0001-01-01"])
def test_boundary_datelines_keep_independent_findings_and_cli_json(day, tmp_path, capsys):
    note = tmp_path / "boundary.md"
    independent = tmp_path / "independent.md"
    note.write_text(f"Date: {day}, 00:01")
    independent.write_text("Date: 2026-11-09, 03:15")
    receipt = tmp_path / "attempt.json"
    receipt.write_text(json.dumps({
        "schema_version": 1, "status": "succeeded",
        "spawned_at": WINDOW.start.isoformat(), "completed_at": WINDOW.end.isoformat(),
    }))

    assert datelines.main(["--receipt", str(receipt), "--json", str(note), str(independent)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "checked"
    assert [(item["file"], item["code"]) for item in result["findings"]] == [
        (str(note), "outside_window"),
        (str(independent), "outside_window"),
    ]


def test_unrepresentable_range_end_does_not_suppress_other_findings():
    result = datelines.audit({}, {
        NOTE: "Clock read 9999-12-31T23:57:00Z–00:01",
        Path("other.md"): "Date: 2026-11-09, 03:15",
    }, WINDOW)
    assert result.status == "unchecked"
    assert [(item.file, item.code) for item in result.findings] == [
        (str(NOTE), "unrepresentable_clock"),
        ("other.md", "outside_window"),
    ]


def test_header_metadata_cues_are_checked_without_scanning_body_metadata():
    after = {NOTE: (
        "# Note\n\n**Evidence:** clock read 03:00 CST\n\n"
        "The discussion starts here.\n\n**Other evidence:** clock read 07:00 PST"
    )}
    result = datelines.audit({}, after, WINDOW)
    assert result.status == "unchecked"
    assert [(item.line, item.code) for item in result.findings] == [
        (3, "unsupported_timezone"),
    ]
