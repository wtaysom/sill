"""The Compose services default to automatic restart and share one override."""

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
EXPECTED_SERVICES = {"db", "embeddings", "rabbitmq", "maintenance_worker"}


def test_all_compose_services_use_the_same_restart_policy():
    text = COMPOSE.read_text(encoding="utf-8")
    # Read the top-level services block, then match only service keys and
    # their direct properties. Nested volumes and depends_on are indented.
    services = re.search(r"(?ms)^services:[ \t]*\n(.*?)(?=^[^\s#]|\Z)", text)
    assert services is not None
    blocks = re.split(r"(?m)^  ([A-Za-z0-9_.-]+):(?=[ \t]|$)", services.group(1))
    declared = dict(zip(blocks[1::2], blocks[2::2]))
    assert EXPECTED_SERVICES <= declared.keys()
    for name, body in declared.items():
        policies = re.findall(r"(?m)^    restart:[ \t]*(\S.*)$", body)
        assert policies == ["${SILL_RESTART_POLICY:-unless-stopped}"], name
