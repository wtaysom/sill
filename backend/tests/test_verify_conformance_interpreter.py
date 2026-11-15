"""verify.sh check 6 runs pytest with the interpreter that has the backend.

The defect: check 6 tested `python3 -c "import pytest"` and, on success, ran
`python3 -m pytest`. But `install.sh` never puts the backend in the system
python3 — it installs into a pipx venv (`sill-memory`) or, without pipx, into
`~/.local/share/sill-venv`. So on a normal install the probe failed, check 6
always took its degraded capture-slot branch, and the full four-slot
conformance suite never ran during verification. Observed on a clean install:
even after `pipx inject sill-memory pytest pytest-asyncio`, check 6 still
reported the fallback.

What made it more than a missed check is the remedy it printed. The degraded
branch told the operator to set `SILL_PYTHON` and pip-install into it — and
check 6 never read `SILL_PYTHON`. Following the instruction exactly could not
change the outcome, and the check reported `pass:` either way, so nothing
signalled that the advice was inert.

Check 6 now resolves the interpreter the same way `install.sh`'s
`sill_python()` does, with an explicit `SILL_PYTHON` winning — the variable
`docs/onboarding/01-install.md` and `scheduling/README.md` already tell
operators to set.

Static assertions only: running verify.sh needs a live stack.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_SH = REPO_ROOT / "verify.sh"


def verify_text() -> str:
    return VERIFY_SH.read_text(encoding="utf-8")


def check_6_body() -> str:
    text = verify_text()
    start = text.index("Check 6/6")
    return text[start:]


def test_check_6_does_not_probe_or_run_the_bare_system_python():
    body = check_6_body()
    assert 'python3 -c "import pytest"' not in body, (
        "check 6 probes the system python3 again — install.sh never puts the "
        "backend there, so this always takes the degraded branch"
    )
    assert "python3 -m pytest" not in body, (
        "check 6 runs pytest under the system python3 again"
    )


def test_check_6_uses_the_resolved_interpreter_for_both_probe_and_run():
    body = check_6_body()
    assert '"$SILL_PY" -c "import pytest"' in body
    assert '"$SILL_PY" -m pytest' in body


def test_check_6_names_the_interpreter_it_chose():
    """A check that silently picks an interpreter is unfalsifiable from its
    own output — the operator cannot tell which python was consulted."""
    assert "conformance interpreter:" in check_6_body()


def test_the_degraded_branch_advice_is_now_actionable():
    """It must name the interpreter it actually resolved, not a shell
    incantation that reproduces the guess."""
    body = check_6_body()
    fallback = body[body.index("pytest not importable"):]
    assert "$SILL_PY" in fallback, "the advice does not name the resolved interpreter"
    assert "SILL_PYTHON=" in fallback, "the override is no longer mentioned"
