"""Every service comes back on its own after an engine restart.

None of the four services declared a `restart:` policy, so Docker defaulted to
`no`. The consequence is not that Sill stops — it is that Sill stops
*quietly*. `spontaneous-recall.py` is built to degrade gracefully when the
store is unreachable: it still emits its `[TIME]` header and simply returns no
memories. That is correct behavior for a transient outage and exactly wrong as
a permanent state, because the output is byte-for-byte what a healthy store
looks like when a query genuinely matches nothing.

Observed on a live install (2026-08-12): all four containers exited cleanly
during an OrbStack restart and stayed down for over an hour across two active
work projects. Nothing surfaced it. The host had not rebooted; `docker inspect`
reported `RestartPolicy: no`.

`unless-stopped` rather than `always`, deliberately: it survives reboots,
engine restarts and crashes, but honors a deliberate `docker compose stop`.
`always` would fight an operator who is trying to shut the thing off, which
matters more than usual here — one of the charter questions this project asks
an operator to answer is what would make them shut it down.

Overridable via `SILL_RESTART_POLICY` for anyone who prefers to start the
stack by hand.

Needs no docker, database, or network.
"""

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
COMPOSE = BACKEND / "docker-compose.yml"
ENV_EXAMPLE = BACKEND / ".env.example"

SERVICES = ["db", "embeddings", "rabbitmq", "maintenance_worker"]
EXPECTED = "${SILL_RESTART_POLICY:-unless-stopped}"


def service_block(name: str) -> str:
    """The YAML block for one service, up to the next top-level key."""
    text = COMPOSE.read_text(encoding="utf-8")
    m = re.search(rf"(?m)^  {re.escape(name)}:\n(?P<body>(?:^(?:    |\n).*\n)+)", text)
    assert m, f"no service block found for {name!r}"
    return m.group("body")


@pytest.mark.parametrize("service", SERVICES)
def test_every_service_declares_a_restart_policy(service):
    body = service_block(service)
    m = re.search(r"(?m)^    restart:\s*(?P<value>\S.*)$", body)
    assert m, (
        f"service {service!r} has no restart: policy — one engine restart "
        "leaves it down, and spontaneous-recall's graceful degrade makes that "
        "indistinguishable from an empty result"
    )
    assert m.group("value").strip() == EXPECTED, (
        f"{service}: expected {EXPECTED}, got {m.group('value').strip()!r}"
    )


@pytest.mark.parametrize("service", SERVICES)
def test_the_policy_is_overridable(service):
    """An operator who wants to start the stack by hand must not have to edit
    a tracked file to do it."""
    assert "${SILL_RESTART_POLICY" in service_block(service)


def test_the_default_is_unless_stopped_not_always():
    """`always` restarts a container the operator deliberately stopped. This
    project asks operators to name a shutdown condition in their charter;
    fighting them when they act on it would be the wrong default."""
    text = COMPOSE.read_text(encoding="utf-8")
    assert "unless-stopped" in text
    bad = re.findall(r"(?m)^    restart:\s*always\s*$", text)
    assert not bad, "restart: always overrides a deliberate `compose stop`"


def test_no_service_is_left_out():
    """A fifth service added later without a policy reintroduces the bug for
    whichever component it is."""
    text = COMPOSE.read_text(encoding="utf-8")
    declared = re.findall(r"(?m)^  ([a-z_]+):$", text.split("volumes:")[0])
    declared = [d for d in declared if d not in ("services", "networks", "volumes")]
    missing = [d for d in declared if "restart:" not in service_block(d)]
    assert not missing, f"services with no restart policy: {missing}"


def test_env_example_explains_why_it_defaults_on():
    """A default that silently changes behavior needs its reason written down
    where the operator configuring the stack will read it."""
    body = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "SILL_RESTART_POLICY" in body
    assert "unless-stopped" in body
    low = body.lower()
    assert "[time]" in low or "degrade" in low, (
        "the .env.example note never explains the invisible-failure reason"
    )
