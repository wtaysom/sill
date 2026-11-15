"""A default v0.2.0 install, used the ordinary way, then upgraded in place.

The install is the one v0.2's README produced: no backend/.env, the default
container names and ports, embeddings started without a revision, and a bare
`sill-mcp` client entry. A default install owns names and ports that on a
personal machine belong to somebody's real store, so this runs only on a
disposable GitHub-hosted Linux amd64 runner and never removes anything.
"""
import asyncio
import gzip
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4

OLD_TAG = "v0.2.0"
OLD_VERSION = "0.2.0"
OLD_LEDGER = [f"{i:03}" for i in range(1, 8)]
PROJECT = "backend"  # Compose names the project after the directory it runs from.
CONTAINERS = {"db": "sill_db", "embeddings": "sill_embeddings", "rabbitmq": "sill_rabbitmq",
              "maintenance_worker": "sill_maintenance_worker"}
VOLUMES = ("postgres_data", "embeddings_cache", "rabbitmq_data")
PORTS = (5432, 5672, 15672)
MODEL = "unsloth/embeddinggemma-300m"
MODEL_CACHE = "/data/models--unsloth--embeddinggemma-300m"
LABEL = "com.docker.compose."
UNRELATED = {"command": "/synthetic/unrelated"}
LEGACY = {"type": "stdio", "command": "sill-mcp", "args": [], "env": {}}
SPEAKER = "Upgrade rehearsal synthetic speaker"
CLIENT = ("--client", "claude-code")
REQUIRED = ("bash", "cat", "date", "dirname", "git", "gzip", "ln", "mkdir", "mktemp", "mv", "rm",
            "sed", "sleep")
OPTIONAL = ("basename", "chmod", "cp", "crontab", "cut", "env", "grep", "head", "id", "ls",
            "readlink", "sh", "sort", "tail", "tar", "touch", "tr", "uname", "wc")
HEX = re.compile(r"[0-9a-f]{40}")
# v0.2.0's database image no longer builds from its tag. Debian 11 left
# deb.debian.org's security pool and PostgreSQL retired bullseye-pgdg, so the
# first apt-get in its Dockerfile fails. The same packages are in the two
# archives; the archived PostgreSQL source is https-only, hence the
# certificates. Nothing else about the image changes.
OLD_BASE = "FROM postgres:16-bullseye\n"
ARCHIVES = ("RUN sed -i 's|http://deb.debian.org/|http://archive.debian.org/|g' /etc/apt/sources.list && "
            "sed -i 's|http://apt.postgresql.org/|https://apt-archive.postgresql.org/|' "
            "/etc/apt/sources.list.d/pgdg.list && apt-get update -o Dir::Etc::sourceparts=- && "
            "apt-get install -y --no-install-recommends ca-certificates\n")
MEMORIES = """SELECT coalesce(json_agg(json_build_object(
  'id', id::text, 'content_md5', md5(content), 'type', type::text, 'status', status::text,
  'live', archived_at IS NULL, 'created_at', created_at, 'vector_md5', md5(embedding::text))
  ORDER BY id), '[]') FROM public.memories"""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args, cwd=None, env=None, timeout=60):
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, text=True, errors="replace",
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    require(result.returncode == 0,
            f"{Path(str(args[0])).name} {args[1]} exited {result.returncode}: {result.stderr[-1500:]}")
    return result.stdout.strip()


def redact(value, temporary):
    return value.replace(str(temporary), "<fixture>").replace("sill_password", "<fixture-password>")


def read_env(text):
    """Settings from a Compose env file, with one layer of quotes removed."""
    settings = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        settings[key.strip()] = value
    return settings


def cached_revision(listing):
    """The one snapshot an unpinned start resolved `main` to, from the model cache."""
    pointer, separator, names = listing.partition("\n--\n")
    snapshots = names.split()
    require(separator and HEX.fullmatch(pointer.strip() or "-"), "the model cache has no refs/main pointer")
    require(snapshots == [pointer.strip()],
            f"expected the cache to hold exactly the snapshot refs/main names; found {snapshots}")
    return snapshots[0]


def with_archived_sources(dockerfile):
    """The v0.2.0 database Dockerfile with its package sources pointed at the archives."""
    require(dockerfile.startswith(OLD_BASE) and "archive" not in dockerfile,
            "the v0.2.0 database Dockerfile is not the one this rehearsal knows how to build today")
    return OLD_BASE + ARCHIVES + dockerfile[len(OLD_BASE):]


def compare_memories(before, after, keep, newer):
    """Every row survives unchanged, except that the newer repeated copy is archived."""
    old, new = ({row["id"]: row for row in rows} for rows in (before, after))
    require(len(old) == len(before) and len(new) == len(after), "memory ids are not unique")
    require(old.keys() == new.keys(),
            f"the upgrade lost or added memories: lost {sorted(old.keys() - new.keys())}, "
            f"added {sorted(new.keys() - old.keys())}")
    require(keep in old and newer in old and keep != newer, "the repeated pair is not in the store")
    for memory_id, row in old.items():
        expected = dict(row, status="archived", live=False) if memory_id == newer else row
        changed = sorted(key for key in expected if expected[key] != new[memory_id].get(key))
        require(not changed, f"memory {memory_id} changed across the upgrade: {changed}")
    require(old[keep]["live"] and old[newer]["live"], "the repeated pair was not live before the upgrade")


def command_path(directory):
    # The scripts look on PATH for pipx and agent CLIs. Offer only ordinary
    # system commands, so the install takes its venv route and finds neither.
    directory.mkdir(mode=0o700)
    (directory / "python3").symlink_to(sys.executable)
    docker = shutil.which("docker")
    require(docker, "Docker CLI is missing on the runner")
    (directory / "docker").symlink_to(Path(docker).resolve())
    for name in REQUIRED + OPTIONAL:
        command = shutil.which(name, path=os.defpath)
        require(command or name in OPTIONAL, f"required system command is missing: {name}")
        if command:
            (directory / name).symlink_to(command)
    return directory


def script(path, cwd, env, evidence, name, temporary, timeout, *flags):
    """Run one of the checkout's scripts as a user would; keep its whole output."""
    log = evidence / name
    started = time.monotonic()
    with log.open("w") as stream:
        with subprocess.Popen([str(path), *flags], cwd=cwd, env=env, stdout=stream,
                              stderr=subprocess.STDOUT, start_new_session=True) as proc:
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise
    text = redact(log.read_text(errors="replace"), temporary)
    log.write_text(text)
    return code, text, round(time.monotonic() - started, 1)


async def mcp_calls(registry, calls):
    # Runs under the installed environment's Python, outside the checkout,
    # and starts the server exactly as the client entry names it.
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    entry = json.loads(registry.read_text())["mcpServers"]["sill"]
    server = StdioServerParameters(command=entry["command"], args=entry.get("args", []), env=dict(os.environ))

    async def converse():
        async with stdio_client(server) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                init = await session.initialize()
                tools = sorted(tool.name for tool in (await session.list_tools()).tools)
                results = []
                for call in calls:
                    result = await session.call_tool(call["tool"], call["args"])
                    require(not result.isError and len(result.content) == 1 and result.content[0].type == "text",
                            f"MCP {call['tool']} did not return a successful text result")
                    require(not result.content[0].text.startswith("Error:"),
                            f"MCP {call['tool']}: {result.content[0].text}")
                    results.append(result.content[0].text)
                return dict(server=init.serverInfo.name, version=init.serverInfo.version,
                            instructions=getattr(init, "instructions", None), tools=tools, results=results)

    return await asyncio.wait_for(converse(), 120)


def rehearse(source, temporary, evidence, report):
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
            "use a disposable GitHub-hosted runner: a default install takes the default container names and ports")
    require(platform.system() == "Linux" and platform.machine() == "x86_64", "native Linux x86_64 host required")
    home, checkout, outside = temporary / "home", temporary / "sill", temporary / "elsewhere"
    for path in (home, outside):
        path.mkdir(mode=0o700)
    tools = command_path(temporary / "bin")
    # v0.2 links its commands into ~/.local/bin and then calls them by name.
    env = dict(HOME=str(home), PATH=f"{home / '.local/bin'}:{tools}", LANG="C.UTF-8",
               DOCKER_HOST="unix:///var/run/docker.sock", PIP_CONFIG_FILE=os.devnull)
    backend, env_file, registry = checkout / "backend", checkout / "backend/.env", home / ".claude.json"
    installed = home / ".local/share/sill-venv/bin"

    def docker(*args, **kwargs):
        return run("docker", *args, cwd=outside, env=env, **kwargs)

    def psql(sql):
        return json.loads(docker("exec", CONTAINERS["db"], "psql", "-h", "127.0.0.1", "-XAt", "-v",
                                 "ON_ERROR_STOP=1", "-U", "sill", "-d", "sill", "-c", sql))

    def phase(name):
        report["phase"] = name
        write_report(evidence, report)
        print(f"v0.2 upgrade rehearsal: {name}", flush=True)

    def version():
        return re.search(r'^version = "([^"]+)"', (backend / "pyproject.toml").read_text(), re.M).group(1)

    def runtime(settled=False):
        # v0.2 calls its database healthy while initdb's socket-only server is
        # still loading the schema, starts the worker against it, and has no
        # restart policy: after a first install the worker may have exited.
        # That is a state the upgrade has to accept, so it is recorded, and
        # only an upgraded runtime is required to have its worker running.
        state = {}
        for service, name in CONTAINERS.items():
            info, = json.loads(docker("container", "inspect", name))
            labels = info["Config"]["Labels"]
            require(labels.get(LABEL + "project") == PROJECT and labels.get(LABEL + "service") == service
                    and labels.get(LABEL + "project.working_dir") == str(backend),
                    f"{name} does not belong to this checkout's default Compose project")
            require(info["State"]["Status"] == "running" or (service == "maintenance_worker" and not settled),
                    f"{name} is {info['State']['Status']}, not running")
            state[service] = dict(
                id=info["Id"], image=info["Image"], image_ref=info["Config"]["Image"],
                status=info["State"]["Status"], exit_code=info["State"]["ExitCode"],
                command=info["Config"]["Cmd"], restart=info["HostConfig"]["RestartPolicy"]["Name"],
                environment=sorted(e for e in info["Config"]["Env"] if e.startswith(("REVISION=", "EMBEDDING_"))),
                # Named project volumes only; an image may declare anonymous ones of its own.
                mounts=sorted((m["Name"], m["Destination"]) for m in info["Mounts"]
                              if m["Type"] == "volume" and m["Name"].startswith(PROJECT + "_")),
                ports={port: sorted((b["HostIp"], b["HostPort"]) for b in bindings)
                       for port, bindings in (info["NetworkSettings"]["Ports"] or {}).items() if bindings})
        return state

    def store():
        return dict(ledger=psql("SELECT json_agg(migration_id ORDER BY migration_id) FROM schema_migrations"),
                    memories=psql(MEMORIES))

    def embeddings():
        name = CONTAINERS["embeddings"]
        info = json.loads(docker("exec", name, "curl", "-fsS", "http://localhost:80/info"))
        listing = docker("exec", name, "sh", "-c",
                         f'cat {MODEL_CACHE}/refs/main 2>/dev/null; printf "\\n--\\n"; ls -1 {MODEL_CACHE}/snapshots')
        refs = docker("exec", name, "sh", "-c", f"ls -1 {MODEL_CACHE}/refs").split()
        return dict(model_id=info.get("model_id"), model_sha=info.get("model_sha"), listing=listing, refs=refs,
                    cache_kb=int(docker("exec", name, "du", "-sk", "/data").split()[0]))

    def probe(name, calls):
        result = subprocess.run([str(installed / "python"), "-I", __file__, "--mcp", str(registry), json.dumps(calls)],
                                cwd=outside, env=env, capture_output=True, text=True, timeout=180)
        (evidence / f"mcp-{name}.log").write_text(redact(result.stderr, temporary))
        require(result.returncode == 0, f"MCP session {name} failed; see mcp-{name}.log")
        return json.loads(result.stdout)

    def backups():
        return sorted((checkout / "backups").glob("preupgrade-*.sql.gz"))

    def upgrade(name, *flags, timeout=900):
        return script(checkout / "upgrade.sh", checkout, env, evidence, name, temporary, timeout, *CLIENT, *flags)

    phase("preflight")
    candidate = run("git", "rev-parse", "HEAD", cwd=source, env=env)
    require(candidate == os.environ.get("GITHUB_SHA"), "checkout does not match the workflow commit")
    names = set(docker("container", "ls", "-a", "--format", "{{.Names}}").splitlines())
    require(not names & set(CONTAINERS.values()), "a default Sill container already exists on this host")
    volumes = set(docker("volume", "ls", "--format", "{{.Name}}").splitlines())
    require(not volumes & {f"{PROJECT}_{volume}" for volume in VOLUMES}, "a default Sill volume already exists")
    for port in PORTS:
        with socket.socket() as sock:
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                raise RuntimeError(f"default port {port} is already in use on this host") from None
    # One clone that the person keeps: v0.2.0 first, the candidate later in the same directory.
    run("git", "clone", "--quiet", source, checkout, env=env, timeout=300)
    run("git", "checkout", "--quiet", "--detach", OLD_TAG, cwd=checkout, env=env)
    require(version() == OLD_VERSION, f"{OLD_TAG} does not carry version {OLD_VERSION}")
    registry.write_text(json.dumps({"mcpServers": {"unrelated": UNRELATED}}, indent=2) + "\n")
    report.update(candidate=candidate, old_tag=OLD_TAG, host=dict(os=platform.system(), arch=platform.machine()),
                  engine=dict(version=docker("version", "--format", "{{.Server.Version}}"),
                              compose=docker("compose", "version", "--short")))

    dockerfile = backend / "Dockerfile"
    tagged = dockerfile.read_text()
    dockerfile.write_text(with_archived_sources(tagged))
    report["old_image_build"] = ("package sources pointed at archive.debian.org and apt-archive.postgresql.org; "
                                 "the tagged Dockerfile is restored before the upgrade")

    try:
        phase(f"default {OLD_TAG} install")
        code, text, seconds = script(checkout / "install.sh", checkout,
                                     dict(env, SILL_INSTALL_WAIT_HEALTHY_S="1800"),
                                     evidence, "install-v0.2.0.log", temporary, 3000)
        require(code == 0, f"{OLD_TAG} install.sh exited {code}; see install-v0.2.0.log")
        dockerfile.write_text(tagged)
        require(run("git", "status", "--porcelain", "--untracked-files=no", cwd=checkout, env=env) == "",
                "the v0.2 install left modified tracked files in its checkout")
        require(not env_file.exists() and not env_file.is_symlink(),
                "the default v0.2 install wrote backend/.env; this rehearsal's premise changed")
        require(json.loads(registry.read_text())["mcpServers"] == {"unrelated": UNRELATED, "sill": LEGACY},
                "v0.2 did not leave the bare sill-mcp client entry")
        require(run(installed / "sill-mcp", "--version", cwd=outside, env=env) == f"sill-mcp {OLD_VERSION}",
                "the installed package is not v0.2.0")
        old_runtime, old_embeddings = runtime(), embeddings()
        require("--revision" not in old_runtime["embeddings"]["command"]
                and not any(e.startswith("REVISION=") for e in old_runtime["embeddings"]["environment"]),
                "v0.2 started embeddings with a revision; this rehearsal's premise changed")
        require(old_runtime["db"]["mounts"] == [(f"{PROJECT}_postgres_data", "/var/lib/postgresql/data")]
                and old_runtime["embeddings"]["mounts"] == [(f"{PROJECT}_embeddings_cache", "/data")],
                "the v0.2 data or model cache is not on the default project volumes")
        require(old_embeddings["model_id"] == MODEL, "v0.2 embeddings serve another model")
        revision = cached_revision(old_embeddings["listing"])
        require(psql("SELECT json_agg(migration_id ORDER BY migration_id) FROM schema_migrations") == OLD_LEDGER,
                "the v0.2 store is not at migration 007")
        report["old"] = dict(install_seconds=seconds, runtime=old_runtime, embeddings=old_embeddings,
                             maintenance_worker=old_runtime["maintenance_worker"]["status"],
                             cached_revision=revision, env_file="absent", client_entry=LEGACY)

        phase("ordinary v0.2 use through its MCP server")
        token = uuid4().hex
        repeated = f"Synthetic rehearsal: the green ledger is kept in the hall cupboard ({token})"
        single = f"Synthetic rehearsal: the ferry timetable changes on the first of March ({token})"
        said = []
        for index, args in enumerate((dict(content=repeated), dict(content=repeated),
                                      dict(content=single, type="semantic"))):
            result = probe(f"v0.2-remember-{index + 1}", [dict(tool="remember", args=args)])
            require(result["version"] == OLD_VERSION, "the bare sill-mcp entry did not start v0.2")
            found = re.search(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", result["results"][0])
            require(found, "v0.2 remember did not return a memory id")
            said.append(str(UUID(found.group())))
        marker = said[2]
        require(token in probe("v0.2-recall", [dict(tool="recall", args=dict(query=single, limit=3))])["results"][0],
                "v0.2 could not recall what it had just stored")
        pair = psql("SELECT json_agg(json_build_array(id::text, archived_at IS NULL) ORDER BY created_at, id) "
                    f"FROM public.memories WHERE content = $sill${repeated}$sill$")
        require(len(pair) == 2 and all(live for _, live in pair) and {row[0] for row in pair} == set(said[:2]),
                f"saying one thing twice through v0.2 did not keep two live rows: {pair}")
        keep, newer = pair[0][0], pair[1][0]
        before = store()
        seeds = sum(1 for line in (checkout / "seed/methodology.jsonl").read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith("#"))
        require(seeds and len(before["memories"]) >= seeds + 3,
                "the v0.2 store does not hold the seed pack and the three memories just stored")
        registry_before = registry.read_bytes()
        report["use"] = dict(keep=keep, newer=newer, marker=marker, memories=len(before["memories"]), seeds=seeds,
                             said_in_order=said)

        phase("the candidate checked out in the same directory")
        run("git", "checkout", "--quiet", "--detach", candidate, cwd=checkout, env=env)
        new_version = version()
        ledger = sorted(path.name[:3] for path in (backend / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
        pending = len(ledger) - len(OLD_LEDGER)
        require(new_version != OLD_VERSION and ledger[:len(OLD_LEDGER)] == OLD_LEDGER and pending > 0,
                "the candidate does not extend the v0.2 migration ledger")
        report.update(new_version=new_version, pending=pending)

        def untouched(when):
            require(not env_file.exists() and not env_file.is_symlink(), f"backend/.env exists {when}")
            require(registry.read_bytes() == registry_before, f"the client entry changed {when}")
            require(not backups(), f"a backup was written {when}")
            require(store() == before, f"the store changed {when}")
            require({s: (r["id"], r["status"]) for s, r in runtime().items()}
                    == {s: (r["id"], r["status"]) for s, r in old_runtime.items()},
                    f"a container was replaced, started or stopped {when}")

        phase("dry run: stops on the repeated memories")
        code, text, _ = upgrade("upgrade-1-dry-run.log", "--dry-run")
        require(code != 0 and "--archive-duplicates" in text and "nothing changed" in text
                and f"keep {keep}" in text and newer in text,
                "the dry run did not stop on the repeated pair and name both copies; see upgrade-1-dry-run.log")
        untouched("after the dry run that stopped on repeated memories")

        phase("dry run: stops on the bare client entry")
        code, text, _ = upgrade("upgrade-2-dry-run-archive.log", "--archive-duplicates", "--dry-run")
        require(code != 0 and "--adopt-legacy" in text,
                "the dry run did not stop on the bare sill-mcp entry; see upgrade-2-dry-run-archive.log")
        require("would create an empty private one" in text, "the dry run did not report the absent backend/.env")
        untouched("after the dry run that stopped on the client entry")

        phase("dry run: passes with both choices made")
        code, text, _ = upgrade("upgrade-3-dry-run-both.log", "--adopt-legacy", "--archive-duplicates", "--dry-run")
        require(code == 0 and "DRY: no backup, data, runtime, package or registration changed." in text
                and f"{pending} pending migrations." in text,
                "the full dry run did not pass; see upgrade-3-dry-run-both.log")
        untouched("after the passing dry run")

        phase("the upgrade")
        code, text, seconds = upgrade("upgrade-4.log", "--adopt-legacy", "--archive-duplicates", timeout=3600)
        require(code == 0 and f"Upgrade complete: version {new_version}; {pending} migrations applied." in text,
                f"upgrade.sh exited {code}; see upgrade-4.log")
        require("Created an empty private" in text, "the upgrade did not say it created backend/.env")
        require(stat.S_IMODE(env_file.stat().st_mode) == 0o600 and not env_file.is_symlink(),
                "backend/.env is not a private regular file")
        settings = read_env(env_file.read_text())
        require(settings.get("EMBEDDING_MODEL_ID") == MODEL and settings.get("EMBEDDING_REVISION") == revision,
                "backend/.env does not record the model and the cached snapshot the store was built with")
        after = store()
        require(after["ledger"] == ledger, f"the migration ledger is {after['ledger']}")
        compare_memories(before["memories"], after["memories"], keep, newer)
        require(psql(f"SELECT json_build_array(archive_reason) FROM public.memories WHERE id = '{newer}'")
                == [f"duplicate of {keep}"], "the archived copy does not name the copy that was kept")
        archive, = backups()
        dump = gzip.decompress(archive.read_bytes()).decode("utf-8", "replace")
        require(stat.S_IMODE(archive.stat().st_mode) == 0o600 and keep in dump and newer in dump
                and repeated in dump, "the backup is not private or was not taken before the archive")
        new_runtime, new_embeddings = runtime(settled=True), embeddings()
        require(new_runtime["db"]["id"] == old_runtime["db"]["id"]
                and new_runtime["db"]["image"] == old_runtime["db"]["image"],
                "the upgrade replaced the database container")
        for service in ("db", "embeddings", "rabbitmq"):
            require(new_runtime[service]["mounts"] == old_runtime[service]["mounts"],
                    f"{service} is on another volume after the upgrade")
        require(f"REVISION={revision}" in new_runtime["embeddings"]["environment"]
                and (new_embeddings["model_id"], new_embeddings["model_sha"]) == (MODEL, revision),
                "embeddings are not serving the snapshot the store was built with")
        entries = json.loads(registry.read_text())["mcpServers"]
        entry = entries.get("sill", {})
        require(entries.get("unrelated") == UNRELATED and set(entries) == {"unrelated", "sill"}
                and set(entry) == {"type", "command", "args"} and entry["type"] == "stdio"
                and Path(entry["command"]).resolve() == (installed / "sill-mcp").resolve()
                and entry["args"][0] == "--env-file" and Path(entry["args"][1]).resolve() == env_file.resolve(),
                "the client entry does not name this instance, or an unrelated entry changed")
        worker_version = docker("exec", CONTAINERS["maintenance_worker"], "python", "-c",
                                "from importlib.metadata import version; print(version('sill-memory'))")
        require(run(installed / "sill-mcp", "--version", cwd=outside, env=env) == f"sill-mcp {new_version}"
                and worker_version == new_version, "the installed package or the worker is not the candidate")
        waits = [(int(s), status) for s, status in re.findall(r"^  \[(\d+)s\] embeddings: (.+)$", text, re.M)]
        report["upgrade"] = dict(seconds=seconds, runtime=new_runtime, embeddings=new_embeddings,
                                 embeddings_readiness=waits, backup=archive.name,
                                 cache_growth_kb=new_embeddings["cache_kb"] - old_embeddings["cache_kb"],
                                 env_keys=sorted(settings), ledger=after["ledger"])

        phase("recall of v0.2 memories through the adopted entry")
        result = probe("recall-old", [dict(tool="recall", args=dict(query=single, limit=5)),
                                      dict(tool="recall", args=dict(query=repeated, limit=5))])
        require((result["server"], result["version"]) == ("sill", new_version),
                "the adopted client entry did not start the candidate")
        require(isinstance(result["instructions"], str) and "recall" in result["instructions"],
                "the server sent no usage instructions at initialize")
        top = json.loads(result["results"][0])["memories"]
        require(top and (top[0]["id"], top[0]["content"]) == (marker, single),
                "a memory stored under v0.2 is not the first result for its own words")
        same = [memory["id"] for memory in json.loads(result["results"][1])["memories"]
                if memory["content"] == repeated]
        require(same == [keep], f"recall of the repeated content returned {same}, not the one kept copy")
        fresh = f"Synthetic rehearsal: the upgraded store took a new memory ({token})"
        made = str(UUID(json.loads(probe("remember-new", [dict(tool="remember",
                                                              args=dict(content=fresh, speaker=SPEAKER))])["results"][0])))
        found, = json.loads(probe("recall-new", [dict(tool="recall", args=dict(
            query=fresh, speaker=SPEAKER, limit=1, include_partial=False))])["results"][0])["memories"]
        require((found["id"], found["content"], found["speaker"]) == (made, fresh, SPEAKER),
                "a memory stored after the upgrade did not come back from a new process")
        report["recall"] = dict(old_score=top[0].get("score"), instructions_chars=len(result["instructions"]),
                                tools=len(result["tools"]), new_memory=made)

        phase("a second upgrade finds nothing to do")
        settled, env_bytes, registry_after = store(), env_file.read_bytes(), registry.read_bytes()
        code, text, seconds = upgrade("upgrade-5-repeat.log", timeout=1800)
        require(code == 0 and "0 pending migrations." in text
                and f"Upgrade complete: version {new_version}; 0 migrations applied." in text,
                f"the repeated upgrade exited {code}; see upgrade-5-repeat.log")
        require(store() == settled and env_file.read_bytes() == env_bytes
                and registry.read_bytes() == registry_after
                and runtime(settled=True)["db"]["id"] == old_runtime["db"]["id"],
                "the repeated upgrade changed the store, backend/.env, the client entry or the database container")
        report["repeat"] = dict(seconds=seconds, backups=[path.name for path in backups()])
    finally:
        # Never remove resources. The runner tears down its own daemon.
        for service, name in CONTAINERS.items():
            logs = subprocess.run(["docker", "logs", "--timestamps", name], cwd=outside, env=env,
                                  capture_output=True, text=True, errors="replace", timeout=30)
            if logs.returncode == 0:
                (evidence / f"container-{service}.log").write_text(redact(logs.stdout + logs.stderr, temporary))


def write_report(evidence, report):
    (evidence / "evidence.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main(evidence):
    evidence.mkdir(parents=True, exist_ok=False)
    report = dict(result="failed", phase="guard")
    write_report(evidence, report)
    with tempfile.TemporaryDirectory(prefix="sill-v02-upgrade-") as tmp:
        try:
            rehearse(Path(__file__).resolve().parents[2], Path(tmp).resolve(), evidence, report)
            report.update(result="passed", phase="complete")
        except Exception as exc:
            report["failure"] = redact(f"{type(exc).__name__}: {exc}", Path(tmp).resolve())
            print(report["failure"], file=sys.stderr)
        finally:
            write_report(evidence, report)
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    if sys.argv[1] == "--mcp":
        print(json.dumps(asyncio.run(mcp_calls(Path(sys.argv[2]), json.loads(sys.argv[3])))))
    else:
        raise SystemExit(main(Path(sys.argv[1]).resolve()))
