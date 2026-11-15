"""Store recall failures remain visible while a prompt can continue."""

import json
import subprocess
from pathlib import Path

import pytest


def recall_host_timeout():
    template = Path(__file__).resolve().parents[2] / "plugin" / "codex.hooks.json.template"
    prompt_hooks = json.loads(template.read_text())["hooks"]["UserPromptSubmit"]
    return next(
        hook["timeout"] for group in prompt_hooks for hook in group["hooks"]
        if "spontaneous-recall.py" in hook["command"]
    )


@pytest.mark.parametrize("response", [
    pytest.param(subprocess.CompletedProcess([], 1, "", "private error detail"),
                 id="nonzero-exit"),
    pytest.param(FileNotFoundError("private error detail"), id="missing-executable"),
    pytest.param(subprocess.TimeoutExpired("docker", 15, stderr="private error detail"),
                 id="timeout"),
])
def test_store_failure_is_visible_in_quiet_mode(recall_hook, tmp_path, response):
    payload, _ = recall_hook(settings={"SILL_RECALL_QUIET": "1"}, store_response=response)
    visible = payload["systemMessage"]
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert visible.startswith("[TIME]")
    assert "Store recall failed" in visible
    assert str(tmp_path / "spontaneous-recall.log") in visible
    assert "private error detail" not in visible
    assert "private error detail" not in context


@pytest.mark.parametrize("quiet", ["0", "1"])
def test_store_failure_preserves_episodic_results(recall_hook, quiet):
    snippet = "Surveyors measured cedar seedlings after the spring rain"
    payload, _ = recall_hook(
        settings={"SILL_RECALL_QUIET": quiet},
        store_response=subprocess.CompletedProcess([], 1, "", "private error detail"),
        episodes=f'1. [survey, 2026-10-01] - 90% match\n   "{snippet}"\n',
    )
    visible = payload["systemMessage"]
    assert "Store recall failed" in visible
    assert "recall returned nothing" not in visible
    assert (snippet in visible) is (quiet == "0")
    assert snippet in payload["hookSpecificOutput"]["additionalContext"]


def test_successful_empty_recall_shows_only_the_time_header(recall_hook):
    payload, calls = recall_hook(settings={"SILL_RECALL_QUIET": "1"})
    assert calls["store"] and calls["episodes"]
    assert payload["systemMessage"].startswith("[TIME]")
    assert len(payload["systemMessage"].splitlines()) == 1


def test_slow_store_and_archive_leave_time_to_report_the_warning(recall_hook):
    payload, calls = recall_hook(
        settings={"SILL_RECALL_QUIET": "1"},
        delays={"project": 4, "clock": 9, "store": 16, "episodes": 31},
    )
    assert calls["elapsed"] <= recall_host_timeout() - 5
    assert "Store recall failed" in payload["systemMessage"]


def test_slow_successful_archive_survives_a_store_timeout(recall_hook):
    snippet = "Surveyors measured cedar seedlings after the spring rain"
    payload, calls = recall_hook(
        settings={"SILL_RECALL_QUIET": "1"},
        episodes=f'1. [survey, 2026-10-01] - 90% match\n   "{snippet}"\n',
        delays={"project": 2, "clock": 7, "store": 16, "episodes": 15},
    )
    assert calls["elapsed"] <= recall_host_timeout() - 5
    assert "Store recall failed" in payload["systemMessage"]
    assert snippet in payload["hookSpecificOutput"]["additionalContext"]


def test_access_tracking_leaves_time_to_emit_both_sources(recall_hook):
    memory = "The cedar meadow has three survey plots"
    memory_row = json.dumps(dict(id="7e40b444-12ba-40a5-9215-cc1340bbbe25",type="semantic",score=.5,importance=.2,content=memory))
    snippet = "Surveyors measured cedar seedlings after the spring rain"
    payload, calls = recall_hook(
        store_response=subprocess.CompletedProcess([], 0, memory_row, ""),
        episodes=f'1. [survey, 2026-10-01] - 90% match\n   "{snippet}"\n',
        delays={"project": 2, "clock": 7, "store": 14, "episodes": 16, "access": 11},
    )
    assert calls["elapsed"] <= recall_host_timeout() - 5
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert memory in context
    assert snippet in context
