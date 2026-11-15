"""The v0.2 upgrade rehearsal's own judgments, offline.

The rehearsal itself needs a disposable runner: it installs v0.2.0 on the
default names and ports. These tests hold the parts that decide pass or fail
from what it observed, and the guard that keeps it off anyone's machine.
"""
import json
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[2]
KEEP, NEWER, OTHER = "aaaa", "bbbb", "cccc"
REVISION = "bfa3c846ac738e62aa61806ef9112d34acb1dc5a"


@pytest.fixture(scope="module")
def rehearsal():
    return runpy.run_path(str(ROOT / ".github/scripts/v02_upgrade_rehearsal.py"))


def row(memory_id, **changes):
    return dict(dict(id=memory_id, content_md5="c-" + memory_id, type="episodic", status="active", live=True,
                     created_at="2026-11-15T00:00:00+00:00", vector_md5="v-" + memory_id), **changes)


def archived(memory_id):
    return row(memory_id, status="archived", live=False)


def test_only_the_newer_repeated_copy_may_change(rehearsal):
    before = [row(KEEP), row(NEWER), row(OTHER)]
    rehearsal["compare_memories"](before, [row(KEEP), archived(NEWER), row(OTHER)], KEEP, NEWER)


@pytest.mark.parametrize("after, message", [
    ([row(KEEP), archived(NEWER)], "lost or added"),
    ([row(KEEP), archived(NEWER), row(OTHER), row("dddd")], "lost or added"),
    ([row(KEEP), row(NEWER), row(OTHER)], f"memory {NEWER} changed"),
    ([archived(KEEP), row(NEWER), row(OTHER)], f"memory {KEEP} changed"),
    ([archived(KEEP), archived(NEWER), row(OTHER)], f"memory {KEEP} changed"),
    ([row(KEEP), archived(NEWER), archived(OTHER)], f"memory {OTHER} changed"),
    ([row(KEEP), archived(NEWER), row(OTHER, vector_md5="re-embedded")], "vector_md5"),
    ([row(KEEP), archived(NEWER), row(OTHER, content_md5="rewritten")], "content_md5"),
    ([row(KEEP), row(NEWER, status="archived", live=False, created_at="2026-11-16T00:00:00+00:00"),
      row(OTHER)], "created_at"),
])
def test_any_other_change_to_the_store_fails(rehearsal, after, message):
    with pytest.raises(RuntimeError, match=message):
        rehearsal["compare_memories"]([row(KEEP), row(NEWER), row(OTHER)], after, KEEP, NEWER)


def test_a_pair_that_was_not_live_beforehand_fails(rehearsal):
    before = [row(KEEP), archived(NEWER)]
    with pytest.raises(RuntimeError, match="not live before"):
        rehearsal["compare_memories"](before, [row(KEEP), archived(NEWER)], KEEP, NEWER)


def test_cached_revision_is_the_single_snapshot_main_names(rehearsal):
    assert rehearsal["cached_revision"](f"{REVISION}\n--\n{REVISION}") == REVISION


@pytest.mark.parametrize("listing", [
    f"{REVISION}\n--",                                   # pointer, no snapshot
    f"\n--\n{REVISION}",                                 # snapshot, no pointer
    f"{REVISION}\n--\n{REVISION}\n{'0' * 40}",           # a second snapshot
    f"{REVISION}\n--\n{'0' * 40}",                       # pointer names another
    f"main\n--\n{REVISION}",                             # not a hash
])
def test_cached_revision_refuses_anything_it_cannot_prove(rehearsal, listing):
    with pytest.raises(RuntimeError):
        rehearsal["cached_revision"](listing)


def test_the_old_image_build_changes_only_where_packages_come_from(rehearsal):
    """v0.2.0's Dockerfile no longer builds: its Debian and PostgreSQL sources were retired."""
    tagged = ("FROM postgres:16-bullseye\n\n# 1. Install Build Dependencies\n"
              "RUN apt-get update && apt-get install -y \\\n    postgresql-server-dev-16 \\\n    gcc\n")
    built = rehearsal["with_archived_sources"](tagged)
    lines = built.splitlines(keepends=True)
    assert lines[0] == "FROM postgres:16-bullseye\n" and "".join(lines[:1] + lines[2:]) == tagged
    added = lines[1]
    assert added.startswith("RUN ") and added.count("\n") == 1
    for needed in ("http://archive.debian.org/", "https://apt-archive.postgresql.org/",
                   "/etc/apt/sources.list.d/pgdg.list", "ca-certificates"):
        assert needed in added
    # It refuses a base it was not written for, and never applies twice.
    for other in (tagged.replace("bullseye", "bookworm"), built):
        with pytest.raises(RuntimeError, match="not the one this rehearsal knows"):
            rehearsal["with_archived_sources"](other)


def test_env_file_values_lose_one_layer_of_quotes(rehearsal):
    text = (f"# written by the upgrade\n\nEMBEDDING_REVISION='{REVISION}'\n"
            'EMBEDDING_MODEL_ID="unsloth/embeddinggemma-300m"\nEMBEDDING_DIMENSION=768\n'
            "NOTE = it's = fine \nnot a setting\n")
    assert rehearsal["read_env"](text) == {
        "EMBEDDING_REVISION": REVISION, "EMBEDDING_MODEL_ID": "unsloth/embeddinggemma-300m",
        "EMBEDDING_DIMENSION": "768", "NOTE": "it's = fine"}


def test_it_refuses_to_run_anywhere_but_a_hosted_runner(rehearsal, tmp_path, monkeypatch):
    """On a personal machine the default names are somebody's real store."""
    called = tmp_path / "docker-was-called"
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("docker", "git"):
        (tools / name).write_text(f"#!/bin/sh\ntouch '{called}'\n")
        (tools / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    evidence = tmp_path / "evidence"
    assert rehearsal["main"](evidence) == 1
    report = json.loads((evidence / "evidence.json").read_text())
    assert report["result"] == "failed" and report["phase"] == "guard"
    assert "GitHub-hosted runner" in report["failure"]
    assert not called.exists()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")
    assert rehearsal["main"](tmp_path / "evidence-2") == 1 and not called.exists()


def test_the_workflow_runs_it_with_the_tags_it_starts_from():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    job = workflow.split("  v02-default-upgrade:", 1)[1]
    assert "fetch-depth: 0" in job and ".github/scripts/v02_upgrade_rehearsal.py" in job
    assert "if: always()" in job and "sill-v02-upgrade-evidence" in job
