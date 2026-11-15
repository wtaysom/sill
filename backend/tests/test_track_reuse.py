"""Guard logic is pure; import the hook as a module via importlib."""

import importlib.util
import json
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / "plugin" / "hooks" / "track-reuse.py"
spec = importlib.util.spec_from_file_location("track_reuse", HOOK)
tr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tr)


def _mem(mid, content):
    return {"id": mid, "content": content}


def test_phrase_sampling_skips_the_head():
    head = "Alpha beta gamma delta epsilon zeta"          # 6 head words
    body = "the distinctive body phrase continues here uniquely"
    m = _mem("m1", head + " " + body)
    resp = "Unrelated text ... the distinctive body phrase continues ... more"
    ev = tr.find_reuse_phrase(m, resp.lower())
    assert ev and ev != "__ID__"
    resp_head_only = "quoting: Alpha beta gamma delta epsilon zeta"
    assert tr.find_reuse_phrase(m, resp_head_only.lower()) is None


def test_shared_phrase_rejected_as_title_not_evidence():
    shared = "the certification queue rule holds"
    m1 = _mem("m1", "x " * 6 + shared + " tail one")
    m2 = _mem("m2", "y " * 6 + shared + " tail two")
    reused = tr.detect_reuse([m1, m2], ("prefix " + shared + " suffix").lower())
    assert reused == []


def test_burst_limit_zeroes_citation_sweeps():
    mems, resp = [], ""
    for i in range(5):
        body = f"unique body phrase number {i} continues distinctly onward here"
        mems.append(_mem(f"m{i}", "h " * 6 + body))
        resp += " ... " + body
    assert tr.detect_reuse(mems, resp.lower()) == []


def test_sidecar_reader_time_gates_recent_only(tmp_path, monkeypatch):
    # Timezone-aware on purpose: `now` in read_recall_sidecars() is built via
    # datetime.now(timezone.utc), so a naive "2000-01-01T00:00:00" fixture
    # made `now - ts` raise TypeError (naive vs aware) — caught by the
    # surrounding `except Exception: continue` — which excluded the stale
    # entry for the wrong reason (any parse failure, not staleness) and left
    # the actual `now - ts > window` comparison unexercised. This form goes
    # through that comparison for real.
    monkeypatch.setenv("SILL_LOG_DIR", str(tmp_path))
    (tmp_path / "recall-sidecar-recent.jsonl").write_text(json.dumps(
        {"ts": "2000-01-01T00:00:00+00:00", "source": "spontaneous-recall",
         "memories": [{"id": "00000000-0000-0000-0000-000000000000",
                       "content": "stale entry"}]}) + "\n")
    assert tr.read_recall_sidecars(None) == []


def test_sidecar_reader_time_gates_per_session_too(tmp_path, monkeypatch):
    """The per-session file used to be read un-time-gated (time_gated=False),
    so it grew monotonically for the life of a session — every recall turn
    added memories that never dropped off, eventually tripping BURST_LIMIT
    on pool size alone. A stale and a fresh entry in the SAME per-session
    file confirm the fix filters by recency, not by dropping the
    per-session file's contents wholesale."""
    from datetime import datetime, timezone
    monkeypatch.setenv("SILL_LOG_DIR", str(tmp_path))
    fresh_ts = datetime.now(timezone.utc).isoformat()
    lines = [
        json.dumps({"ts": "2000-01-01T00:00:00+00:00", "source": "spontaneous-recall",
                     "memories": [{"id": "00000000-0000-0000-0000-000000000000",
                                   "content": "stale per-session entry"}]}),
        json.dumps({"ts": fresh_ts, "source": "spontaneous-recall",
                     "memories": [{"id": "11111111-1111-1111-1111-111111111111",
                                   "content": "fresh per-session entry"}]}),
    ]
    (tmp_path / "recall-sidecar-sha256-95f1697502db2b8afa53499e60cd8d9b34de26fb5708861ef2dc843457f5f0f7.jsonl").write_text("\n".join(lines) + "\n")
    out = tr.read_recall_sidecars("sessA")
    ids = {m["id"] for m in out}
    assert ids == {"11111111-1111-1111-1111-111111111111"}


def test_burst_limit_fires_alone_when_guard2_passes():
    """Guard 3 isolated (review addendum): unique first-window phrases pass
    guard 2; volume alone trips the burst zeroing at n > BURST_LIMIT."""
    def mem(i):
        body = f"distinct{i} marker{i} phrase{i} continues onward here"
        return _mem(f"m{i}", "h " * 6 + body)
    resp3 = " ... ".join(f"distinct{i} marker{i} phrase{i}" for i in range(3))
    assert len(tr.detect_reuse([mem(i) for i in range(3)], resp3.lower())) == 3
    resp4 = " ... ".join(f"distinct{i} marker{i} phrase{i}" for i in range(4))
    assert tr.detect_reuse([mem(i) for i in range(4)], resp4.lower()) == []


def test_fresh_global_recent_never_becomes_another_sessions_context(tmp_path,monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setenv('SILL_LOG_DIR',str(tmp_path))
    (tmp_path/'recall-sidecar-recent.jsonl').write_text(json.dumps({
        'ts':datetime.now(timezone.utc).isoformat(),'source':'spontaneous-recall',
        'memories':[{'id':'unrelated','content':'Other session context'}]})+'\n')
    assert tr.read_recall_sidecars(None) == []
    assert tr.read_recall_sidecars('known-session') == []
