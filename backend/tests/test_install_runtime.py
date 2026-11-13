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
if args[:2] == ["container", "ls"]:
    print("sill_db\nsill_embeddings" if (root / "volume.json").exists() and (root / "embedding-choice").exists() else "")
    sys.exit()
if args[:2] == ["container", "inspect"]:
    service = "db" if args[-1] == "sill_db" else "embeddings"
    choice = json.loads((root / "embedding-choice").read_text())
    print(json.dumps([{"State": {"Running": True}, "Config": {"Labels": {"com.docker.compose.project": "backend", "com.docker.compose.service": service}, "Cmd": ["--model-id", choice["EMBEDDING_MODEL_ID"]], "Env": ["REVISION=" + choice["EMBEDDING_REVISION"], "EMBEDDING_DIMENSION=768"]}, "Mounts": [{"Destination": "/var/lib/postgresql/data", "Name": "backend_postgres_data"}]}]))
    sys.exit()
if args[0] == "exec":
    if "http://localhost:80/info" in args:
        choice = json.loads((root / "embedding-choice").read_text())
        print(json.dumps({"model_id": choice["EMBEDDING_MODEL_ID"], "model_sha": choice["EMBEDDING_REVISION"]}))
        sys.exit()
    sql = args[-1]
    if "embedding_dimension()" in sql: print("768")
    elif "to_regclass" in sql: print("t")
    elif "SELECT profile" in sql:
        state = root / "profile-state.json"
        value = json.loads(state.read_text())["active_profile"] if state.exists() else None
        print(json.dumps(value) if value else "")
    else: raise AssertionError(args)
    sys.exit()
if args[0] == "volume":
    volume = root / "volume.json"
    if args[1] == "ls":
        print("backend_postgres_data" if volume.exists() or os.getenv("TEST_EXISTING_VOLUME") else "")
    elif args[1] == "create":
        assert not os.getenv("TEST_EXISTING_VOLUME")
        receipt_path = checkout / "backend/.env.embedding-profile.json"
        receipt = json.loads(receipt_path.read_text())
        assert receipt_path.stat().st_mode & 0o777 == 0o600
        assert receipt["selection"]["source"] == {
            "model_id": "unsloth/embeddinggemma-300m", "revision": "bfa3c846ac738e62aa61806ef9112d34acb1dc5a",
            "dimension": 768, "normalization": "l2", "query_prefix": "", "document_prefix": ""}
        labels = dict(args[i+1].split("=", 1) for i, v in enumerate(args) if v == "--label")
        if os.getenv("TEST_VOLUME_RACE"):
            labels = {}
        volume.write_text(json.dumps({"Name": args[-1], "Labels": labels, "CreatedAt": "fixture-created", "Driver": "local", "Options": None}))
        if os.getenv("TEST_PROFILE_FAILURE") == "volume-created":
            sys.exit(72)
    elif args[1] == "inspect":
        print("[" + volume.read_text() + "]")
    else:
        raise AssertionError(args)
    sys.exit()
if args[0] == "inspect":
    if args[1] != "--format":
        volume = json.loads((root / "volume.json").read_text())
        print(json.dumps([{"Mounts": [{"Type": "volume", "Name": volume["Name"], "Destination": "/var/lib/postgresql/data"}]}]))
        sys.exit()
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
    db["volumes"] = [{"type": "volume", "source": "postgres_data", "target": "/var/lib/postgresql/data"}]
    db["command"] = ["postgres", "-c", "app.embedding_dimension=" + settings.get("EMBEDDING_DIMENSION", "768")]
    emb = {"container_name": "sill_embeddings", "command": ["--model-id", settings.get("EMBEDDING_MODEL_ID", "unsloth/embeddinggemma-300m")], "environment": {
        "REVISION": settings.get("EMBEDDING_REVISION", settings.get("SILL_DEFAULT_EMBEDDING_REVISION", "main")),
        "EMBEDDING_MODEL_ID": settings.get("EMBEDDING_MODEL_ID", "unsloth/embeddinggemma-300m"),
        "EMBEDDING_DIMENSION": settings.get("EMBEDDING_DIMENSION", "768")}}
    print(json.dumps({"name": settings.get("COMPOSE_PROJECT_NAME", "backend"), "services": {"db": db, "embeddings": emb}, "volumes": {"postgres_data": {"name": "backend_postgres_data"}}}))
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
        ids = [p.name[:3] for p in sorted((checkout / "backend/migrations").glob("[0-9][0-9][0-9]_*.sql"))]
        print("\n".join(ids[:-1] if os.getenv("TEST_FAILURE") == "ledger" else ids))
    elif "count(*) FROM memories" in command[-1]:
        print(22)
    else:
        print("t")
    sys.exit()
if command[0] == "up":
    if os.getenv("TEST_REQUIRE_PROFILE_RECEIPT"):
        receipt = json.loads((checkout / "backend/.env.embedding-profile.json").read_text())
        assert receipt["volume_identity"]["name"] == "backend_postgres_data"
        assert receipt["volume_identity"]["nonce"] == receipt["nonce"]
    dot = checkout / "backend/.env"
    saved = dict(line.split("=", 1) for line in dot.read_text().splitlines()
                 if line and not line.startswith("#")) if dot.exists() else {}
    settings = saved | dict(os.environ)
    names = {"EMBEDDING_IMAGE", "EMBEDDING_PLATFORM", "EMBEDDING_REVISION", "EMBEDDING_MODEL_ID", "EMBEDDING_DIMENSION"}
    chosen = {key: value for key, value in settings.items()
              if key.startswith("SILL_DEFAULT_EMBEDDING_")}
    chosen["SILL_DEFAULT_EMBEDDING_MODEL_ID"] = "unsloth/embeddinggemma-300m"
    chosen["SILL_DEFAULT_EMBEDDING_DIMENSION"] = "768"
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
    calls = [json.loads(line) for line in (Path(env["TEST_FIXTURE"]) / "docker-calls").read_text().splitlines()]
    assert not any("up" in c or "build" in c or "create" in c for c in calls)


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


def test_explicit_raw_selection_keeps_normal_install_behavior(runtime):
    result = run(runtime, "install.sh", "--no-seed", "--embedding-profile", "raw")
    assert result.returncode == 0, details(result)
    assert not (runtime[0] / "backend/.env.embedding-profile.json").exists()


def test_prompted_profile_dry_run_does_not_create_receipt(runtime):
    result = run(runtime, "install.sh", "--dry-run", "--embedding-profile", "embeddinggemma-search-v1")
    assert result.returncode == 0, details(result)
    assert "embeddinggemma-search-v1" in result.stdout
    assert not (runtime[0] / "backend/.env.embedding-profile.json").exists()


@pytest.mark.parametrize("value", ["unknown", ""])
def test_invalid_profile_is_rejected_before_runtime(runtime, value):
    result = run(runtime, "install.sh", "--embedding-profile", value)
    assert result.returncode == 2
    assert "embedding-profile" in result.stderr
    assert not (Path(runtime[2]["TEST_FIXTURE"]) / "docker-calls").exists()


@pytest.fixture
def profile_runtime(runtime):
    # M5 itself has real PostgreSQL integration coverage. Replace that API at
    # the process boundary here; run the real installer/receipt protocol.
    checkout, _, env = runtime
    (checkout / "backend/scripts/reembed_documents.py").write_text(PROFILE_OPERATOR)
    env["TEST_REQUIRE_PROFILE_RECEIPT"] = "1"
    return runtime


PROFILE_OPERATOR = r'''
import json, os
from pathlib import Path
from contextlib import contextmanager
from embedding_profiles import EmbeddingProfile
root = Path(os.environ["TEST_FIXTURE"])
state_file = root / "profile-state.json"
receipt_file = Path(os.environ["TEST_CHECKOUT"]) / "backend/.env.embedding-profile.json"
class Cursor:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, query):
        assert "pg_control_system" in query
    def fetchone(self):
        return ["7531438976150207200", "sill", os.getenv("TEST_DATABASE_OID", "16384")]
class Connection:
    def cursor(self): return Cursor()
class ProfileMigration:
    def __init__(self, dsn):
        assert dsn.startswith("postgresql://")
    @contextmanager
    def connection(self): yield Connection()
    def status(self):
        return json.loads(state_file.read_text()) if state_file.exists() else {
            "active_profile": None, "active_fingerprint": "legacy-raw-unknown", "operation": None}
    def prepare(self, target, *, source_attestation, backup_dir, pg_tools_container):
        assert pg_tools_container == os.getenv("TEST_EXPECT_TOOLS_CONTAINER", "sill_db")
        assert source_attestation.query_prefix == source_attestation.document_prefix == ""
        assert target.query_prefix == "task: search result | query: "
        assert target.document_prefix == "title: none | text: "
        receipt = json.loads(receipt_file.read_text())
        assert receipt["prepare_started"] and receipt["database_identity"]
        assert not self.status()["operation"], "second prepare"
        Path(backup_dir).mkdir(parents=True, exist_ok=True)
        (Path(backup_dir) / "retained-backup").write_text("fixture archive")
        state = self.status()
        state["operation"] = {"id": "fixture-operation", "state": "preparing",
            "source_profile": source_attestation.to_dict(), "target_profile": target.to_dict()}
        state_file.write_text(json.dumps(state))
        if os.getenv("TEST_PROFILE_FAILURE") == "commit-gap":
            raise ValueError("fixture interruption after prepare commit")
        return {"id": "fixture-operation", "state": "preparing"}
    def resume(self):
        assert json.loads(receipt_file.read_text())["operation_id"] == "fixture-operation"
        if os.getenv("TEST_PROFILE_FAILURE") == "resume":
            raise ValueError("fixture interrupted resume")
        return {"remaining": 0}
    def activate(self):
        state = self.status()
        state["active_profile"] = state["operation"]["target_profile"]
        state["active_fingerprint"] = EmbeddingProfile(**state["active_profile"]).fingerprint
        state["operation"]["state"] = "active"
        state_file.write_text(json.dumps(state))
        return {"fingerprint": state["active_fingerprint"], "state": "active"}
'''


def profile_receipt(runtime):
    return runtime[0] / "backend/.env.embedding-profile.json"


def test_fresh_prompted_install_activates_before_worker_seed_and_clients(profile_runtime):
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1",
                 "--client", "claude-desktop")
    assert result.returncode == 0, details(result)
    assert (profile_runtime[1] / "Library/Application Support/Claude/claude_desktop_config.json").exists()
    receipt = profile_receipt(profile_runtime)
    saved = json.loads(receipt.read_text())
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert saved["operation_id"] == "fixture-operation"
    assert saved["database_identity"] == ["7531438976150207200", "sill", "16384"]
    assert result.stdout.index("Embedding profile active:") < result.stdout.index("maintenance_worker: running")
    assert result.stdout.index("Embedding profile active:") < result.stdout.index("import methodology seed")
    before = receipt.read_bytes()
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1")
    assert result.returncode == 0, details(result)
    assert receipt.read_bytes() == before


@pytest.mark.parametrize("failure", ["resume", "commit-gap"])
def test_prompted_interruption_preserves_operation_and_never_prepares_twice(profile_runtime, failure):
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1",
                 "--client", "claude-desktop", TEST_PROFILE_FAILURE=failure)
    assert result.returncode != 0
    assert not (profile_runtime[1] / "Library").exists()
    assert "maintenance_worker: running" not in result.stdout
    assert "import methodology seed" not in result.stdout
    receipt = json.loads(profile_receipt(profile_runtime).read_text())
    assert receipt["prepare_started"]
    assert (profile_receipt(profile_runtime).parent / ".env.embedding-profile-backups/retained-backup").exists()
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1")
    if failure == "resume":
        assert result.returncode == 0, details(result)
    else:
        assert result.returncode != 0
        assert "reconciliation" in result.stderr
        assert "status" in result.stderr and "resume" in result.stderr


@pytest.mark.parametrize("override", [
    {"TEST_EXISTING_VOLUME": "1"}, {"TEST_VOLUME_RACE": "1"},
    {"EMBEDDING_MODEL_ID": "custom/model"}, {"EMBEDDING_REVISION": "main"},
    {"EMBEDDING_DIMENSION": "384"},
])
def test_prompted_rejects_existing_or_incompatible_before_start(profile_runtime, override):
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1", **override)
    assert result.returncode != 0
    assert any(message in result.stderr for message in ("creation evidence", "requires the tested", "Could not validate the existing runtime"))
    assert "Step 3/10" not in result.stdout
    assert not (Path(profile_runtime[2]["TEST_FIXTURE"]) / "profile-state.json").exists()


def test_prompted_resume_refuses_replaced_database_and_changed_intent(profile_runtime):
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1",
                 TEST_PROFILE_FAILURE="resume")
    assert result.returncode != 0
    for overrides in ({"TEST_DATABASE_OID": "16385"}, {"EMBEDDING_REVISION": "changed"}):
        result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1", **overrides)
        assert result.returncode != 0
        assert "Embedding profile active:" not in result.stdout


def test_default_rerun_preserves_active_prompted_profile(profile_runtime):
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1")
    assert result.returncode == 0, details(result)
    state_file = Path(profile_runtime[2]["TEST_FIXTURE"]) / "profile-state.json"
    original = state_file.read_bytes()
    result = run(profile_runtime, "install.sh", "--no-seed")
    assert result.returncode == 0, details(result)
    assert state_file.read_bytes() == original


def test_prompted_resume_retains_nonce_after_volume_creation_interruption(profile_runtime):
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1",
                 TEST_PROFILE_FAILURE="volume-created")
    assert result.returncode != 0
    assert "Step 3/10" not in result.stdout
    receipt = json.loads(profile_receipt(profile_runtime).read_text())
    assert receipt["volume_identity"] is None
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1")
    assert result.returncode == 0, details(result)
    assert json.loads(profile_receipt(profile_runtime).read_text())["nonce"] == receipt["nonce"]


@pytest.mark.parametrize("changed", ["operation_id", "target_profile", "source_profile", "volume", "active_profile"])
def test_prompted_resume_refuses_unowned_operation_or_replaced_volume(profile_runtime, changed):
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1",
                 TEST_PROFILE_FAILURE="resume")
    assert "fixture interrupted resume" in result.stderr
    root = Path(profile_runtime[2]["TEST_FIXTURE"])
    if changed == "volume":
        path = root / "volume.json"
        state = json.loads(path.read_text())
        state["CreatedAt"] = "replacement"
    else:
        path = root / "profile-state.json"
        state = json.loads(path.read_text())
        if changed == "operation_id":
            state["operation"]["id"] = "somebody-elses-operation"
        elif changed == "active_profile":
            state["active_profile"] = dict(state["operation"]["source_profile"], revision="other")
        else:
            state["operation"][changed]["query_prefix"] = "other"
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1")
    assert result.returncode != 0
    assert path.read_bytes() == before
    assert "maintenance_worker: running" not in result.stdout


def test_commit_gap_can_accept_independently_activated_exact_fingerprint(profile_runtime):
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1",
                 TEST_PROFILE_FAILURE="commit-gap")
    assert "fixture interruption after prepare commit" in result.stderr
    path = Path(profile_runtime[2]["TEST_FIXTURE"]) / "profile-state.json"
    state = json.loads(path.read_text())
    receipt = json.loads(profile_receipt(profile_runtime).read_text())
    assert receipt["operation_id"] is None
    state["active_profile"] = state["operation"]["target_profile"]
    state["active_fingerprint"] = receipt["selection"]["fingerprint"]
    state["operation"]["state"] = "active"
    path.write_text(json.dumps(state))
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1")
    assert result.returncode == 0, details(result)
    assert "already satisfied" in result.stdout
    assert json.loads(profile_receipt(profile_runtime).read_text())["operation_id"] is None


@pytest.mark.parametrize("failure", ["ledger", "terminal"])
def test_prompted_profile_waits_for_schema_and_service_readiness(profile_runtime, failure):
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1",
                 TEST_FAILURE=failure)
    assert result.returncode != 0
    receipt = json.loads(profile_receipt(profile_runtime).read_text())
    assert not receipt["prepare_started"]
    assert receipt["database_identity"] is None
    assert not (Path(profile_runtime[2]["TEST_FIXTURE"]) / "profile-state.json").exists()


def test_prompted_backup_uses_selected_database_container(profile_runtime):
    result = run(profile_runtime, "install.sh", "--embedding-profile", "embeddinggemma-search-v1",
                 SILL_DB_CONTAINER="selected_fixture_db", TEST_EXPECT_TOOLS_CONTAINER="selected_fixture_db")
    assert result.returncode == 0, details(result)
    receipt = json.loads(profile_receipt(profile_runtime).read_text())
    assert receipt["selection"]["container"] == "selected_fixture_db"


@pytest.mark.parametrize("scope", ["project", "home"])
@pytest.mark.parametrize("override", [{"POSTGRES_PORT": "55432"}, {"COMPOSE_PROJECT_NAME": "another"}])
def test_hook_only_conflicts_refused_before_runtime(runtime, scope, override):
    checkout, home, env = runtime
    project = checkout / "hook project"
    project.mkdir()
    args = ["--scope", scope]
    if scope == "project": args += ["--hooks-for", str(project)]
    result = run(runtime, "install.sh", "--no-seed", *args, **override)
    assert result.returncode != 0, details(result)
    calls = [json.loads(line) for line in (Path(env["TEST_FIXTURE"]) / "docker-calls").read_text().splitlines()]
    assert not any("build" in c or "up" in c for c in calls)
    for target in (home, project):
        assert not (target / ".codex/hooks.json").exists()
        assert not (target / ".claude/settings.json").exists()
