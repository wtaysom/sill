"""Four defects the clean-machine acceptance rehearsal found, pinned.

All four were invisible on a development machine and fatal (or silently
wrong) on a fresh one, and the first three have the same shape: a step
reported success while writing — or reading — somewhere nothing else looks.

1. **`upgrade.sh` ignored `backend/.env`.** Check 1 runs
   `docker compose` from `backend/`, so Compose loads `.env` and check 1 goes
   green; check 3 shells `docker exec "$SILL_DB_CONTAINER"` straight from the
   process environment, which nothing sets. An operator following this repo's
   own advice — "set `SILL_DB_CONTAINER` in `backend/.env` if you need two
   stacks side by side" — got a green check 1 and a check 3 aimed at whatever
   container happened to be named `sill_db`. With two Sills installed, that is
   a *pass* reported off the wrong database.

2. **install.sh wrote the MCP entry to `~/.claude/.mcp.json`.** Claude Code's
   user-scope MCP registry is `~/.claude.json`; `~/.claude/.mcp.json` is not a
   path it reads. Step 7 printed `added mcpServers.sill -> …` and
   `claude mcp list` then said "No MCP servers configured", which is exactly
   the loop the runbook's phase-2 remedy ("re-run ./install.sh and read step
   7's output") cannot break.

3. **`verify.sh` check 2 could not see a broken MCP server.** It ran
   `sill-mcp --help`, which exits before importing the MCP SDK. When an
   unpinned `mcp>=1.0.0` resolved to 2.x — removing the `Server.list_tools()`
   API the server registers through — check 2 stayed green over a server that
   died on its first handshake. The pin in backend/pyproject.toml blocks that
   one resolution; `--help` would go green again the next time the SDK moves,
   so the check now speaks MCP.

4. **`sill.py` never read `backend/.env`.** A beat's bare `sill notice` found
   the right database and the same command from the operator's shell did not,
   and the rehearsal could not say why. The route was `worker.py`'s
   module-level `load_dotenv()` — which resolves `.env` against worker.py's
   own directory, not the cwd — leaking `backend/.env` into the worker's
   environment, which `beat_worker.spawn_beat()` forwards wholesale to the
   child. The mint path now loads that file itself, so it no longer depends
   on an unrelated module having been imported first.

None of these tests needs docker, a database, or a network.
"""

import json
import os
import stat
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SH = REPO_ROOT / "verify.sh"
UPGRADE_SH = REPO_ROOT / "upgrade.sh"
INSTALL_SH = REPO_ROOT / "install.sh"

ENV_LOADING_SCRIPTS = [UPGRADE_SH]


def extract_function(script: Path, name: str) -> str:
    """Pull one shell function's source out of a script.

    Slicing the text is deliberate: neither script is source-guarded, so
    `source verify.sh` would run all six checks against a live stack. This
    exercises the real function body without executing anything else in the
    file.
    """
    text = script.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"{script.name}: {name}() has unbalanced braces")


def run_loader(script: Path, env_body: str, tmp_path: Path, preset: dict) -> dict:
    """Run the script's own load_env_file() against a fixture backend/.env."""
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / ".env").write_text(env_body, encoding="utf-8")
    program = "\n".join([
        "set -euo pipefail",
        f'SILL_DIR="{tmp_path}"',
        extract_function(script, "load_env_file"),
        "load_env_file",
        'printf "%s\\n" "SILL_DB_CONTAINER=${SILL_DB_CONTAINER:-<unset>}"',
        'printf "%s\\n" "POSTGRES_USER=${POSTGRES_USER:-<unset>}"',
        'printf "%s\\n" "POSTGRES_DB=${POSTGRES_DB:-<unset>}"',
        'printf "%s\\n" "QUOTED=${QUOTED:-<unset>}"',
    ])
    env = {k: v for k, v in os.environ.items() if k not in
           ("SILL_DB_CONTAINER", "POSTGRES_USER", "POSTGRES_DB", "QUOTED")}
    env.update(preset)
    result = subprocess.run(["bash", "-c", program], capture_output=True,
                            text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in result.stdout.strip().splitlines())


FIXTURE_ENV = """\
# a comment line
SILL_DB_CONTAINER=fixture_db

POSTGRES_USER=fixture_user
POSTGRES_DB=fixture_db_name
QUOTED="quoted value"
not a key=value pair
"""


@pytest.mark.parametrize("script", ENV_LOADING_SCRIPTS, ids=lambda p: p.name)
def test_backend_env_supplies_the_db_coordinates(script, tmp_path):
    out = run_loader(script, FIXTURE_ENV, tmp_path, preset={})
    assert out["SILL_DB_CONTAINER"] == "fixture_db"
    assert out["POSTGRES_USER"] == "fixture_user"
    assert out["POSTGRES_DB"] == "fixture_db_name"
    assert out["QUOTED"] == "quoted value", "surrounding quotes should be stripped"


@pytest.mark.parametrize("script", ENV_LOADING_SCRIPTS, ids=lambda p: p.name)
def test_an_exported_variable_beats_the_env_file(script, tmp_path):
    """Compose's precedence, matched: shell environment wins over .env."""
    out = run_loader(script, FIXTURE_ENV, tmp_path,
                     preset={"SILL_DB_CONTAINER": "from_shell"})
    assert out["SILL_DB_CONTAINER"] == "from_shell"
    assert out["POSTGRES_USER"] == "fixture_user", "unset keys still come from .env"


@pytest.mark.parametrize("script", ENV_LOADING_SCRIPTS, ids=lambda p: p.name)
def test_a_missing_env_file_is_not_an_error(script, tmp_path):
    out = run_loader(script, "", tmp_path, preset={})
    (tmp_path / "backend" / ".env").unlink()
    out = run_loader(script, "", tmp_path, preset={})
    assert out["SILL_DB_CONTAINER"] == "<unset>"


@pytest.mark.parametrize("script", ENV_LOADING_SCRIPTS, ids=lambda p: p.name)
def test_the_db_coordinates_are_derived_after_the_env_file_is_loaded(script):
    """Ordering, not just presence: a CONTAINER= line above load_env_file
    would read the pre-.env environment and the fix would be inert."""
    text = script.read_text(encoding="utf-8")
    assert text.index("\nload_env_file\n") < text.index('CONTAINER="${SILL_DB_CONTAINER'), (
        f"{script.name}: CONTAINER is derived before backend/.env is loaded"
    )


# --- install.sh step 7 --------------------------------------------------------


def test_install_never_writes_the_path_claude_code_does_not_read():
    """`~/.claude/.mcp.json` is not Claude Code's user-scope MCP registry.

    Comment lines are exempt on purpose: the step's header explains the old
    path so a reader of a v0.1.0 install can tell what happened to it.
    """
    code = [line for line in INSTALL_SH.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")]
    hits = [line for line in code if ".claude/.mcp.json" in line]
    assert not hits, (
        "install.sh writes ~/.claude/.mcp.json, which Claude Code never reads; "
        f"the user-scope registry is ~/.claude.json — {hits}"
    )


# --- verify.sh check 2: a real MCP handshake -----------------------------------
#
# Each stub below is a whole MCP "server" — a script that either speaks the
# protocol or fails in one of the specific ways a real server has failed. They
# are the point of these tests: the old check could not tell any of them apart
# from a healthy server, because none of them is reached by `--help`.

GOOD_SERVER = """\
import json, sys
line = sys.stdin.readline()
req = json.loads(line)
sys.stdout.write(json.dumps({
    "jsonrpc": "2.0", "id": req["id"],
    "result": {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "sill", "version": "0.2.0"},
    },
}) + "\\n")
sys.stdout.flush()
sys.stdin.readline()
"""

# The real F4 failure, verbatim: the server starts, accepts stdin, and dies
# on the SDK call. `sill-mcp --help` exits 0 on exactly this build.
DEAD_SERVER = """\
import sys
sys.stderr.write("'Server' object has no attribute 'list_tools'\\n")
sys.exit(1)
"""

# The shape of the old check's false green: exit 0, having answered nothing.
SILENT_SERVER = "import sys\nsys.exit(0)\n"

HANGING_SERVER = """\
import sys, time
sys.stdin.readline()
time.sleep(300)
"""

WRONG_SERVER = """\
import json, sys
req = json.loads(sys.stdin.readline())
sys.stdout.write(json.dumps({
    "jsonrpc": "2.0", "id": req["id"],
    "result": {"protocolVersion": "2024-11-05", "capabilities": {},
               "serverInfo": {"name": "someone-elses-server", "version": "9"}},
}) + "\\n")
sys.stdout.flush()
"""

ERRORING_SERVER = """\
import json, sys
req = json.loads(sys.stdin.readline())
sys.stdout.write(json.dumps({
    "jsonrpc": "2.0", "id": req["id"],
    "error": {"code": -32603, "message": "database is not accepting connections"},
}) + "\\n")
sys.stdout.flush()
"""


def make_stub(tmp_path: Path, name: str, body: str) -> Path:
    stub = tmp_path / name
    stub.write_text(f"#!{sys.executable}\n{body}")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def run_handshake(stub: Path, timeout_s: str = "10") -> subprocess.CompletedProcess:
    """Run verify.sh's own mcp_handshake() against one stub server."""
    program = "\n".join([
        "set -uo pipefail",
        extract_function(VERIFY_SH, "mcp_handshake"),
        "mcp_handshake",
    ])
    env = {**os.environ, "SILL_MCP_CMD": str(stub),
           "SILL_VERIFY_MCP_TIMEOUT_S": timeout_s}
    return subprocess.run(["bash", "-c", program], capture_output=True,
                          text=True, env=env, timeout=90)


def test_handshake_passes_a_server_that_speaks_mcp(tmp_path):
    result = run_handshake(make_stub(tmp_path, "good", GOOD_SERVER))
    assert result.returncode == 0, result.stderr
    assert "sill 0.2.0" in result.stdout
    assert "2024-11-05" in result.stdout


def test_handshake_fails_the_server_that_used_to_pass_dead(tmp_path):
    """F4 itself. This is the case the whole change exists for: `--help` exits
    0 on this build, and the old check called that a pass."""
    result = run_handshake(make_stub(tmp_path, "dead", DEAD_SERVER))
    assert result.returncode != 0
    assert "without answering" in result.stderr
    assert "list_tools" in result.stderr, (
        "the server's own error must reach the operator — it is the entire "
        f"diagnosis: {result.stderr}"
    )


def test_handshake_fails_a_server_that_exits_0_saying_nothing(tmp_path):
    """The `--help` shape, generalised: exit 0 is not an answer."""
    result = run_handshake(make_stub(tmp_path, "silent", SILENT_SERVER))
    assert result.returncode != 0
    assert "without answering" in result.stderr


def test_handshake_fails_a_hanging_server_within_its_budget(tmp_path):
    """A check that hangs is a check nobody runs twice."""
    result = run_handshake(make_stub(tmp_path, "hang", HANGING_SERVER),
                           timeout_s="3")
    assert result.returncode != 0
    assert "no answer to `initialize`" in result.stderr


def test_handshake_fails_a_server_that_is_not_this_one(tmp_path):
    """`claude mcp add` registers the name `sill`; a handshake answered by
    something else means the registration points somewhere unintended."""
    result = run_handshake(make_stub(tmp_path, "wrong", WRONG_SERVER))
    assert result.returncode != 0
    assert "someone-elses-server" in result.stderr


def test_handshake_fails_a_server_that_returns_a_jsonrpc_error(tmp_path):
    result = run_handshake(make_stub(tmp_path, "erroring", ERRORING_SERVER))
    assert result.returncode != 0
    assert "refused `initialize`" in result.stderr
    assert "not accepting connections" in result.stderr


# --- the mint path reads backend/.env -----------------------------------------


def run_mint_path_in(tmp_path: Path, env_body: str | None, preset: dict) -> dict:
    """Import a copy of the real sill.py beside a fixture .env and report the
    database coordinates it resolved.

    A copy, not the installed module: sill.py finds `.env` next to its own
    file, so pointing it at a fixture means putting the file there. It imports
    nothing but the standard library, so a copy is the real thing.
    """
    (tmp_path / "sill.py").write_bytes((BACKEND_ROOT / "sill.py").read_bytes())
    if env_body is not None:
        (tmp_path / ".env").write_text(env_body, encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SILL_DB_", "POSTGRES_"))}
    env["PYTHONPATH"] = str(tmp_path)
    env.update(preset)
    result = subprocess.run(
        [sys.executable, "-c",
         "import sill, json; print(json.dumps({'container': sill.DB_CONTAINER, "
         "'user': sill.DB_USER, 'name': sill.DB_NAME}))"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path.parent),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


MINT_ENV = """\
# side-by-side install
SILL_DB_CONTAINER=other_db
SILL_DB_USER=other_user
SILL_DB_NAME="other_name"
"""


def test_the_mint_path_reads_backend_env(tmp_path):
    """The observed asymmetry, removed: a bare `sill notice` from the
    operator's shell now resolves the same container a beat's does."""
    out = run_mint_path_in(tmp_path, MINT_ENV, preset={})
    assert out == {"container": "other_db", "user": "other_user",
                   "name": "other_name"}, "quotes should be stripped, too"


def test_an_exported_variable_still_beats_the_env_file_for_the_mint_path(tmp_path):
    out = run_mint_path_in(tmp_path, MINT_ENV,
                           preset={"SILL_DB_CONTAINER": "from_shell"})
    assert out["container"] == "from_shell"
    assert out["user"] == "other_user", "unset keys still come from .env"


def test_the_mint_path_falls_back_to_defaults_with_no_env_file(tmp_path):
    """The single-stack install has no backend/.env at all, and must not
    become an error case."""
    out = run_mint_path_in(tmp_path, None, preset={})
    assert out == {"container": "sill_db", "user": "sill", "name": "sill"}


def test_the_mint_path_loads_the_env_file_before_resolving_coordinates():
    """Ordering: a `DB_CONTAINER = …` above the loader call would read the
    pre-.env environment and the fix would be inert."""
    text = (BACKEND_ROOT / "sill.py").read_text(encoding="utf-8")
    assert text.index("\n_load_backend_env()\n") < text.index("\nDB_CONTAINER = "), (
        "sill.py resolves DB_CONTAINER before it loads backend/.env"
    )


def test_the_mint_path_does_not_depend_on_the_worker_being_imported():
    """Before the fix, the only thing putting `backend/.env` into a beat's
    environment was `worker.py`'s module-level `load_dotenv()` plus
    `spawn_beat()`'s wholesale env forward. `python -m beat_worker` imports
    neither, so a worker started that way minted against the defaults."""
    text = (BACKEND_ROOT / "sill.py").read_text(encoding="utf-8")
    assert "_load_backend_env" in text
    assert "import worker" not in text and "from worker" not in text


# --- explicit MCP instance selection -----------------------------------------


def run_instance_server(tmp_path, monkeypatch, capsys, env_body=None, extra=(), attempts=None):
    """Run the MCP entry point with a real SDK initialize and no live database."""
    from inspect import signature
    from io import StringIO

    from mcp import types
    from mcp.server import stdio
    import anyio
    import sill_mcp_server as server

    home = tmp_path / "gui home"
    home.mkdir()
    (home / ".env").write_text("SILL_DB_DSN=postgresql://wrong@cwd.invalid/db\n")
    monkeypatch.chdir(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SILL_DB_DSN", "postgresql://wrong@ambient.invalid/db")
    monkeypatch.setenv("POSTGRES_HOST", "ambient.invalid")
    monkeypatch.setenv("POSTGRES_PORT", "59999")
    monkeypatch.setenv("POSTGRES_PASSWORD", "ambient secret")
    monkeypatch.setenv("PGPORT", "invalid ambient port")
    monkeypatch.setenv("PGHOST", "stale-pg-host.invalid")
    monkeypatch.setenv("PGUSER", "stale-pg-user")
    monkeypatch.setenv("PGDATABASE", "stale-pg-db")

    # asyncpg's connection parameter parser remains real. Substitute only the
    # DB-backed client constructor, then let the actual SDK handle initialize.
    from asyncpg.connect_utils import _parse_connect_arguments

    seen = []

    class FixtureClient:
        async def close(self):
            pass

    async def capture_connection(dsn):
        if attempts is not None:
            attempts.append(dsn)
        parameters = signature(_parse_connect_arguments).parameters
        options = dict.fromkeys(parameters)
        options.update(dsn=dsn, command_timeout=1, statement_cache_size=0,
                       max_cached_statement_lifetime=0, max_cacheable_statement_size=0)
        addresses, connection, _ = _parse_connect_arguments(**options)
        seen.append((addresses, connection.user, connection.password, connection.database))
        return FixtureClient()

    monkeypatch.setattr(server.CognitiveMemory, "create", capture_connection)
    stdin = StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
        '{"protocolVersion":"2024-11-05","capabilities":{},'
        '"clientInfo":{"name":"fixture","version":"1"}}}\n'
    )
    transport = stdio.stdio_server
    monkeypatch.setattr(stdio, "stdio_server",
                        lambda: stdio_server_with_stdout(transport, anyio, stdin))
    env_file = tmp_path / "instance settings.env"
    if env_body is not None:
        env_file.write_text(env_body, encoding="utf-8")
    status = server.main(["--env-file", str(env_file), *extra])
    captured = capsys.readouterr()
    if status == 0:
        messages = captured.out.splitlines()
        assert len(messages) == 1
        response = types.JSONRPCResponse.model_validate_json(messages[0])
        assert response.id == 1
        assert response.result["serverInfo"]["name"] == "sill"
    else:
        assert captured.out == ""
    return status, seen, captured.err, env_file


@asynccontextmanager
async def stdio_server_with_stdout(transport, anyio, stdin):
    # A captured Python text stream has no .buffer, so pass it to the real
    # stdio transport explicitly rather than replacing the MCP protocol.
    async with transport(stdin=anyio.wrap_file(stdin),
                         stdout=anyio.wrap_file(sys.stdout)) as streams:
        yield streams


def test_explicit_instance_ignores_stale_dsn_and_cwd_dotenv(tmp_path, monkeypatch, capsys):
    status, seen, stderr, _ = run_instance_server(
        tmp_path, monkeypatch, capsys, "POSTGRES_PORT=15432\nPOSTGRES_USER=fixture\n",
    )
    assert status == 0, stderr
    assert seen == [([("localhost", 15432)], "fixture", "sill_password", "sill")]
    assert stderr == ""
    assert os.environ["PGPORT"] == "invalid ambient port"


def test_explicit_instance_defaults_to_localhost(tmp_path, monkeypatch, capsys):
    status, seen, stderr, _ = run_instance_server(tmp_path, monkeypatch, capsys, "")
    assert status == 0, stderr
    assert seen == [([("localhost", 5432)], "sill", "sill_password", "sill")]


def test_unreadable_instance_fails_before_connecting(tmp_path, monkeypatch, capsys):
    status, seen, stderr, env_file = run_instance_server(
        tmp_path, monkeypatch, capsys, extra=("--dsn", "postgresql://override@localhost/db"),
    )
    assert status != 0
    assert not seen
    assert str(env_file) in stderr
    assert "read" in stderr.lower()
    assert "ambient secret" not in stderr and "override@" not in stderr


def test_explicit_instance_quotes_dsn_fields(tmp_path, monkeypatch, capsys):
    body = """
POSTGRES_HOST=::1
POSTGRES_USER="fixture user@a"
POSTGRES_PASSWORD=' a:/?#@ +% ${POSTGRES_PASSWORD} $(touch marker) '
POSTGRES_DB="db/with spaces"
"""
    status, seen, stderr, _ = run_instance_server(tmp_path, monkeypatch, capsys, body)
    assert status == 0, stderr
    assert seen == [
        ([("::1", 5432)], "fixture user@a", " a:/?#@ +% ${POSTGRES_PASSWORD} $(touch marker) ",
         "db/with spaces")
    ]
    assert not (tmp_path / "gui home" / "marker").exists()


@pytest.mark.parametrize(("override", "user", "port"), [
    ((), "from-file", 16432),
    (("--dsn", "postgresql://from-flag:pass@localhost:17432/fixture"), "from-flag", 17432),
])
def test_file_dsn_precedes_file_postgres_fields_and_explicit_dsn_precedes_both(
    tmp_path, monkeypatch, capsys, override, user, port,
):
    body = "SILL_DB_DSN=postgresql://from-file:pass@localhost:16432/fixture\nPOSTGRES_PORT=18432\n"
    status, seen, stderr, _ = run_instance_server(tmp_path, monkeypatch, capsys, body, override)
    assert status == 0, stderr
    assert seen == [([("localhost", port)], user, "pass", "fixture")]


@pytest.mark.parametrize("body", [
    'POSTGRES_PASSWORD="bad secret\n',
    'POSTGRES_USER=\n',
    'POSTGRES_PORT=99999\n',
    'POSTGRES_USER=sill\nPOSTGRES_USER=other\n',
    'POSTGRES_USER\n',
    'SILL_DB_DSN=\n',
    'SILL_DB_DSN=postgresql://\n',
    'SILL_DB_DSN=postgresql:///missing-target\n',
    'SILL_DB_DSN=postgresql://user:bad%2secret@localhost/database\n',
    'SILL_DB_DSN=postgresql://localhost:65536/database\n',
    'SILL_DB_DSN=postgresql:///database?host=localhost&port=0\n',
])
def test_explicit_instance_rejects_invalid_settings_without_revealing_values(
    tmp_path, monkeypatch, capsys, body,
):
    status, seen, stderr, env_file = run_instance_server(tmp_path, monkeypatch, capsys, body)
    assert status != 0
    assert not seen
    assert str(env_file) in stderr
    assert "bad secret" not in stderr
    assert "bad%2secret" not in stderr


@pytest.mark.parametrize("dsn", [
    "postgresql://user:secret@[::1]:%31%35%34%33%32/db",
    "postgresql:///db?host=one.invalid,two.invalid&port=15432,15433,15434",
    "postgresql://localhost/db?port=15432",
    "postgresql://localhost/db?host=other.invalid",
    "postgresql:///db?host=localhost:15432&port=15433",
])
def test_dsn_port_mismatches_are_rejected_before_asyncpg_startup(tmp_path, monkeypatch, capsys, dsn):
    attempts = []
    status, seen, stderr, env_file = run_instance_server(
        tmp_path, monkeypatch, capsys, f"SILL_DB_DSN={dsn}\n", attempts=attempts,
    )
    assert status != 0
    assert not attempts
    assert not seen
    assert str(env_file) in stderr
    assert "SILL_DB_DSN" in stderr
    assert "secret" not in stderr


@pytest.mark.parametrize(("dsn", "addresses"), [
    ("postgresql://user:pass@%2Ftmp%2Ffixture%20socket:15432/db",
     ["/tmp/fixture socket/.s.PGSQL.15432"]),
    ("postgresql://user:pass@/db?host=/tmp/fixture%20socket&port=15432",
     ["/tmp/fixture socket/.s.PGSQL.15432"]),
    ("postgresql://user:pass@one.invalid:15432,two.invalid:15433/db",
     [("one.invalid", 15432), ("two.invalid", 15433)]),
    ("postgresql://user:pass@[::1]:15432/db", [("::1", 15432)]),
    ("postgresql:///db?user=user&password=pass&host=one.invalid,two.invalid&port=15432",
     [("one.invalid", 15432), ("two.invalid", 15432)]),
    ("postgresql:///db?user=user&password=pass&host=one.invalid,two.invalid&port=15432,15433",
     [("one.invalid", 15432), ("two.invalid", 15433)]),
])
def test_explicit_dsn_preserves_socket_and_multihost_targets(
    tmp_path, monkeypatch, capsys, dsn, addresses,
):
    status, seen, stderr, _ = run_instance_server(
        tmp_path, monkeypatch, capsys, f"SILL_DB_DSN={dsn}\n",
    )
    assert status == 0, stderr
    assert seen == [(addresses, "user", "pass", "db")]


@pytest.mark.parametrize("arg", ["--help", "--version"])
def test_instance_help_and_version_ignore_unreadable_file(tmp_path, arg):
    result = subprocess.run(
        [sys.executable, "-m", "sill_mcp_server", "--env-file", str(tmp_path / "absent"), arg],
        capture_output=True, text=True, timeout=20, cwd=BACKEND_ROOT,
        env={**os.environ, "HOME": str(tmp_path), "SILL_DB_DSN": "invalid"},
    )
    assert result.returncode == 0, result.stderr
    assert "sill-mcp" in result.stdout
    assert not result.stderr
