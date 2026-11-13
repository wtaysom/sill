import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import quote_check


NOTE = Path("note.md")


def evidence(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def findings(result):
    return [(finding.line, finding.code, finding.origin) for finding in result.findings]


def test_quote_verifies_matching_text_and_reports_wrong_caption_time(tmp_path):
    article = evidence(tmp_path, "article.txt", "The café opens after lunch &amp; closes at dusk.")
    vtt = evidence(tmp_path, "speech.vtt", """WEBVTT

00:04:21.000 --> 00:04:22.000
<v Speaker>We test</v>

00:04:22.000 --> 00:04:23.000
We test
[Music] each new wheel
""")
    result = quote_check.audit({}, {NOTE: (
        '“The cafe opens after lunch & closes at dusk.” [Article](a)\n\n'
        '00:03:00: "We test each new wheel" [Speech](b)\n\n'
        '“We test each new wheel” ([Speech](b), 00:00:15).\n'
    )}, {"a": [article], "b": [vtt]})
    assert result.status == "checked"
    assert findings(result) == [(5, "wrong_timestamp", "new")]
    assert "00:04:21" in result.findings[0].detail


def test_quote_dropped_negation_is_a_mismatch_not_harmless_elision(tmp_path):
    captions = evidence(tmp_path, "speech.srt", """1
00:01:00,000 --> 00:01:03,000
We could not in principle predict the delay.

2
00:01:04,000 --> 00:01:08,000
Then we, you know, inspected the northern bridge.
""")
    result = quote_check.audit({}, {NOTE: (
        '“We could in principle predict the delay” [Speech](src) (00:01:00).\n\n'
        '“Then we inspected the northern bridge” [Speech](src) (00:01:04).\n'
    )}, {"src": [captions]})
    assert result.status == "checked"
    assert findings(result) == [(1, "text_mismatch", "new")]
    assert "harmless elision" in result.reason.lower()
    assert quote_check.audit({}, {NOTE: (
        '“Then we inspected the northern bridge” [Speech](src) (00:01:04).\n'
    )}, {"src": [captions]}).exit_code == 0


def test_quote_requires_whole_words_and_checks_short_quotes_with_direct_citations(tmp_path):
    article = evidence(tmp_path, "article.txt", "It was impossible to predict the outcome. A disagreement followed.")
    result = quote_check.audit({}, {NOTE: (
        '“Possible to predict the outcome” [Article](src).\n\n'
        '“Agreement” [Article](src).\n\n'
        'The author used the term “outcome”.\n'
    )}, {"src": [article]})
    assert result.status == "checked"
    assert findings(result) == [(1, "text_mismatch", "new"), (3, "text_mismatch", "new")]


def test_quote_declared_ellipsis_allows_omissions_but_not_polarity_changes(tmp_path):
    article = evidence(tmp_path, "article.txt", (
        "The team tested wheels in the workshop before the rain stopped. "
        "We could not move the cart during the storm. "
        "They recorded only the final result after the test."
    ))
    result = quote_check.audit({}, {NOTE: (
        '“The team tested wheels […] before the rain stopped” [Article](src).\n\n'
        '“The team tested wheels before the rain stopped” [Article](src).\n\n'
        '“We could ... move the cart during the storm” [Article](src).\n\n'
        '“They recorded … the final result after the test” [Article](src).\n'
    )}, {"src": [article]})
    assert result.status == "checked"
    assert findings(result) == [(3, "text_mismatch", "new"), (5, "text_mismatch", "new"), (7, "text_mismatch", "new")]


@pytest.mark.parametrize("citation,sources,reason", [
    ("", {"src": [Path("unused.txt")]}, "no mapped citation"),
    ("[Source](other)", {"src": [Path("unused.txt")]}, "no mapped citation"),
    ("[Source](src)", {"src": [Path("unavailable.txt")]}, "unavailable"),
    ("[Source](src)", {"src": []}, "unavailable"),
])
def test_quote_source_unresolved_is_unchecked(citation, sources, reason):
    result = quote_check.audit({}, {NOTE: f'“The winds have changed” {citation}'}, sources)
    assert result.status == "unchecked"
    assert result.exit_code == 3
    assert result.findings == []
    assert reason in result.reason.lower()


def test_quote_association_prefers_the_block_and_does_not_guess_among_sources(tmp_path):
    article = evidence(tmp_path, "article.txt", "The winds have changed since yesterday.")
    other = evidence(tmp_path, "other.txt", "The sky is clear above us.")
    result = quote_check.audit({}, {NOTE: (
        '[Article](a) and [Other](b)\n\n'
        '“The winds have changed” [Article](a)\n\n'
        '“The winds have changed” [Other](b)\n\n'
        '“The winds have changed”\n\n'
        '“The winds have changed” [Article](a) [Other](b)\n\n'
        '“The winds have changed” [Missing](c)\n'
    )}, {"a": [article], "b": [other], "c": [tmp_path / "absent.txt"]})
    assert result.status == "unchecked"
    assert "ambiguous" in result.reason.lower()
    assert "unavailable" in result.reason.lower()
    assert findings(result) == [(5, "text_mismatch", "new")]


def test_quote_reference_citation_and_unique_file_fallback_ignore_code(tmp_path):
    article = evidence(tmp_path, "article.txt", "The winds have changed since yesterday.")
    text = (
        '[Talk]: <a path#part> "Citation title"\n'
        '[Other]: b\n\n'
        '“The winds have changed” [the talk][TALK]\n\n'
        '“The winds have changed”\n\n'
        '```md\n“An invented sentence” [Other]\n```\n\n'
        'Example: `[Other][]` or ``“A made-up sentence”``.\n'
    )
    result = quote_check.audit({}, {NOTE: text}, {"a path#part": [article], "b": [tmp_path / "missing.txt"]})
    assert result.status == "checked"
    assert result.findings == []


def test_quote_reference_citation_in_block_is_used_among_several_targets(tmp_path):
    a = evidence(tmp_path, "a.txt", "The winds have changed since yesterday.")
    b = evidence(tmp_path, "b.txt", "The sky is clear above us.")
    result = quote_check.audit({}, {NOTE: (
        '[Talk]: a\n[Other]: b\n\n'
        '“The winds have changed” [Talk][]\n\n'
        '“The winds have changed” [Other]\n'
    )}, {"a": [a], "b": [b]})
    assert result.status == "checked"
    assert findings(result) == [(6, "text_mismatch", "new")]


def test_quote_does_not_use_other_sentence_or_link_title_as_a_stamp(tmp_path):
    captions = evidence(tmp_path, "sample.vtt", "WEBVTT\n\n00:05:00.000 --> 00:05:03.000\nThe sky is clear.\n")
    result = quote_check.audit({}, {NOTE: (
        'An earlier part ran at 00:00:10. “The sky is clear” [Other](src "00:00:30")\n\n'
        'The introduction (00:05:00) says “The sky is clear” [Other](src) (00:00:10).\n'
    )}, {"src": [captions]})
    assert result.status == "checked"
    assert findings(result) == [(3, "wrong_timestamp", "new")]


def test_quote_positive_match_survives_unavailable_alternative(tmp_path):
    captions = evidence(tmp_path, "sample.vtt", "WEBVTT\n\n00:05:00.000 --> 00:05:03.000\nThe sky is clear.\n")
    sources = {"src": [captions, tmp_path / "missing.vtt"]}
    match = quote_check.audit({}, {NOTE: '“The sky is clear” [Speech](src) (00:05:00)'}, sources)
    wrong_time = quote_check.audit({}, {NOTE: '“The sky is clear” [Speech](src) (00:00:10)'}, sources)
    assert (match.status, match.findings) == ("checked", [])
    assert (wrong_time.status, wrong_time.findings) == ("unchecked", [])


def test_quote_baseline_preserves_older_mismatch_and_detects_changed_stamp(tmp_path):
    captions = evidence(tmp_path, "sample.vtt", "WEBVTT\n\n00:05:00.000 --> 00:05:03.000\nThe sky is clear.\n")
    original = '“The sky is cloudy” [Speech](src) (00:05:00).'
    old_time = '“The sky is clear” [Speech](src) (00:00:10).'
    new_time = '“The sky is clear” [Speech](src) (00:00:20).'
    after = {NOTE: f'# Edited note\n\n{original}\n\n{original}\n\n{new_time}\n'}
    before = {NOTE: f'{original}\n\n{old_time}\n'}
    sources = {"src": [captions]}
    assert findings(quote_check.audit(before, after, sources)) == [
        (3, "text_mismatch", "preexisting"),
        (5, "text_mismatch", "new"),
        (7, "wrong_timestamp", "new"),
    ]
    unknown = quote_check.audit(None, after, sources)
    assert unknown.status == "checked"
    assert [item.origin for item in unknown.findings] == ["unknown", "unknown", "unknown"]
    assert [item.origin for item in quote_check.audit({}, after, sources).findings] == ["new", "new", "new"]


def test_quote_changed_citation_or_ellipsis_uses_a_new_fingerprint(tmp_path):
    article = evidence(tmp_path, "article.txt", "The winds have never changed since yesterday.")
    sources = {"a": [article], "b": [article]}
    before = {NOTE: '“The winds have changed” [Article](a)\n\n“The winds have changed since yesterday” [Article](a)'}
    after = {NOTE: '“The winds have changed” [Article](b)\n\n“The winds have … changed since yesterday” [Article](a)'}
    assert findings(quote_check.audit(before, after, sources)) == [
        (1, "text_mismatch", "new"), (3, "text_mismatch", "new"),
    ]


def test_quote_cli_resolves_explicit_source_files_relative_to_the_mapping(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    source = evidence(config_dir, "source.txt", "The boat is red.")
    mapping = evidence(config_dir, "sources.json", json.dumps({"src": [source.name]}))
    note = evidence(tmp_path, "note.md", '“The boat is red” [Speech](src)\n\n“The boat is blue” [Speech](src)')
    run = subprocess.run(
        [sys.executable, "-m", "scripts.quote_check", "--sources", str(mapping), "--json", str(note)],
        capture_output=True, text=True, check=False,
    )
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert payload["status"] == "checked"
    assert [(item["line"], item["code"], item["origin"]) for item in payload["findings"]] == [
        (3, "text_mismatch", "unknown"),
    ]


@pytest.mark.parametrize("mapping_text", ['["source.txt"]', '{"src": "source.txt"}', '{"src": [3]}'])
def test_quote_cli_rejects_invalid_source_mapping(tmp_path, capsys, mapping_text):
    mapping = evidence(tmp_path, "sources.json", mapping_text)
    note = evidence(tmp_path, "note.md", '“The boat is red” [Speech](src)')
    assert quote_check.main(["--sources", str(mapping), "--json", str(note)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_quote_timing_remains_unchecked_when_only_plain_text_can_match(tmp_path):
    article = evidence(tmp_path, "article.txt", "The sky is clear.")
    result = quote_check.audit({}, {NOTE: '“The sky is clear” [Speech](src) (00:05:00)'}, {"src": [article]})
    assert result.status == "unchecked"
    assert not result.findings
    assert "wording found" in result.reason.lower()
    assert "00:05:00" in result.reason


def test_quote_bracketed_caption_file_handles_preamble_and_dual_stamp_readings(tmp_path):
    captions = evidence(tmp_path, "cue-log.txt", "Caption transcript\n\n[00:55:30,000] The sky is clear.\n")
    result = quote_check.audit({}, {NOTE: '“The sky is clear” [Speech](src) (00:55)'}, {"src": [captions]})
    assert result.status == "checked"
    assert result.findings == []


def test_quote_ignores_escaped_links_and_keeps_code_words_inside_a_real_quote(tmp_path):
    article = evidence(tmp_path, "article.txt", "We could go to town.")
    notes = {
        Path("escaped.md"): '“We could go to town” \\[Speech](src)',
        Path("quoted.md"): '“We could `never` go to town” [Speech](src)',
    }
    result = quote_check.audit({}, notes, {"src": [article]})
    assert result.status == "unchecked"
    assert "no mapped citation" in result.reason.lower()
    assert findings(result) == [(1, "text_mismatch", "new")]
    assert result.findings[0].file == "quoted.md"


@pytest.mark.parametrize("written,source", [
    ("We tested … samples", "We tested each of the samples"),
    ("We considered … options before choosing", "We considered other options before choosing"),
])
def test_quote_declared_ellipsis_cannot_drop_meaning_bearing_quantifiers(tmp_path, written, source):
    article = evidence(tmp_path, "article.txt", source)
    result = quote_check.audit({}, {NOTE: f'“{written}” [Talk](src)'}, {"src": [article]})
    assert result.status == "checked"
    assert findings(result) == [(1, "text_mismatch", "new")]


@pytest.mark.parametrize("note", [
    '“The sky is clear.” Then at 00:00:10 something else happened. [Talk](src)',
    'Earlier: 00:00:10. later, “The sky is clear” [Talk](src).',
])
def test_quote_does_not_borrow_time_across_sentence_boundaries(tmp_path, note):
    captions = evidence(tmp_path, "cue-log.txt", "[00:05:00] The sky is clear.\n")
    result = quote_check.audit({}, {NOTE: note}, {"src": [captions]})
    assert result.status == "checked"
    assert result.findings == []


@pytest.mark.parametrize("citation", [
    "(00:00:10) [Talk](src)",
    "[Talk](src) (00:00:10)",
    "([Talk](src), 00:00:10)",
])
def test_quote_keeps_parenthetical_cue_after_sentence_punctuation_inside_quotes(tmp_path, citation):
    captions = evidence(tmp_path, "cue-log.txt", "[00:05:00] The sky is clear.\n")
    result = quote_check.audit({}, {NOTE: f'“The sky is clear.” {citation}'}, {"src": [captions]})
    assert result.status == "checked"
    assert findings(result) == [(1, "wrong_timestamp", "new")]


@pytest.mark.parametrize("quote", ["The sky is clear", "The sky is … clear"])
def test_quote_cannot_join_separate_caption_passages(tmp_path, quote):
    captions = evidence(tmp_path, "cue-log.txt", "[00:00:02] The sky is\n[00:10:00] clear\n")
    notes = {NOTE: f'“{quote}” (00:05:00) [Talk](src)\n\n“{quote}” [Talk](src)'}
    result = quote_check.audit({}, notes, {"src": [captions]})
    assert result.status == "checked"
    assert findings(result) == [(1, "text_mismatch", "new"), (3, "text_mismatch", "new")]


@pytest.mark.parametrize("indent", ["    ", "\t"])
def test_quote_ignores_indented_code_but_checks_a_list_continuation(tmp_path, indent):
    article = evidence(tmp_path, "article.txt", "There is nothing else to say.")
    notes = {NOTE: (
        f'{indent}“No such passage exists” [Talk](src)\n\n'
        '1. Quoted passage:\n'
        '    “This list quotes missing words” [Talk](src)\n'
    )}
    result = quote_check.audit({}, notes, {"src": [article]})
    assert result.status == "checked"
    assert findings(result) == [(4, "text_mismatch", "new")]


@pytest.mark.parametrize("image", ["![Photo][cover]", "image![Photo][cover]"])
def test_quote_does_not_use_an_image_reference_as_a_citation(tmp_path, image):
    article = evidence(tmp_path, "article.txt", "The sky is clear.")
    result = quote_check.audit({}, {NOTE: f'[cover]: src\n\n“The sky is clear” {image}'}, {"src": [article]})
    assert result.status == "unchecked"
    assert "no mapped citation" in result.reason.lower()
    assert result.findings == []


def test_quote_near_harmless_elision_survives_unreadable_alternative(tmp_path):
    captions = evidence(tmp_path, "cue-log.txt", "[00:05:00] The sky, you know, is clear.\n")
    notes = {NOTE: '“The sky is clear” (00:05:00) [Talk](src)'}
    result = quote_check.audit({}, notes, {"src": [captions, tmp_path / "missing.vtt"]})
    assert result.status == "checked"
    assert result.findings == []
    assert "harmless elision" in result.reason.lower()


def test_quote_exact_elsewhere_precedes_harmless_elision_with_complete_evidence(tmp_path):
    captions = evidence(tmp_path, "cue-log.txt", (
        "[00:05:00] The sky, you know, is clear.\n[00:10:00] The sky is clear.\n"
    ))
    notes = {NOTE: '“The sky is clear” (00:05:00) [Talk](src)'}
    result = quote_check.audit({}, notes, {"src": [captions]})
    assert result.status == "checked"
    assert findings(result) == [(1, "wrong_timestamp", "new")]


def test_quote_ellipsis_requires_matched_words_near_the_cited_time(tmp_path):
    captions = evidence(tmp_path, "cue-log.txt", (
        "[00:00:00] The sky is\n"
        "[00:01:00] We paused\n"
        "[00:02:00] We walked\n"
        "[00:03:00] The cart rattled\n"
        "[00:04:00] The door opened\n"
        "[00:05:00] clear\n"
    ))
    note = '“The sky is … clear” (00:02:30) [Talk](src)'
    result = quote_check.audit({}, {NOTE: note}, {"src": [captions]})
    assert result.status == "checked"
    assert findings(result) == [(1, "wrong_timestamp", "new")]
