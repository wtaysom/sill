"""install.sh --scope (home|project) and upgrade.sh's Codex hook-refresh path.

Hard constraint for this file: never write to the real ``~/.claude`` or
``~/.codex``. Three techniques keep that true:

1. Every ``install.sh`` invocation below passes ``--dry-run`` (which never
   performs a single write anywhere in the script — see ``run()`` and every
   ``DRY_RUN``-gated branch in install.sh) *and* overrides ``HOME`` to a
   pytest ``tmp_path``, so even a latent dry-run bug can't reach the real
   home directory.
2. The two tests that exercise install.sh's real (non-dry-run) file-writing
   logic for the new home-scope helpers do so by *sourcing* install.sh in a
   subprocess (install.sh's own "main" section is guarded by a
   ``BASH_SOURCE[0] == $0`` check, so sourcing it does not run the installer
   pipeline) and calling the specific helper function directly, with ``HOME``
   pointed at a tmp_path. Nothing outside that tmp_path is ever touched.
3. upgrade.sh's Codex-hooks tests always pass ``--hooks-for <tmp_path>``,
   which scopes every write to that path, and use ``--hooks-only`` to skip
   the database steps entirely. This matters beyond hygiene: this
   development machine has a *live* ``sill_db`` container (confirmed via
   ``docker ps`` while writing this file), so any invocation that reached
   upgrade.sh's DB preflight without ``--hooks-only`` would talk to a real
   database. One test deliberately checks the ordering (hooks refresh runs
   before DB preflight) using a nonexistent container name instead, the same
   safe pattern ``test_hook_safety.py`` already uses.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"
UPGRADE_SH = REPO_ROOT / "upgrade.sh"
AMBIENT_TEMPLATE = REPO_ROOT / "plugin" / "claude.home.md.template"


def run_install(args, home, extra_env=None, timeout=60):
    """Run install.sh as a real subprocess with HOME overridden to a tmp
    path. Callers must pass --dry-run unless they have their own reason to
    believe the invocation cannot reach a real step (none in this file do
    this without --dry-run)."""
    env = {**os.environ, "HOME": str(home)}
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def run_upgrade(args, extra_env=None, timeout=60):
    env = {**os.environ}
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(UPGRADE_SH), *args],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def source_and_call(shell_body, home, timeout=30):
    """Source install.sh (guarded main, so nothing auto-runs) and execute
    shell_body, with HOME pointed at a tmp path. Lets tests call install.sh's
    real helper functions directly without running the full 10-step
    pipeline (which would build docker images, etc.)."""
    script = f'set -euo pipefail\nsource "{INSTALL_SH}"\n{shell_body}\n'
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=timeout,
    )


# --- install.sh --help documents both scopes ----------------------------------

def test_help_documents_the_scope_flag_and_both_values():
    r = run_install(["--help"], home="/nonexistent-unused-for-help")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "--scope" in out
    assert "home" in out
    assert "project" in out


def test_help_states_the_tradeoff_honestly_for_both_directions():
    r = run_install(["--help"], home="/nonexistent-unused-for-help")
    out = r.stdout.lower()
    # project: narrow blast radius, no cross-project mixing
    assert "blast radius" in out
    assert "cross-project" in out
    # home: latency paid on every prompt, one store reachable by every project
    assert "latency" in out
    assert "every project" in out or "every prompt" in out


def test_help_documents_default_scope_is_project():
    r = run_install(["--help"], home="/nonexistent-unused-for-help")
    assert "default" in r.stdout.lower()
    assert "project" in r.stdout


# --- invalid --scope exits non-zero, naming valid values ----------------------

def test_invalid_scope_value_exits_nonzero_naming_valid_values(tmp_path):
    r = run_install(["--dry-run", "--scope", "bogus"], home=tmp_path)
    assert r.returncode != 0
    combined = r.stdout + r.stderr
    assert "bogus" in combined
    assert "home" in combined
    assert "project" in combined


def test_scope_flag_missing_value_exits_nonzero(tmp_path):
    r = run_install(["--dry-run", "--scope"], home=tmp_path)
    assert r.returncode != 0


# --- --dry-run renders different targets per scope -----------------------------

def test_dry_run_home_scope_targets_user_scope_paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = run_install(["--dry-run", "--scope", "home"], home=home)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert f"{home}/.codex/hooks.json" in out
    assert f"{home}/.claude/settings.json" in out
    assert f"{home}/.claude/CLAUDE.md" in out
    # Home scope without --hooks-for never mentions a project-local file.
    assert "settings.local.json" not in out


def test_dry_run_project_scope_targets_the_hooks_for_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    r = run_install(
        ["--dry-run", "--scope", "project", "--hooks-for", str(project)], home=home,
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert f"{project}/.codex/hooks.json" in out
    assert f"{project}/.claude/settings.local.json" in out
    # Project scope never installs the ambient file or writes home-scope
    # hook targets.
    assert "CLAUDE.md" not in out
    assert f"{home}/.claude/settings.json" not in out


def test_dry_run_targets_actually_differ_between_scopes(tmp_path):
    """The two prior tests each check one scope in isolation; this one
    proves the difference directly by diffing the two output sets."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()

    home_out = run_install(["--dry-run", "--scope", "home"], home=home).stdout
    project_out = run_install(
        ["--dry-run", "--scope", "project", "--hooks-for", str(project)], home=home,
    ).stdout

    assert home_out != project_out
    home_targets = {line for line in home_out.splitlines() if ".codex/hooks.json" in line or "settings.json" in line}
    project_targets = {line for line in project_out.splitlines() if ".codex/hooks.json" in line or "settings" in line}
    assert home_targets.isdisjoint(project_targets)


def test_dry_run_default_scope_is_project_and_does_nothing_without_hooks_for(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = run_install(["--dry-run"], home=home)
    assert r.returncode == 0, r.stderr
    assert "scope: project" in r.stdout
    assert "--hooks-for not provided; skipping" in r.stdout
    assert f"{home}/.codex/hooks.json" not in r.stdout
    assert f"{home}/.claude/settings.json" not in r.stdout


def test_dry_run_home_scope_also_honors_hooks_for_additively(tmp_path):
    """--scope home does user-scope wiring unconditionally; --hooks-for
    layers project-scope wiring on top rather than being ignored."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    r = run_install(
        ["--dry-run", "--scope", "home", "--hooks-for", str(project)], home=home,
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert f"{home}/.codex/hooks.json" in out
    assert f"{project}/.codex/hooks.json" in out
    assert f"{project}/.claude/settings.local.json" in out


# --- real (non-dry-run) home-scope helper behavior, via source+call -----------

def test_home_scope_ambient_file_is_idempotent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = source_and_call(
        '_install_home_ambient_file "$SILL_DIR/plugin"\n'
        '_install_home_ambient_file "$SILL_DIR/plugin"\n',
        home=home,
    )
    assert r.returncode == 0, r.stderr
    dst = home / ".claude" / "CLAUDE.md"
    assert dst.exists()
    content = dst.read_text()
    assert content.count("sill:home-scope-ambient") == 1
    assert "Sill" in content


def test_home_scope_ambient_file_never_clobbers_existing_claude_md(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    preexisting = "# My own global instructions\n\nDo not delete this line.\n"
    (home / ".claude" / "CLAUDE.md").write_text(preexisting)
    r = source_and_call('_install_home_ambient_file "$SILL_DIR/plugin"\n', home=home)
    assert r.returncode == 0, r.stderr
    content = (home / ".claude" / "CLAUDE.md").read_text()
    assert preexisting in content
    assert "sill:home-scope-ambient" in content


def test_home_scope_codex_render_writes_fresh_then_leaves_existing_alone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    body = (
        'sill_py="$(sill_python)"\n'
        '_render_codex_hooks "$SILL_DIR/plugin/codex.hooks.json.template" '
        '"$HOME/.codex/hooks.json" "$SILL_DIR/plugin" "$sill_py"\n'
    )
    r1 = source_and_call(body, home=home)
    assert r1.returncode == 0, r1.stderr
    dst = home / ".codex" / "hooks.json"
    assert dst.exists()
    rendered = dst.read_text()
    assert "{{SILL_PLUGIN_DIR}}" not in rendered
    assert str(REPO_ROOT / "plugin") in rendered

    dst.write_text("TAMPERED-BY-TEST")
    r2 = source_and_call(body, home=home)
    assert r2.returncode == 0, r2.stderr
    assert dst.read_text() == "TAMPERED-BY-TEST"
    assert "already exists" in r2.stdout


def test_rendered_hook_command_handles_spaces_and_apostrophes(tmp_path):
    import shlex

    home = tmp_path / "new home"
    home.mkdir()
    plugin = tmp_path / "owner's checkout" / "plugin"
    interpreter = home / "python tools" / "python"
    r = source_and_call(
        '_render_codex_hooks "$SILL_DIR/plugin/codex.hooks.json.template" '
        f'"$HOME/hooks.json" {shlex.quote(str(plugin))} {shlex.quote(str(interpreter))}',
        home=home,
    )
    assert r.returncode == 0, r.stderr
    command = json.loads((home / "hooks.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert shlex.split(command) == ["env", "SILL_INSTANCE_ENV_FILE=" + str(REPO_ROOT / "backend/.env"), str(interpreter), str(plugin / "hooks/response-patterns.py")]


def test_home_scope_claude_merge_is_idempotent_and_preserves_other_content(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    dst = home / ".claude" / "settings.json"
    preexisting = {
        "model": "some-model",
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "my-own-hook.py"}]}],
        },
    }
    dst.write_text(json.dumps(preexisting, indent=2))
    body = (
        'sill_py="$(sill_python)"\n'
        '_merge_claude_hooks "$SILL_DIR/plugin/codex.hooks.json.template" '
        '"$HOME/.claude/settings.json" "$SILL_DIR/plugin" "$sill_py"\n'
        '_merge_claude_hooks "$SILL_DIR/plugin/codex.hooks.json.template" '
        '"$HOME/.claude/settings.json" "$SILL_DIR/plugin" "$sill_py"\n'
    )
    r = source_and_call(body, home=home)
    assert r.returncode == 0, r.stderr
    data = json.loads(dst.read_text())
    assert data["model"] == "some-model"
    commands = [
        h["command"]
        for entry in data["hooks"]["Stop"]
        for h in entry["hooks"]
    ]
    assert any("my-own-hook.py" in c for c in commands)
    matching = [c for c in commands if "response-patterns.py" in c]
    assert len(matching) == 1, "merge should be idempotent, not duplicate on a second run"


# --- plugin/claude.home.md.template itself -------------------------------------

def test_ambient_template_exists_and_is_sanitized():
    assert AMBIENT_TEMPLATE.is_file()
    text = AMBIENT_TEMPLATE.read_text()
    assert text.strip()
    lowered = text.lower()
    # No house lore / personal names / internal env-var families.
    for forbidden in ("william", "wtaysom", "sili ", "gnomon", "beat "):
        assert forbidden not in lowered, f"ambient template leaks house content: {forbidden!r}"
    assert "SILI_" not in text
    assert "GNOMON_" not in text
    assert "/Users/" not in text


def test_ambient_template_explains_what_how_and_off_switch():
    text = AMBIENT_TEMPLATE.read_text().lower()
    assert "sill" in text
    assert "recall" in text  # automatic recall behavior
    assert "notice" in text or "remember" in text  # deliberate mint path
    assert "turn" in text and "off" in text  # how to disable


# --- upgrade.sh: --help mentions the Codex hooks file and the new flags -------

def test_upgrade_help_mentions_codex_hooks_file_and_new_flags():
    r = run_upgrade(["--help"])
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert ".codex/hooks.json" in out
    assert "--hooks-for" in out
    assert "--force-hooks" in out
    assert "--hooks-only" in out


def test_upgrade_help_explains_the_diff_before_overwrite_behavior():
    out = run_upgrade(["--help"]).stdout.lower()
    assert "diff" in out
    assert "force-hooks" in out


# --- upgrade.sh: flag validation -----------------------------------------------

def test_upgrade_hooks_only_without_hooks_for_errors(tmp_path):
    r = run_upgrade(["--hooks-only"])
    assert r.returncode != 0
    assert "--hooks-for" in (r.stdout + r.stderr)


def test_upgrade_force_hooks_without_hooks_for_errors(tmp_path):
    r = run_upgrade(["--force-hooks"])
    assert r.returncode != 0
    assert "--hooks-for" in (r.stdout + r.stderr)


# --- upgrade.sh: real hook-refresh behavior, scoped to tmp_path, DB-free ------

def test_upgrade_hooks_only_writes_fresh_codex_hooks_when_absent(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    r = run_upgrade(["--hooks-for", str(project), "--hooks-only"])
    assert r.returncode == 0, r.stderr
    dst = project / ".codex" / "hooks.json"
    assert dst.exists()
    content = dst.read_text()
    assert "{{SILL_PLUGIN_DIR}}" not in content
    assert str(REPO_ROOT / "plugin") in content


def test_upgrade_hooks_only_second_run_reports_up_to_date(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    r1 = run_upgrade(["--hooks-for", str(project), "--hooks-only"])
    assert r1.returncode == 0, r1.stderr
    before = (project / ".codex" / "hooks.json").read_text()

    r2 = run_upgrade(["--hooks-for", str(project), "--hooks-only"])
    assert r2.returncode == 0, r2.stderr
    assert "up to date" in r2.stdout.lower()
    assert (project / ".codex" / "hooks.json").read_text() == before


def test_upgrade_hooks_only_detects_stale_codex_hooks_and_does_not_overwrite(tmp_path):
    project = tmp_path / "proj"
    (project / ".codex").mkdir(parents=True)
    stale = json.dumps({"hooks": {"PreToolUse": []}}, indent=2)
    (project / ".codex" / "hooks.json").write_text(stale)

    r = run_upgrade(["--hooks-for", str(project), "--hooks-only"])
    assert r.returncode == 0, r.stderr
    assert "stale" in r.stdout.lower()
    assert "PreToolUse" in r.stdout  # the diff was actually printed
    assert (project / ".codex" / "hooks.json").read_text() == stale, \
        "must not overwrite without --force-hooks"


def test_upgrade_force_hooks_overwrites_stale_codex_hooks(tmp_path):
    project = tmp_path / "proj"
    (project / ".codex").mkdir(parents=True)
    stale = json.dumps({"hooks": {"PreToolUse": []}}, indent=2)
    (project / ".codex" / "hooks.json").write_text(stale)

    r = run_upgrade(["--hooks-for", str(project), "--hooks-only", "--force-hooks"])
    assert r.returncode == 0, r.stderr
    new_content = (project / ".codex" / "hooks.json").read_text()
    assert new_content != stale
    assert "{{SILL_PLUGIN_DIR}}" not in new_content
    out_lower = r.stdout.lower()
    assert "sha-256" in out_lower or "re-approv" in out_lower or "trust" in out_lower


def test_upgrade_hooks_only_dry_run_never_writes(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    r = run_upgrade(["--hooks-for", str(project), "--hooks-only", "--dry-run"])
    assert r.returncode == 0, r.stderr
    assert not (project / ".codex" / "hooks.json").exists()
    assert "DRY" in r.stdout


def test_upgrade_dry_run_with_stale_codex_hooks_shows_diff_but_never_writes(tmp_path):
    project = tmp_path / "proj"
    (project / ".codex").mkdir(parents=True)
    stale = json.dumps({"hooks": {"PreToolUse": []}}, indent=2)
    (project / ".codex" / "hooks.json").write_text(stale)

    r = run_upgrade(["--hooks-for", str(project), "--hooks-only", "--dry-run"])
    assert r.returncode == 0, r.stderr
    assert "stale" in r.stdout.lower()
    assert (project / ".codex" / "hooks.json").read_text() == stale


def test_upgrade_hooks_for_also_merges_claude_settings_local(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    r = run_upgrade(["--hooks-for", str(project), "--hooks-only"])
    assert r.returncode == 0, r.stderr
    dst = project / ".claude" / "settings.local.json"
    assert dst.exists()
    data = json.loads(dst.read_text())
    assert "hooks" in data
    assert "PreToolUse" in data["hooks"] or "Stop" in data["hooks"]


def test_upgrade_claude_merge_preserves_hand_written_entries(tmp_path):
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    dst = project / ".claude" / "settings.local.json"
    preexisting = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-hook.py"}]}]}}
    dst.write_text(json.dumps(preexisting, indent=2))

    r = run_upgrade(["--hooks-for", str(project), "--hooks-only"])
    assert r.returncode == 0, r.stderr
    data = json.loads(dst.read_text())
    commands = [h["command"] for entry in data["hooks"]["Stop"] for h in entry["hooks"]]
    assert any("my-hook.py" in c for c in commands)


def test_upgrade_never_writes_outside_hooks_for_path(tmp_path):
    """Belt-and-suspenders: point HOME at a decoy directory and confirm
    nothing lands there even under --force-hooks."""
    decoy_home = tmp_path / "decoy-home"
    decoy_home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    r = run_upgrade(
        ["--hooks-for", str(project), "--hooks-only", "--force-hooks"],
        extra_env={"HOME": str(decoy_home)},
    )
    assert r.returncode == 0, r.stderr
    assert not (decoy_home / ".codex").exists()
    assert not (decoy_home / ".claude").exists()


def test_upgrade_does_not_refresh_hooks_before_db_preflight(tmp_path):
    """Full upgrade must not write hook settings when DB preflight fails."""
    project = tmp_path / "proj"
    project.mkdir()
    r = run_upgrade(
        ["--hooks-for", str(project)],
        extra_env={"SILL_DB_CONTAINER": "sill_absent_container_for_tests"},
        timeout=30,
    )
    assert not (project / ".codex/hooks.json").exists()
    assert not (project / ".claude/settings.local.json").exists()
    assert r.returncode != 0
    assert "sill_absent_container_for_tests" in (r.stdout + r.stderr)
