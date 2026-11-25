"""A message over 2,000 characters gets recall too, passage by passage.

The prompt hook used to skip any prompt over 2,000 characters: a guard against a
big paste timing out the embedder. A query is cut to 1,000 characters before it
is embedded (recall_core.recall_rows), so the guard protected nothing, and it
took a person's longest messages with it. A typed long message is now split into
what the person wrote and what they quoted or pasted, and recalled for passage
by passage (plugin/hooks/_recall_passages.py).

The passage design was chosen by a blind rating of five candidates on 24 long
messages, all from one person. It has not been rated on anyone else's. The tests
below pin the rated behaviour on invented text; a change to it should be rated
again, not just tested again.

No database and no subprocess: see the prompt_hook fixture in conftest.py.
"""
import json
import stat
import subprocess
import threading
import time
import types

import pytest

from scripts import recall_core as core

REMARKS = [
    "The lighthouse keeper logged every tide by hand — Why did the harbour board stop paying for the ledgers? "
    "The minute book for 1911 ought to say, and I have never seen it quoted anywhere.",
    "Marguerite's orchard lost its Bramley grafts to canker in the wet spring. I would like to know which "
    "rootstock the nursery in Hereford recommended, and whether the replacement scions ever arrived.",
    "On the ferry timetable: the Tuesday crossing was moved to accommodate the cattle market, and nobody told "
    "the schoolchildren on the island. That seems worth a paragraph of its own in the parish history.",
    "You summarised the choir dispute as a quarrel over hymn books — It was about the organist's salary, and "
    "the vestry minutes for March say so plainly. Please correct the chapter before it goes to the printer.",
    "The beekeeping notes are thinner than I hoped. Did the apiary inspector's report mention foulbrood, and "
    "which hives were burned? Old Pritchard kept skeps long after everyone else had moved to frames.",
    "About the mill race: the sluice gate was rebuilt twice, once after the flood and once when the miller "
    "Ambrose Fenn fell out with the water bailiff over the eel traps below the weir.",
    "The school log book has a gap of eleven weeks in the winter of the diphtheria. I suspect the mistress "
    "Henrietta Cole was nursing her own sister, and the curate's diary might confirm the dates.",
    "Nothing in the draft mentions the lace makers — They paid the rent on half the cottages in Tanner's Row, "
    "and the pattern books went to the county museum when the last of them died.",
    "The railway halt opened late because the embankment kept slipping on the blue clay. The contractor's "
    "letters to the directors are in the record office under the name Josiah Hartwell.",
    "The almshouse accounts do not balance for the year the roof was releaded. The treasurer Septimus Oake "
    "resigned at Michaelmas, and the bishop's visitation asked about the missing forty pounds.",
    "I liked the passage on the fair — The cheese rolling was older than the charter, though, and the "
    "magistrates tried to ban it twice before the war.",
    "Finally the quarry: the blasting licence lapsed in the autumn, the foreman Tobias Wren kept working, and "
    "the insurers refused the claim after the slip on the north face.",
]
LONG_TYPED = "\n".join(REMARKS)
LONG_NOTICE = ("<task-notification>\n<task-id>w317</task-id>\n<summary>Workflow finished</summary>\n<result>"
               + "The designer reported on the harbour ledgers and the orchard grafts in detail. " * 40
               + "</result>\n</task-notification>")
ROW = {"id": "00e013f6-0000-4000-8000-000000000001", "type": "semantic", "score": 0.4,
       "importance": 0.7, "content": "The harbour board stopped paying for ledgers in 1911.", "ref": ""}
TIME_HEADER = "[TIME] Wed 2026-11-25 12:20 CST"

assert len(LONG_TYPED) > 2000 and len(LONG_NOTICE) > 2000


def payload_for(prompt_hook, prompt, sid="long-message-test-session", **extra):
    return {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "cwd": str(prompt_hook.log_dir.parent),
            "session_id": sid, "transcript_path": "", **extra}


def run_hook(prompt_hook, prompt, stubs=None, sid="long-message-test-session", whole=False):
    """The whole hook on one prompt, every service stubbed; returns (context, calls)."""
    hook = prompt_hook.head()
    calls = []
    hook.__dict__.update(
        build_time_header=lambda sid, project, **kw: TIME_HEADER,
        query_memories_vector=lambda prompt, keywords=None: calls.append("sill") or [dict(ROW)],
        search_episodic_memory=lambda query: calls.append("history") or [],
    )
    hook.__dict__.update(stubs or {})
    out = prompt_hook.run(hook, payload_for(prompt_hook, prompt, sid))
    return (out, calls) if whole else (out["hookSpecificOutput"]["additionalContext"], calls)


# --- the gate ---------------------------------------------------------------------------

def test_a_typed_message_over_2000_characters_gets_recall(prompt_hook):
    context, calls = run_hook(prompt_hook, LONG_TYPED)
    assert "sill" in calls
    assert "[SPONTANEOUS RECALL]" in context and "harbour board stopped paying" in context


def test_a_long_typed_message_gets_conversation_history_like_any_other(prompt_hook):
    _, calls = run_hook(prompt_hook, LONG_TYPED)
    assert "history" in calls


def test_a_long_notice_still_gets_no_recall(prompt_hook):
    """Long machine-written prompts (a workflow report runs to 10,000 characters) are left as they were."""
    context, calls = run_hook(prompt_hook, LONG_NOTICE)
    assert calls == []
    assert context.startswith("[TIME]") and "[SPONTANEOUS RECALL]" not in context


def test_the_minimum_length_setting_still_applies_to_a_long_message(prompt_hook, monkeypatch):
    monkeypatch.setenv("SILL_RECALL_MIN_QUERY_LENGTH", "10000")
    context, calls = run_hook(prompt_hook, LONG_TYPED)
    assert calls == [] and context == TIME_HEADER


# --- a typed message behind the app's own notes --------------------------------------------
#
# The Claude desktop app puts <system-reminder> blocks in front of what a person typed: a
# session started from a card, the list of linked sessions. The hook took such a prompt
# for a notice: no count, and for a long message no recall.

APP_NOTE = ("<system-reminder>\nThe user started your proposal task_0a1b2c3d (\"Mend the harbour ledger index\") as "
            "session local_0000. It runs on its own; a note reaches you when it ends a turn.\n</system-reminder>\n")


def asked_with(prompt_hook, prompt):
    """What the hook asked the store and the conversation search with, for one prompt."""
    sill, history = [], []
    run_hook(prompt_hook, prompt, stubs={
        "query_memories_vector": lambda prompt, keywords=None: sill.append(prompt) or [dict(ROW)],
        "search_episodic_memory": lambda query: history.append(query) or []})
    return sill, history


def test_a_long_message_behind_the_apps_note_is_asked_with_the_persons_words(prompt_hook):
    sill, history = asked_with(prompt_hook, APP_NOTE + LONG_TYPED)
    assert sill == [LONG_TYPED] and history == [LONG_TYPED[:2000]]


def test_a_message_of_ordinary_length_behind_the_apps_note_is_asked_with_the_persons_words(prompt_hook):
    sill, history = asked_with(prompt_hook, APP_NOTE + REMARKS[0])
    assert sill == [REMARKS[0]] and history == [REMARKS[0]]


def test_an_answer_too_short_to_ask_with_leaves_the_whole_prompt_to_be_asked(prompt_hook):
    """"Yes, try it" asks nothing of the store. The note about what the person just started
    does, so the prompt is asked whole, as it was before."""
    sill, history = asked_with(prompt_hook, APP_NOTE + "Yes, try it")
    assert sill == [APP_NOTE + "Yes, try it"] and history == [APP_NOTE + "Yes, try it"]


def test_a_short_answer_behind_notes_over_2000_characters_gets_the_header_alone(prompt_hook):
    notes = "<system-reminder>\n" + "The list of linked sessions changed again. " * 60 + "\n</system-reminder>\n"
    assert len(notes) > 2000
    sill, history = asked_with(prompt_hook, notes + "Yes, try it")
    assert sill == [] and history == []


def test_the_log_opens_with_the_persons_words_not_the_apps_note(prompt_hook):
    asked_with(prompt_hook, APP_NOTE + REMARKS[0])
    logged = (prompt_hook.log_dir / "spontaneous-recall.log").read_text()
    assert "Query for: The lighthouse keeper" in logged and "system-reminder" not in logged


# --- how a long message is read: a person's remarks, and what they quoted or pasted ----
#
# These pin the behaviour that was rated, on invented text.

def passages_module():
    import _recall_passages
    return _recall_passages


PASTE = ("The orchard at Hereford belongs to Marguerite Fenn. In the spring Marguerite grafted Bramley "
         "scions, and the Hereford nursery sent rootstock by rail. " + "The canker came back each wet year. " * 40)
LONG_PASTE = "\n\n".join("Paragraph %d of the surveyor's report on the quarry face and the blue clay beneath it. " % k * 6
                         for k in range(400))


def test_a_reply_line_splits_into_what_was_quoted_and_what_the_person_wrote():
    text = ("The tide tables were copied by hand — Who kept them after the keeper died?\n"
            "and did the board ever pay him for it?\n"
            "\n"
            "The ferry moved its Tuesday crossing for the cattle market. Nobody told the island school.")
    remarks, docs = passages_module().split_message(text)
    assert remarks == [
        ("The tide tables were copied by hand",
         "Who kept them after the keeper died? and did the board ever pay him for it?"),
        ("", "The ferry moved its Tuesday crossing for the cattle market. Nobody told the island school."),
    ]
    assert docs == []


def test_a_bullet_before_a_reply_line_is_dropped():
    remarks, _ = passages_module().split_message("* Which to keep: the ledger. — How did you choose between them?")
    assert remarks == [("Which to keep: the ledger.", "How did you choose between them?")]


def test_pasted_quoted_and_spoken_text_is_material_not_the_persons_own_words():
    text = ("Here is what she wrote:\n"
            "<pasted_content id=\"a1\">\nDear Edith, the hives are thriving and the heather honey sold out at the fair.\n"
            "</pasted_content>\n\n"
            "> The inspector found no foulbrood in any of the eleven hives this year.\n"
            "> He will return in the spring.\n\n"
            "Claude: I would move the hives before the first frost.\n\n"
            "What should I tell her about the frost?")
    remarks, docs = passages_module().split_message(text)
    assert remarks == [("", "Here is what she wrote: What should I tell her about the frost?")]
    assert docs == [
        "Dear Edith, the hives are thriving and the heather honey sold out at the fair.",
        "The inspector found no foulbrood in any of the eleven hives this year. He will return in the spring.",
        "I would move the hives before the first frost.",
    ]


@pytest.mark.parametrize("label", ["Assistant", "Claude", "Codex", "ChatGPT", "Gemini"])
def test_a_line_that_opens_with_an_assistants_name_quotes_a_model(label):
    text = f"{label}: I would move the hives before the first frost, and sooner on the north side.\n\nWould you?"
    remarks, docs = passages_module().split_message(text)
    assert remarks == [("", "Would you?")]
    assert docs == ["I would move the hives before the first frost, and sooner on the north side."]


def test_a_line_that_opens_with_any_other_word_and_a_colon_is_the_persons_own():
    remarks, docs = passages_module().split_message("Orchard: the grafts failed again this spring, all of them.")
    assert remarks == [("", "Orchard: the grafts failed again this spring, all of them.")] and docs == []


def test_an_unmarked_paste_after_a_short_lead_is_material_too():
    remarks, docs = passages_module().split_message("Here you go...\n\n" + PASTE)
    assert remarks == [("", "Here you go...")]
    assert len(docs) == 1 and docs[0].startswith("The orchard at Hereford belongs to Marguerite Fenn.")


def test_a_url_is_reduced_to_the_words_of_its_path():
    remarks, _ = passages_module().split_message(
        "See https://example.org/archive/harbour-board-minutes-1911 for the vote on the ledgers.")
    assert remarks == [("", "See archive harbour board minutes for the vote on the ledgers.")]


def test_a_question_goes_ahead_of_a_remark_of_the_same_kind_that_asks_nothing():
    text = ("You said the ledgers were burned — They were sold to a dealer in Hereford the following year.\n\n"
            "You said the orchard failed — Which rootstock did the nursery recommend in the end?")
    first, second = passages_module().message_passages(text)
    assert first == "You said the orchard failed — Which rootstock did the nursery recommend in the end?"
    assert second.startswith("You said the ledgers were burned — They were sold")


def test_a_short_reply_keeps_the_line_it_answers_and_a_long_one_keeps_only_its_opening():
    quote = "The lighthouse keeper logged every tide by hand for thirty years, and the harbour board " \
            "stopped paying for the ledgers in 1911 after a vote that the minute book records in full"
    long_own = "That is not what the minute book says. " + "The board voted twice, and the second vote reversed the first. " * 3
    passages = passages_module().message_passages(f"{quote} — Right, who voted against?\n\n{quote} — {long_own}")
    assert len(passages) == 2
    short = next(p for p in passages if p.endswith("who voted against?"))
    long_ = next(p for p in passages if "That is not what" in p)
    assert short == f"{quote} — Right, who voted against?"
    assert long_.startswith(quote[:120] + " — That is not what the minute book says.")
    assert quote[130:] not in long_


def test_a_question_about_a_paste_is_asked_with_the_pastes_names():
    passages = passages_module().message_passages(
        "What do you make of this?\n\n<pasted_content>\n" + PASTE + "\n</pasted_content>")
    assert passages[0].startswith("What do you make of this? (")
    assert "Hereford" in passages[0] and "Marguerite" in passages[0]
    assert passages[1] == " ".join(PASTE.split())[:500]


def test_a_paste_with_no_names_lends_nothing_to_the_question():
    plain = "the canker came back each wet year and the grafts failed again. " * 30
    passages = passages_module().message_passages(
        "What should I tell her about all of this?\n\n<pasted_content>\n" + plain + "\n</pasted_content>")
    assert passages[0] == "What should I tell her about all of this?"


def test_a_paste_leads_only_when_the_person_wrote_almost_nothing_and_asked_nothing():
    passages = passages_module().message_passages("Here you go...\n\n" + PASTE)
    assert passages[0] == " ".join(PASTE.split())[:500]
    assert any(p.startswith("Here you go...") for p in passages[1:])


MANY = "\n\n".join(f"Quoted line number {k} about the parish — My own answer number {k}, long enough to stand "
                   f"as a remark of its own about the parish history." for k in range(40))


def test_passages_are_capped_whatever_the_length():
    module = passages_module()
    assert len(module.message_passages(MANY)) == module.MAX_PASSAGES == 12
    huge = "Here is the whole report:\n\n" + LONG_PASTE
    assert len(huge) > 100_000
    assert 1 <= len(module.message_passages(huge)) <= module.MAX_PASSAGES


def test_odd_input_gives_no_passages_and_no_error():
    module = passages_module()
    for text in ("", "   \n\n  ", "<pasted_content></pasted_content>", "—", "> \n> ", "x" * 5000, None, 7, ["a"]):
        assert isinstance(module.message_passages(text), list)


# --- which memories are shown ---------------------------------------------------------------

THREE = ("You said the ledgers were burned — They were sold to a dealer in the county town the following year.\n\n"
         "You said the orchard failed — Which rootstock did the nursery recommend in the end?\n\n"
         "You said the ferry ran daily — It ran on market days only, and never in the winter months at all.")


def row(mid):
    return {"id": mid, "type": "semantic", "content": f"memory {mid}", "score": 0.4, "importance": 0.5}


def fake_fetch(table, calls=None):
    """fetch(fn, query) answering from {(fn, word in the passage): [ids]}."""
    def fetch(fn, query):
        if calls is not None:
            calls.append((fn, query))
        for (want_fn, word), ids in table.items():
            if want_fn == fn and word in query:
                return [row(i) for i in ids]
        return []
    return fetch


TABLE = {("hybrid", "orchard"): ["A", "X"], ("fast", "orchard"): ["A", "B"],
         ("hybrid", "ferry"): ["C", "X"], ("fast", "ferry"): ["C"],
         ("hybrid", "ledgers"): ["D", "X"], ("fast", "ledgers"): ["E", "D"]}


def test_shown_memories_are_the_shared_one_then_one_for_each_passage_in_turn():
    module, calls = passages_module(), []
    passages = module.message_passages(THREE)
    assert ["orchard" in passages[0], "ferry" in passages[1], "ledgers" in passages[2]] == [True, True, True]
    shown = module.passage_recall(THREE, fetch=fake_fetch(TABLE, calls), limit=8)
    # X was found for all three passages: it goes first. Then each passage's best in turn, then seconds.
    assert [(m["id"], m["passage"]) for m in shown] == [("X", 0), ("A", 0), ("C", 1), ("D", 2), ("B", 0), ("E", 2)]
    assert all(m["passage_text"] == passages[m["passage"]] for m in shown)
    assert shown[1]["content"] == "memory A"
    assert sorted(calls) == sorted((fn, p) for p in passages for fn in ("fast", "hybrid"))


def test_the_default_is_five_memories():
    module = passages_module()
    shown = module.passage_recall(THREE, fetch=fake_fetch(TABLE))
    assert [m["id"] for m in shown] == ["X", "A", "C", "D", "B"]


def test_no_more_than_24_calls_for_any_message():
    module = passages_module()
    for text in (MANY, "Here is the whole report:\n\n" + LONG_PASTE, LONG_TYPED):
        calls = []
        module.passage_recall(text, fetch=fake_fetch({}, calls))
        assert 1 <= len(calls) <= module.PASSAGE_CALLS == 24
        assert all(len(query) <= 1000 for _, query in calls)


def test_no_more_calls_run_at_once_than_the_recall_core_allows():
    """recall_core's rule for one recall is at most MAX_WORKERS child transports at a time.
    Twenty-four calls for one message keep to it."""
    running, peak, lock = [0], [0], threading.Lock()

    def fetch(fn, query):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.02)
        with lock:
            running[0] -= 1
        return []

    passages_module().passage_recall(MANY, fetch=fetch)
    assert 1 < peak[0] <= core.MAX_WORKERS


def test_one_failing_call_loses_only_its_own_rows():
    def fetch(fn, query):
        if fn == "fast":
            raise RuntimeError("the database went away")
        return fake_fetch(TABLE)(fn, query)
    shown = passages_module().passage_recall(THREE, fetch=fetch)
    assert [m["id"] for m in shown] == ["X", "A", "C", "D"]


def test_nothing_found_is_an_empty_list():
    module = passages_module()
    assert module.passage_recall(THREE, fetch=fake_fetch({})) == []
    assert module.passage_recall("", fetch=fake_fetch({})) == []


@pytest.mark.parametrize("error", [core.RecallUnavailable("offline"), RuntimeError("the database went away"),
                                   subprocess.TimeoutExpired("docker", 15)])
def test_when_every_call_fails_the_store_is_unavailable_which_is_not_the_same_as_nothing_found(error):
    """recall_core.fused_recall raises when all its lanes fail, and the hook shows a failed
    store to the person. A long message keeps that."""
    def fetch(fn, query):
        raise error
    with pytest.raises(core.RecallUnavailable):
        passages_module().passage_recall(THREE, fetch=fetch)


# --- names: asked for by full-text match only -----------------------------------------------
#
# hybrid_recall ANDs every word of a query in its full-text half (plainto_tsquery), so a
# list of names matches only a memory that holds all of them. That is rare and, when it
# happens, exact.

NAMED = ("You said the grafts failed — I asked Marguerite about them, and Tobias wrote from Hereford.\n\n"
         "You said nobody answered — Marguerite replied to Tobias the same week, about the Hereford nursery.\n\n"
         "You said the ferry ran daily — It ran on market days only, and never in the winter months at all.")


def test_names_from_the_persons_own_words_make_a_passage_of_their_own():
    passages = passages_module().message_passages(NAMED)
    assert passages[2] == "Marguerite Tobias Hereford"
    assert len(passages) == 4


def test_one_name_alone_is_not_a_names_passage():
    text = ("You said the grafts failed — I asked Marguerite about them and she had kept every letter.\n\n"
            "You said the ferry ran daily — It ran on market days only, and never in the winter.")
    assert len(passages_module().message_passages(text)) == 2


def test_a_pastes_names_follow_its_opening():
    passages = passages_module().message_passages("Here you go...\n\n" + PASTE)
    assert passages[0] == " ".join(PASTE.split())[:500]
    assert passages[1] == "Hereford Marguerite Fenn Bramley"


def test_a_names_passage_is_asked_of_full_text_only_and_counts_only_where_the_words_matched():
    calls = []

    def fetch(fn, query):
        calls.append((fn, query))
        if query == "Marguerite Tobias Hereford":
            return [dict(row("V"), source="vector"), dict(row("N"), source="hybrid"), dict(row("F"), source="fts")]
        return []

    shown = passages_module().passage_recall(NAMED, fetch=fetch, limit=8)
    assert [m["id"] for m in shown] == ["N", "F"]
    assert ("hybrid", "Marguerite Tobias Hereford") in calls
    assert ("fast", "Marguerite Tobias Hereford") not in calls


# --- the hook: routing, what is printed, what is kept ---------------------------------------

PASSAGE_ROW = dict(ROW, passage=1, passage_text='You said the "ledgers" were burned — They were sold to a dealer '
                                               'in the county town the following year.')


def routed(hook, monkeypatch, passage_rows):
    """Stub both retrieval paths; returns the list of calls made."""
    calls = []
    hook.input_data = {}
    hook.recall_passages = types.SimpleNamespace(
        passage_recall=lambda text, **kw: calls.append("passages") or [dict(r) for r in passage_rows])
    monkeypatch.setattr(core, "fused_recall", lambda text, previous="", **kw: calls.append("fused") or [dict(ROW)])
    return calls


def test_a_long_message_is_recalled_for_by_passage_and_a_short_one_as_before(prompt_hook, monkeypatch):
    hook = prompt_hook.head()
    calls = routed(hook, monkeypatch, [PASSAGE_ROW])
    assert hook.query_memories_vector(LONG_TYPED, keywords=["ledgers"])[0]["passage"] == 1
    assert calls == ["passages"]
    del calls[:]
    assert hook.query_memories_vector("Why did the harbour board stop paying for the ledgers?") == [ROW]
    assert calls == ["fused"]


def test_when_passage_recall_finds_nothing_the_opening_of_the_message_is_asked(prompt_hook, monkeypatch):
    hook = prompt_hook.head()
    calls = routed(hook, monkeypatch, [])
    assert hook.query_memories_vector(LONG_TYPED) == [ROW]
    assert calls == ["passages", "fused"]


def test_without_the_passage_module_a_long_message_still_gets_recall(prompt_hook, monkeypatch):
    hook = prompt_hook.head()
    calls = routed(hook, monkeypatch, [PASSAGE_ROW])
    hook.recall_passages = None
    assert hook.query_memories_vector(LONG_TYPED) == [ROW]
    assert calls == ["fused"]


def test_a_passage_module_that_breaks_costs_a_long_message_nothing_but_its_passages(prompt_hook, monkeypatch):
    hook = prompt_hook.head()
    calls = routed(hook, monkeypatch, [])

    def broken(text, **kw):
        raise ValueError("a bug in the splitting")

    hook.recall_passages = types.SimpleNamespace(passage_recall=broken)
    assert hook.query_memories_vector(LONG_TYPED) == [ROW]
    assert calls == ["fused"]


def test_a_store_that_fails_on_the_passages_is_reported_and_not_asked_again(prompt_hook, monkeypatch):
    hook = prompt_hook.head()
    calls = routed(hook, monkeypatch, [])

    def unavailable(text, **kw):
        calls.append("passages")
        raise core.RecallUnavailable("every call failed")

    hook.recall_passages = types.SimpleNamespace(passage_recall=unavailable)
    assert hook.query_memories_vector(LONG_TYPED) is None
    assert calls == ["passages"]


@pytest.mark.parametrize("value,expected", [(None, 5), ("3", 3), ("50", 50)])
def test_the_memory_count_setting_reaches_the_passage_path(prompt_hook, monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("SILL_RECALL_MAX_MEMORIES", value)
    hook, limits = prompt_hook.head(), []
    hook.input_data = {}
    hook.recall_passages = types.SimpleNamespace(
        passage_recall=lambda text, limit, **kw: limits.append(limit) or [dict(PASSAGE_ROW)])
    assert hook.query_memories_vector(LONG_TYPED)
    assert limits == [expected]


def test_a_memory_count_of_zero_skips_store_recall_for_a_long_message_too(prompt_hook, monkeypatch):
    monkeypatch.setenv("SILL_RECALL_MAX_MEMORIES", "0")
    hook = prompt_hook.head()
    calls = routed(hook, monkeypatch, [PASSAGE_ROW])
    assert hook.query_memories_vector(LONG_TYPED) == []
    assert calls == []


def test_each_memory_says_which_part_of_the_message_it_was_recalled_for(prompt_hook):
    out = prompt_hook.head().format_results([dict(PASSAGE_ROW)], [])
    assert out.startswith("[SPONTANEOUS RECALL] Relevant memories (long message; on= is the part each was recalled for):")
    hint = PASSAGE_ROW["passage_text"].replace('"', "'")[:60]      # one line: sixty characters, quotes made safe
    assert hint == "You said the 'ledgers' were burned — They were sold to a dea"
    assert f' on="{hint}…"]' in out
    assert "harbour board stopped paying" in out


def test_a_memory_recalled_the_old_way_prints_as_before(prompt_hook):
    out = prompt_hook.head().format_results([dict(ROW)], [])
    assert out.startswith("[SPONTANEOUS RECALL] Relevant memories:\n")
    assert "on=" not in out


def test_a_memory_recalled_for_a_passage_is_excerpted_around_that_passages_words(prompt_hook, monkeypatch):
    """What is printed of a long memory is its opening and windows near the caller's terms.
    For a long message those are the words of the passage the memory was recalled for, not
    the first eight keywords of the whole message."""
    memory = ("The parish history has a chapter on the orchard. " + "It is a long chapter. " * 30
              + "The rootstock came from the Hereford nursery by rail. " + "It ends with the fair. " * 30)
    found = dict(ROW, content=memory[:500], passage=0,
                 passage_text="You said the orchard failed — Which rootstock did the nursery recommend in the end?")
    hook = prompt_hook.head()
    hook.input_data = {}
    hook.recall_passages = types.SimpleNamespace(passage_recall=lambda text, **kw: [dict(found)])
    monkeypatch.setattr(core, "full_contents", lambda ids, **kw: {ROW["id"]: memory})
    [shown] = hook.query_memories_vector(LONG_TYPED, keywords=["lighthouse", "keeper", "tide"])
    assert shown["content"].startswith("The parish history has a chapter on the orchard.")
    assert "rootstock came from the Hereford nursery" in shown["content"]
    assert len(shown["content"]) < 450


def fat(k):
    return dict(ROW, id=f"00e013f6-0000-4000-8000-{k:012d}", content="The harbour board. " * 26,
                ref="docs/" + "a-very-long-path-segment/" * 40 + f"{k}.md")


def test_what_is_shown_is_cut_to_fit_under_the_harness_cap(prompt_hook):
    """Over 10,000 characters Claude Code swaps a hook's context for a file path and a preview."""
    hook = prompt_hook.head()
    memories, conversations = [fat(k) for k in range(8)], []
    kept, kept_conversations = hook.fit_to_cap(memories, conversations, overhead=1200)
    assert 1 <= len(kept) < 8 and kept == memories[:len(kept)]
    assert len(hook.format_results(kept, kept_conversations)) + 1200 <= hook.CONTEXT_CAP <= 10_000
    assert len(hook.format_results(memories[:len(kept) + 1], [])) + 1200 > hook.CONTEXT_CAP
    assert hook.fit_to_cap([dict(ROW)], [], overhead=1200) == ([ROW], [])


def test_hook_output_stays_under_the_cap_with_fat_memories(prompt_hook):
    stubs = {"query_memories_vector": lambda prompt, keywords=None: [fat(k) for k in range(8)]}
    out, _ = run_hook(prompt_hook, LONG_TYPED, stubs, whole=True)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert len(context) <= 10_000 and len(out["systemMessage"]) <= 10_000
    assert context.startswith("[TIME]") and "[SPONTANEOUS RECALL]" in context


def test_only_what_was_printed_is_counted_as_shown(prompt_hook, monkeypatch):
    """A memory cut to fit the cap was never in front of the model: it is not journalled
    for reuse tracking and its exposure is not counted."""
    exposed = []
    monkeypatch.setattr(core, "record_exposure", lambda ids, **kw: exposed.extend(ids))
    stubs = {"query_memories_vector": lambda prompt, keywords=None: [fat(k) for k in range(8)]}
    context, _ = run_hook(prompt_hook, LONG_TYPED, stubs)
    printed = [fat(k)["id"] for k in range(8) if f"/{k}.md" in context]
    assert 1 <= len(printed) < 8 and exposed == printed
    [journal] = prompt_hook.log_dir.glob("recall-sidecar-*.jsonl")
    [entry] = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [m["id"] for m in entry["memories"]] == printed


def test_the_session_journal_keeps_no_part_of_the_message(prompt_hook):
    """What the hook showed is journalled for reuse tracking: ids and the memories' text. The
    passage a memory was recalled for is the person's own words, and is not written there."""
    stubs = {"query_memories_vector": lambda prompt, keywords=None: [dict(PASSAGE_ROW)]}
    run_hook(prompt_hook, LONG_TYPED, stubs)
    [journal] = prompt_hook.log_dir.glob("recall-sidecar-*.jsonl")
    [entry] = [json.loads(line) for line in journal.read_text().splitlines()]
    assert entry["memories"] == [{"id": ROW["id"], "content": ROW["content"]}]
    written = "".join(p.read_text() for p in prompt_hook.log_dir.iterdir() if p.is_file())
    assert "county town" not in written


def test_fitting_to_the_cap_does_not_log_the_recalled_ids_again(prompt_hook):
    hook = prompt_hook.head()
    lines = []
    hook.log = lines.append
    hook.fit_to_cap([dict(ROW)], [], overhead=100)
    assert lines == []
    hook.format_results([dict(ROW)], [])
    assert len([line for line in lines if line.startswith("Recalled IDs")]) == 1


def test_the_conversation_search_is_asked_with_the_opening_of_what_was_typed(prompt_hook):
    """The optional conversation search takes its query on the command line. It is given no
    more of a message than it was ever given: 2,000 characters, and never a wrapper."""
    cursor = ("<timestamp>Tuesday, November 25, 2026, 2:00 PM</timestamp>\n<user_query>\n" + LONG_TYPED
              + "\n</user_query>")
    short = "Why did the harbour board stop paying for the ledgers?"
    for prompt, expected in ((LONG_TYPED, LONG_TYPED[:2000]), (cursor, LONG_TYPED[:2000]), (short, short)):
        asked = []
        run_hook(prompt_hook, prompt, {"search_episodic_memory": lambda query: asked.append(query) or []})
        assert asked == [expected]


# --- hardening carried over with the port ---------------------------------------------------

def test_a_names_row_that_also_crossed_a_bridge_still_counts():
    def fetch(fn, query):
        if query == "Marguerite Tobias Hereford":
            return [dict(row("B1"), source="hybrid+bridge"), dict(row("B2"), source="fts+bridge"),
                    dict(row("B3"), source="bridge"), dict(row("B4"), source="vector")]
        return []
    assert [m["id"] for m in passages_module().passage_recall(NAMED, fetch=fetch, limit=8)] == ["B1", "B2"]


def test_a_row_without_an_id_is_skipped_and_the_rest_are_kept():
    def fetch(fn, query):
        rows = fake_fetch(TABLE)(fn, query)
        return [{"content": "no id"}, "not a row"] + rows if "orchard" in query else rows
    assert [m["id"] for m in passages_module().passage_recall(THREE, fetch=fetch)] == ["X", "A", "C", "D", "B"]


def test_a_long_run_without_full_stops_is_cut_into_pieces_not_cut_off():
    bullets = "\n".join(f"- item {k} about the harbour ledgers and the orchard grafts in the wet spring"
                        for k in range(60))
    assert len(bullets) > 3500
    remarks, _ = passages_module().split_message(bullets)
    assert all(not quoted and len(own) <= 900 for quoted, own in remarks)
    assert sum(len(own) for _, own in remarks) >= len(bullets) - 60 * 3 - len(remarks)
    assert "item 59 about" in remarks[-1][1]


def test_thousands_of_unclosed_paste_tags_do_not_stall_the_hook():
    """A pattern that rescanned to the end for each unclosed tag took 39 seconds on 20,000 of them."""
    module = passages_module()
    prose = "The harbour board stopped paying for the ledgers in the wet spring of that year. " * 40
    for text in (prose + "<pasted_content>" * 20_000, prose + "<pasted_content" * 60_000,
                 prose + "<pasted_content>one</pasted_content>" * 20_000):
        started = time.monotonic()
        assert isinstance(module.message_passages(text), list)
        assert time.monotonic() - started < 5.0


def test_a_message_of_megabytes_is_read_only_as_far_as_it_can_matter():
    module = passages_module()
    text = "Harbour ledger orchard graft words\n" * 200_000
    started = time.monotonic()
    passages = module.message_passages(text)
    assert time.monotonic() - started < 5.0
    assert 1 <= len(passages) <= module.MAX_PASSAGES


def test_the_message_is_split_once_however_often_its_passages_are_asked_for(monkeypatch):
    module = passages_module()
    splits, real = [], module.split_message
    monkeypatch.setattr(module, "split_message", lambda text: splits.append(1) or real(text))
    text = THREE + " A fresh sentence so no earlier call has cached this exact message."
    module.passage_recall(text, fetch=fake_fetch(TABLE))
    assert module.passage_queries(text) == module.message_passages(text)
    assert len(splits) == 1


def test_one_memory_can_be_asked_for():
    module = passages_module()
    assert [m["id"] for m in module.passage_recall(THREE, fetch=fake_fetch(TABLE), limit=1)] == ["X"]
    assert module.passage_recall(THREE, fetch=fake_fetch(TABLE), limit=0) == []


WRAPPED = {
    "desktop paste": '<pasted_content id="a1">\n' + LONG_TYPED + "\n</pasted_content>",
    "cursor": "<timestamp>Tuesday, November 25, 2026, 2:00 PM</timestamp>\n<user_query>\n" + LONG_TYPED
              + "\n</user_query>",
    "codex files": "# Files mentioned by the user:\n\n## notes.md: /home/x/notes.md\n\n"
                   "## My request for Codex:\n" + LONG_TYPED,
}


def test_a_wrapped_message_is_unwrapped_before_it_is_split(prompt_hook):
    """Otherwise Cursor's timestamp line becomes the first passage, and under Codex's file
    preamble the whole request is filed as a paste."""
    for name, prompt in WRAPPED.items():
        hook, seen = prompt_hook.head(), []
        hook.input_data = {}
        hook.recall_passages = types.SimpleNamespace(
            passage_recall=lambda text, **kw: seen.append(text) or [dict(PASSAGE_ROW)])
        assert hook.query_memories_vector(prompt), name
        assert seen == [LONG_TYPED], name
        assert hook.typed_body(prompt) == LONG_TYPED, name
    assert prompt_hook.head().typed_body(LONG_TYPED) == LONG_TYPED


def test_a_query_with_no_time_left_is_not_started(monkeypatch):
    def run(*args, **kwargs):
        raise AssertionError("the database must not be asked")

    monkeypatch.delenv("SILL_INSTANCE_ENV_FILE", raising=False)
    monkeypatch.setattr(core.subprocess, "run", run)
    for lane in ("fast", "hybrid"):
        with pytest.raises(core.RecallUnavailable):
            core.recall_rows(lane, "the harbour board ledgers", deadline=time.monotonic() - 1)


def test_a_query_waits_no_longer_than_it_is_given(monkeypatch):
    waits = []

    def run(command, **kwargs):
        waits.append(kwargs["timeout"])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.delenv("SILL_INSTANCE_ENV_FILE", raising=False)
    monkeypatch.setattr(core.subprocess, "run", run)
    assert core.recall_rows("fast", "the harbour board ledgers", deadline=time.monotonic() + 4.5) == []
    assert core.recall_rows("fast", "the harbour board ledgers") == []
    assert 4 < waits[0] <= 4.5 and waits[1] == core.QUERY_TIMEOUT == 15


def test_when_time_has_run_out_the_message_is_not_asked_a_second_time(prompt_hook, monkeypatch):
    """Nothing came back because the calls ran out of time, not because nothing matched.
    Asking again would wait as long again."""
    hook = prompt_hook.head()
    calls = routed(hook, monkeypatch, [])
    hook._SUBPROCESS_DEADLINE = time.monotonic() + 6       # retrieval's share of the hook's time is spent
    assert hook.query_memories_vector(LONG_TYPED) == []
    assert calls == ["passages"]
    hook._SUBPROCESS_DEADLINE = time.monotonic() + 40
    assert hook.query_memories_vector(LONG_TYPED) == [ROW]
    assert calls == ["passages", "passages", "fused"]


def test_every_retrieval_call_for_one_message_shares_one_deadline(prompt_hook, monkeypatch):
    """Each passage call is given the hook's settings and the deadline the fallback gets too."""
    hook, given = prompt_hook.head(), {}
    hook.input_data = {}

    def rows(fn, query, limit=15, **kw):
        given["rows"] = (fn, limit, kw)
        return []

    monkeypatch.setattr(core, "recall_rows", rows)
    monkeypatch.setattr(core, "fused_recall", lambda text, previous="", **kw: given.update(fused=kw) or [dict(ROW)])
    hook.recall_passages = types.SimpleNamespace(
        passage_recall=lambda text, fetch, **kw: fetch("fast", "the harbour board ledgers"))
    started = time.monotonic()
    assert hook.query_memories_vector(LONG_TYPED) == [ROW]
    fn, limit, kw = given["rows"]
    assert (fn, limit, kw["fast_min_similarity"]) == ("fast", core.FUSED_POOL, hook.FAST_MIN_SCORE)
    assert started + 14 < kw["deadline"] <= time.monotonic() + 15
    assert kw["deadline"] <= hook._SUBPROCESS_DEADLINE - 7
    assert given["fused"]["deadline"] == kw["deadline"]


def test_a_hung_store_costs_a_long_message_one_wait_and_is_reported(prompt_hook, monkeypatch):
    """With the database hung, every call waits out its time. Passage by passage and then the
    opening would be two waits; one deadline makes it one, as for a short message, and what
    the person sees is a failed store."""
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    hook, asked = prompt_hook.head(), []

    def hung(fn, query, limit=15, *, fast_min_similarity=.25, deadline=None):
        asked.append(fn)
        if deadline <= clock[0]:
            raise core.RecallUnavailable("Recall deadline exceeded")
        clock[0] = deadline
        raise core.RecallUnavailable("Recall store request could not complete")

    def second_ask(*args, **kwargs):
        raise AssertionError("the message must not be asked a second time")

    monkeypatch.setattr(core, "recall_rows", hung)
    monkeypatch.setattr(core, "fused_recall", second_ask)
    hook.build_time_header = lambda sid, project, **kw: TIME_HEADER
    hook.search_episodic_memory = lambda query: []
    out = prompt_hook.run(hook, payload_for(prompt_hook, LONG_TYPED))
    assert asked and clock[0] == 1015.0
    assert out["systemMessage"].startswith("[TIME]") and "Store recall failed" in out["systemMessage"]
    assert out["hookSpecificOutput"]["additionalContext"] == TIME_HEADER


def test_an_oversized_pattern_check_is_cut_and_the_time_header_kept(prompt_hook):
    """The cap applies to the whole context: trimming memories alone left a large notice over it."""
    sid = "00000000-0000-4000-8000-00000000c0de"
    for prompt in (LONG_TYPED, "yes"):
        (prompt_hook.log_dir / f"response-patterns-last-{sid}.json").write_text(
            json.dumps({"warnings": ["You agreed without checking. " * 600]}))
        context, _ = run_hook(prompt_hook, prompt, sid=sid)
        assert len(context) <= 10_000
        assert TIME_HEADER in context and context.startswith("[PATTERN CHECK")


def test_memories_go_before_conversations_when_fitting(prompt_hook):
    hook = prompt_hook.head()
    talk = {"content": "We measured the downloads. " * 10, "date": "2026-10-29"}
    memories, conversations = [fat(k) for k in range(3)], [dict(talk), dict(talk, date="2026-10-30")]
    kept, kept_talk = hook.fit_to_cap(memories, conversations, overhead=hook.CONTEXT_CAP - 2500)
    assert len(kept) < 3 and kept_talk == conversations
    assert hook.fit_to_cap(memories, conversations, overhead=hook.CONTEXT_CAP - 300) == ([], [])


def test_the_cap_is_measured_as_the_harness_would_measure_it(prompt_hook):
    """An emoji is one character to Python and two to anything that counts UTF-16 units."""
    hook = prompt_hook.head()
    emoji = [dict(ROW, id=f"00e013f6-0000-4000-8000-{k:012d}", content="\U0001F41D" * 500) for k in range(8)]
    kept, _ = hook.fit_to_cap(emoji, [], overhead=2000)
    printed = hook.format_results(kept, [], log_ids=False)
    assert len(printed.encode("utf-16-le")) // 2 + 2000 <= hook.CONTEXT_CAP
    assert 1 <= len(kept) < 8


def test_the_log_takes_any_text_and_keeps_the_line(prompt_hook):
    """A prompt can carry a lone surrogate. The line used to be dropped whole."""
    hook = prompt_hook.head()
    hook.log("Query for: \ud800 a lone surrogate, as a prompt can carry")
    assert "a lone surrogate" in hook.LOG_FILE.read_text(encoding="utf-8")


def test_the_log_is_readable_by_its_owner_only(prompt_hook):
    """It holds the opening of each prompt recalled for, and by default it lives in /tmp."""
    hook = prompt_hook.head()
    hook.log("first line")
    assert stat.S_IMODE(hook.LOG_FILE.stat().st_mode) == 0o600
    hook.LOG_FILE.chmod(0o644)              # as an earlier version of the hook created it
    hook.log("second line")
    assert stat.S_IMODE(hook.LOG_FILE.stat().st_mode) == 0o600
    assert hook.LOG_FILE.read_text().count(" line\n") == 2


def test_a_failed_conversation_search_does_not_write_the_message_into_the_log(prompt_hook, monkeypatch, tmp_path):
    """A timeout's own text holds the command, and the command holds the query."""
    secret = "Dear Edith, it was lovely to see you at the quarry"
    cli = tmp_path / "episodic-memory"
    cli.touch()
    monkeypatch.setenv("SILL_EPISODIC_MEMORY_PATH", str(cli))
    hook = prompt_hook.head()

    def timed_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 30))

    def failed(command, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout="", stderr=f"error: no index for '{secret}'")

    def crashed(command, **kwargs):
        raise OSError(7, f"Argument list too long: {secret}")

    for run in (timed_out, failed, crashed):
        monkeypatch.setattr(subprocess, "run", run)
        assert hook.search_episodic_memory(secret + ", and so to the blasting licence.") == []
    logged = hook.LOG_FILE.read_text()
    assert logged.count("Episodic search") == 3 and "lovely to see you" not in logged


def test_a_long_message_that_opens_with_a_lone_surrogate_still_gets_its_header(prompt_hook):
    context, calls = run_hook(prompt_hook, "\ud800 " + LONG_TYPED)
    assert context.startswith("[TIME]") and "sill" in calls
