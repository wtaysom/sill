"""Deterministic rank fusion, distinct failures, and bounded fan-out."""
import importlib
from pathlib import Path
import pytest

@pytest.fixture
def core():
    assert (Path(__file__).parents[1] / 'scripts/recall_core.py').is_file(), 'shared recall core missing'
    return importlib.import_module('scripts.recall_core')


def test_fusion_uses_unique_queries_and_ids(core):
    queries = core.fused_subqueries('alpha','alpha',['alpha','alpha'])
    assert len(queries) == len(set(queries))
    def fetch(fn, query, limit=15, *, fast_min_similarity=.25):
        return [{'id':'a','score':.9},{'id':'a','score':.9},{'id':'b','score':.5}]
    result = core.fused_recall('alpha','alpha',fetch=fetch,keywords=['alpha'])
    assert [r['id'] for r in result] == ['a','b']
    assert result[0]['score'] == .9  # Ranking fusion does not relabel public score.


def test_healthy_empty_distinct_from_failure(core):
    def fail(*a, **kw): raise core.RecallUnavailable('offline')
    with pytest.raises(core.RecallUnavailable): core.fused_recall('query',fetch=fail)
    assert core.fused_recall('query',fetch=lambda *a,**kw: []) == []
    def partial(fn,*a,**kw):
        if fn == 'fast': raise core.RecallUnavailable('offline')
        return [{'id':'healthy','score':.1}]
    assert core.fused_recall('query',fetch=partial)[0]['id'] == 'healthy'
    assert core.fused_recall('',fetch=fail) == []


def test_keywords_and_previous_are_deterministic(core,tmp_path):
    assert core.extract_keywords('ask Inez about API and vector-search') == core.extract_keywords('ask Inez about API and vector-search')
    path = tmp_path / 'session.jsonl'
    path.write_text('{"type":"user","message":{"content":"prior question"}}\n'
                    '{"type":"assistant","message":{"content":"prior reply"}}\n')
    assert core.previous_turn(str(path)) == '[previous reply] prior reply\n[previous message] prior question'
    assert core.previous_turn(str(tmp_path/'absent')) == ''
    assert 'target' in core.focus_snippet('head '*80+'target evidence', ['target'])


def test_production_deadline_reaps_hanging_children(core, monkeypatch, tmp_path):
    import os
    import subprocess
    import sys
    import time
    import threading
    instance = tmp_path/'selected.env'
    instance.write_text('SILL_DB_DSN=postgresql://sill_test:fixture_only@localhost:6543/sill_test_none\n')
    monkeypatch.setenv('SILL_INSTANCE_ENV_FILE',str(instance))
    monkeypatch.setattr(core,'FUSION_TIMEOUT',.3)
    real_run = subprocess.run
    children = tmp_path/'children'
    children.mkdir()
    def hang(cmd, **kwargs):
        script = 'import os,time,pathlib; pathlib.Path('+repr(str(children))+',str(os.getpid())).touch(); time.sleep(30)'
        return real_run([sys.executable,'-c',script],**kwargs)
    monkeypatch.setattr(core.subprocess,'run',hang)
    threads = {t.ident for t in threading.enumerate()}
    start = time.monotonic()
    with pytest.raises(core.RecallUnavailable):
        core.fused_recall('several different keyword queries',previous='another question')
    assert time.monotonic()-start < 2
    assert len(list(children.iterdir())) <= core.MAX_WORKERS
    assert list(children.iterdir())
    for path in children.iterdir():
        with pytest.raises(ProcessLookupError): os.kill(int(path.name),0)
    assert {t.ident for t in threading.enumerate()} == threads


def test_previous_turn_skips_malformed_records(core,tmp_path):
    path = tmp_path/'malformed.jsonl'
    path.write_text('[]\n{"message":7}\n')
    assert core.previous_turn(str(path)) == ''


def test_legacy_two_argument_fetch_injection(core):
    calls = []
    def fetch(fn,query):
        calls.append((fn,query))
        return [{'id':'legacy','score':.2}]
    assert core.fused_recall('query',fetch=fetch)[0]['id'] == 'legacy'
    assert len(calls) == len(set(calls))
