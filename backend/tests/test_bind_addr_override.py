"""Published ports bind to an operator-chosen interface.

All three published ports were bound to every interface with no way to change
it short of editing a tracked file. What sits behind them makes that worth a
decision rather than a default:

- `db` runs `POSTGRES_HOST_AUTH_METHOD=trust`. It asks for no password from
  anyone. Reaching 5432 *is* being a superuser on the whole memory store —
  read, write, and delete — and the db image carries `pgsql-http`, so it is
  also outbound HTTP from inside the container network.
- `rabbitmq` publishes its management UI on 15672 with the credentials from
  `.env.example`.

Nothing in a single-host install needs any of that reachable off-box: the
hooks reach Postgres by `docker exec`, and the MCP server connects to
localhost. The default binds to loopback; an explicit SILL_BIND_ADDR opts in to exposure.

The tests assert the shape (every published port is overridable, with loopback
defaults) rather than probing a live socket — the rest of backend/tests/
needs no docker, and a bind test would need a running daemon.
"""

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

DEFAULT_BIND = "127.0.0.1"


def published_ports() -> list[str]:
    """Every entry under a `ports:` block, across all services."""
    text = COMPOSE.read_text(encoding="utf-8")
    entries = []
    # (?m) only — never (?ms). With DOTALL the `.*` below matches newlines and
    # the first block swallows the rest of the file.
    for block in re.finditer(r"(?m)^    ports:\n(?P<body>(?:^\s+[-#].*\n)+)", text):
        for line in block.group("body").splitlines():
            line = line.strip()
            if line.startswith("- "):
                entries.append(line[2:].strip().strip('"'))
    return entries


def test_there_are_still_three_published_ports():
    """A guard on the guard: if a service starts publishing a port, this test
    fails and whoever added it has to decide about its bind address too."""
    assert len(published_ports()) == 3, published_ports()


def test_every_published_port_takes_the_bind_address_variable():
    offenders = [p for p in published_ports() if not p.startswith("${SILL_BIND_ADDR:-")]
    assert not offenders, (
        "these ports ignore SILL_BIND_ADDR and always bind every interface: "
        f"{offenders}"
    )


def test_the_default_bind_address_limits_the_ports_to_this_host():
    for entry in published_ports():
        assert entry.startswith(f"${{SILL_BIND_ADDR:-{DEFAULT_BIND}}}:"), entry


def test_the_container_side_ports_are_untouched():
    """The last field is the in-container port; the middle is the host port.
    A bind-address prefix must not have shifted either."""
    expected = {
        "${SILL_BIND_ADDR:-127.0.0.1}:${POSTGRES_PORT:-5432}:5432",
        "${SILL_BIND_ADDR:-127.0.0.1}:${RABBITMQ_PORT:-5672}:5672",
        "${SILL_BIND_ADDR:-127.0.0.1}:${RABBITMQ_MANAGEMENT_PORT:-15672}:15672",
    }
    assert set(published_ports()) == expected


def test_env_example_explains_the_choice_and_why_it_matters():
    """`trust` is the fact that turns this from a preference into a decision.
    An operator who is not told it will read 0.0.0.0 as ordinary."""
    body = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "SILL_BIND_ADDR" in body
    assert "127.0.0.1" in body, "loopback default is missing"
    assert "trust" in body, "the passwordless-auth reason is not stated"
