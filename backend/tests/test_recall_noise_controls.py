"""Behavior of the prompt hook's recall settings with synthetic results."""

import subprocess
import json

import pytest


@pytest.mark.parametrize("value,expected", [
    ("-1", 5), ("51", 5), ("99999999999", 5), ("invalid", 5), (" 1 ", 1), ("50", 50),
])
def test_memory_count_controls_query_limit(recall_hook, value, expected):
    _, calls = recall_hook(settings={"SILL_RECALL_MAX_MEMORIES": value})
    query, = calls["store"]
    assert query["limit"] == expected


@pytest.mark.parametrize("value,expected", [
    ("-1", 2), ("51", 2), ("99999999999", 2), ("invalid", 2), (" 1 ", 1), ("50", 50),
])
def test_conversation_count_controls_archive_limit(recall_hook, value, expected):
    _, calls = recall_hook(settings={"SILL_RECALL_MAX_CONVERSATIONS": value})
    assert calls["episodes"] == [expected]


@pytest.mark.parametrize("setting,disabled,enabled", [
    ("SILL_RECALL_MAX_MEMORIES", "store", "episodes"),
    ("SILL_RECALL_MAX_CONVERSATIONS", "episodes", "store"),
])
def test_zero_count_skips_only_that_recall_source(recall_hook, setting, disabled, enabled):
    payload, calls = recall_hook(settings={setting: "0"})
    assert calls[disabled] == []
    assert len(calls[enabled]) == 1
    assert payload["systemMessage"].splitlines()[0].startswith("[TIME]")


@pytest.mark.parametrize("value", ["-1", "10001", "99999999999", "invalid"])
def test_invalid_minimum_length_uses_the_twenty_character_default(recall_hook, value):
    settings = {"SILL_RECALL_MIN_QUERY_LENGTH": value}
    _, below = recall_hook(settings=settings, prompt="Red cedar meadow")
    _, above = recall_hook(settings=settings, prompt="Cedar meadow survey records")
    assert below == {"store": [], "episodes": []}
    assert above["store"] and above["episodes"]


def test_zero_minimum_length_allows_short_topical_prompts(recall_hook):
    _, calls = recall_hook(settings={"SILL_RECALL_MIN_QUERY_LENGTH": "0"},
                           prompt="Red cedar meadow")
    assert calls["store"] and calls["episodes"]


@pytest.mark.parametrize("value,expected", [
    ("-0.1", 0.25), ("1.01", 0.25), ("1e400", 0.25), ("NaN", 0.25),
    ("Inf", 0.25), ("-Inf", 0.25), ("invalid", 0.25),
    (" 0.75 ", 0.75), ("1", 1.0),
])
def test_similarity_setting_controls_fast_recall_only(recall_hook, value, expected):
    _, calls = recall_hook(settings={"SILL_RECALL_MIN_SIMILARITY": value})
    query, = calls["store"]
    assert query["fast_min_similarity"] == expected



def test_zero_fast_score_floor_omits_the_filter(recall_hook):
    _, calls = recall_hook(settings={"SILL_RECALL_MIN_SIMILARITY": "0"})
    query, = calls["store"]
    assert query["fast_min_similarity"] == 0


def test_maximum_supported_minimum_is_honored(recall_hook):
    _, calls = recall_hook(settings={"SILL_RECALL_MIN_QUERY_LENGTH": "10000"})
    assert calls == {"store": [], "episodes": []}


MEMORY = "The cedar meadow has three survey plots"
MEMORY_ID = "7e40b444-12ba-40a5-9215-cc1340bbbe25"
CONVERSATION = "Surveyors measured cedar seedlings after the spring rain"
EPISODE = f'1. [survey, 2026-10-01] - 90% match\n   "{CONVERSATION}"\n'


def test_fast_floor_does_not_filter_hybrid_rrf_scores(recall_hook):
    payload, calls = recall_hook(
        settings={"SILL_RECALL_MIN_SIMILARITY": "1"},
        prompt="Codex hook settings for cedar meadow",
        store_response=subprocess.CompletedProcess(
            [], 0, json.dumps(dict(id=MEMORY_ID,type="semantic",score=0.0164,importance=.2,content=MEMORY)), "",
        ),
    )
    query, = calls["store"]
    assert query["fast_min_similarity"] == 1
    assert MEMORY in payload["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("setting,quiet", [
    (None, False), ("0", False), ("1", True), (" TrUe ", True), ("YES", True),
])
def test_quiet_only_hides_the_displayed_result_list(recall_hook, setting, quiet):
    settings = {"SILL_RECALL_QUIET": setting} if setting is not None else {}
    payload, _ = recall_hook(
        settings=settings,
        store_response=subprocess.CompletedProcess(
            [], 0, json.dumps(dict(id=MEMORY_ID,type="semantic",score=0.5,importance=.2,content=MEMORY)), "",
        ),
        episodes=EPISODE,
    )
    visible = payload["systemMessage"]
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert visible.startswith("[TIME]")
    assert ("[sill] Recalled:" in visible) is (not quiet)
    for result in (MEMORY, CONVERSATION):
        assert (result in visible) is (not quiet)
        assert result in context
