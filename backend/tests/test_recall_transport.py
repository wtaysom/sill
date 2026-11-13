"""Lossless shared recall wire and fail-closed instance selection."""
import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

@pytest.fixture
def core():
    assert (Path(__file__).parents[1] / 'scripts/recall_core.py').is_file(), 'shared recall core missing'
    return importlib.import_module('scripts.recall_core')


def test_transport_preserves_content_and_metadata(core, monkeypatch):
    row = dict(id='00000000-0000-0000-0000-000000000001', type='semantic',
               content='line\nwith\ttab|||pipe "quote" 臺灣', score=0.0328,
               importance=.7, created_at='2020-01-01T00:00:00Z', source='vector\nsource',
               ref='path\nref', source_store=None, force='assertive', speaker='Inez\tA',
               production={'made': []}, minted_in={'session':'s|1'}, truncated=False)
    monkeypatch.delenv('SILL_INSTANCE_ENV_FILE', raising=False)
    monkeypatch.setattr(core.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(row)+'\n'))
    assert core.recall_rows('hybrid', 'test') == [row]


@pytest.mark.parametrize('wire', ['not json', '[]', '{"id": "x"}', '{"score":NaN}'])
def test_malformed_rows_raise(core, monkeypatch, wire):
    monkeypatch.setattr(core, '_run_sql', lambda *a, **k: wire)
    with pytest.raises(core.RecallUnavailable):
        core.recall_rows('fast', 'test')


def test_selected_invalid_never_falls_back(core, monkeypatch):
    monkeypatch.setenv('SILL_INSTANCE_ENV_FILE', 'relative.env')
    monkeypatch.setattr(core.subprocess, 'run', lambda *a, **k: pytest.fail('fallback attempted'))
    with pytest.raises(core.RecallUnavailable):
        core.recall_rows('hybrid', 'test')
    with pytest.raises(core.RecallUnavailable):
        core.full_contents(['00000000-0000-0000-0000-000000000001'])


def test_selected_target_excludes_ambient_coordinates(core, monkeypatch, tmp_path):
    selected = tmp_path / 'instance.env'
    selected.write_text('SILL_DB_DSN=postgresql://selected:secret@localhost:6543/fixture\n')
    monkeypatch.setenv('SILL_INSTANCE_ENV_FILE', str(selected))
    for key in ('PGHOST','PGDATABASE','SILL_DB_DSN','SILL_DB_CONTAINER','POSTGRES_HOST'):
        monkeypatch.setenv(key, 'stale-ambient')
    calls = []
    def run(cmd, **kwargs):
        calls.append((cmd,kwargs))
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(core.subprocess,'run',run)
    assert core.recall_rows('fast','query') == []
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert 'docker' not in cmd and 'secret' not in ' '.join(cmd)
    assert json.loads(kwargs['input'])['dsn'] == 'postgresql://selected:secret@localhost:6543/fixture'
    assert not any(k.startswith(('PG','POSTGRES_','SILL_DB_')) for k in kwargs['env'])


def test_query_similarity_is_distinct_from_score(core, monkeypatch):
    sql = []
    monkeypatch.setattr(core, '_run_sql', lambda query, **kw: sql.append(query) or '')
    core.recall_rows('fast','query',fast_min_similarity=.4)
    assert "get_embedding('query', 'query')" in sql[0]
    assert 'query_similarity >= 0.4' in sql[0]
    assert 'fr.score >=' not in sql[0]
    assert 'round(fr.score::numeric, 4)' in sql[0]
    core.recall_rows('hybrid','query',fast_min_similarity=.9)
    assert 'query_similarity >=' not in sql[1]


def test_legacy_failure_and_typed_opt_in(core, monkeypatch):
    from scripts import recall_lib
    def fail(*args, **kwargs):
        raise core.RecallUnavailable('unavailable')
    monkeypatch.setattr(core,'fused_recall',fail)
    assert recall_lib.query_memories('query') == []
    with pytest.raises(core.RecallUnavailable):
        recall_lib.query_memories('query',raise_on_error=True)


def test_exposure_selected_failure_is_typed(core, monkeypatch):
    monkeypatch.setenv('SILL_INSTANCE_ENV_FILE','relative.env')
    monkeypatch.setattr(core.subprocess,'run',lambda *a,**kw: pytest.fail('fallback attempted'))
    with pytest.raises(core.RecallUnavailable):
        core.record_exposure(['00000000-0000-0000-0000-000000000001'])


def test_display_phases_share_absolute_deadline(monkeypatch):
    from scripts import recall_core as core
    import time
    import pytest
    past = time.monotonic() - 1
    mid = '00000000-0000-0000-0000-000000000001'
    with pytest.raises(core.RecallUnavailable):
        core.full_contents([mid], deadline=past)
    with pytest.raises(core.RecallUnavailable):
        core.record_exposure([mid], deadline=past)
    with pytest.raises(core.RecallUnavailable):
        core.fused_recall('Cedar meadow water', deadline=past)


def test_callback_deadline_bounds_every_production_child(core,monkeypatch):
    import time
    calls=[]
    monkeypatch.delenv('SILL_INSTANCE_ENV_FILE',raising=False)
    def transport(*a,**kw):
        calls.append(kw['timeout'])
        return SimpleNamespace(returncode=0,stdout='')
    monkeypatch.setattr(core.subprocess,'run',transport)
    deadline=time.monotonic()+.4
    assert core.fused_recall('Cedar meadow water survey',deadline=deadline) == []
    mid='00000000-0000-0000-0000-000000000001'
    assert core.full_contents([mid],deadline=deadline) == {}
    core.record_exposure([mid],deadline=deadline)
    assert len(calls)>2 and all(0<t<=.4 for t in calls)


def test_selected_child_row_array_mode_preserves_all_columns_and_nulls(core,monkeypatch):
    import asyncio
    import sys
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def transaction():
        yield
    class Connection:
        def transaction(self): return transaction()
        async def execute(self,*args): pass
        async def fetch(self,*args): return [('line\n|||臺灣','inserted',None)]
        async def close(self,**kw): pass
    async def connect(*args,**kw): return Connection()
    monkeypatch.setitem(sys.modules,'asyncpg',SimpleNamespace(connect=connect))
    output=asyncio.run(core._selected_query({'dsn':'unused','timeout':1,'sql':'SELECT fixture','row_arrays':True}))
    assert json.loads(output) == ['line\n|||臺灣','inserted',None]
