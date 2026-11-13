"""Run with a clean wheel venv, from outside the repository (python -I)."""
import asyncio
import importlib
from importlib.metadata import distribution
import os
from pathlib import Path
import sys


def main():
    package = distribution('sill-memory')
    root = Path(package.locate_file('')).resolve()
    assert root.is_relative_to(Path(sys.prefix).resolve()), root
    for name in package.read_text('top_level.txt').splitlines():
        if name == 'memory_health':
            # Diagnostics explicitly require the optional numpy extra. A basic
            # wheel must retain its actionable missing-extra error.
            try:
                module = importlib.import_module(name)
            except ImportError as exc:
                assert isinstance(exc.__cause__, ModuleNotFoundError)
                assert exc.__cause__.name == 'numpy' and 'diagnostics' in str(exc)
                continue
        else:
            module = importlib.import_module(name)
        assert Path(module.__file__).resolve().is_relative_to(root), module.__file__
    import sill_mcp_server as server
    from mcp.server import Server
    assert callable(Server('wheel-smoke').list_tools)
    os.environ.pop('SILL_ENABLE_STAGE', None)
    names = {tool.name for tool in server._tools()}
    assert {'remember','remember_batch','recall','speakers','link_commitment','change_goal_priority'} <= names, names
    assert 'stage' not in names
    os.environ['SILL_ENABLE_STAGE'] = '1'
    assert 'stage' not in {tool.name for tool in server._tools()}
    try:
        asyncio.run(server._dispatch_tool(None, 'stage', {}))
    except ValueError as exc:
        assert 'Chorus' in str(exc), exc
    else:
        raise AssertionError('standalone wheel unexpectedly has Chorus stage')
    print(f'Clean wheel {package.version}: installed imports and {len(names)} MCP tools; optional stage absent')


if __name__ == '__main__':
    main()
