"""notice() SQL shape + parser contract, DB faked via _query_db patching."""

import importlib.util
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import sill

# The auto-store hook's own module, loaded by path (a standalone script, not
# an installed package) so its REAL argv-building function can be driven
# directly — see test_auto_store_argv_parses_for_both_harness_calling_shapes
# below, the mint-slot conformance evidence docs/adapters.md points at.
_RP_PATH = Path(__file__).resolve().parents[2] / "plugin" / "hooks" / "response-patterns.py"
_rp_spec = importlib.util.spec_from_file_location("response_patterns_mint_conformance", _RP_PATH)
rp = importlib.util.module_from_spec(_rp_spec)
_rp_spec.loader.exec_module(rp)


def _fake_query(sql, timeout=60):
    if "mint_memory" in sql:
        return [("11111111-2222-3333-4444-555555555555", "inserted", None)]
    return ["ok"]


def test_notice_mints_force_speaker_atomically():
    with mock.patch.object(sill, "_query_db", side_effect=_fake_query) as q:
        sill.notice("a fact", "semantic", speaker="Ada", force="directive")
    sqls = [c.args[0] for c in q.call_args_list]
    assert len(sqls) == 1
    assert "public.mint_memory" in sqls[0]
    assert "p_force => 'directive'" in sqls[0] and "p_speaker => 'Ada'" in sqls[0]


def test_notice_escapes_apostrophes_in_speaker():
    with mock.patch.object(sill, "_query_db", side_effect=_fake_query) as q:
        sill.notice("a fact", "semantic", speaker="O'Brien")
    assert len(q.call_args_list) == 1
    assert "O''Brien" in q.call_args.args[0]


def test_notice_rejects_bad_force_before_db():
    with mock.patch.object(sill, "_query_db", side_effect=AssertionError("no DB")):
        with pytest.raises(ValueError):
            sill.notice("x", "semantic", speaker="Ada", force="rhetorical")


def test_parser_concepts_append_and_comma_split():
    p = sill.build_parser()
    a = p.parse_args(["notice", "c", "--speaker", "Ada",
                      "--concepts", "x,y", "--concepts", "z"])
    assert sill.flatten_concepts(a.concepts) == ["x", "y", "z"]


def test_parser_requires_speaker():
    p = sill.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["notice", "c"])


def test_hook_call_shape_parses():
    """The auto-store hook's constructed argv must parse — this contract broke
    silently in v0.1.0 (the CLI had no notice subcommand at all)."""
    p = sill.build_parser()
    argv = ["notice", "content here", "--importance", "0.6",
            "--force", "assertive", "--speaker", "instance",
            "--concepts", "a,b"]
    a = p.parse_args(argv)
    assert a.speaker == "instance" and a.importance == 0.6


def _run_auto_store_capturing_argv(cwd, transcript_path):
    """Call the REAL plugin/hooks/response-patterns.py auto_store_insight()
    — not a hand-typed stand-in like test_hook_call_shape_parses above —
    with subprocess.run mocked to capture argv instead of shelling out.
    Returns the captured cmd list."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Stored: 99999999-8888-7777-6666-555544443333 [1 tags]", stderr="")

    with mock.patch("subprocess.run", side_effect=fake_run):
        mem_id = rp.auto_store_insight(
            "a response worth storing, reused across a conformance fixture",
            "a summary", ["adapter-contract"], cwd=cwd, transcript_path=transcript_path)
    return mem_id, captured["cmd"]


@pytest.mark.parametrize("cwd,transcript_path", [
    pytest.param("/Users/x/code/some-project",
                 "/Users/x/.claude/projects/-Users-x-code-some-project/session-abc123.jsonl",
                 id="claude-cwd-and-transcript-path"),
    pytest.param("/Users/x/code/some-project", None,
                 id="codex-cwd-only-no-transcript-path"),
])
def test_auto_store_argv_parses_for_both_harness_calling_shapes(
        cwd, transcript_path, monkeypatch, tmp_path):
    """Mint-slot conformance (docs/adapters.md): extends test_hook_call_shape_parses
    above by driving the REAL argv-building function rather than a hand-typed
    stand-in, and by doing it under the two calling shapes this codebase's own
    source_project() branches on — cwd+transcript_path both present (how a
    Claude Stop payload supplies them) vs. cwd present alone (Codex's Stop
    payload hands the hook last_assistant_message directly per
    docs/hooks.md's response-patterns section, so response-patterns.py never
    needs transcript_path to get response text on that harness — see
    get_response_text's own first branch). auto_store_insight() itself has no
    harness branching at all; this proves that's actually true rather than
    assumed, for both shapes. A specific Codex transcript_path naming
    convention is deliberately NOT asserted here — it isn't verified
    anywhere in this repo (docs/adapters.md's version-fragility section)."""
    monkeypatch.setattr(rp, "AUTO_STORE_LOG", tmp_path / "auto-store.jsonl")
    monkeypatch.delenv("SILL_HOME_PROJECT", raising=False)

    mem_id, cmd = _run_auto_store_capturing_argv(cwd, transcript_path)

    assert mem_id == "99999999"
    assert cmd[0] == rp.SILL_CLI

    args = sill.build_parser().parse_args(cmd[1:])
    assert args.cmd == "notice"
    assert args.speaker == rp.SPEAKER_SELF
    assert args.force == "assertive"
    assert args.content.startswith("[AUTO-STORED BY HOOK")
    assert "adapter-contract" in sill.flatten_concepts(args.concepts)


def test_main_success_prints_receipt_and_returns_zero(capsys):
    with mock.patch.object(sill, "_query_db", side_effect=_fake_query):
        rc = sill.main(["notice", "a fact", "--speaker", "Ada"])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("Stored: ")


def test_main_failure_returns_one(capsys):
    with mock.patch.object(sill, "_query_db", return_value=[]) as q:
        rc = sill.main(["notice", "a fact", "--speaker", "Ada"])
    assert rc == 1 and "Failed to store" in capsys.readouterr().out
    assert q.call_count == 1  # No unsafe legacy fallback or prefix-content adoption.


@pytest.fixture(autouse=True)
def synthetic_mint_context(monkeypatch):
    monkeypatch.setattr("sill.mint_context", lambda via: {"session": None, "as": None, "when": "W", "via": via, "model": None})


def test_notice_optional_production_and_json_null():
    with mock.patch.object(sill, '_query_db', side_effect=_fake_query) as q:
        assert sill.main(['notice','synthetic words','--speaker','Inez','--made-json','null']) == 0
        assert 'p_production => NULL' in q.call_args.args[0]
        assert 'p_minted_in =>' in q.call_args.args[0]
    with mock.patch.object(sill, '_query_db') as q:
        with pytest.raises(ValueError):
            sill.notice('invalid declaration', production={'made':'invalid'})
        q.assert_not_called()
