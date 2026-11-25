"""A long message's passages against the real recall functions.

The passage design leans on something only the database can show: the full-text
half of hybrid_recall matches a memory that holds every word of a query and no
other, and its `source` column says whether the words matched. A list of names is
asked that way, and only rows where they matched are kept
(plugin/hooks/_recall_passages.py). The unit tests check that rule on invented
rows; the first test here checks that the store returns rows of that shape, and
the second that the whole path runs on the real functions.

Synthetic text, in a transaction that is rolled back.
"""
import json
from uuid import uuid4


def recall_through(db, monkeypatch):
    """Send recall_core's SQL down the fixture's own connection, uncommitted rows and all.

    Returns (recall_core, errors). One connection and one transaction serve every call,
    and passage_recall makes its calls from several threads and treats a failed one as
    lost rows. A statement that fails aborts the transaction for the rest, so the
    failures are kept here for the test to assert on directly.
    """
    from scripts import recall_core as core

    errors = []

    def query(sql):
        try:
            with db.cursor() as cur:
                cur.execute(sql)
                return "\n".join(json.dumps(row[0]) for row in cur.fetchall())
        except Exception as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(core, "_run_sql", query)
    return core, errors


def mint(db, *texts):
    ids = []
    with db.cursor() as cur:
        cur.execute("UPDATE memories SET archived_at=now()")
        for text in texts:
            cur.execute("SELECT memory_id FROM public.mint_memory('semantic',%s)", (text,))
            ids.append(str(cur.fetchone()[0]))
    return ids


def named(tag):
    return tuple(name + tag for name in ("Marguerite", "Tobias", "Hereford"))


def test_the_full_text_half_marks_only_the_memory_that_holds_every_name(db, monkeypatch):
    core, errors = recall_through(db, monkeypatch)
    one, two, three = named(uuid4().hex[:10])
    holds_all, holds_one, holds_none = mint(
        db,
        f"{one} wrote to {two} from {three} about the orchard grafts.",
        f"{one} kept the tide tables for thirty years.",
        "The ferry ran on market days only.")
    sources = {row["id"]: row["source"] for row in core.recall_rows("hybrid", f"{one} {two} {three}", 100)}
    assert sources[holds_all] in ("hybrid", "fts")
    assert sources.get(holds_one, "vector") == "vector"
    assert sources.get(holds_none, "vector") == "vector"
    assert not errors


def test_a_long_message_is_recalled_for_through_the_real_functions(db, monkeypatch):
    import _recall_passages as passages

    _, errors = recall_through(db, monkeypatch)
    one, two, three = named(uuid4().hex[:10])
    holds_all, _, _ = mint(
        db,
        f"{one} wrote to {two} from {three} about the orchard grafts.",
        f"{one} kept the tide tables for thirty years.",
        "The ferry ran on market days only.")
    message = (f"You said the grafts failed — I asked {one} about them, and {two} wrote from {three}.\n\n"
               f"You said nobody answered — {one} replied to {two} the same week, about the {three} nursery.\n\n"
               "You said the ferry ran daily — It ran on market days only, and never in the winter months at all.")
    names = f"{one} {two} {three}"
    assert names in passages.message_passages(message)
    shown = passages.passage_recall(message, limit=50)
    assert not errors
    assert holds_all in [memory["id"] for memory in shown]
    assert len({memory["id"] for memory in shown}) == len(shown)
    assert all(isinstance(memory["passage"], int) and memory["passage_text"] for memory in shown)
