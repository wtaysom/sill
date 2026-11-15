"""Version stamp: backend/pyproject.toml, plugin/plugin.json, and
`sill-mcp --version` must all agree, and the CHANGELOG must
have a dated heading for the current version.

v0.1.0's own CHANGELOG "Known issues" list named `sill-mcp` having no
`--version` flag as an open issue for the whole 0.1.0 line (see
CHANGELOG.md's "## v0.1.0" section) — this file is the regression pin for
closing it in 0.2.0.
"""
import json
import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
PYPROJECT = BACKEND_ROOT / "pyproject.toml"
PLUGIN_JSON = REPO_ROOT / "plugin" / "plugin.json"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

EXPECTED_VERSION = "0.3.0"


def _pyproject_version() -> str:
    text = PYPROJECT.read_text()
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert m, "could not find a top-level version = \"...\" in backend/pyproject.toml"
    return m.group(1)


def _plugin_json_version() -> str:
    data = json.loads(PLUGIN_JSON.read_text())
    assert "version" in data, "plugin/plugin.json has no 'version' key"
    return data["version"]


# --- Step 1: pyproject.toml and plugin.json both read the current version -----------------

def test_pyproject_version_matches_expected():
    assert _pyproject_version() == EXPECTED_VERSION


def test_plugin_json_version_matches_expected():
    assert _plugin_json_version() == EXPECTED_VERSION


def test_pyproject_and_plugin_json_versions_match():
    """One source of version truth, expressed twice — they must never drift
    independently of each other."""
    assert _pyproject_version() == _plugin_json_version()


# --- Step 2: sill-mcp --version -----------------------------------------

def test_version_flag_wired_into_build_parser():
    """Static check that --version is a real argparse action, not just a
    string this file happens to match — belt to the subprocess test's
    braces."""
    import sill_mcp_server as srv

    actions = {a.option_strings[0]: a for a in srv.build_parser()._actions if a.option_strings}
    assert "--version" in actions, "sill-mcp has no --version flag wired into build_parser()"
    assert actions["--version"].__class__.__name__ == "_VersionAction", (
        "--version should use argparse's built-in version action (exits 0 after "
        "printing, no DB/asyncio machinery involved)"
    )


def test_version_flag_exits_zero_and_prints_something_unconditionally():
    """Environment-independent half of the claim: regardless of whether this
    checkout is pip-installed anywhere importlib.metadata can find it, the
    flag itself must exit 0 and print a non-empty, prog-prefixed string —
    never a traceback, never a hang waiting on a DB connection."""
    r = subprocess.run(
        [sys.executable, "-m", "sill_mcp_server", "--version"],
        cwd=BACKEND_ROOT, capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "Traceback" not in r.stderr
    out = r.stdout.strip()
    assert out, "sill-mcp --version printed nothing"
    assert out.startswith("sill-mcp"), f"expected a 'sill-mcp <version>'-shaped line, got {out!r}"


def _sill_memory_dist_resolvable() -> bool:
    try:
        version("sill-memory")
        return True
    except PackageNotFoundError:
        return False


@pytest.mark.skipif(
    not _sill_memory_dist_resolvable(),
    reason="sill-memory isn't registered with importlib.metadata in this "
           "environment (run `pip install -e backend` to exercise this path); "
           "CI's 'pip install -e .[dev]' step always does, so this runs there.",
)
def test_version_flag_matches_pyproject_when_package_is_resolvable():
    """The real claim: when this checkout is installed (editable or not —
    CI always is, via 'pip install -e .[dev]'), --version's printed number
    must be the same number pyproject.toml declares, not a stale or
    hardcoded one."""
    r = subprocess.run(
        [sys.executable, "-m", "sill_mcp_server", "--version"],
        cwd=BACKEND_ROOT, capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert r.stdout.strip() == f"sill-mcp {_pyproject_version()}", (
        f"expected pyproject version {_pyproject_version()!r} in --version "
        f"output, got: {r.stdout!r}"
    )


# --- CHANGELOG: dated version heading ---------------------------------------

def test_changelog_has_no_dev_marker_for_current_version():
    text = CHANGELOG.read_text()
    dev_heading = rf"^## v{re.escape(EXPECTED_VERSION)}-dev\b"
    assert not re.search(dev_heading, text, re.M), "CHANGELOG still has a -dev heading"


def test_changelog_has_dated_heading_for_current_version():
    text = CHANGELOG.read_text()
    heading = rf"^## v{re.escape(EXPECTED_VERSION)} — \d{{4}}-\d{{2}}-\d{{2}}$"
    assert re.search(heading, text, re.M), (
        f"could not find a '## v{EXPECTED_VERSION} — YYYY-MM-DD' heading in CHANGELOG.md"
    )


def test_changelog_current_version_precedes_release_history():
    text = CHANGELOG.read_text()
    versions = (EXPECTED_VERSION, "0.2.1", "0.2.0", "0.1.0")
    positions = [text.index(f"## v{version} — ") for version in versions]
    assert positions == sorted(positions), "version headings must be newest first"
