"""Store recall failures remain visible while a prompt can continue."""

import subprocess

import pytest


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
