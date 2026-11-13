"""No live instruction still sends anyone to `~/.claude/.mcp.json`.

v0.2.0 fixed *the code*: install.sh had been writing the MCP entry to
`~/.claude/.mcp.json`, which Claude Code never reads, so a step that printed
`added mcpServers.sill -> …` could leave the server unregistered. That fix is
already pinned by `test_env_and_mcp_wiring.py`'s
`test_install_never_writes_the_path_claude_code_does_not_read`.

What the fix did not do is sweep the *documentation*. Seven live references
to the dead path survived it, in the places an operator actually looks when
the thing they are debugging is a missing server:

  * README's harness-support table, its "What gets installed" section, and
    its uninstall note
  * `docs/adapters.md`'s four-slot contract table, the reference for anyone
    porting a third harness
  * `docs/onboarding/01-install.md`'s phase-2 remedy — the paragraph whose
    entire job is telling you where to look when `sill` is not connected
  * `uninstall.sh`'s own on-screen hand-edit instructions
  * `plugin/claude.home.md.template`, which `--scope home` installs into
    `~/.claude/CLAUDE.md`, i.e. the one that ends up teaching the dead path
    to every session in every directory

Documentation that contradicts the code is a defect with a longer half-life
than the code bug was: the bug is gone in one release, but a reader sent to
an empty file concludes their install is broken and has no way to discover
otherwise. So sweep the tree, and allow the mention only where naming the
old path *is* the point — the changelog and rehearsal entries recording the
defect, the comment in install.sh warning the next editor off it, and the
tests that pin all of this.

Needs no docker, database, or network.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEAD_PATH = ".claude/.mcp.json"
LIVE_PATH = "~/.claude.json"

# Files where naming the dead path is the content, not a mistake: three
# historical records, one warning comment, and the two tests pinning it.
HISTORICAL = {
    "CHANGELOG.md",
    "README.md",  # one line, checked precisely below
    "docs/RELEASE-REHEARSAL.md",
    "install.sh",
    "backend/tests/test_env_and_mcp_wiring.py",
    "backend/tests/test_mcp_registry_path_docs.py",
}

SEARCHED_SUFFIXES = {".md", ".sh", ".py", ".json", ".template", ".toml", ".yml"}


def tracked_files():
    """Files this repo actually ships — `git ls-files`, not a filesystem walk.

    This was an rglob over the whole tree, which also swept files git ignores:
    a contributor's scratch output, local notes, or — the specimen that caught
    it — 86 gitignored development reports whose entire subject is the old
    path, correctly named there as the bug it was. Those aren't offenders, and
    a sweep that fails depending on what happens to be sitting untracked in
    someone's working copy pins nothing. The claim is about what the repo
    SAYS, so ask git what the repo contains. Falls back to the walk where git
    isn't available (a tarball export), which is the pre-existing behavior.
    """
    try:
        listing = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
            capture_output=True, text=True, timeout=30, check=True).stdout
        paths = (REPO_ROOT / rel for rel in listing.split("\0") if rel)
    except Exception:
        paths = (p for p in REPO_ROOT.rglob("*") if ".git" not in p.parts)
    for path in paths:
        if not path.is_file():
            continue
        if path.suffix not in SEARCHED_SUFFIXES:
            continue
        yield path


def offenders():
    hits = []
    for path in tracked_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in HISTORICAL:
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(body.splitlines(), 1):
            if DEAD_PATH in line:
                hits.append(f"{rel}:{n}: {line.strip()}")
    return hits


def test_no_live_file_names_the_registry_claude_code_does_not_read():
    hits = offenders()
    assert not hits, (
        "these send a reader to a file Claude Code never reads; the user-scope "
        f"MCP registry is {LIVE_PATH}:\n  " + "\n  ".join(hits)
    )


def test_the_readme_names_the_dead_path_only_to_warn_against_it():
    """README keeps exactly one mention — the upgrade note contrasting the two
    paths. If a second appears, the sweep above has been undone in the file
    that is hardest to notice it in."""
    body = (REPO_ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    hits = [n for n, line in enumerate(body) if DEAD_PATH in line]
    assert len(hits) == 1, (
        f"expected 1 contrasting mention, found {len(hits)}:\n"
        + "\n".join(body[n] for n in hits)
    )
    # The contrast wraps across lines, so read a window rather than the one
    # line: what matters is that the dead path is named next to the live one
    # and to the reason it is dead, not how the prose happens to be filled.
    window = " ".join(body[max(0, hits[0] - 3): hits[0] + 4])
    assert LIVE_PATH in window, "the dead path is named without naming the live one"
    assert "never reads" in window, "the dead path is named without saying why it is dead"


@pytest.mark.parametrize("rel,needle", [
    ("uninstall.sh", "~/.claude.json"),
    ("plugin/claude.home.md.template", "~/.claude.json"),
    ("docs/adapters.md", "`~/.claude.json`"),
    ("docs/onboarding/01-install.md", "`~/.claude.json`"),
])
def test_each_swept_file_names_the_live_registry(rel, needle):
    """Absence of the wrong path isn't enough — the right one has to be there,
    or the sweep just deleted the operator's only pointer."""
    body = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert needle in body, f"{rel} no longer tells the reader where the registry is"


def test_uninstall_help_lists_three_aligned_manual_removal_paths():
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "uninstall.sh"), "--help"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    expected = {
        "Claude Code": "mcpServers.sill in ~/.claude.json",
        "Codex": "mcp_servers.sill in ${CODEX_HOME:-$HOME/.codex}/config.toml",
        "Claude Desktop": "mcpServers.sill in ~/Library/Application Support/Claude/claude_desktop_config.json",
    }
    rows = {
        match[1]: (match.start(2), match[2])
        for line in result.stdout.splitlines()
        if (match := re.fullmatch(r"#   (Claude Code|Codex|Claude Desktop) +(mcp.*)", line))
    }
    assert {client: instruction for client, (_, instruction) in rows.items()} == expected
    assert len({column for column, _ in rows.values()}) == 1
