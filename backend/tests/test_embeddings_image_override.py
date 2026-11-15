"""Direct Compose has an immutable amd64 default and permits paired overrides."""

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
ENV_EXAMPLE = COMPOSE.with_name(".env.example")
AMD = "ghcr.io/huggingface/text-embeddings-inference@sha256:8de25e75ce39617f17f2f6c77d60a4f75b65e779ed5005420eb1400072a15c1c"
ARM = "ghcr.io/huggingface/text-embeddings-inference@sha256:798cd7615e060a72ccbdf19aa0ada52289c6a0147490195bafaece6ce04d70f1"


def test_direct_compose_uses_an_immutable_amd64_default():
    body = COMPOSE.read_text()
    assert re.search(r"^    image: \${EMBEDDING_IMAGE:-.*" + re.escape(AMD), body, re.M)
    assert re.search(r"^    platform: \${EMBEDDING_PLATFORM:-.*linux/amd64", body, re.M)


def test_env_example_documents_the_exact_native_override_pair():
    body = ENV_EXAMPLE.read_text()
    assert f"EMBEDDING_IMAGE={ARM}" in body
    assert "EMBEDDING_PLATFORM=linux/arm64" in body
    assert "cpu-arm64-latest" not in body
