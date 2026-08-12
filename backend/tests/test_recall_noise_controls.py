"""The recall hook's tunables are reachable without editing the hook.

Five constants governed how much recall injects and how much of it is shown,
and all five were hardcoded: MAX_MEMORIES, MAX_CONVERSATIONS,
MIN_QUERY_LENGTH, FAST_MIN_SIMILARITY, and the TUI display itself. An
operator whose recall was technically working and practically noise had no
move except editing a tracked file — which then fights every `git pull`.

That state is normal early rather than exceptional. A young store answers
every query out of whatever it happens to contain, so the first weeks of any
install produce confident, irrelevant hits. Observed here on 2026-08-12: a
prompt about an analytics discrepancy surfaced four generic methodology
memories above the one genuinely relevant entry, plus an episodic hit whose
content was a past user prompt ("give me a note that shows what's been
installed") — noise by construction, since episodic search matches user
prompts rather than assistant content (see the store's own memory be75e541).

The important split is that the hook emits two separate payloads: a
`systemMessage` the operator reads in the TUI, and an `additionalContext`
the model reads. They are built independently, so display volume and recall
strength are genuinely separable. `SILL_RECALL_QUIET` silences the former and
leaves the latter untouched — the operator stops reading a list they did not
find useful without weakening what the model gets.

Defaults are unchanged in every case; an operator who sets nothing sees
exactly what they saw before.

Needs no docker, database, or network.
"""

import re
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "plugin" / "hooks" / "spontaneous-recall.py"


def source() -> str:
    return HOOK.read_text(encoding="utf-8")


@pytest.mark.parametrize("const,env,default", [
    ("MAX_MEMORIES", "SILL_RECALL_MAX_MEMORIES", "5"),
    ("MAX_CONVERSATIONS", "SILL_RECALL_MAX_CONVERSATIONS", "2"),
    ("MIN_QUERY_LENGTH", "SILL_RECALL_MIN_QUERY_LENGTH", "20"),
])
def test_int_tunables_read_the_environment_and_keep_their_defaults(const, env, default):
    m = re.search(rf"(?m)^{const} = _env_int\(\"{env}\", (?P<default>\d+)\)$", source())
    assert m, f"{const} is not environment-configurable via {env}"
    assert m.group("default") == default, (
        f"{const} default changed to {m.group('default')} — an operator who "
        f"sets nothing must see what they saw before"
    )


def test_the_similarity_floor_is_configurable_and_unchanged():
    m = re.search(r"(?m)^FAST_MIN_SIMILARITY = _env_float\(\"SILL_RECALL_MIN_SIMILARITY\", (?P<d>[\d.]+)\)$", source())
    assert m, "FAST_MIN_SIMILARITY is not environment-configurable"
    assert float(m.group("d")) == 0.25


def test_a_bad_env_value_falls_back_instead_of_raising():
    """This hook runs on every prompt. A ValueError here costs the operator
    their turn, which is a far worse outcome than an ignored tunable."""
    body = re.search(r"(?ms)^def _env_int\(.*?^def _env_float", source())
    assert body, "could not isolate _env_int"
    assert "except" in body.group(0), "_env_int does not guard against a bad value"
    assert "ValueError" in body.group(0)


def test_quiet_is_opt_in_and_off_by_default():
    m = re.search(r'(?m)^QUIET_TUI = os\.environ\.get\("SILL_RECALL_QUIET", ""\)', source())
    assert m, "SILL_RECALL_QUIET is not read"
    # Absent env -> "" -> not in the truthy set -> False.
    assert re.search(r'in \("1", "true", "yes"\)', source()), (
        "quiet must be opt-in via an explicit truthy value"
    )


def test_quiet_gates_the_display_and_only_the_display():
    """The whole point: silence the TUI list, leave the model's payload alone.
    If QUIET_TUI ever guards the additionalContext assembly, an operator who
    just wanted less on screen would be silently weakening recall."""
    src = source()
    for guard in ("if (memories or conversations) and not QUIET_TUI:",
                  "if memories and not QUIET_TUI:",
                  "if conversations and not QUIET_TUI:"):
        assert guard in src, f"missing TUI guard: {guard}"

    # The context the model reads is built by format_results(); QUIET_TUI must
    # not appear anywhere in it.
    fmt = re.search(r"(?ms)^def format_results\(.*?(?=^def |\Z)", src)
    assert fmt, "could not isolate format_results()"
    assert "QUIET_TUI" not in fmt.group(0), (
        "quiet is reaching the model's payload — it must only affect the TUI"
    )


def test_the_time_header_survives_quiet():
    """One line of visible evidence that the hook ran at all. Without it a
    quiet install and a dead store look identical — the same invisible-failure
    shape the restart policy exists to prevent."""
    src = source()
    m = re.search(r"(?m)^    tui_lines = \[time_header\] if time_header else \[\]$", src)
    assert m, "the TUI no longer starts from the time header"
    assert "QUIET_TUI" not in m.group(0)
