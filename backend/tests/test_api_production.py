import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from cognitive_memory_api import CognitiveMemory, MemoryInput
import sill_mcp_server as mcp

PRODUCTION = {'made':[{'kind':'person','who':'Inez','work':'experiment','at':[],
                     'declared':{'by':None,'when':'W','basis':'stated'}}]}
STAMP = {'session':None,'as':None,'when':'W','via':'test','model':None}

class Pool:
    def __init__(self):
        self.conn = AsyncMock()
        self.conn.fetchrow.return_value = {'memory_id':uuid4(), 'outcome':'created', 'restates':None}
    @asynccontextmanager
    async def acquire(self):
        yield self.conn

@pytest.mark.asyncio
async def test_production_is_optional_end_to_end():
    pool = Pool(); client = CognitiveMemory(pool)
    for declaration in (None, PRODUCTION):
        await client.remember('synthetic words', production=declaration, minted_in=STAMP, speaker='Inez')
        sql, *args = pool.conn.fetchrow.call_args.args
        assert 'p_production' in sql and 'p_minted_in' in sql and 'p_speaker' in sql
        assert (json.dumps(declaration) if declaration else None) in args
    pool.conn.reset_mock()
    with pytest.raises(ValueError):
        await client.remember('bad', production={'made':'invalid'})
    pool.conn.fetchrow.assert_not_called()
    with pytest.raises(ValueError):
        await client.remember_batch([MemoryInput('good'), MemoryInput('bad', production={'made':'invalid'})])
    pool.conn.fetch.assert_not_called()

@pytest.mark.asyncio
async def test_stage_policy_discovery_and_dispatch(monkeypatch):
    monkeypatch.delenv('SILL_ENABLE_STAGE', raising=False)
    assert 'stage' not in {t.name for t in mcp._tools()}
    with pytest.raises(ValueError, match='SILL_ENABLE_STAGE'):
        await mcp._dispatch_tool(None, 'stage', {})
    monkeypatch.setenv('SILL_ENABLE_STAGE','1')
    import sys
    monkeypatch.setitem(sys.modules, 'chorus.stage', None)
    assert 'stage' not in {t.name for t in mcp._tools()}
    with pytest.raises(ValueError, match='Chorus'):
        await mcp._dispatch_tool(None, 'stage', {})
    import types
    fn = AsyncMock(return_value={'synthetic':True})
    monkeypatch.setitem(sys.modules, 'chorus.stage', types.SimpleNamespace(run_beat=fn))
    assert 'stage' in {t.name for t in mcp._tools()}
    assert await mcp._dispatch_tool(None, 'stage', {'transcript_path':'/synthetic','beat':{}}) == {'synthetic':True}
    fn.assert_awaited_once()


def test_discovery_declares_attribution():
    tools = {t.name:t.inputSchema for t in mcp._tools()}
    assert 'speakers' in tools
    assert 'speaker' in tools['recall']['properties']
    for name in ('force','speaker','production','minted_in'):
        assert name in tools['remember']['properties']
        assert name not in tools['remember'].get('required',[])


@pytest.fixture(autouse=True)
def synthetic_mint_context(monkeypatch):
    monkeypatch.setattr("cognitive_memory_api.mint_context", lambda via: {"session": None, "as": None, "when": "W", "via": via, "model": None})
