"""The worker's mode surface. No DB, no docker — argparse only."""

import importlib
import re
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
WORKER = BACKEND_ROOT / "worker.py"
COMPOSE = BACKEND_ROOT / "docker-compose.yml"
RABBIT_BRIDGE = BACKEND_ROOT / "rabbit_bridge.py"
PYPROJECT = BACKEND_ROOT / "pyproject.toml"
DOCKERFILE_WORKER = BACKEND_ROOT / "Dockerfile.worker"
EXTENDING_DOC = BACKEND_ROOT.parent / "docs" / "extending.md"
MCP_SERVER = BACKEND_ROOT / "sill_mcp_server.py"
COGNITIVE_API = BACKEND_ROOT / "cognitive_memory_api.py"


def test_heartbeat_mode_is_gone_from_choices():
    src = WORKER.read_text()
    m = re.search(r'"--mode".*?choices=\[(.*?)\]', src, re.S)
    assert m, "could not find the --mode choices list"
    choices = m.group(1)
    assert "heartbeat" not in choices, "the dreaming heartbeat mode still ships"
    assert '"both"' not in choices, "the 'both' mode implies heartbeat"
    assert "maintenance" in choices


def test_beat_mode_is_present_in_choices():
    src = WORKER.read_text()
    m = re.search(r'"--mode".*?choices=\[(.*?)\]', src, re.S)
    assert m, "could not find the --mode choices list"
    choices = m.group(1)
    assert "beat" in choices, "the beat mode is not wired into --mode choices"


def test_compose_has_no_heartbeat_service():
    text = COMPOSE.read_text()
    assert "heartbeat_worker" not in text
    assert "profiles:" not in text or "heartbeat" not in text


@pytest.mark.parametrize("doc", ["README.md", "docs/concepts.md"])
def test_docs_do_not_advertise_heartbeat(doc):
    p = Path(__file__).resolve().parents[2] / doc
    text = p.read_text().lower()
    assert "heartbeat_worker" not in text
    assert "heartbeat profile" not in text


# --- Task 6: heartbeat residue ---------------------------------------------
#
# Task 1's review reported HeartbeatWorker (~1,866 lines) as "confirmed
# inert" and recommended deleting it outright. That was false: worker.py's
# MaintenanceWorker instantiates it at three call sites to reuse its
# RabbitMQ bridge (ensure_rabbitmq_ready / publish_outbox_messages /
# poll_inbox_messages), and the maintenance_worker Compose service ships
# with RABBITMQ_ENABLED=1 by default. So the bridge is extracted into
# backend/rabbit_bridge.py first, and only the LLM-decision residue around
# it is deleted.

def test_rabbitmq_bridge_does_not_borrow_from_a_retired_class():
    src = WORKER.read_text()
    assert "HeartbeatWorker(" not in src, (
        "the maintenance worker still instantiates the retired heartbeat class; "
        "extract the RabbitMQ bridge instead")
    assert "class HeartbeatWorker" not in src


def test_llm_provider_plumbing_is_gone():
    src = WORKER.read_text()
    for marker in ("import openai", "import anthropic", "cognitive core of an autonomous"):
        assert marker not in src, f"heartbeat-era residue survives: {marker}"


def test_heartbeat_logger_name_is_gone():
    src = WORKER.read_text()
    assert "heartbeat_worker" not in src, (
        "worker.py's module-level logger is still named for the retired class")


def test_rabbit_bridge_module_defines_the_bridge_class():
    assert RABBIT_BRIDGE.exists(), "backend/rabbit_bridge.py was not created"
    src = RABBIT_BRIDGE.read_text()
    assert "class RabbitBridge" in src
    for method in ("ensure_rabbitmq_ready", "publish_outbox_messages", "poll_inbox_messages"):
        assert f"def {method}" in src, f"RabbitBridge is missing {method}"
    # The bridge must not drag any heartbeat-era LLM plumbing along with it.
    for marker in ("import openai", "import anthropic", "class HeartbeatWorker"):
        assert marker not in src


def test_worker_imports_the_extracted_bridge():
    src = WORKER.read_text()
    assert "rabbit_bridge" in src, "worker.py does not import the extracted bridge module"


def test_maintenance_worker_imports_and_constructs_cleanly():
    """The correction this task exists to satisfy: the maintenance path must
    still import and construct without a live DB or RabbitMQ connection."""
    worker = importlib.import_module("worker")
    importlib.reload(worker)
    mw = worker.MaintenanceWorker()
    assert mw is not None
    rabbit_bridge = importlib.import_module("rabbit_bridge")
    bridge = rabbit_bridge.RabbitBridge(pool=None)
    assert bridge.pool is None
    assert bridge._last_rabbit_inbox_poll == 0.0


def test_pyproject_has_no_heartbeat_extra():
    text = PYPROJECT.read_text()
    assert "heartbeat = [" not in text, (
        "the orphaned 'heartbeat' optional-dependency extra is still declared")


def test_pyproject_packages_rabbit_bridge():
    text = PYPROJECT.read_text()
    m = re.search(r"py-modules\s*=\s*\[(.*?)\]", text, re.S)
    assert m, "could not find the py-modules list in pyproject.toml"
    assert '"rabbit_bridge"' in m.group(1), (
        "rabbit_bridge is importable but not packaged — pipx/Docker installs "
        "would silently miss it (see commit 8be59b4 for the beat_worker precedent)")


def test_dockerfile_worker_copies_rabbit_bridge():
    text = DOCKERFILE_WORKER.read_text()
    assert "rabbit_bridge.py" in text or "COPY *.py /app/" in text, (
        "Dockerfile.worker's COPY line does not carry rabbit_bridge.py into the image")


def test_extending_doc_drops_heartbeat_container_row():
    text = EXTENDING_DOC.read_text()
    assert "SILL_HEARTBEAT_WORKER_CONTAINER" not in text, (
        "extending.md still documents an override for a service that no longer exists")


def test_mcp_tool_description_drops_heartbeat_language():
    text = MCP_SERVER.read_text()
    assert "Request the heartbeat to explore" not in text
    assert "Request a background exploration of a specific query." in text


def test_cognitive_api_docstring_drops_heartbeat_language():
    text = COGNITIVE_API.read_text()
    assert "heartbeat explore" not in text.lower()


def test_once_is_rejected_in_maintenance_mode(monkeypatch, capsys):
    import worker

    def unexpected_maintenance(mode):
        pytest.fail(f"worker launched maintenance in mode {mode!r}")

    monkeypatch.setattr(worker, "_amain", unexpected_maintenance)
    monkeypatch.setattr(sys, "argv", ["sill-worker", "--mode", "maintenance", "--once"])
    with pytest.raises(SystemExit) as exc:
        worker.main()
    assert exc.value.code == 2
    assert "--once requires --mode beat" in capsys.readouterr().err
