"""Exercise the installed registration command against disposable client homes."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def registration(tmp_path):
    home = tmp_path / "gui home"
    home.mkdir()
    instance = tmp_path / "installed instance.env"
    instance.write_text('POSTGRES_PASSWORD="test secret"\n', encoding="utf-8")
    command = tmp_path / "bin with spaces" / "sill-mcp"
    command.parent.mkdir()
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o755)
    paths = {
        "claude-code": home / ".claude.json",
        "claude-desktop": (
            home / "Library/Application Support/Claude/claude_desktop_config.json"
        ),
        "codex": home / ".codex/config.toml",
    }

    def connect(client, *extra, env_file=instance, mcp_command=command, **env):
        argv = [
            sys.executable, "-m", "sill_cli", "connect", client,
            "--env-file", str(env_file),
        ]
        if mcp_command is not None:
            argv.extend(["--mcp-command", str(mcp_command)])
        # No Codex, Claude, docker, or other commands can be resolved on PATH.
        process_env = {
            "HOME": str(home),
            "PATH": str(home / "empty-bin"),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            **env,
        }
        return subprocess.run(
            [*argv, *extra], cwd=home, env=process_env,
            capture_output=True, text=True, timeout=20,
        )

    return connect, paths, instance, command


def parsed_config(client, path):
    if client == "codex":
        import tomlkit

        return tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
    return json.loads(path.read_text(encoding="utf-8"))


def servers_key(client):
    return "mcp_servers" if client == "codex" else "mcpServers"


@pytest.mark.parametrize("client", ["claude-code", "codex", "claude-desktop"])
def test_clean_home_uses_absolute_argv_with_no_client_cli(registration, client):
    connect, paths, instance, command = registration
    result = connect(client)
    assert result.returncode == 0, result.stderr
    assert "registered" in result.stdout
    config = paths[client]
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    expected = {"command": str(command), "args": ["--env-file", str(instance)]}
    if client == "claude-code":
        expected["type"] = "stdio"
    assert parsed_config(client, config) == {servers_key(client): {"sill": expected}}
    assert "test secret" not in config.read_text(encoding="utf-8")
    assert list(config.parent.glob(config.name + ".bak-*")) == []


def test_installed_entry_point_finds_its_sibling_with_empty_path(registration):
    _, paths, instance, _ = registration
    console = Path(sys.executable).with_name("sill")
    if not console.is_file():
        pytest.skip("sill console script is unavailable in the test environment")
    result = subprocess.run(
        [str(console), "connect", "claude-code", "--env-file", str(instance)],
        cwd=instance.parent, env={"HOME": str(paths["claude-code"].parent), "PATH": ""},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    config = parsed_config("claude-code", paths["claude-code"])
    assert config["mcpServers"]["sill"]["command"] == str(console.with_name("sill-mcp"))


def test_codex_home_override_and_dry_run_leave_home_empty(registration, tmp_path):
    connect, paths, _, _ = registration
    override = tmp_path / "custom Codex home"
    result = connect("codex", "--dry-run", CODEX_HOME=str(override))
    assert result.returncode == 0, result.stderr
    assert "would register" in result.stdout
    assert str(override / "config.toml") in result.stdout
    assert not override.exists()
    assert not paths["codex"].parent.exists()
    result = connect("codex", CODEX_HOME=str(override))
    assert result.returncode == 0, result.stderr
    assert "sill" in parsed_config("codex", override / "config.toml")["mcp_servers"]
    assert not paths["codex"].parent.exists()


@pytest.mark.parametrize("client", ["claude-code", "claude-desktop"])
def test_json_preserves_unrelated_servers_and_uses_unique_private_backups(
    registration, client, tmp_path,
):
    connect, paths, instance, command = registration
    config = paths[client]
    config.parent.mkdir(parents=True, exist_ok=True)
    old_bytes = b'{"mcpServers":{"other":{"command":"/bin/other"}},"enabled":false}\r\n'
    config.write_bytes(old_bytes)
    config.chmod(0o640)
    prior_backup = config.with_name(config.name + ".bak-earlier")
    prior_backup.write_bytes(b"older backup")

    result = connect(client)
    assert result.returncode == 0, result.stderr
    data = parsed_config(client, config)
    assert data["enabled"] is False
    assert data["mcpServers"]["other"] == {"command": "/bin/other"}
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    backups = set(config.parent.glob(config.name + ".bak-*"))
    assert len(backups) == 2
    backup, = backups - {prior_backup}
    assert backup.read_bytes() == old_bytes
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert prior_backup.read_bytes() == b"older backup"

    current = config.read_bytes()
    result = connect(client)
    assert result.returncode == 0, result.stderr
    assert "already registered" in result.stdout
    assert config.read_bytes() == current
    assert set(config.parent.glob(config.name + ".bak-*")) == backups

    new_command = tmp_path / "updated sill-mcp"
    new_command.write_bytes(command.read_bytes())
    new_command.chmod(0o755)
    alias = tmp_path / "same instance.env"
    alias.symlink_to(instance)
    result = connect(client, env_file=alias, mcp_command=new_command)
    assert result.returncode == 0, result.stderr
    assert parsed_config(client, config)["mcpServers"]["sill"]["command"] == str(new_command)
    next_backup, = set(config.parent.glob(config.name + ".bak-*")) - backups
    assert next_backup.read_bytes() == current


@pytest.mark.parametrize(("client", "body"), [
    ("claude-code", '{"mcpServers":{},"mcpServers":{}}'),
    ("claude-desktop", '{"mcpServers":'),
    ("claude-code", '[]'),
    ("claude-desktop", '{"mcpServers":[]}'),
    ("claude-code", '{"mcpServers":{"sill":null}}'),
    ("claude-code", '{"mcpServers":{"other":{"command":"other","bad":NaN}}}'),
    ("codex", '[mcp_servers\ncommand="sill-mcp"'),
    ("codex", '[mcp_servers.sill]\ncommand="sill-mcp"\ncommand="other"'),
    ("codex", 'mcp_servers=[]'),
    ("codex", 'mcp_servers.sill="other"'),
])
def test_invalid_registry_is_left_byte_identical(registration, client, body):
    connect, paths, _, _ = registration
    config = paths[client]
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(body, encoding="utf-8")
    before = config.read_bytes()
    result = connect(client, "--adopt-legacy")
    assert result.returncode != 0
    assert str(config) in result.stderr
    assert config.read_bytes() == before
    assert list(config.parent.glob(config.name + ".bak-*")) == []


@pytest.mark.parametrize(("client", "body"), [
    ("claude-code", '{"mcpServers":{"sill":{"type":"stdio","command":"sill-mcp","args":[],"env":{}}}}'),
    ("claude-desktop", '{"mcpServers":{"sill":{"command":"sill-mcp"}}}'),
    ("codex", '[mcp_servers.sill]\ncommand = "sill-mcp"\n'),
])
def test_bare_legacy_entry_requires_explicit_adoption(registration, client, body):
    connect, paths, instance, command = registration
    config = paths[client]
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(body, encoding="utf-8")
    result = connect(client)
    assert result.returncode != 0
    assert "ambiguous" in result.stderr
    assert "--adopt-legacy" in result.stderr
    assert config.read_text(encoding="utf-8") == body
    result = connect(client, "--adopt-legacy")
    assert result.returncode == 0, result.stderr
    expected = {"command": str(command), "args": ["--env-file", str(instance)]}
    if client == "claude-code":
        expected["type"] = "stdio"
    assert parsed_config(client, config)[servers_key(client)]["sill"] == expected
    backup, = config.parent.glob(config.name + ".bak-*")
    assert backup.read_bytes() == body.encode()


@pytest.mark.parametrize("extra", [
    {"env": {"SETTING": "private value"}},
    {"args": ["--other"]},
    {"type": "sse"},
    {"url": "https://other.invalid"},
])
def test_adoption_refuses_custom_legacy_entries(registration, extra):
    connect, paths, _, _ = registration
    config = paths["claude-code"]
    entry = {"command": "sill-mcp", **extra}
    before = json.dumps({"mcpServers": {"sill": entry}})
    config.write_text(before, encoding="utf-8")
    result = connect("claude-code", "--adopt-legacy")
    assert result.returncode != 0
    assert "conflict" in result.stderr.lower()
    assert "private value" not in result.stderr
    assert config.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_adoption_refuses_another_instances_entry(registration, client, tmp_path):
    connect, paths, _, _ = registration
    result = connect(client)
    assert result.returncode == 0, result.stderr
    config = paths[client]
    before = config.read_bytes()
    other_file = tmp_path / "another instance.env"
    other_file.touch()
    result = connect(client, "--adopt-legacy", env_file=other_file)
    assert result.returncode != 0
    assert str(config) in result.stderr
    assert "conflict" in result.stderr.lower()
    assert config.read_bytes() == before


@pytest.mark.parametrize("invalid", ["relative-env", "missing-env", "malformed-env", "empty-user",
                                    "relative-command", "missing-command", "nonexecutable-command"])
def test_preflight_checks_inputs_before_writing_even_in_dry_run(registration, invalid):
    connect, paths, instance, command = registration
    kwargs = {}
    if invalid == "relative-env":
        kwargs["env_file"] = instance.name
    elif invalid == "missing-env":
        instance.unlink()
    elif invalid == "malformed-env":
        instance.write_text('POSTGRES_PASSWORD="test secret\n', encoding="utf-8")
    elif invalid == "empty-user":
        instance.write_text("POSTGRES_USER=\n", encoding="utf-8")
    elif invalid == "relative-command":
        kwargs["mcp_command"] = command.name
    elif invalid == "missing-command":
        command.unlink()
    else:
        command.chmod(0o600)

    result = connect("claude-desktop", "--dry-run", **kwargs)
    assert result.returncode != 0
    config = paths["claude-desktop"]
    assert str(config) in result.stderr
    assert "test secret" not in result.stderr
    assert not config.parent.exists()


def test_registry_symlink_is_refused_without_touching_its_target(registration, tmp_path):
    connect, paths, _, _ = registration
    config = paths["claude-code"]
    target = tmp_path / "external.json"
    target.write_bytes(b'{"setting":true}\n')
    config.symlink_to(target)
    result = connect("claude-code")
    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
    assert config.is_symlink()
    assert target.read_bytes() == b'{"setting":true}\n'


def test_registry_parent_symlink_is_refused(registration, tmp_path):
    connect, paths, _, _ = registration
    target = tmp_path / "another Codex directory"
    target.mkdir()
    paths["codex"].parent.symlink_to(target, target_is_directory=True)
    result = connect("codex")
    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("failure", ["replace", "concurrent-edit"])
def test_atomic_update_keeps_original_or_concurrent_writer(
    registration, failure, monkeypatch,
):
    # Calling register_client directly lets us simulate a filesystem failure
    # at the commit boundary without changing any ordinary file operations.
    from client_config import ClientConfigError, register_client

    _, paths, instance, command = registration
    config = paths["claude-code"]
    monkeypatch.setenv("HOME", str(config.parent))
    original = b'{"mcpServers":{"other":{"command":"other"}}}'
    config.write_bytes(original)
    current = original
    if failure == "replace":
        def fail_replace(src, dst):
            assert Path(dst) == config
            assert Path(src).is_file()
            raise PermissionError("simulated replace failure")

        monkeypatch.setattr(os, "replace", fail_replace)
    else:
        import client_config

        stage = client_config._stage_bytes
        current = b'{"operatorEdit":true}'

        def stage_then_edit(*args, **kwargs):
            staged = stage(*args, **kwargs)
            config.write_bytes(current)
            return staged

        monkeypatch.setattr(client_config, "_stage_bytes", stage_then_edit)
    with pytest.raises(ClientConfigError, match="config|changed"):
        register_client("claude-code", instance, command)
    assert config.read_bytes() == current
    assert not list(config.parent.glob("." + config.name + ".tmp-*"))


def test_home_can_be_below_a_platform_directory_symlink(registration, tmp_path):
    connect, paths, _, _ = registration
    alias = tmp_path / "home parent alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    home = paths["claude-code"].parent
    result = connect("claude-code", HOME=str(alias / home.name))
    assert result.returncode == 0, result.stderr
    assert "sill" in parsed_config("claude-code", paths["claude-code"])["mcpServers"]
