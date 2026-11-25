"""Recall for a long message, passage by passage: the person's own words first.

recall_core.fused_recall() reads the first 500 characters of a message plus
eight keywords. For a message over 2,000 characters (several separate remarks,
or a line with a pasted document) that leaves most of it unasked. Here the
message is split into what the person wrote and what they quoted or pasted;
their remarks drive the queries, one passage each, and a paste is material: its
opening, two inner pieces, and its names lent to the question when the person
wrote little else.

Not the person's own: <pasted_content> blocks, ">" lines, lines that open with
an assistant's name and a colon ("Claude: ..."), the run after a short lead
ending "..." or ":" when no other paste is marked, and the text before " — " on
a reply line. That half quotes the reply being answered; it rides along as the
thing the remark is about, more of it when the person's own words are few. A
person who never types a spaced dash is unaffected by that last rule. One who
uses it as punctuation has the words before it treated as the quoted half:
they stay in the passage (their first 120 characters when what follows is
long), and names are taken only from what follows.

Names get a passage of their own, asked of full-text search only: hybrid_recall
ANDs every word of a query, so a list of names matches just the memories that
hold them all, and such a row counts only where the words did match.

Shown: up to two memories that a third of the passages found (the message's
subject), then one memory per passage in turn.

The design was chosen by blind rating, not by eye: five candidates (this one,
two other independent designs, a line-by-line one, and "just delete the length
gate") on 24 real long messages, every shown memory rated by three kinds of
reader who did not know which design showed it. All 24 messages were one
person's; the design has not been rated on anyone else's.
backend/tests/test_recall_long_messages.py pins the rated behaviour on invented
text; a change here should be rated again, not just tested again.

A failed call loses its own rows and nothing else. When every call fails,
passage_recall() raises recall_core.RecallUnavailable, as fused_recall() does:
a store that could not answer is not a store with nothing to say. Anything else
that goes wrong gives [].
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from _store import core

PASSAGE_CALLS = 24     # database calls for one message, core.MAX_WORKERS of them at a time
MAX_PASSAGES = 12
CHUNK = 450            # unquoted prose is gathered into pieces of about this many characters
CONSENSUS = 2          # slots for memories that many passages found
MIN_PASSAGE = 30       # a remark shorter than this asks nothing a query can use
MAX_TEXT = 200_000     # characters of a message that are read at all: twelve passages cannot use more
DASH = " — "
NAMES = "\x01"         # marks a list-of-names passage inside _plan()
# A line that opens with one of these and a colon quotes a model. An instance's own
# christened name is not known here and is not in the list.
SPEAKER_RE = re.compile(r"^\s*(Assistant|Claude|Codex|ChatGPT|Gemini)\s*:\s*")
PASTE_OPEN, PASTE_CLOSE, TAG_ROOM = "<pasted_content", "</pasted_content", 300
TAG_RE = re.compile(r"</?pasted_content[^>\n]{0,200}>")     # bounded: a tag is short
LEAD_RE = re.compile(r"(\.\.\.|…|:)\s*$")
STOP = set("the this that these those there then they them what when where which with from have "
           "your you and but for not are was were will would could should can its it's i'm i've "
           "here also just very some into about over after before because while she her him his who how why".split())


def _clean(text: str) -> str:
    """Collapse whitespace; a URL keeps only the words of its path (the slug names the thing)."""
    text = re.sub(r"https?://\S+",
                  lambda m: " ".join(re.findall(r"[A-Za-z]{3,}", m.group(0).split("/", 3)[-1])), text)
    return re.sub(r"\s+", " ", text).strip()


def _names(doc: str, n: int) -> list[str]:
    """The most repeated names: capitalised in mid-sentence, never written in lower case."""
    count, first, mid = {}, {}, set()
    lower = set(re.findall(r"\b[a-z][\w'-]+", doc))
    for i, m in enumerate(re.finditer(r"[A-Za-z][\w'’-]{2,}", doc)):
        word = re.sub(r"['’]s$", "", m.group(0)).strip("'-")
        if word.lower() in STOP or word.lower() in lower or not word[:1].isupper():
            continue
        before = doc[max(0, m.start() - 3):m.start()].rstrip(" \"“(*")
        if before and before[-1] not in ".!?:\n#>=":
            mid.add(word)
        count[word] = count.get(word, 0) + 1
        first.setdefault(word, i)
    return sorted((w for w in count if w in mid), key=lambda w: (-count[w], first[w]))[:n]


def _take_pastes(text: str) -> tuple[str, list[str]]:
    """(the text with each <pasted_content> block replaced by a blank line, the blocks).

    One pass with str.find. A pattern rescanned to the end of the text for every
    opening tag that was never closed: 20,000 of them took 39 seconds.
    """
    docs, kept, pos = [], [], 0
    while True:
        start = text.find(PASTE_OPEN, pos)
        if start < 0:
            break
        opened = text.find(">", start, start + TAG_ROOM)
        if opened < 0:                          # not a tag after all: leave it and move on
            kept.append(text[pos:start + len(PASTE_OPEN)])
            pos = start + len(PASTE_OPEN)
            continue
        close = text.find(PASTE_CLOSE, opened + 1)
        closed = text.find(">", close, close + TAG_ROOM) if close >= 0 else -1
        if closed < 0:                          # never closed: the rest stays as it is
            break
        kept += [text[pos:start], "\n\n"]
        docs.append(text[opened + 1:close])
        pos = closed + 1
    kept.append(text[pos:])
    return "".join(kept), docs


def _pieces(sentence: str, width: int) -> list[str]:
    """A run with no sentence end (a list without full stops) in pieces of at most `width`,
    cut at a space where there is one, so that none of it goes unasked."""
    pieces = []
    while len(sentence) > width:
        cut = sentence.rfind(" ", width // 2, width + 1)
        cut = cut if cut > 0 else width
        pieces.append(sentence[:cut].rstrip())
        sentence = sentence[cut:].lstrip()
    return pieces + ([sentence] if sentence else [])


def split_message(text) -> tuple[list[tuple[str, str]], list[str]]:
    """(remarks, documents): remarks are (quoted line, the person's words) in message order."""
    if not isinstance(text, str):
        return [], []
    body, docs = _take_pastes(text[:MAX_TEXT])
    kept, block, said = [], [], []
    for line in TAG_RE.sub("", body).split("\n"):
        if line.lstrip().startswith(">"):
            block.append(line.lstrip()[1:].strip(" \\"))
            continue
        if SPEAKER_RE.match(line):          # "Claude: ..." quotes a model's words, even inside a draft
            said.append(SPEAKER_RE.sub("", line))
            kept.append("")
            continue
        if block:
            docs.append("\n".join(block))
            block = []
        kept.append(line)
    if block:
        docs.append("\n".join(block))
    if said:
        docs.append("\n".join(said))
    paras = [p.strip() for p in re.split(r"\n\s*\n", "\n".join(kept)) if p.strip()]
    for i, para in enumerate(paras[:3] if not docs else []):
        # An unmarked paste after a short lead such as "Here you go..." or "She wrote:".
        rest = "\n\n".join(paras[i + 1:])
        if (LEAD_RE.search(para) and len(rest) > 1000 and rest.count(DASH) < 2
                and len(" ".join(paras[:i + 1])) < 800):
            docs.append(rest)
            paras = paras[:i + 1]
            break
    remarks: list[list[str]] = []
    for para in paras:
        current = None
        for line in para.split("\n"):
            line = line.strip().lstrip("*•- ").strip()
            if not line:
                continue
            if DASH in line:
                quoted, own = line.split(DASH, 1)
                current = [quoted, own]
                remarks.append(current)
            elif current is not None:       # a later line of the same paragraph continues the answer
                current[1] += " " + line
            else:
                current = ["", line]
                remarks.append(current)
    out: list[tuple[str, str]] = []
    for quoted, own in remarks:
        if quoted:
            out.append((_clean(quoted), _clean(own)))
            continue
        for sentence in re.split(r"(?<=[.?!])\s+", _clean(own)):    # gather prose, cut at sentence ends
            for piece in _pieces(sentence, CHUNK * 2):
                if out and not out[-1][0] and len(out[-1][1]) + len(piece) < CHUNK:
                    out[-1] = ("", out[-1][1] + " " + piece)
                else:
                    out.append(("", piece))
    return out, [d for d in (_clean(d) for d in docs) if len(d) > 40]


def _plan(text) -> list[str]:
    """The passages of a message (see _plan_once), worked out once however often asked."""
    return list(_plan_once(text)) if isinstance(text, str) else []


@lru_cache(maxsize=4)
def _plan_once(text: str) -> tuple[str, ...]:
    """The texts to query, most deserving first, at most MAX_PASSAGES; a list of
    names carries the NAMES mark.

    Remarks go by how much they ask: a question counts for most, then a reply by
    how much the person wrote, with unquoted prose in between; the names they used
    come third. A paste's opening and its names follow the first six remarks; a
    paste leads only when the person wrote almost nothing and asked nothing
    ("Here you go...").
    """
    try:
        remarks, docs = split_message(text)
        doc_names = [w for d in docs for w in _names(d, 4)]
        own_total = sum(len(own) for _, own in remarks)
        scored = []
        for i, (quoted, own) in enumerate(remarks):
            if doc_names and len(remarks) <= 2:     # one line of the person's, asked with the paste's subject
                own = own + " (" + " ".join(doc_names) + ")"
            if quoted and len(own) < 120:
                passage = quoted[-300:] + DASH + own
            else:
                passage = (quoted[:120] + DASH + own) if quoted else own
            if len(passage) >= MIN_PASSAGE:
                weight = min(len(own), 400) if quoted else 300
                scored.append((-(200 * ("?" in own) + weight), i, _clean(passage)))
        own_passages = [p for _, _, p in sorted(scored)]
        names = _names(" ".join(own for _, own in remarks), 8)
        if len(names) >= 2:
            own_passages[2:2] = [NAMES + " ".join(names)]
        doc_passages = []
        for doc in docs[:3]:
            inner = [doc[k:k + 500] for k in range(500, len(doc) - 200, max(500, (len(doc) - 500) // 2))][:2]
            doc_passages += [doc[:500], NAMES + " ".join(_names(doc, 8))] + inner
        doc_passages = [p for p in doc_passages if p.strip(NAMES + " ")]
        if own_total < 80 and not any("?" in own for _, own in remarks):
            ordered = doc_passages[:2] + own_passages + doc_passages[2:]
        else:
            ordered = own_passages[:6] + doc_passages[:2] + own_passages[6:] + doc_passages[2:]
        return tuple(p for p in dict.fromkeys(ordered) if p.strip(NAMES + " "))[:MAX_PASSAGES]
    except Exception:
        return ()


def message_passages(text) -> list[str]:
    """The passages a long message is recalled for, in the order they take turns."""
    return [p.lstrip(NAMES) for p in _plan(text)]


def _queries(text) -> list[tuple[bool, str]]:
    """(is a list of names, query text) for each passage, cut as recall_rows cuts."""
    plan = _plan(text)
    if not plan and isinstance(text, str):
        plan = [_clean(text)[:500]]             # nothing to split: ask with its opening
    return [(p.startswith(NAMES), p.lstrip(NAMES)[:1000]) for p in plan if p.strip(NAMES + " ")]


def passage_queries(text) -> list[str]:
    """The query texts passage_recall(text) embeds, in passage order."""
    return [query for _, query in _queries(text)]


def passage_recall(text, fetch=None, limit: int = 5, log=None) -> list[dict]:
    """Memories for a long message: rows as `fetch` returns them, plus `passage` (its
    index) and `passage_text` (what the memory was recalled for).

    Every passage gets a hybrid call, and a fast call while PASSAGE_CALLS lasts; a
    passage's two lists are fused by rank. A list of names gets the hybrid call
    alone and keeps only rows whose text matched. `fetch(lane, query)` is injectable,
    as in fused_recall, and defaults to recall_core.recall_rows; the prompt hook
    hands over one that carries its settings and its deadline. `log`, if given,
    takes one line saying how much was asked.
    """
    try:
        fetch = core.recall_rows if fetch is None else fetch
        queries = _queries(text)
        if not queries or limit <= 0:
            return []
        passages = [query for _, query in queries]
        name_lists = {i for i, (is_names, _) in enumerate(queries) if is_names}
        jobs = [(i, "hybrid") for i in range(len(passages))][:PASSAGE_CALLS]
        jobs += [(i, "fast") for i in range(len(passages)) if i not in name_lists][:PASSAGE_CALLS - len(jobs)]

        def run(job):
            try:
                found = fetch(job[1], passages[job[0]])
            except Exception:
                return None                 # the call failed, which is not the same as finding nothing
            found = [row for row in found if isinstance(row, dict) and row.get("id")] \
                if isinstance(found, list) else []
            if job[0] in name_lists:        # where the words matched, not where the vector fell
                # hybrid_recall's source: vector, fts or hybrid, each perhaps with "+bridge"; or bridge.
                found = [row for row in found if str(row.get("source") or "").split("+")[0] in ("hybrid", "fts")]
            return found

        with ThreadPoolExecutor(max_workers=min(len(jobs), core.MAX_WORKERS)) as pool:
            lists = list(pool.map(run, jobs))
        if all(found is None for found in lists):
            raise core.RecallUnavailable("All passage recall calls failed")
        scores: list[dict[str, float]] = [{} for _ in passages]
        rows: dict[str, dict] = {}
        for (i, _), ranked in zip(jobs, lists):
            for rank, row in enumerate(ranked or []):
                mid = row["id"]
                scores[i][mid] = scores[i].get(mid, 0.0) + 1.0 / (core.FUSION_K + rank + 1)
                rows.setdefault(mid, row)
        order = [sorted(s, key=lambda mid: (-s[mid], mid)) for s in scores]
        total = {mid: sum(s.get(mid, 0.0) for s in scores) for mid in rows}
        found_by = {mid: sum(mid in s for s in scores) for mid in rows}
        needed = max(2, -(-len(passages) // 3))                 # a third of the passages, at least two
        shared = sorted((mid for mid in rows if found_by[mid] >= needed),
                        key=lambda mid: (-found_by[mid], -total[mid], mid))
        chosen = [(mid, max(range(len(passages)), key=lambda i: scores[i].get(mid, 0.0)))
                  for mid in shared[:CONSENSUS]][:limit]
        seen = {mid for mid, _ in chosen}
        while len(chosen) < limit and any(order):               # then passages take turns
            for i, ranked in enumerate(order):
                while ranked and ranked[0] in seen:
                    ranked.pop(0)
                if ranked and len(chosen) < limit:
                    seen.add(ranked[0])
                    chosen.append((ranked.pop(0), i))
        if log is not None:
            failed = sum(found is None for found in lists)
            try:
                log(f"Passage recall: {len(passages)} passages, {len(jobs)} calls ({failed} failed), "
                    f"{len(rows)} candidates")
            except Exception:
                pass
        return [dict(rows[mid], passage=i, passage_text=passages[i]) for mid, i in chosen]
    except core.RecallUnavailable:
        raise
    except Exception:
        return []
