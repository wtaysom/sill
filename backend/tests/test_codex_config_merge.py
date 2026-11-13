"""The shared registration path preserves valid Codex TOML layouts."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(("before", "features"), [
    ('[features] # keep the header\nhooks = false # keep the choice\n', {"hooks": False}),
    ('features.codex_hooks = false # keep the choice\n', {"codex_hooks": False}),
    ('features = {codex_hooks=false, js_repl=true} # keep this\n',
     {"codex_hooks": False, "js_repl": True}),
    ('[features] # keep header\njs_repl = false', {"js_repl": False}),
])
def test_connect_preserves_codex_features_and_comments(tmp_path, before, features):
    import tomlkit

    result, config, _, _ = connect_codex(tmp_path, before)
    assert result.returncode == 0, result.stderr
    after = config.read_text()
    assert before.strip() in after
    assert tomlkit.parse(after).unwrap()["features"] == features


@pytest.mark.parametrize("before", [
    ('mcp_servers.one.command = "first" # first server\n'
     'mcp_servers.two.command = "second" # second server\n'
     'model = "fixture" # keep model\n'),
    ('mcp_servers = {one = {command = "first"}, two = {command = "second"}} # all servers\n'
     'model = "fixture" # keep model\n'),
    ('model = "fixture" # keep model\n'
     '["mcp_servers"."one"] # first server\ncommand = "first"\n'
     '["mcp_servers"."two"] # second server\ncommand = "second"\n'),
])
def test_connect_preserves_valid_codex_server_layouts(tmp_path, before):
    import tomlkit

    result, config, command, instance = connect_codex(tmp_path, before)
    assert result.returncode == 0, result.stderr
    after = config.read_text()
    data = tomlkit.parse(after).unwrap()
    assert data["model"] == "fixture"
    assert data["mcp_servers"] == {
        "one": {"command": "first"},
        "two": {"command": "second"},
        "sill": {"command": str(command), "args": ["--env-file", str(instance)]},
    }
    for line in before.splitlines():
        if "#" in line:
            assert line[line.index("#"):] in after


@pytest.mark.parametrize("before", [
    '["mcp_servers"."sill"] # existing registration\ncommand = "sill-mcp" # old launcher\n',
    'mcp_servers.sill.command = "sill-mcp" # old launcher\n',
    'mcp_servers = {sill = {command="sill-mcp"}} # existing registration\n',
])
def test_connect_adopts_one_legacy_codex_entry_in_place(tmp_path, before):
    import tomlkit

    result, config, command, instance = connect_codex(tmp_path, before, "--adopt-legacy")
    assert result.returncode == 0, result.stderr
    after = config.read_text()
    assert tomlkit.parse(after).unwrap() == {
        "mcp_servers": {
            "sill": {"command": str(command), "args": ["--env-file", str(instance)]}
        }
    }
    for line in before.splitlines():
        assert line[line.index("#"):] in after


def connect_codex(tmp_path, before, *args):
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(before, encoding="utf-8")
    command = tmp_path / "sill-mcp"
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    command.chmod(0o700)
    instance = tmp_path / "instance.env"
    instance.touch()
    result = subprocess.run(
        [sys.executable, "-m", "sill_cli", "connect", "codex", "--env-file", str(instance),
         "--mcp-command", str(command), *args],
        capture_output=True, text=True, timeout=20,
        cwd=REPO_ROOT / "backend",
        env={"HOME": str(home), "PATH": "", "LC_ALL": os.environ.get("LC_ALL", "C")},
    )
    return result, config, command, instance
