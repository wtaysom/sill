"""install.sh step 7's merge into ~/.codex/config.toml, pinned.

The defect these tests exist for: step 7 extended an existing ``[features]``
section by *appending a second one*.

    [features]
    js_repl = false
    ...
    [features]        # <- appended by install.sh
    hooks = true

TOML forbids declaring a table twice, so the result is a file Codex cannot
parse — and an unparseable config.toml does not degrade Codex, it stops it,
taking every unrelated MCP server, plugin, and trusted-project entry in that
file down with it. The installer printed ``updated <path>`` either way.

Any config.toml carrying a ``[features]`` section without a ``hooks`` key hit
this, which is every Codex install that has ever set a feature flag. The
version that shipped it never saw the bug because the machine it was written
on had no ``[features]`` section at all — the merge took the ``else`` branch,
appended the only ``[features]`` in the file, and parsed fine.

Two properties are pinned here, and the second matters as much as the first:
the merge must extend the section in place, and it must never write a file
that does not parse, even if a future edit to the section-matching regexes
gets a layout wrong. `test_a_header_the_regexes_misread_is_refused_not_written`
covers the backstop by feeding it exactly such a layout.

Needs no docker, database, or network.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # 3.10, this project's CI floor
    tomllib = None

# A bare `import tomllib` at module scope broke COLLECTION on 3.10, which no
# per-test skipif can rescue — the module fails before any marker is read, so
# CI's 3.10 leg errored out on this whole file. Skipping is the right answer
# rather than a 3.10 TOML shim: install.sh's own tomllib import is guarded the
# same way and is likewise inactive below 3.11, so the parse-based assertions
# here AND the two refuse-rather-than-write backstops describe behavior this
# interpreter does not have. The in-place-extension fix itself does run on
# 3.10 — that is the part that matters there — and CI's 3.12 leg pins all of
# it.

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"


def wire_codex(home: Path, config_text: str | None):
    """Run step 7 against a throwaway HOME and return (result, config text).

    Sources install.sh rather than executing it — the main pipeline is guarded
    by a ``BASH_SOURCE[0] == $0`` check — so only step 7 runs. HOME points at a
    tmp_path, so neither the real ~/.codex nor the real ~/.claude.json is
    reachable even if the step misbehaves.
    """
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    cfg = codex_dir / "config.toml"
    if config_text is not None:
        cfg.write_text(config_text)

    result = subprocess.run(
        ["bash", "-c", f"source {INSTALL_SH!s}; DRY_RUN=0; step_mcp_wiring"],
        capture_output=True, text=True, timeout=60,
        env={"HOME": str(home), "PATH": _path_with_fake_codex(home)},
    )
    return result, (cfg.read_text() if cfg.exists() else None)


def _path_with_fake_codex(home: Path) -> str:
    """A PATH carrying a stub `codex` so the step takes its Codex branch on a
    machine that has no Codex installed (CI, most contributors' laptops), and
    no `claude`, so the Claude side takes its self-contained fallback."""
    stub_dir = home / "stubbin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "codex"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    real = [d for d in ("/usr/bin", "/bin", "/usr/sbin", "/sbin") if Path(d).exists()]
    return ":".join([str(stub_dir), str(Path(sys.executable).parent), *real])


EXISTING_FEATURES = """\
model = "gpt-5.6-sol"

[features]
js_repl = false

[mcp_servers.node_repl]
command = "/opt/node_repl"
"""


@pytest.mark.skipif(tomllib is None, reason="legacy installer uses tomllib")
def test_an_existing_features_section_is_extended_not_redeclared(tmp_path):
    result, text = wire_codex(tmp_path, EXISTING_FEATURES)
    assert result.returncode == 0, result.stderr
    assert text.count("[features]") == 1, f"table declared twice:\n{text}"
    data = tomllib.loads(text)
    assert data["features"] == {"js_repl": False, "hooks": True}


@pytest.mark.skipif(tomllib is None, reason="legacy installer uses tomllib")
def test_the_merged_config_still_parses_as_toml(tmp_path):
    _, text = wire_codex(tmp_path, EXISTING_FEATURES)
    tomllib.loads(text)  # the whole point: this raised TOMLDecodeError before


@pytest.mark.skipif(tomllib is None, reason="legacy installer uses tomllib")
def test_unrelated_entries_survive_the_merge(tmp_path):
    _, text = wire_codex(tmp_path, EXISTING_FEATURES)
    data = tomllib.loads(text)
    assert data["model"] == "gpt-5.6-sol"
    assert data["mcp_servers"]["node_repl"]["command"] == "/opt/node_repl"
    assert data["mcp_servers"]["sill"]["command"] == "sill-mcp"


@pytest.mark.skipif(tomllib is None, reason="legacy installer uses tomllib")
def test_a_config_with_no_features_section_still_gets_one(tmp_path):
    _, text = wire_codex(tmp_path, 'model = "gpt-5.6-sol"\n')
    data = tomllib.loads(text)
    assert data["features"]["hooks"] is True
    assert data["mcp_servers"]["sill"]["command"] == "sill-mcp"


@pytest.mark.skipif(tomllib is None, reason="legacy installer uses tomllib")
def test_a_missing_config_is_created(tmp_path):
    _, text = wire_codex(tmp_path, None)
    data = tomllib.loads(text)
    assert data["features"]["hooks"] is True
    assert data["mcp_servers"]["sill"]["command"] == "sill-mcp"


@pytest.mark.skipif(tomllib is None, reason="legacy installer uses tomllib")
def test_the_merge_is_idempotent(tmp_path):
    _, once = wire_codex(tmp_path, EXISTING_FEATURES)
    result, twice = wire_codex(tmp_path, None)  # None = leave the merged file alone
    assert once == twice, "a second run changed the file"
    assert "already has sill MCP" in result.stdout


@pytest.mark.skipif(tomllib is None, reason="legacy installer uses tomllib")
def test_an_unparseable_config_is_left_alone(tmp_path):
    """Someone's hand-broken config is theirs; layering a merge on top only
    makes the breakage harder to attribute."""
    broken = "[features\nthis is not toml\n"
    result, text = wire_codex(tmp_path, broken)
    assert text == broken, "install.sh edited a file it could not parse"
    assert "isn't valid TOML" in result.stdout


@pytest.mark.skipif(tomllib is None, reason="legacy installer uses tomllib")
def test_a_header_the_regexes_misread_is_refused_not_written(tmp_path):
    """A trailing comment on the header defeats both section regexes, so the
    merge would append a second [features] and produce invalid TOML. The
    tomllib backstop must catch that and write nothing — while saying so, since
    a step that reports success having wired nothing is the failure mode this
    release exists to kill."""
    tricky = "[features]  # inline comment\njs_repl = false\n"
    result, text = wire_codex(tmp_path, tricky)
    assert text == tricky, "wrote a config that would not parse"
    assert "NOT writing" in result.stdout
    assert "Codex is NOT wired" in result.stdout


@pytest.mark.skipif(tomllib is None, reason="legacy installer uses tomllib")
def test_every_shipped_branch_produces_parseable_toml(tmp_path):
    """Sweep the input shapes step 7 can meet, and assert the invariant that
    actually matters across all of them: whatever it writes, Codex can read."""
    cases = {
        "empty": "",
        "no-features": 'model = "x"\n',
        "features-without-hooks": EXISTING_FEATURES,
        "features-with-hooks": "[features]\nhooks = true\n",
        "sill-already-present": '[mcp_servers.sill]\ncommand = "sill-mcp"\n',
        "no-trailing-newline": '[features]\njs_repl = false',
    }
    for name, body in cases.items():
        home = tmp_path / name
        home.mkdir()
        _, text = wire_codex(home, body)
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:  # pragma: no cover - failure path
            pytest.fail(f"case {name!r} produced invalid TOML: {exc}\n{text}")


# Exercise the shared registration entry point on both supported interpreters.
# The legacy installer tests above stay until install.sh delegates step 7.


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
