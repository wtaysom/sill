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


# --- depth -----------------------------------------------------------------------------------
#
# The passage design was rated through an installation whose fast_recall takes as many
# nearest memories as it is asked for as seeds and grows its answer from them. There the
# number asked for changes the list, and long messages had to be asked at the depth of the
# rating (150) before the hook showed what the raters saw. The fast_recall here takes five
# seeds whatever it is asked for, so asking for more only lengthens its list, and recall_rows
# needs no depth. This test is the tripwire for that: if it fails, fast_recall has begun to
# seed by its limit, and the long-message path must then ask at the rated depth.

TOPICAL_EMBEDDING = """
CREATE OR REPLACE FUNCTION public.fixture_hash_vec(seed text) RETURNS float8[] LANGUAGE sql IMMUTABLE AS $$
  SELECT array_agg(((get_byte(decode(md5(seed || ':' || (i / 16)::text), 'hex'), i % 16))::float8 - 127.5) / 127.5 ORDER BY i)
  FROM generate_series(0, public.embedding_dimension() - 1) AS i
$$;
CREATE OR REPLACE FUNCTION public.fixture_embedding(text_content text) RETURNS vector LANGUAGE sql IMMUTABLE AS $$
  SELECT (SELECT array_agg(0.8 * t.v + 0.6 * w.v ORDER BY t.i)
          FROM unnest(public.fixture_hash_vec(COALESCE(substring(text_content from 'topic[0-9]+'), 'none'))) WITH ORDINALITY AS t(v, i)
          JOIN unnest(public.fixture_hash_vec(text_content)) WITH ORDINALITY AS w(v, i) USING (i))::vector
$$;
CREATE OR REPLACE FUNCTION public.get_embedding(text_content text) RETURNS vector LANGUAGE sql AS $$
  SELECT public.fixture_embedding(text_content) $$;
CREATE OR REPLACE FUNCTION public.get_embedding(text_content text, kind text) RETURNS vector LANGUAGE sql AS $$
  SELECT public.fixture_embedding(text_content) $$;
"""


def a_store_with_neighbourhoods(db, tag):
    """Eighty memories on one topic and twenty on another, each with a different embedding
    (the fixture's usual stand-in gives every text the same one), and their neighbourhoods."""
    with db.cursor() as cur:
        cur.execute(TOPICAL_EMBEDDING)
        cur.execute("UPDATE memories SET archived_at=now()")
        for topic, count in (("topic01", 80), ("topic02", 20)):
            for k in range(count):
                cur.execute("SELECT memory_id FROM public.mint_memory('semantic',%s)",
                            (f"{topic} {tag} note {k}: parish record number {k * 7} of the harbour board.",))
        cur.execute("SELECT count(*) FROM (SELECT public.recompute_neighborhood(id) FROM memories "
                    "WHERE archived_at IS NULL) done")


def test_asking_fast_recall_for_more_rows_only_lengthens_its_list(db, monkeypatch):
    core, errors = recall_through(db, monkeypatch)
    tag = uuid4().hex[:10]
    a_store_with_neighbourhoods(db, tag)
    query = f"What does the topic01 {tag} note say about the harbour board?"
    with db.cursor() as cur:
        cur.execute("SELECT memory_id::text FROM public.fast_recall(%s, 45)", (query,))
        shallow = [row[0] for row in cur.fetchall()]
        cur.execute("SELECT memory_id::text FROM public.fast_recall(%s, 150)", (query,))
        deep = [row[0] for row in cur.fetchall()]
    assert len(shallow) == 45 < len(deep), "the store must hold more candidates than the shallower call takes"
    assert set(shallow) == set(deep[:45])
    # And as the hook asks it: a limit of 15 asks fast_recall for 45 rows, a limit of 50 for 150.
    fifteen = [row["id"] for row in core.recall_rows("fast", query, 15)]
    of_fifty = [row["id"] for row in core.recall_rows("fast", query, 50)]
    assert len(fifteen) == 15 and fifteen == of_fifty[:15]
    assert not errors
