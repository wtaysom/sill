"""Cold Sill installer acceptance on a disposable GitHub-hosted Linux amd64 VM."""
import asyncio
from collections import Counter
import hashlib
import json
import math
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
import tomllib
from uuid import UUID, uuid4

MODEL = "unsloth/embeddinggemma-300m"
REVISION = "bfa3c846ac738e62aa61806ef9112d34acb1dc5a"
IMAGE = ("ghcr.io/huggingface/text-embeddings-inference@"
         "sha256:8de25e75ce39617f17f2f6c77d60a4f75b65e779ed5005420eb1400072a15c1c")
BUDGET = 1800
SERVICES = {"db", "embeddings", "rabbitmq", "maintenance_worker"}
VOLUMES = {"postgres_data", "embeddings_cache", "rabbitmq_data"}
NETWORKS = {"private", "public"}
BINDINGS = {"db": {5432: "POSTGRES_PORT"},
            "rabbitmq": {5672: "RABBITMQ_PORT", 15672: "RABBITMQ_MANAGEMENT_PORT"}}
MOUNTS = {"db": ("postgres_data", "/var/lib/postgresql/data"),
          "embeddings": ("embeddings_cache", "/data"), "rabbitmq": ("rabbitmq_data", "/var/lib/rabbitmq")}
LABEL = "com.docker.compose."
CODEX_CONFIG = 'model = "synthetic"\n\n[mcp_servers.unrelated]\ncommand = "/synthetic/unrelated"\n'
CLAUDE_CONFIG = '{"mcpServers":{"unrelated":{"command":"/synthetic/unrelated"}}}\n'
SPEAKER = "Native CI synthetic speaker"
PRODUCTION = {"made": [{"kind": "person", "who": SPEAKER, "work": "synthetic probe", "at": [],
                        "declared": {"by": None, "when": "synthetic", "basis": "stated"}}]}
PROFILE = dict(model_id=MODEL, revision=REVISION, dimension=768, normalization="l2",
               query_prefix="task: search result | query: ", document_prefix="title: none | text: ")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args, cwd=None, env=None, timeout=45):
    result = subprocess.run(args, cwd=cwd, env=env, text=True, errors="replace",
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    require(result.returncode == 0,
            f"{Path(args[0]).name} {args[1]} exited {result.returncode}: {result.stderr[-1500:]}")
    return result.stdout.strip()


def object_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def redact(value, temporary):
    return value.replace(str(temporary), "<fixture>").replace("sill_password", "<fixture-password>")


def loopback_ports():
    # Keep sockets open together so all three selected ports are distinct.
    with socket.socket() as db, socket.socket() as amqp, socket.socket() as management:
        sockets = (db, amqp, management)
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return dict(zip(("POSTGRES_PORT", "RABBITMQ_PORT", "RABBITMQ_MANAGEMENT_PORT"),
                        (sock.getsockname()[1] for sock in sockets)))


def schedule_state(home, env):
    unit = "sill-beat-worker.service"
    directories = [home / ".config/systemd/user", home / ".local/share/systemd/user"]
    directories += [Path("/etc/systemd") / scope for scope in ("system", "user")]
    require(not any(any(path.rglob(unit)) for path in directories), "beat systemd unit exists")
    if shutil.which("crontab", path=env["PATH"]):
        cron = subprocess.run(["crontab", "-l"], env=env, capture_output=True, text=True, timeout=15)
        require(cron.returncode == 0 or (cron.returncode == 1 and "no crontab for" in cron.stderr),
                "could not determine runner crontab")
        require("sill-worker" not in cron.stdout and "sill-beat" not in cron.stdout,
                "beat cron entry exists")
        return object_digest(cron.stdout)
    return "crontab-unavailable"


def install(checkout, cwd, env, evidence, name, timeout, *flags):
    log = cwd / name
    started = time.monotonic()
    with log.open("w") as stream:
        with subprocess.Popen([checkout / "install.sh", *flags], cwd=cwd, env=env,
                              stdout=stream, stderr=subprocess.STDOUT, start_new_session=True) as proc:
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise
            finally:
                evidence.joinpath(name).write_text(redact(log.read_text(errors="replace"), cwd.parent))
    require(code == 0, f"install.sh exited {code}; see {name}")
    return round(time.monotonic() - started, 1)


def readiness(log):
    core_wait = log.split("Step 4/10:", 1)[1].split("Embedding profile active:", 1)[0]
    states = {service: (int(seconds), status) for seconds, service, status in
              re.findall(r"^  \[(\d+)s\] (\w+): (.+)$", core_wait, re.M)}
    require(set(states) == SERVICES - {"maintenance_worker"}
            and all(seconds <= BUDGET and status == "healthy" for seconds, status in states.values()),
            "cold install did not report healthy core services within the managed readiness budget")
    return {service: seconds for service, (seconds, _) in states.items()}


async def mcp_probe(mode, config, token, version):
    # This mode runs under the installed venv's Python, outside the checkout.
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    entry = tomllib.loads(config.read_text())["mcp_servers"]["sill"]
    server = StdioServerParameters(command=entry["command"], args=entry["args"], env=dict(os.environ))

    async def call(session, tool, args):
        result = await session.call_tool(tool, args)
        require(not result.isError and len(result.content) == 1 and result.content[0].type == "text",
                f"MCP {tool} did not return a successful text result")
        raw = result.content[0].text
        require(not raw.startswith("Error:"), f"MCP {tool}: {raw}")
        return json.loads(raw)

    content = f"Synthetic blue umbrella rehearsal {token}"
    async with asyncio.timeout(75), stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            init = await session.initialize()
            require((init.serverInfo.name, init.serverInfo.version) == ("sill", version),
                    f"unexpected MCP initialize identity: {init.serverInfo}")
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            require({"remember", "recall"} <= tools.keys() and "stage" not in tools,
                    "MCP discovery did not expose remember/recall with stage disabled")
            require("reciprocal-rank-fusion" in (tools["recall"].description or "").lower(),
                    "MCP discovery no longer documents the RRF score")
            evidence = dict(server_version=init.serverInfo.version, protocol=init.protocolVersion,
                            stage_discovered=False)
            if mode == "remember":
                memory_id = await call(session, "remember",
                                       dict(content=content, speaker=SPEAKER, production=PRODUCTION))
                evidence["id"] = str(UUID(memory_id))
            else:
                result = await call(session, "recall",
                                    dict(query=content, speaker=SPEAKER, limit=1, include_partial=False))
                memory, = result["memories"]
                require(memory["content"] == content and memory["speaker"] == SPEAKER
                        and memory["production"] == PRODUCTION, "recalled content or provenance changed")
                require("similarity" not in memory and math.isclose(memory["score"], 2 / 61),
                        "expected the top result in both rankings to have RRF score 2/61")
                evidence.update(id=memory["id"], content=memory["content"], speaker=memory["speaker"],
                                production=memory["production"], score=memory["score"])
    # stdio_client has closed the server before handing the result to the caller.
    return evidence


def owned_resources(docker, backend, project, *, complete):
    owned = {}
    for kind, keys, name_field, id_field, key_label in (
        ("container", SERVICES, "Names", "ID", "service"),
        ("volume", VOLUMES, "Name", "Name", "volume"),
        ("network", NETWORKS, "Name", "ID", "network"),
    ):
        flags = {"container": ("-a", "--no-trunc"), "network": ("--no-trunc",), "volume": ()}
        rows = docker(kind, "ls", *flags[kind], "--filter", f"label={LABEL}project={project}",
                      "--format", "{{json .}}")
        by_name = {f"{project}_{key}": key for key in keys}
        for line in rows.splitlines():
            row = json.loads(line)
            name = row[name_field]
            require(name in by_name and name not in owned, f"unexpected {kind} in fixture project: {name}")
            key = by_name[name]
            info, = json.loads(docker(kind, "inspect", row[id_field]))
            labels = info["Config"]["Labels"] if kind == "container" else info["Labels"]
            require(labels.get(LABEL + "project") == project and labels.get(LABEL + key_label) == key,
                    f"{kind} does not have matching fixture ownership labels: {name}")
            if kind == "container":
                require(info["Name"] == "/" + name
                        and labels.get(LABEL + "project.config_files") == str(backend / "docker-compose.yml")
                        and labels.get(LABEL + "project.working_dir") == str(backend),
                        f"container belongs to another Compose checkout: {name}")
            else:
                require(info["Name"] == name, f"{kind} resolved to another resource: {name}")
            owned[name] = info
    if complete:
        require(len(owned) == len(SERVICES | VOLUMES | NETWORKS), "some fixture resources are missing")
    return {name.removeprefix(project + "_"): info for name, info in owned.items()}


def preflight(docker, project):
    for kind, keys, args, field in (
        ("container", SERVICES, ("-a",), "Names"),
        ("volume", VOLUMES, (), "Name"),
        ("network", NETWORKS, (), "Name"),
    ):
        names = docker(kind, "ls", *args, "--format", "{{." + field + "}}").splitlines()
        require(not {f"{project}_{key}" for key in keys}.intersection(names),
                f"fixture {kind} name already exists")
        require(not docker(kind, "ls", *args, "--filter", f"label={LABEL}project={project}", "-q"),
                f"fixture project label already exists on a {kind}")


def snapshot(docker, owned, checkout, pack):
    def db(sql):
        return json.loads(docker("exec", owned["db"]["Id"], "psql", "-h", "127.0.0.1",
                                "-XAt", "-v", "ON_ERROR_STOP=1", "-U", "sill", "-d", "sill", "-c", sql))

    ledger = db("SELECT json_agg(migration_id ORDER BY migration_id) FROM ag_catalog.schema_migrations WHERE source='initdb'")
    require(ledger == [f"{i:03}" for i in range(1, 16)]
            and db("SELECT count(*) FROM ag_catalog.schema_migrations") == len(ledger),
            "unexpected migration ledger")
    profile = db("""SELECT json_build_object('profile',profile,'provenance',provenance,
                     'fingerprint',public.embedding_profile_fingerprint(profile))
                     FROM public.embedding_regime WHERE singleton""")
    require(profile["profile"] == PROFILE
            and profile["provenance"]["historical_identity"] == "declared-unverified",
            "requested profile or historical provenance differs")
    operations = db("""SELECT json_agg(json_build_object('id',id,'state',state,'source',source_profile,
                        'target',target_profile,'backup_transport',backup->>'transport',
                        'backup_verification',backup->>'verification')) FROM public.embedding_operations""")
    operation, = operations
    source = dict(PROFILE, query_prefix="", document_prefix="")
    require(operation["state"] == "active" and operation["source"] == source and operation["target"] == PROFILE
            and operation["backup_transport"] == "explicit-container"
            and operation["backup_verification"].startswith("pg_restore --list;"), "profile operation differs")
    path = checkout / "backend/.env.embedding-profile.json"
    raw = path.read_bytes()
    receipt = json.loads(raw)
    identity = db("""SELECT json_build_array(system_identifier::text,current_database(),
                      (SELECT oid::text FROM pg_database WHERE datname=current_database()))
                      FROM pg_catalog.pg_control_system()""")
    volume = owned["postgres_data"]
    require(stat.S_IMODE(path.stat().st_mode) == 0o600 and receipt["prepare_started"]
            and receipt["operation_id"] == operation["id"] and receipt["database_identity"] == identity
            and receipt["selection"]["source"] == source and receipt["selection"]["target"] == PROFILE
            and receipt["selection"]["fingerprint"] == profile["fingerprint"]
            and receipt["volume_identity"] == dict(name=volume["Name"], created_at=volume["CreatedAt"],
                                                  nonce=volume["Labels"]["org.sill.fresh-profile-intent"])
            and receipt["nonce"] == receipt["volume_identity"]["nonce"]
            and any(mount["Name"] == volume["Name"] and mount["Destination"] == "/var/lib/postgresql/data"
                    for mount in owned["db"]["Mounts"] if mount["Type"] == "volume"),
            "creation receipt does not match the operation, profile, database and owned volume")
    rows = db("""SELECT json_agg(json_build_array(id, content, source_attribution, speaker,
                       archived_at, vector_dims(embedding)) ORDER BY id) FROM public.memories
                 WHERE source_attribution->>'author'='sill-seed-v0.1'""")
    require(Counter((row[1], json.dumps(row[2], sort_keys=True)) for row in rows) ==
            Counter((row["content"], json.dumps(row["source_attribution"], sort_keys=True)) for row in pack)
            and all(row[3:] == [None, None, 768] for row in rows),
            "methodology seed rows, attribution, or vector shape differ from the pack")
    return dict(ledger=ledger, profile=profile, operation=operation, database_identity=identity,
                receipt_sha256=hashlib.sha256(raw).hexdigest(), seed_count=len(rows),
                seed_rows_sha256=object_digest(rows))


def runtime_evidence(docker, owned, project, ports, installed, checkout, outside, env, version):
    containers = {name: owned[name] for name in SERVICES}
    for service, (volume, destination) in MOUNTS.items():
        mounts = containers[service]["Mounts"]
        require(any(m["Type"] == "volume" and m["Name"] == owned[volume]["Name"]
                    and m["Destination"] == destination for m in mounts), f"wrong volume on {service}")
    for name, info in containers.items():
        published = {port: values for port, values in (info["NetworkSettings"]["Ports"] or {}).items() if values}
        expected = {f"{port}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(ports[key])}]
                    for port, key in BINDINGS.get(name, {}).items()}
        networks = info["NetworkSettings"]["Networks"]
        require(published == expected and {entry["NetworkID"] for entry in networks.values()}
                == {owned[key]["Id"] for key in NETWORKS}, f"fixture ports or networks differ for {name}")
        require(info["State"]["Status"] == "running" and
                (name == "maintenance_worker" or info["State"]["Health"]["Status"] == "healthy"),
                f"{name} is not running/healthy")
    tei = containers["embeddings"]
    images = {}
    for name, info in containers.items():
        image, = json.loads(docker("image", "inspect", info["Image"]))
        require(image["Os"] == "linux" and image["Architecture"] == "amd64",
                f"{name} image is not native Linux amd64")
        images[name] = dict(id=image["Id"], repo_digests=image["RepoDigests"] or [])
    require(tei["Config"]["Image"] == IMAGE and IMAGE in images["embeddings"]["repo_digests"],
            "embeddings container did not run the pinned TEI image")
    info = json.loads(docker("exec", tei["Id"], "curl", "-fsS", "http://localhost:80/info"))
    require(info.get("model_id") == MODEL and info.get("model_sha") == REVISION, "wrong live TEI model/revision")
    vector, = json.loads(docker("exec", tei["Id"], "curl", "-fsS", "http://localhost:80/embed",
                               "-H", "Content-Type: application/json", "-d",
                               json.dumps({"inputs": PROFILE["document_prefix"] + "synthetic finite vector", "normalize": True})))
    require(len(vector) == 768 and all(math.isfinite(x) for x in vector), "TEI did not return 768 finite values")
    norm = math.sqrt(sum(x*x for x in vector))
    require(math.isclose(norm, 1, abs_tol=1e-4), "TEI embedding was not L2-normalized")
    worker = containers["maintenance_worker"]
    version_probe = "from importlib.metadata import version; print(version('sill-memory'))"
    versions = dict(package=run(installed / "python", "-I", "-c", version_probe, cwd=outside, env=env),
                    command=run(installed / "sill-mcp", "--version", cwd=outside, env=env),
                    plugin=json.loads((checkout / "plugin/plugin.json").read_text())["version"],
                    worker=docker("exec", worker["Id"], "python", "-c", version_probe))
    require(versions == dict(package=version, command=f"sill-mcp {version}", plugin=version, worker=version)
            and worker["Config"]["Cmd"] == ["sill-worker", "--mode", "maintenance"]
            and docker("image", "inspect", "--format", "{{.Id}}", f"{project}-maintenance_worker") == worker["Image"],
            "candidate version, running worker image or worker mode differs")
    return dict(images=images, model_id=MODEL, model_revision=REVISION, dimension=len(vector),
                finite=True, l2_norm=round(norm, 6), versions=versions)


def rehearse(source, temporary, evidence, report):
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
            "use a disposable GitHub-hosted runner")
    require(platform.system() == "Linux" and platform.machine() == "x86_64", "native Linux x86_64 host required")
    home = temporary / "private home"
    checkout = temporary / "committed checkout"
    outside = temporary / "other cwd"
    for path in (home, checkout, outside, home / ".codex", home / ".claude"):
        path.mkdir(mode=0o700)
    env = dict(HOME=str(home), PATH=f"{Path(sys.executable).parent}:/usr/bin:/bin", LANG="C.UTF-8",
               DOCKER_HOST="unix:///var/run/docker.sock", PIP_CONFIG_FILE=os.devnull)
    require(not any(shutil.which(name, path=env["PATH"]) for name in ("pipx", "claude", "codex")),
            "expected the installer venv path without native client binaries")
    config = home / ".codex/config.toml"
    claude = home / ".claude.json"
    config.write_text(CODEX_CONFIG)
    claude.write_text(CLAUDE_CONFIG)
    archive = temporary / "head.tar"
    commit = run("git", "rev-parse", "HEAD", cwd=source, env=env)
    require(commit == os.environ.get("GITHUB_SHA"), "checkout does not match the workflow commit")
    run("git", "archive", f"--output={archive}", commit, cwd=source, env=env)
    run("tar", "-xf", archive, "-C", checkout, env=env)
    backend = checkout / "backend"
    version = tomllib.loads((backend / "pyproject.toml").read_text())["project"]["version"]
    project = f"sillnative_{uuid4().hex[:16]}"
    ports = loopback_ports()

    def docker(*args, **kwargs):
        return run("docker", *args, cwd=outside, env=env, **kwargs)

    engine_os, engine_arch = docker("info", "--format", "{{.OSType}} {{.Architecture}}").split()
    require(engine_os == "linux" and engine_arch in {"amd64", "x86_64"}, "native Linux amd64 engine required")
    report.update(commit=commit, expected_version=version, host=dict(os=platform.system(), arch=platform.machine()),
                  engine=dict(os=engine_os, arch=engine_arch, endpoint=env["DOCKER_HOST"],
                              version=docker("version", "--format", "{{.Server.Version}}"),
                              compose=docker("compose", "version", "--short")),
                  project=project, ports=ports, bind="127.0.0.1", readiness_budget_seconds=BUDGET)
    env_file = backend / ".env"
    settings = dict(COMPOSE_PROJECT_NAME=project, SILL_BIND_ADDR="127.0.0.1", **ports,
                    EMBEDDING_IMAGE=IMAGE, EMBEDDING_PLATFORM="linux/amd64", EMBEDDING_MODEL_ID=MODEL,
                    EMBEDDING_REVISION=REVISION, EMBEDDING_DIMENSION=768, SILL_INSTALL_WAIT_HEALTHY_S=BUDGET)
    for service in SERVICES:
        settings[f"SILL_{service.upper()}_CONTAINER"] = f"{project}_{service}"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in sorted(settings.items())))
    env_file.chmod(0o600)
    compose = ("compose", "--project-directory", backend, "--env-file", env_file, "-f", backend / "docker-compose.yml",
               "config")
    services = sorted(docker(*compose, "--services").splitlines())
    resolved = json.loads(docker(*compose, "--format", "json"))
    require(set(services) == SERVICES and resolved["name"] == project, "unexpected resolved Compose project/services")
    for kind, keys in (("volumes", VOLUMES), ("networks", NETWORKS)):
        require(set(resolved[kind]) == keys and all(
            resolved[kind][key]["name"] == f"{project}_{key}" and not resolved[kind][key].get("external")
            and not resolved[kind][key].get("driver_opts") for key in keys), f"unexpected {kind} in Compose config")
    for service, config_service in resolved["services"].items():
        expected_ports = {("127.0.0.1", target, str(ports[key]), "tcp")
                          for target, key in BINDINGS.get(service, {}).items()}
        actual_ports = {(port.get("host_ip"), port["target"], str(port["published"]), port["protocol"])
                        for port in config_service.get("ports", [])}
        expected_mounts = {("volume", *MOUNTS[service])} if service in MOUNTS else set()
        actual_mounts = {(mount["type"], mount["source"], mount["target"])
                         for mount in config_service.get("volumes", [])}
        require(config_service["container_name"] == f"{project}_{service}" and actual_ports == expected_ports
                and actual_mounts == expected_mounts, f"unexpected container name, ports or mounts for {service}")
    require(resolved["networks"]["private"]["internal"], "private Compose network is not internal")
    preflight(docker, project)
    report["preflight"] = dict(services=services, containers=sorted(f"{project}_{s}" for s in SERVICES),
                               absent_volumes=sorted(f"{project}_{v}" for v in VOLUMES),
                               absent_networks=sorted(f"{project}_{n}" for n in NETWORKS),
                               model_cache="absent before install", embedding_platform="linux/amd64",
                               requested_image=IMAGE, requested_model=MODEL, requested_revision=REVISION)
    pack = [json.loads(line) for line in (checkout / "seed/methodology.jsonl").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    require(bool(pack), "methodology seed pack is empty")
    initial_schedule = schedule_state(home, env)
    installed = home / ".local/share/sill-venv/bin"

    def phase(name):
        report["phase"] = name
        write_report(evidence, report)
        print(f"Native Sill runtime: {name}", flush=True)

    def registrations():
        after = tomllib.loads(config.read_text())
        expected = tomllib.loads(CODEX_CONFIG)
        expected["mcp_servers"]["sill"] = dict(command=str(installed / "sill-mcp"),
                                               args=["--env-file", str(env_file)])
        require(after == expected and claude.read_text() == CLAUDE_CONFIG, "client registration or unrelated entries differ")
        return config.read_bytes()

    token = uuid4().hex

    def probe(mode):
        with (evidence / f"mcp-{mode}.log").open("w") as log:
            result = subprocess.run([installed / "python", "-I", __file__, "--mcp", mode, config, token, version],
                                    cwd=outside, env=env, capture_output=True, text=True, timeout=90)
            log.write(redact(result.stderr, temporary))
        require(result.returncode == 0, f"installed MCP {mode} failed; see mcp-{mode}.log")
        return json.loads(result.stdout)

    def owned():
        return owned_resources(docker, backend, project, complete=True)

    try:
        phase("cold prompted install")
        report["cold_install_seconds"] = install(checkout, outside, env, evidence, "install-cold.log", BUDGET + 1100,
                                                "--embedding-profile", "embeddinggemma-search-v1", "--client", "codex")
        report["cold_readiness_seconds"] = readiness((evidence / "install-cold.log").read_text())
        resources = owned()
        report["cold_store"] = snapshot(docker, resources, checkout, pack)
        report["runtime"] = runtime_evidence(docker, resources, project, ports, installed, checkout, outside, env, version)
        registered = registrations()
        require(schedule_state(home, env) == initial_schedule, "cold install changed the runner schedule")
        report["remember"] = probe("remember")

        phase("default raw rerun")
        report["rerun_seconds"] = install(checkout, outside, env, evidence, "install-rerun.log", 360, "--client", "codex")
        resources = owned()
        require(snapshot(docker, resources, checkout, pack) == report["cold_store"],
                "default raw rerun changed the profile, creation receipt, operations, ledger or seed rows")
        rerun = runtime_evidence(docker, resources, project, ports, installed, checkout, outside, env, version)
        require(rerun["images"]["embeddings"] == report["runtime"]["images"]["embeddings"],
                "default raw rerun changed the TEI image")
        report["rerun_runtime"] = rerun
        require(registered == registrations(), "Codex registration changed on rerun")
        require(schedule_state(home, env) == initial_schedule, "rerun changed the runner schedule")
        summary = f"seed_import: inserted=0, skipped={len(pack)}, errors=0"
        require(summary in (evidence / "install-rerun.log").read_text(), "installer did not skip the full seed pack")
        report.update(seed_rerun=summary, client_config="temporary Codex registration; unrelated entries preserved",
                      beat_schedule="absent", default_rerun_preserved=True)

        phase("recall in a new MCP process")
        report["recall"] = probe("recall")
        require(report["recall"]["id"] == report["remember"]["id"], "recall returned another UUID")
    finally:
        # Never remove resources. The runner tears down its own daemon.
        try:
            resources = owned_resources(docker, backend, project, complete=False)
            report["owned_resources"] = {
                key: info["Id"] if "Id" in info else {"name": info["Name"], "created_at": info["CreatedAt"]}
                for key, info in sorted(resources.items())
            }
            for service in sorted(SERVICES.intersection(resources)):
                logs = subprocess.run(["docker", "logs", "--timestamps", resources[service]["Id"]],
                                      cwd=outside, env=env, capture_output=True, text=True, errors="replace", timeout=20)
                (evidence / f"container-{service}.log").write_text(redact(logs.stdout + logs.stderr, temporary))
                require(logs.returncode == 0, f"could not collect owned {service} logs")
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            report["collection_failure"] = redact(str(exc), temporary)


def write_report(evidence, report):
    (evidence / "evidence.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main(evidence):
    evidence.mkdir(parents=True, exist_ok=False)
    report = dict(result="failed", phase="preflight")
    write_report(evidence, report)
    with tempfile.TemporaryDirectory(prefix="sill-native-") as tmp:
        try:
            rehearse(Path(__file__).resolve().parents[2], Path(tmp), evidence, report)
            require("collection_failure" not in report, "runtime checks passed but owned logs could not be collected")
            report.update(result="passed", phase="complete")
        except Exception as exc:
            report["failure"] = redact(f"{type(exc).__name__}: {exc}", Path(tmp))
            print(report["failure"], file=sys.stderr)
        finally:
            write_report(evidence, report)
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    if sys.argv[1] == "--mcp":
        _, _, mode, config_path, nonce, expected_version = sys.argv
        print(json.dumps(asyncio.run(mcp_probe(mode, Path(config_path), nonce, expected_version))))
    else:
        raise SystemExit(main(Path(sys.argv[1]).resolve()))
