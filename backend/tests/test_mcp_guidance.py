"""What the MCP server tells the model on the other end.

In a client without Sill's hooks, such as a desktop app or a hosted chat, the
initialize instructions and the tool descriptions are the only guidance the
model receives about when to recall, when to remember, and what `speaker`,
`force` and `production` mean. These tests keep that guidance present and in
step with what the store accepts.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import sill
import sill_mcp_server as server

BACKEND = Path(__file__).resolve().parents[1]


def _tool(name: str):
    return next(tool for tool in server._tools() if tool.name == name)


def test_instructions_cover_both_halves_of_the_loop():
    text = server.SERVER_INSTRUCTIONS
    assert "`recall`" in text and "`remember`" in text
    assert "speaker" in text and "production" in text
    # It rides in every session's context, so it stays short.
    assert len(text) < 1800


def test_instructions_reach_the_initialize_options():
    models = pytest.importorskip("mcp.server.models")
    types = pytest.importorskip("mcp.types")
    options = server._initialization_options(
        models.InitializationOptions,
        server_name="sill",
        server_version="0",
        capabilities=types.ServerCapabilities(tools=types.ToolsCapability()),
    )
    if "instructions" not in models.InitializationOptions.model_fields:
        pytest.skip("this MCP SDK predates initialize instructions")
    assert options.instructions == server.SERVER_INSTRUCTIONS


def test_an_sdk_without_instructions_still_gets_valid_options():
    class OldOptions:
        model_fields = {"server_name": None, "server_version": None, "capabilities": None}

        def __init__(self, **kwargs):
            self.kwargs = kwargs

    options = server._initialization_options(OldOptions, server_name="sill")
    assert options.kwargs == {"server_name": "sill"}


def test_remember_says_what_speaker_and_production_are():
    description = _tool("remember").description
    assert "speaker" in description and "production" in description
    assert "never evidence" in description
    assert "restatement" in description


def test_recall_says_results_are_pointers():
    description = _tool("recall").description
    assert "speaker" in description and "production" in description
    assert "pointer" in description


def test_force_enum_matches_the_mint_path_and_the_schema():
    force = _tool("remember").inputSchema["properties"]["force"]
    offered = {value for value in force["enum"] if value is not None}
    assert None in force["enum"]
    assert offered == set(sill.VALID_FORCES)

    migration = (BACKEND / "migrations" / "001_speech_act_force_speaker.sql").read_text()
    check = re.search(r"force IN\s*\(([^)]*)\)", migration)
    assert check, "the force CHECK constraint moved; update this test's source of truth"
    assert offered == set(re.findall(r"'([a-z]+)'", check.group(1)))
    for value in offered:
        assert value in force["description"]


def test_the_production_example_is_one_the_store_accepts():
    import json

    import provenance

    provenance.validate(server.PRODUCTION_EXAMPLE)
    description = _tool("remember").inputSchema["properties"]["production"]["description"]
    assert json.dumps(server.PRODUCTION_EXAMPLE, separators=(",", ":")) in description


def test_the_compact_form_is_refused_without_a_session_as_described():
    import provenance

    with pytest.raises(provenance.ProvenanceError):
        provenance.production_from_value(
            "Inez: ran the timing experiment [stated]",
            session=None, as_=None, when="2026-11-13T00:00:00Z",
        )
    description = _tool("remember").inputSchema["properties"]["production"]["description"]
    assert "refused" in description


def test_batch_items_carry_the_same_attribution_fields():
    single = _tool("remember").inputSchema["properties"]
    item = _tool("remember_batch").inputSchema["properties"]["memories"]["items"]["properties"]
    for name in ("force", "speaker", "production"):
        assert item[name] == single[name]
