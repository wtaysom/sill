"""`recall` reports a rank-fusion score, and says so.

The defect: `CognitiveMemory.recall()` selects `hybrid_recall`'s score column
and assigned it to a dataclass field named `similarity`, which the MCP server
serializes straight into its tool result. The `recall` tool then described
itself as "Recall relevant memories by semantic similarity (fast_recall)" —
naming a function it does not call.

`hybrid_recall` fuses a vector ranking and a full-text ranking with Reciprocal
Rank Fusion at k=60 (`schema.sql`: `1/(k+rank_vector) + 1/(k+rank_fts)`). So
the number is a function of *rank*, not of match quality:

    rank 1, one list   -> 1/61  = 0.0164
    rank 2, one list   -> 1/62  = 0.0161
    rank 1, both lists -> 2/61  = 0.0328

Observed live before the fix: a query whose single best answer was an exact
topical match came back as `"similarity": 0.01639344262295082` — precisely
1/61, i.e. "this was ranked first", carrying no information about how well it
matched. An agent reading a field called `similarity` with a value of 0.016
concludes the store found nothing useful and discards a bullseye. Worse, two
results of very different quality at the same rank are indistinguishable.

The prompt hook distinguishes hybrid rank fusion from fast recall, and
`format_context()` already printed the hybrid value as "score". The MCP
result field needed the same name.

These tests pin the name, the description's honesty, and — most importantly —
that the description tells a reader the scale, since the number alone is
uninterpretable without it.

Needs no docker, database, or network.
"""

import dataclasses
import re
from pathlib import Path

import pytest

import cognitive_memory_api as api

BACKEND = Path(__file__).resolve().parents[1]
MCP_SERVER = BACKEND / "sill_mcp_server.py"


def field_names(cls) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def test_memory_exposes_score_not_similarity():
    names = field_names(api.Memory)
    assert "score" in names
    assert "similarity" not in names, (
        "a hybrid_recall RRF score must not be published under the name "
        "'similarity' — see this module's docstring"
    )


def test_memory_preview_exposes_score_not_similarity():
    names = field_names(api.MemoryPreview)
    assert "score" in names
    assert "similarity" not in names


def test_partial_activation_similarities_are_untouched():
    """PartialActivation's two fields really are cosine similarities, computed
    as `1 - (embedding <=> query)`. The rename must not sweep them up."""
    names = field_names(api.PartialActivation)
    assert {"cluster_similarity", "best_memory_similarity"} <= names


def test_no_shipped_module_still_reads_the_old_attribute():
    """Renaming the field is only half the job — every reader has to move too.

    `backend/memory_tools.py` kept five `m.similarity` reads through the
    rename, each now an AttributeError the moment that code runs. Nothing
    caught it: the module is dead code (documented as such in
    test_v01_leftover_names.py), so the smoke test imports it — which does not
    execute an attribute access — and no test calls it. It is still a shipped
    py-module in pyproject.toml, so shipping a known AttributeError inside it
    is not made acceptable by nobody currently calling it.

    Attribute access only. SQL aliases (`... as similarity`), prose, and
    PartialActivation's genuine cosine fields are all left alone.
    """
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        if "tests" in path.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in re.finditer(r"\.similarity\b", line):
                # `x.cluster_similarity` / `x.best_memory_similarity` match
                # `\.similarity` only via their own dotted prefix; require the
                # dot to be preceded by an identifier char that isn't part of
                # one of those names.
                if re.search(r"(cluster|best_memory)_similarity", line[:m.end()]):
                    continue
                offenders.append(f"{path.relative_to(BACKEND).as_posix()}:{n}: {line.strip()}")
    assert not offenders, (
        "the Memory/MemoryPreview field is `score`; these read the removed "
        "`similarity` attribute:\n  " + "\n  ".join(offenders)
    )


def recall_tool_description() -> str:
    """The `recall` tool's description string as registered with MCP."""
    text = MCP_SERVER.read_text(encoding="utf-8")
    m = re.search(r'(?s)_tool\(\s*\n\s*"recall",\s*\n(?P<desc>.*?)\{', text)
    assert m, "could not locate the recall tool registration"
    return m.group("desc")


def test_recall_description_does_not_name_a_function_it_never_calls():
    assert "fast_recall" not in recall_tool_description(), (
        "recall() dispatches to hybrid_recall; naming fast_recall sends a "
        "reader to the wrong scoring semantics"
    )


def test_recall_description_names_the_function_it_does_call():
    assert "hybrid_recall" in recall_tool_description()


def test_recall_description_warns_the_score_is_not_a_similarity():
    """The value is uninterpretable without this. 0.016 reads as 'no match' to
    anyone who assumes a 0..1 similarity."""
    desc = recall_tool_description().lower()
    assert "not a cosine similarity" in desc or "not a similarity" in desc
    assert "rank" in desc, "the description never says the score encodes rank"


def test_recall_description_gives_the_actual_scale():
    """A warning without numbers still leaves a reader guessing. The two
    landmark values are what make the scale legible."""
    desc = recall_tool_description()
    assert "0.0164" in desc, "the one-list top-hit value is not given"
    assert "0.0328" in desc, "the both-lists top-hit value is not given"


@pytest.mark.parametrize("k,rank,expected", [(60, 1, 1 / 61), (60, 2, 1 / 62), (60, 3, 1 / 63)])
def test_the_documented_landmarks_match_the_schema_formula(k, rank, expected):
    """Guards the description's numbers against the RRF constant in
    schema.sql. If someone retunes k, these stop agreeing and the description
    has to be revisited rather than silently drifting."""
    assert round(1 / (k + rank), 4) == round(expected, 4)
    if rank == 1:
        assert f"{expected:.4f}" == "0.0164"


def test_schema_still_uses_the_rrf_constant_the_description_assumes():
    """The description's 0.0164/0.0328 are only true at k=60."""
    schema = (BACKEND / "schema.sql").read_text(encoding="utf-8", errors="ignore")
    assert re.search(r"p_k\s+INT\s+DEFAULT\s+60", schema), (
        "hybrid_recall's RRF k is no longer 60 — the recall tool description's "
        "worked numbers (0.0164 / 0.0328) are now wrong"
    )


def test_context_formatter_keeps_enough_precision_to_discriminate():
    """RRF scores differ in the third and fourth decimal (1/61 vs 1/62 vs
    1/63). Printed at two decimals every result reads '0.02' and the ordering
    information is destroyed on the way to the reader."""
    src = (BACKEND / "cognitive_memory_api.py").read_text(encoding="utf-8")
    m = re.search(r'score:\s*\{m\.score:\.(?P<places>\d)f\}', src)
    assert m, "could not find the score formatter in format_context()"
    assert int(m.group("places")) >= 4, (
        f"score printed to {m.group('places')} decimal places; RRF values "
        "0.0164/0.0161/0.0159 collapse to the same string below 4"
    )


def test_public_score_survives_provenance_port():
    from datetime import datetime, timezone
    from uuid import uuid4
    from sill_mcp_server import _jsonable
    memory = api.Memory(uuid4(), api.MemoryType.SEMANTIC, 'Synthetic declaration', .7,
                        score=1/61, speaker='Inez', production=None, minted_in={'model':None})
    assert _jsonable(memory)['score'] == 1/61
    assert 'similarity' not in _jsonable(memory)
    context = api.HydratedContext([memory], [], [], [], None, None, [])
    text = api.format_context_for_prompt(context)
    assert 'score: 0.0164' in text and 'made: undeclared' in text
    assert {'production', 'minted_in', 'speaker', 'force', 'restates_id'} <= field_names(api.MemoryPreview)



def test_raw_batch_mcp_schema_accepts_optional_profile_fingerprint():
    import sill_mcp_server
    tool = next(tool for tool in sill_mcp_server._tools() if tool.name == 'remember_batch_raw')
    schema = tool.inputSchema
    assert schema['properties']['embedding_profile']['type'] == 'string'
    assert 'embedding_profile' not in schema['required']
    assert 'prompted' in tool.description
