"""Run the shell entry points with isolated HOME and a Docker boundary double.

Replace Docker, package installation and the PostgreSQL driver at their
external boundaries. Exercise real registration, dotenv parsing, the MCP
handshake protocol, and installed-script argv.
"""
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def executable(path, body):
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def runtime(tmp_path):
    checkout = tmp_path / "sill checkout"
    home = tmp_path / "new home"
    home.mkdir()
    bin_dir = tmp_path / "test-bin"
    bin_dir.mkdir()
    for path in ("backend", "scripts", "plugin", "seed"):
        source = ROOT / path
        if source.exists():
            shutil.copytree(source, checkout / path, ignore=shutil.ignore_patterns(
                ".env", "__pycache__", "*.egg-info", ".pytest_cache",
            ))
    for path in ("install.sh", "verify.sh", "reset.sh", "uninstall.sh"):
        shutil.copy2(ROOT / path, checkout / path)
    # Use the current Python for package code, but stand in for downloading
    # dependencies. The pipx binary dir is deliberately absent from PATH.
    executable(bin_dir / "python3", f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
    executable(bin_dir / "pipx", f'''#!/bin/sh
case "$*" in
  'environment --value PIPX_BIN_DIR') printf '%s\\n' "$HOME/xdg bin" ;;
  'environment --value PIPX_LOCAL_VENVS') printf '%s\\n' "$HOME/xdg data/venvs" ;;
  'install --force -e '*)
    [ "$4" = "$TEST_CHECKOUT/backend" ] || exit 51
    [ "${{TEST_FAILURE:-}}" != backend ] || exit 52
    venv="$HOME/xdg data/venvs/sill-memory/bin"
    mkdir -p "$venv" "$HOME/xdg bin"
    cp "$TEST_PYTHON_WRAPPER" "$venv/python"
    for name in sill sill-mcp sill-worker; do
      ln -sf "$TEST_FIXTURE/$name" "$venv/$name"
      ln -sf "$venv/$name" "$HOME/xdg bin/$name"
    done
    ;;
  *) exit 53 ;;
esac
''')
    # No PostgreSQL is reached by this unit fixture. It compares the same
    # query's container result above and a host result from the driver edge.
    driver = checkout / "backend/asyncpg.py"
    driver.write_text('''import os
class Connection:
    async def close(self):
        pass
    async def fetchrow(self, query):
        assert "pg_control_system" in query and "current_database" in query
        return [os.getenv("TEST_HOST_SYSTEM_ID", "7531438976150207200"), "sill"]
async def connect(dsn, **kwargs):
    assert dsn.startswith("postgresql://")
    return Connection()
''')
    executable(tmp_path / "sill", f'''#!/bin/sh
if [ "$1" = seed ]; then
  [ "$*" = "seed import $TEST_CHECKOUT/seed/methodology.jsonl --env-file $TEST_CHECKOUT/backend/.env" ] || exit 57
  [ "${{TEST_FAILURE:-}}" != seed ] || exit 58
  exit 0
fi
export PYTHONPATH="$TEST_CHECKOUT/backend"
exec {shlex.quote(sys.executable)} -c 'import sill_cli; raise SystemExit(sill_cli.main())' "$@"
''')
    executable(tmp_path / "sill-mcp", f'''#!{sys.executable}
import json, os, pathlib, sys
assert sys.argv[1] == "--env-file"
assert len(sys.argv) == 3
assert pathlib.Path(sys.argv[2]).is_absolute()
assert pathlib.Path(sys.argv[2]).exists()
pathlib.Path(os.environ["TEST_FIXTURE"], "mcp-env").write_bytes(pathlib.Path(sys.argv[2]).read_bytes())
if os.getenv("TEST_FAILURE") == "mcp":
    raise SystemExit(60)
r = json.loads(sys.stdin.readline())
print(json.dumps({{"jsonrpc": "2.0", "id": r["id"], "result": {{"serverInfo":
    {{"name": "sill", "version": "0.2.1"}}, "protocolVersion": "2024-11-05"}}}}), flush=True)
''')
    executable(tmp_path / "sill-worker", "#!/bin/sh\nexit 0\n")
    bash_env = tmp_path / "bash-env"
    bash_env.write_text('''sleep() {
  SECONDS=$(( SECONDS + ${TEST_TICK:-5} ))
  printf '%s' "$SECONDS" > "$TEST_FIXTURE/clock"
}
''')
    docker = bin_dir / "docker"
    executable(docker, f"#!{sys.executable}\n" + DOCKER)
    env = {
        "HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TEST_FIXTURE": str(tmp_path), "TEST_CHECKOUT": str(checkout),
        "BASH_ENV": str(bash_env), "TEST_PYTHON_WRAPPER": str(bin_dir / "python3"),
    }
    return checkout, home, env


DOCKER = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ["TEST_FIXTURE"])
checkout = pathlib.Path(os.environ["TEST_CHECKOUT"])
with (root / "docker-calls").open("a") as out:
    out.write(json.dumps(args) + "\n")
if args[0] == "info":
    print(os.getenv("TEST_ENGINE", "linux aarch64"))
    sys.exit()
if args[0] == "inspect":
    if ".NetworkSettings.Ports" in args[2]:
        value = os.getenv("TEST_ACTUAL_PORT")
        if not value:
            dot = checkout / "backend/.env"
            saved = dict(line.split("=", 1) for line in dot.read_text().splitlines()
                         if line and not line.startswith("#")) if dot.exists() else {}
            value = os.getenv("POSTGRES_PORT", saved.get("POSTGRES_PORT", "5432"))
        print(json.dumps([{"HostIp": os.getenv("SILL_BIND_ADDR", "127.0.0.1"), "HostPort": value}]))
        sys.exit()
    service = args[-1]
    now = int((root / "clock").read_text()) if (root / "clock").exists() else 0
    state = "running"
    health = "healthy"
    if service == "embeddings":
        if os.getenv("TEST_FAILURE") == "terminal":
            health = "unhealthy"
        if os.getenv("TEST_FAILURE") == "timeout" or (os.getenv("TEST_COLD") and now < 903):
            health = "starting"
    if service == "db" and os.getenv("TEST_COLD") and now < 903:
        health = "starting"
    if service == "maintenance_worker":
        health = ""
    print(f"{state} {health}".strip())
    sys.exit()
assert args[0] == "compose", args
assert args[args.index("--project-directory") + 1] == str(checkout / "backend"), args
assert args[args.index("-f") + 1] == str(checkout / "backend/docker-compose.yml"), args
assert args[args.index("--env-file") + 1] in (str(checkout / "backend/.env"), "/dev/null"), args
command = args[7:]
if command[0] == "config":
    # Resolve literal fixture input with Compose's environment precedence.
    dot = checkout / "backend/.env"
    saved = dict(line.split("=", 1) for line in dot.read_text().splitlines()
                 if line and not line.startswith("#")) if dot.exists() else {}
    settings = saved | dict(os.environ)
    if "--environment" in command:
        for key, value in settings.items():
            print(f"{key}={value}")
        sys.exit()
    if "--format" not in command:
        raise AssertionError(command)
    db = {"container_name": settings.get("SILL_DB_CONTAINER", "sill_db"), "environment": {
        key: settings.get(key, value) for key, value in (
            ("POSTGRES_DB", "sill"), ("POSTGRES_USER", "sill"), ("POSTGRES_PASSWORD", "sill_password"),
        )}, "ports": [{"host_ip": settings.get("SILL_BIND_ADDR", "127.0.0.1"),
                      "published": settings.get("POSTGRES_PORT", "5432"), "target": 5432}]}
    print(json.dumps({"name": settings.get("COMPOSE_PROJECT_NAME", "backend"), "services": {"db": db}}))
    sys.exit()
if command[0] == "ps":
    if "-q" in command:
        print(command[-1])
    else:
        print("fixture ps diagnostic")
    sys.exit()
if command[0] == "exec":
    assert "db" in command and any("psql" in arg for arg in command), command
    if "pg_control_system" in command[-1]:
        print('["7531438976150207200", "sill"]')
    elif "pg_catalog.pg_class" in command[-1]:
        print(2 if os.getenv("TEST_FAILURE") == "duplicate-ledger" else 1)
    elif "schema_migrations" in command[-1]:
        print("001\n002\n003\n004\n005\n006" + ("\n007" if os.getenv("TEST_FAILURE") != "ledger" else ""))
    elif "count(*) FROM memories" in command[-1]:
        print(22)
    else:
        print("t")
    sys.exit()
if command[0] == "up":
    dot = checkout / "backend/.env"
    saved = dict(line.split("=", 1) for line in dot.read_text().splitlines()
                 if line and not line.startswith("#")) if dot.exists() else {}
    settings = saved | dict(os.environ)
    names = {"EMBEDDING_IMAGE", "EMBEDDING_PLATFORM", "EMBEDDING_REVISION"}
    chosen = {key: value for key, value in settings.items()
              if key.startswith("SILL_DEFAULT_EMBEDDING_")}
    for key in names:
        chosen[key] = settings.get(key, chosen.get("SILL_DEFAULT_" + key))
    (root / "embedding-choice").write_text(json.dumps(chosen))
    sys.exit()
assert command[0] in ("build", "logs", "down"), command
'''


def run(runtime, *args, **overrides):
    checkout, home, env = runtime
    return subprocess.run(
        ["/bin/bash", str(checkout / args[0]), *args[1:]], cwd=home,
        env=env | overrides, text=True, capture_output=True, timeout=30,
    )


def details(result):
    return result.stdout + result.stderr


def test_install_without_agent_clis_and_repeat(runtime):
    checkout, home, env = runtime
    assert not (checkout / "backend/.env").exists()
    first = run(runtime, "install.sh", "--no-seed")
    assert first.returncode == 0, details(first)
    assert "Sill is installed" in first.stdout
    instance = checkout / "backend/.env"
    assert instance.read_bytes() == b""
    assert instance.stat().st_mode & 0o777 == 0o600
    assert not (home / ".claude.json").exists()
    assert not (home / ".codex").exists()
    instance.write_text("# keep this comment\nCOMPOSE_PROJECT_NAME=fixture_two\nPOSTGRES_PORT=15436\n")
    second = run(runtime, "install.sh", "--no-seed")
    assert second.returncode == 0, details(second)
    assert instance.read_bytes() == (Path(env["TEST_FIXTURE"]) / "mcp-env").read_bytes()
    assert instance.read_text().startswith("# keep this comment\n")


def test_selected_desktop_registers_absolute_pipx_argv_and_no_other_client(runtime):
    checkout, home, _ = runtime
    cfg = home / "Library/Application Support/Claude/claude_desktop_config.json"
    for _ in range(2):
        result = run(runtime, "install.sh", "--no-seed", "--client", "claude-desktop")
        assert result.returncode == 0, details(result)
        entry = json.loads(cfg.read_text())["mcpServers"]["sill"]
        assert Path(entry["command"]).is_absolute()
        assert Path(entry["command"]).samefile(home / "xdg bin/sill-mcp")
        assert entry["args"] == ["--env-file", str(checkout / "backend/.env")]
    assert not (home / ".claude.json").exists()
    assert not (home / ".codex").exists()


@pytest.mark.parametrize("failure", ["backend", "seed", "mcp", "terminal"])
def test_failure_cannot_print_success_banner(runtime, failure):
    result = run(runtime, "install.sh", TEST_FAILURE=failure)
    assert result.returncode != 0, details(result)
    assert "Sill is installed" not in result.stdout
    if failure == "terminal":
        assert "embeddings" in details(result) and "unhealthy" in details(result)
    elif failure == "mcp":
        assert "without answering" in result.stderr
    elif failure == "seed":
        assert result.returncode == 58
    else:
        assert "Step 3/10" not in result.stdout


def test_dry_run_is_read_only_even_with_desktop_selected(runtime):
    checkout, home, env = runtime
    result = run(runtime, "install.sh", "--dry-run", "--client", "claude-desktop")
    assert result.returncode == 0, details(result)
    assert "claude-desktop" in result.stdout
    assert "Sill is installed" not in result.stdout
    assert not (checkout / "backend/.env").exists()
    assert list(home.iterdir()) == []
    assert not (Path(env["TEST_FIXTURE"]) / "docker-calls").exists()


def test_persisted_registration_rejects_shell_port_conflict_before_start(runtime):
    checkout, home, env = runtime
    (checkout / "backend/.env").write_text("POSTGRES_PORT=15436\n")
    result = run(runtime, "install.sh", "--no-seed", "--client", "claude-desktop",
                 POSTGRES_PORT="15536")
    assert result.returncode != 0, details(result)
    assert "POSTGRES_PORT" in result.stderr and "persist" in result.stderr.lower()
    assert "15536" not in result.stderr
    calls = [json.loads(line) for line in (Path(env["TEST_FIXTURE"]) / "docker-calls").read_text().splitlines()]
    assert not any("up" in call or "build" in call for call in calls)
    assert not (home / "Library").exists()


def test_shell_only_port_override_reaches_verifier(runtime):
    checkout, _, env = runtime
    result = run(runtime, "install.sh", "--no-seed", POSTGRES_PORT="15436")
    assert result.returncode == 0, details(result)
    assert b"15436" in (Path(env["TEST_FIXTURE"]) / "mcp-env").read_bytes()
    assert (checkout / "backend/.env").read_bytes() == b""


@pytest.mark.parametrize(("engine", "platform", "image_digest"), [
    ("linux aarch64", "linux/arm64", "798cd7615e060a72ccbdf19aa0ada52289c6a0147490195bafaece6ce04d70f1"),
    ("linux x86_64", "linux/amd64", "8de25e75ce39617f17f2f6c77d60a4f75b65e779ed5005420eb1400072a15c1c"),
])
def test_install_selects_pinned_image_from_engine_architecture(runtime, engine, platform, image_digest):
    _, _, env = runtime
    result = run(runtime, "install.sh", "--no-seed", TEST_ENGINE=engine)
    assert result.returncode == 0, details(result)
    assert engine in result.stdout
    chosen = json.loads((Path(env["TEST_FIXTURE"]) / "embedding-choice").read_text())
    assert chosen["SILL_DEFAULT_EMBEDDING_PLATFORM"] == platform
    assert chosen["SILL_DEFAULT_EMBEDDING_IMAGE"].endswith("@sha256:" + image_digest)
    assert chosen["SILL_DEFAULT_EMBEDDING_REVISION"] == "bfa3c846ac738e62aa61806ef9112d34acb1dc5a"


def test_wait_survives_reported_cold_start(runtime):
    result = run(runtime, "install.sh", "--no-seed", TEST_COLD="1", TEST_TICK="903")
    assert result.returncode == 0, details(result)
    assert any(int(elapsed) >= 903 for elapsed in
               re.findall(r"\[(\d+)s\] embeddings: healthy", result.stdout))
    assert "embeddings: starting" in result.stdout
    assert "db: starting" in result.stdout
    assert result.stdout.index("db: healthy") < result.stdout.index("maintenance_worker: running")


def test_wait_reports_named_service_on_timeout(runtime):
    result = run(runtime, "install.sh", "--no-seed", TEST_FAILURE="timeout",
                 TEST_TICK="11", SILL_INSTALL_WAIT_HEALTHY_S="10")
    assert result.returncode != 0, details(result)
    assert "embeddings" in result.stderr and "timed out" in result.stderr


def test_current_ledger_is_required_even_when_postgres_is_healthy(runtime):
    result = run(runtime, "install.sh", "--no-seed", TEST_FAILURE="ledger")
    assert result.returncode != 0, details(result)
    assert "schema_migrations" in result.stderr
    assert "Sill is installed" not in result.stdout


def test_verify_rejects_configured_port_that_differs_from_running_database(runtime):
    checkout, _, env = runtime
    result = run(runtime, "install.sh", "--no-seed", POSTGRES_PORT="15436")
    assert result.returncode == 0, details(result)
    (Path(env["TEST_FIXTURE"]) / "mcp-env").unlink()
    result = run(runtime, "verify.sh", "--no-seed", POSTGRES_PORT="25436", TEST_ACTUAL_PORT="15436")
    assert result.returncode != 0, details(result)
    assert "POSTGRES_PORT" in result.stderr
    assert not (Path(env["TEST_FIXTURE"]) / "mcp-env").exists()


@pytest.mark.parametrize(("key", "value"), [
    ("COMPOSE_PROJECT_NAME", "other_fixture"),
    ("SILL_DB_CONTAINER", "other_fixture_db"),
])
def test_client_install_rejects_unpersisted_database_identity(runtime, key, value):
    result = run(runtime, "install.sh", "--no-seed", "--client", "claude-desktop", **{key: value})
    assert result.returncode != 0, details(result)
    assert key in result.stderr and value not in result.stderr
    assert "Step 3/10" not in result.stdout


def test_explicit_embedding_pair_and_model_revision_override_auto_defaults(runtime):
    checkout, _, env = runtime
    (checkout / "backend/.env").write_text(
        "EMBEDDING_IMAGE=fixture/tei:custom\n"
        "EMBEDDING_PLATFORM=linux/amd64\n"
        "EMBEDDING_MODEL_ID=fixture/custom-model\n"
        "EMBEDDING_REVISION=custom-revision\n"
    )
    result = run(runtime, "install.sh", "--no-seed")
    assert result.returncode == 0, details(result)
    chosen = json.loads((Path(env["TEST_FIXTURE"]) / "embedding-choice").read_text())
    assert (chosen["EMBEDDING_IMAGE"], chosen["EMBEDDING_PLATFORM"], chosen["EMBEDDING_REVISION"]) == (
        "fixture/tei:custom", "linux/amd64", "custom-revision",
    )
    assert chosen["SILL_DEFAULT_EMBEDDING_REVISION"] == "main"


def test_explicit_clients_exclude_detected_clients_and_forward_legacy_adoption(runtime):
    checkout, home, env = runtime
    bin_dir = Path(env["PATH"].split(":")[0])
    for name in ("claude", "codex"):
        executable(bin_dir / name, "#!/bin/sh\nexit 91\n")
    claude = home / ".claude.json"
    claude.write_text(json.dumps({"mcpServers": {"sill": {"type": "stdio", "command": "sill-mcp", "args": [], "env": {}}}}))
    result = run(runtime, "install.sh", "--no-seed", "--client", "claude-code",
                 "--client", "claude-desktop", "--adopt-legacy")
    assert result.returncode == 0, details(result)
    entry = json.loads(claude.read_text())["mcpServers"]["sill"]
    assert entry["args"] == ["--env-file", str(checkout / "backend/.env")]
    assert (home / "Library/Application Support/Claude/claude_desktop_config.json").exists()
    assert not (home / ".codex").exists()


def test_seed_and_mcp_never_touch_a_different_store_at_the_expected_port(runtime):
    _, _, env = runtime
    result = run(runtime, "install.sh", TEST_HOST_SYSTEM_ID="7531438976150207210")
    assert result.returncode != 0, details(result)
    assert "different" in result.stderr and "database" in result.stderr
    assert "7531438976150207210" not in result.stderr
    assert not (Path(env["TEST_FIXTURE"]) / "mcp-env").exists()
    assert "Step 6/10" not in result.stdout


def test_current_schema_refuses_ambiguous_ledger(runtime):
    result = run(runtime, "install.sh", "--no-seed", TEST_FAILURE="duplicate-ledger")
    assert result.returncode != 0, details(result)
    assert "schema_migrations" in result.stderr and "multiple" in result.stderr
    assert "Sill is installed" not in result.stdout
