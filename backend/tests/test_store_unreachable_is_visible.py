"""A store that is down says so. Silence is reserved for "nothing matched".

The failure this exists for cost eleven days. Between 2026-08-13 and
2026-08-23 the recall hook ran on 69 prompts across five projects and reached
the store on none of them — OrbStack was not running, so there was no engine
for the containers' restart policy to restart into. The hook behaved exactly
as designed: it logged `Query failed: ...` to /tmp and returned no memories,
which the operator saw as a bare `[TIME]` header. That is byte-identical to a
healthy store answering a query that genuinely matched nothing. There was
nothing to notice, so nobody noticed.

Adeh's rule, stated afterwards and stored as memory 276dce05: "when something
is required to run, something in the stack not running needs to be a VISIBLE
ERROR."

The corollary that makes this subtle: graceful degradation is correct for a
*transient* outage and wrong as a permanent state. Degrading quietly converts
an outage into an invisible one. So the degrade path stays — recall still
returns nothing and the turn still proceeds — but it now announces itself.

The property that matters most here is the interaction with
`SILL_RECALL_QUIET`. An operator who silenced the recall list asked not to
read results they did not find useful. They did not ask to be kept ignorant of
a fault. If a future edit ever gates this behind quiet, the eleven days
happen again to whoever set that flag — which, on this install, is everyone.

Needs no docker, database, or network.
"""

import re
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / "plugin" / "hooks" / "spontaneous-recall.py"


def source() -> str:
    return HOOK.read_text(encoding="utf-8")


def test_the_flag_exists_and_starts_clean():
    assert re.search(r"(?m)^STORE_UNREACHABLE = False$", source()), (
        "no STORE_UNREACHABLE flag — a failed query is indistinguishable "
        "from an empty result again"
    )


def test_a_failed_query_sets_the_flag():
    """Right where the subprocess reports a non-zero return code — the exact
    branch that swallowed eleven days of outage."""
    src = source()
    m = re.search(
        r"if result\.returncode != 0:\s*\n\s*global STORE_UNREACHABLE\s*\n"
        r"\s*STORE_UNREACHABLE = True\s*\n\s*log\(", src)
    assert m, "the query-failure branch no longer raises the flag"


def test_the_operator_is_told_in_the_tui():
    src = source()
    assert "STORE UNREACHABLE" in src, "no visible marker for a dead store"
    assert "query failed, not because nothing matched" in src, (
        "the message must distinguish the two states, not just report an error"
    )


def test_the_warning_is_not_suppressed_by_quiet():
    """The load-bearing test in this file.

    Quiet is a preference about results. A fault is not a result.
    """
    src = source()
    m = re.search(r"(?m)^    if STORE_UNREACHABLE:\n(?P<body>(?:^ {8}[^\n]*\n|^\n)+)", src)
    assert m, "the STORE_UNREACHABLE branch is missing from the TUI assembly"
    # Strip comments first: the branch deliberately *mentions* QUIET_TUI in a
    # comment explaining why it is not gated on it. What must not appear is
    # QUIET_TUI in executable code.
    code = "\n".join(
        line for line in m.group("body").splitlines()
        if not line.strip().startswith("#")
    )
    assert "QUIET_TUI" not in code, (
        "the outage warning is gated on QUIET_TUI — an operator who silenced "
        "the recall list would be blind to a dead store, which is exactly how "
        "this went unnoticed for eleven days"
    )


def test_the_warning_precedes_the_quiet_gated_results():
    """Ordering, so the warning cannot be lost below a list quiet may drop."""
    src = source()
    warn = src.index("if STORE_UNREACHABLE:")
    gate = src.index("if (memories or conversations) and not QUIET_TUI:")
    assert warn < gate, "the outage warning must come before the results block"


def test_the_message_points_at_the_log():
    """The visible line is a signal, not a diagnosis. It has to say where the
    actual error text lives, or the operator's next step is guesswork."""
    m = re.search(r"(?m)^    if STORE_UNREACHABLE:\n(?P<body>(?:^ {8}[^\n]*\n|^\n)+)", source())
    assert m and "LOG_FILE" in m.group("body"), (
        "the warning does not tell the operator where to read the error"
    )
