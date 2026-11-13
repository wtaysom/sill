"""Every DB hook must fail closed on an invalid explicit instance selector."""
import importlib.util
import io
import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HOOKS = Path(__file__).resolve().parents[2] / 'plugin/hooks'


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-','_'),HOOKS / (name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('name', ['goodnight-checkpoint','precompact-snapshot'])
def test_optional_db_hooks_never_fall_back_to_ambient_container(name,monkeypatch,tmp_path,capsys):
    monkeypatch.syspath_prepend(str(HOOKS))
    monkeypatch.setenv('SILL_INSTANCE_ENV_FILE',str(tmp_path/'missing-secret.env'))
    monkeypatch.setenv('SILL_DB_CONTAINER','stale-live-container')
    calls=[]
    monkeypatch.setattr(subprocess,'run',lambda *a, **kw:calls.append(a))
    module=load(name)
    assert module.query_postgres('SELECT 1') == []
    assert not calls
    assert 'unavailable' in capsys.readouterr().err.lower()


def test_reuse_selected_dsn_overrides_all_ambient_settings(monkeypatch,tmp_path):
    monkeypatch.syspath_prepend(str(HOOKS))
    path=tmp_path/'selected.env'
    path.write_text('POSTGRES_HOST=fixture.invalid\nPOSTGRES_PORT=5439\n')
    monkeypatch.setenv('SILL_INSTANCE_ENV_FILE',str(path))
    monkeypatch.setenv('POSTGRES_HOST','stale-live-host')
    calls=[]
    module=load('track-reuse')
    monkeypatch.setattr(module,'psycopg2',SimpleNamespace(connect=lambda *a, **kw:calls.append((a,kw))))
    module.get_db_connection()
    assert 'fixture.invalid:5439' in calls[0][0][0]
    assert 'stale-live-host' not in str(calls)


def test_receipt_guard_invalid_selected_config_is_visibly_unchecked(monkeypatch,tmp_path,capsys):
    monkeypatch.syspath_prepend(str(HOOKS))
    monkeypatch.setenv('SILL_INSTANCE_ENV_FILE',str(tmp_path/'missing.env'))
    monkeypatch.setenv('SILL_BEAT_JOURNAL_DIRS','docs/')
    calls=[]
    monkeypatch.setattr(subprocess,'run',lambda *a, **kw:calls.append(a))
    monkeypatch.setattr(sys,'stdin',io.StringIO(json.dumps({'tool_name':'Write','tool_input':{
        'file_path':'docs/notes.md','content':'Stored: deadbeef'}})))
    with pytest.raises(SystemExit):
        runpy.run_path(str(HOOKS/'stored-slot-guard.py'),run_name='__main__')
    assert not calls
    assert 'unchecked' in capsys.readouterr().out.lower()


def test_reuse_selected_connection_cannot_inherit_pgdefaults(monkeypatch,tmp_path):
    monkeypatch.syspath_prepend(str(HOOKS))
    path=tmp_path/'selected.env'
    path.write_text('SILL_DB_DSN=postgresql://fixture.invalid:5439/fixture\n')
    monkeypatch.setenv('SILL_INSTANCE_ENV_FILE',str(path))
    monkeypatch.setenv('PGUSER','stale-user')
    monkeypatch.setenv('PGSERVICE','stale-service')
    module=load('track-reuse')
    observed=[]
    import os
    def connect(*args,**kwargs):
        observed.append({k:v for k,v in os.environ.items() if k.startswith('PG')})
    monkeypatch.setattr(module,'psycopg2',SimpleNamespace(connect=connect))
    module.get_db_connection()
    assert observed == [{}]
    assert os.environ['PGUSER'] == 'stale-user'


@pytest.mark.parametrize('selector',['','/nonexistent/explicit.env'])
def test_indirect_hook_mint_skips_adjacent_env_even_with_invalid_selector(tmp_path,selector):
    from test_env_and_mcp_wiring import run_mint_path_in, MINT_ENV
    out=run_mint_path_in(tmp_path,MINT_ENV,{'SILL_INSTANCE_ENV_FILE':selector})
    assert out == {'container':'sill_db','user':'sill','name':'sill'}


def test_indirect_hook_mint_selected_transport_preserves_atomic_sql_and_full_rows(monkeypatch,tmp_path):
    import sill
    selected=tmp_path/'selected.env'
    selected.write_text('SILL_DB_DSN=postgresql://fixture:secret@localhost:6543/fixture\n')
    monkeypatch.setenv('SILL_INSTANCE_ENV_FILE',str(selected))
    monkeypatch.setenv('SILL_DB_CONTAINER','stale-live')
    calls=[]
    def run(cmd,**kwargs):
        calls.append((cmd,kwargs))
        return SimpleNamespace(returncode=0,stdout=json.dumps(['id|||newline\n臺灣','inserted',None])+'\n')
    monkeypatch.setattr(subprocess,'run',run)
    sql='WITH linked AS (INSERT INTO fixture VALUES (1) RETURNING id) SELECT id, outcome, restates FROM linked'
    assert sill._query_db(sql,timeout=7) == [['id|||newline\n臺灣','inserted','']]
    cmd,kw=calls[0]
    assert 'docker' not in cmd
    payload=json.loads(kw['input'])
    assert payload['sql'] == sql and payload['row_arrays'] is True
    assert payload['dsn'] == 'postgresql://fixture:secret@localhost:6543/fixture'
    assert 'SILL_DB_CONTAINER' not in kw['env']


def test_indirect_hook_mint_invalid_selector_is_redacted_and_never_falls_back(monkeypatch,capsys):
    import sill
    monkeypatch.setenv('SILL_INSTANCE_ENV_FILE','relative-secret.env')
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw:pytest.fail('ambient DB fallback'))
    assert sill._query_db('SELECT 1') == []
    err=capsys.readouterr().err
    assert 'unavailable' in err and 'secret' not in err
