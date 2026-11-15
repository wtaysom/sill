"""Pure production declaration parsing, validation and rendering. No inferred authors."""
from __future__ import annotations

import json
import re
from typing import Callable

MODEL_FAMILIES = frozenset({"claude", "gpt", "chatgpt", "gemini", "codex", "sonnet", "opus", "haiku"})
KINDS = ("person", "model", "group")
BASES = ("self-report", "witness", "read-back", "testimony", "convention", "stated")
EMPTY = {"made": [], "joint": None, "unknown": None, "none": None, "superseded_declarations": [], "note": None}

STATE_BASES = ("stated", "convention")
SELF_NEEDS_SESSION = "self needs a session; this route has none: pass the maker's id with --made-json"
NO_SESSION_DECLARER = ("this route has no session, so the compact grammar cannot name its declarer: "
                       "pass --made-json, the object form with a typed declared.by.who")
UNSET = object()   # declared_by not given: derive the declarer from the session
ENTRY_SHAPE = '"who [as Voice]: work [@ addr, …] [basis]"'
NOT_CARRIED_SHAPE = '"who: work :: because" or "session=<id>: work :: because"'


class ProvenanceError(ValueError):
    """A declaration that breaks the rules: every violation, one per line, in .args[0]."""


def _raise_if(problems: list[str]) -> None:
    if problems:
        raise ProvenanceError("\n".join(problems))


# --- the tokenizer -----------------------------------------------------------
# One pass with a `quoted` flag masks every double-quoted character (quotes
# included); separators are searched for in the mask and the original text is
# sliced at the same indices, so nothing inside quotes ever splits.

def _mask(text: str) -> str:
    out, quoted = [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        out.append("\0" if quoted or ch == '"' else ch)
    if quoted:
        raise ProvenanceError(f'unbalanced double quote in "{text.strip()}"')
    return "".join(out)


def _split_all(text: str, sep: str) -> list[str]:
    mask, pieces, start = _mask(text), [], 0
    while (i := mask.find(sep, start)) != -1:
        pieces.append(text[start:i])
        start = i + len(sep)
    return pieces + [text[start:]]


def _cut(text: str, sep: str) -> tuple[str, str | None]:
    """Split at the first unquoted `sep`; (text, None) when there is none."""
    i = _mask(text).find(sep)
    return (text, None) if i == -1 else (text[:i], text[i + len(sep):])


def _cut_word(text: str, word: str) -> tuple[str, str | None]:
    """Split at the first unquoted ` word ` (a trailing ` word` counts too)."""
    m = re.search(rf"\s+{word}(?:\s+|$)", _mask(text))
    return (text, None) if m is None else (text[:m.start()], text[m.end():])


def _phrase(text: str) -> str:
    """A phrase with its double quotes removed and its ends trimmed."""
    return text.replace('"', "").strip()


def _fragments(text: str, shape: str) -> list[str]:
    fragments = [f for f in _split_all(text, ";") if f.strip()]
    if not fragments:
        raise ProvenanceError(f"no entries: write {shape}, entries separated by ;")
    return fragments


def _addresses(text: str | None, fragment: str) -> list[str]:
    if text is None:
        return []
    at = [_phrase(a) for a in _split_all(text, ",")]
    if not all(at):
        raise ProvenanceError(f'an empty address after "@" in "{fragment.strip()}"')
    for addr in at:
        head_tail = addr.rsplit(None, 1)
        if len(head_tail) == 2 and head_tail[1].lower() in BASES:
            raise ProvenanceError(f'"{addr}" in "{fragment.strip()}" ends with the basis word {head_tail[1]}; '
                                  f'a basis goes in square brackets after the addresses: @ {head_tail[0]} [{head_tail[1].lower()}]')
    return at


# --- the grammar -------------------------------------------------------------

def is_model_id(text: str) -> bool:
    """A precise model id as a harness reports it: lowercase, no spaces, a digit, not a model family."""
    return (isinstance(text, str) and _model_shaped(text) and text not in MODEL_FAMILIES)


def _model_shaped(text: str) -> bool:
    return (bool(text) and text == text.lower() and not any(c.isspace() for c in text)
            and any(c.isdigit() for c in text))


def _canonical_voice(voice: str) -> str:
    return voice


def parse_made(text: str, *, kind: str | None = None) -> list[dict]:
    """Raw entries from `who [as Voice]: work [@ addr, …] [basis[ from Name]]; …`.

    Syntax only: the values (kinds, voices, bases, the deny list) are validate()'s.
    `who` may be the literal "self"; expand() turns it into a session-anchored entry.
    `who` may instead be `session=<id>`, an earlier instance named by the session it
    ran in: a model entry with that session, whatever `kind` says.
    """
    entries, problems = [], []
    for n, fragment in enumerate(_fragments(text, ENTRY_SHAPE), 1):
        try:
            entries.append(_parse_entry(fragment, kind))
        except ProvenanceError as exc:
            problems.append(f"entry {n}: {exc}")
    _raise_if(problems)
    return entries


def _parse_entry(fragment: str, kind: str | None) -> dict:
    named = _parse_session_head(fragment)
    if named is not None:
        session, voice, body = named
        work, at, basis, source = _parse_body(body, fragment)
        return {"who": None, "kind": "model", "session": session, "as": voice,
                "work": work, "at": at, "basis": basis, "from": source}
    head, body = _cut(fragment, ":")
    if body is None:
        raise ProvenanceError(f'"{fragment.strip()}" needs "who: work"')
    who, voice = _parse_head(head, fragment)
    work, at, basis, source = _parse_body(body, fragment)
    if kind is None:
        kind = "model" if who == "self" or is_model_id(who) or who.lower() in MODEL_FAMILIES else "person"
    return {"who": who, "kind": kind, "as": voice, "work": work, "at": at, "basis": basis, "from": source}


def _parse_head(head: str, fragment: str) -> tuple[str, str | None]:
    who_text, voice_text = _cut_word(head, "as")
    who = _phrase(who_text)
    if not who:
        raise ProvenanceError(f'"{fragment.strip()}" names no who before the ":"')
    if who.lower() == "self":
        who = "self"
    return who, _parse_voice(voice_text, head)


def _parse_voice(voice_text: str | None, head: str) -> str | None:
    if voice_text is None:
        return None
    voice = _phrase(voice_text)
    if not voice:
        raise ProvenanceError(f'"{head.strip()}": "as Voice" needs a nonempty voice name')
    return _canonical_voice(voice)


def _parse_session_head(fragment: str) -> tuple[str, str | None, str] | None:
    """(session, voice, body) for `session=<id> [as Voice]: work…`; None for any other head.

    The id is whitespace-free and may hold colons of its own (beat:<attempt>), so the
    head ends at the colon that closes the id's run of characters, else at a colon
    after spaces, else at the first colon after `as Voice`.
    """
    mask = _mask(fragment)
    m = re.match(r"\s*session\s*=\s*", mask)
    if m is None:
        return None
    start = m.end()
    end = start + re.match(r"\S*", mask[start:]).end()
    shape = f'"{fragment.strip()}" needs "session=<id>: work" or "session=<id> as Voice: work"'
    if mask[start:end].endswith(":"):
        id_text, voice, body = fragment[start:end - 1], None, fragment[end:]
    elif colon := re.match(r"\s*:", mask[end:]):
        id_text, voice, body = fragment[start:end], None, fragment[end + colon.end():]
    elif as_word := re.match(r"\s+as\b", mask[end:]):
        close = mask.find(":", end + as_word.end())
        if close == -1:
            raise ProvenanceError(shape)
        id_text, body = fragment[start:end], fragment[close + 1:]
        voice = _parse_voice(fragment[end + as_word.end():close], fragment[:close])
    else:
        raise ProvenanceError(shape)
    session = _phrase(id_text)
    if not session:
        raise ProvenanceError(f'"{fragment.strip()}": session= needs the id of the session the instance ran in')
    if any(c.isspace() for c in session):
        raise ProvenanceError(f'"{fragment.strip()}": session="{session}" is not a session id (it holds a space)')
    return session, voice, body


def _parse_body(body: str, fragment: str) -> tuple[str, list[str], str, str | None]:
    mask = _mask(body)
    basis, source = "stated", None
    open_at = mask.find("[")
    if open_at != -1:
        close_at = mask.rfind("]")
        if close_at < open_at:
            raise ProvenanceError(f'an unclosed [basis] bracket in "{fragment.strip()}"')
        if body[close_at + 1:].strip():
            raise ProvenanceError(f'text after the [basis] bracket in "{fragment.strip()}"; the bracket comes last')
        if "[" in mask[open_at + 1:close_at] or "]" in mask[open_at + 1:close_at]:
            raise ProvenanceError(f'one [basis] bracket per entry in "{fragment.strip()}"; quote a phrase that holds [ or ]')
        basis, source = _parse_bracket(body[open_at + 1:close_at], fragment)
        body = body[:open_at]
    elif "]" in mask:
        raise ProvenanceError(f'a "]" with no "[" in "{fragment.strip()}"; quote a phrase that holds ]')
    work_text, address_text = _cut(body, "@")
    work = _phrase(work_text)
    if not work:
        raise ProvenanceError(f'"{fragment.strip()}" names no work: say what this contributor supplied')
    return work, _addresses(address_text, fragment), basis, source


def _parse_bracket(inner: str, fragment: str) -> tuple[str, str | None]:
    basis_text, from_text = _cut_word(inner, "from")
    basis = _phrase(basis_text).lower()
    if not basis:
        raise ProvenanceError(f'an empty [basis] bracket in "{fragment.strip()}"; leave it out for stated')
    source = None if from_text is None else _phrase(from_text)
    if from_text is not None and not source:
        raise ProvenanceError(f'"[{inner.strip()}]" names nobody after "from"')
    return basis, source


def parse_not_carried(text: str) -> list[dict]:
    """Instructions from `who: work :: because` or `session=<id>: work :: because`, `;`-separated."""
    items, problems = [], []
    for n, fragment in enumerate(_fragments(text, NOT_CARRIED_SHAPE), 1):
        try:
            items.append(_parse_not_carried_item(fragment))
        except ProvenanceError as exc:
            problems.append(f"not-carried {n}: {exc}")
    _raise_if(problems)
    return items


def _parse_not_carried_item(fragment: str) -> dict:
    # A beat's session id holds a colon of its own (beat:<attempt>); the head ends at the next one.
    beat = re.match(r"\s*session\s*=\s*beat:", _mask(fragment))
    skip = beat.end() if beat else 0
    head, rest = _cut(fragment[skip:], ":")
    head = fragment[:skip] + head
    work_text, because_text = _cut(rest, "::") if rest is not None else (None, None)
    if because_text is None:
        raise ProvenanceError(f'"{fragment.strip()}" needs {NOT_CARRIED_SHAPE}')
    work, because, name = _phrase(work_text), _phrase(because_text), _phrase(head)
    if not (work and because and name):
        raise ProvenanceError(f'"{fragment.strip()}" needs a who (or session=<id>), a work and a reason after ::')
    m = re.fullmatch(r"session\s*=\s*(\S.*)", name)
    match = {"session": m.group(1).strip(), "work": work} if m else {"who": name, "work": work}
    return {"match": match, "because": because}


# --- expansion and the routes ------------------------------------------------

def declarer(*, session: str | None, as_: str | None = None, person: str | None = None) -> dict | None:
    """Who makes a declaration: a person (§4f transport), this instance, or None for a pipeline."""
    if person:
        return {"kind": "person", "who": person}
    if session is None:
        return None
    return {"kind": "model", "who": None, "session": session, "as": as_}


def expand(entries: list[dict], *, declared_by: dict | None, when: str, session: str | None) -> list[dict]:
    """Stored entries from raw ones: `self` becomes this session's model entry, a
    `session=<id>` entry keeps the session written; each gains `declared`."""
    if session is None and any(e["who"] == "self" for e in entries):
        raise ProvenanceError(SELF_NEEDS_SESSION)
    return [_expand_entry(e, declared_by, when, session) for e in entries]


def _expand_entry(raw: dict, declared_by: dict | None, when: str, session: str | None) -> dict:
    if raw["who"] == "self":
        maker = {"who": None, "kind": "model", "session": session, "as": raw.get("as")}
    elif raw.get("session") is not None:   # session=<id>: an earlier instance, never the mint's session
        maker = {"who": None, "kind": "model", "session": raw["session"], "as": raw.get("as")}
    else:
        maker = {"who": raw["who"], "kind": raw["kind"]}
        if raw["kind"] == "model" or raw.get("as") is not None:
            maker["as"] = raw.get("as")   # kept off a person so validate() can refuse it
    declared = {"by": declared_by, "when": when, "basis": raw["basis"]}
    if raw.get("from") is not None:
        declared["from"] = raw["from"]
    return {**maker, "work": raw["work"], "at": list(raw["at"]), "declared": declared}


def _declaration(by: dict | None, when: str) -> dict:
    return {"by": by, "when": when, "basis": "stated"}


def _declarer_voice(raw: list[dict], as_: str | None) -> str | None:
    """The stamp's voice; failing that, the one voice this command wrote on `self`.

    `self as Guide` is the declarer's own statement of its voice, so it names the
    declarer too on a route (an interactive session) whose stamp names none.
    """
    if as_ is not None:
        return as_
    voices = {e["as"] for e in raw if e["who"] == "self" and e.get("as")}
    return voices.pop() if len(voices) == 1 else None


def _command_declarer(declared_by, *, session: str | None, as_: str | None, person: str | None,
                      raw: list[dict]) -> dict | None:
    """An explicit declarer (None = a pipeline, a dict = a maker), else one derived from the route."""
    if declared_by is UNSET:
        return declarer(session=session, as_=_declarer_voice(raw, as_), person=person)
    if declared_by is not None and not isinstance(declared_by, dict):
        raise ProvenanceError("declared_by must be a maker object, or None for a pipeline")
    if declared_by is not None:
        _raise_if([f"declared_by: {p}" for p in _maker_problems(declared_by)])
    return declared_by


def _refuse_sessionless_compact(declared_by, session: str | None, person: str | None, compact: bool) -> None:
    """No session, no person and no explicit declarer: the grammar has nobody to name as declarer."""
    if compact and declared_by is UNSET and session is None and not person:
        raise ProvenanceError(NO_SESSION_DECLARER)


def _compact_production(raw: list[dict], *, jointly: str | None, unknown: str | None, none: str | None,
                        by: dict | None, when: str, session: str | None) -> dict | None:
    if not raw and jointly is None and unknown is None and none is None:
        return None
    production = {**EMPTY, "made": expand(raw, declared_by=by, when=when, session=session),
                  "superseded_declarations": []}   # a fresh list, never EMPTY's own
    if jointly is not None:
        how, address_text = _cut(jointly, "@")
        production["joint"] = {"how": _phrase(how), "at": _addresses(address_text, jointly),
                               "declared": _declaration(by, when)}
    if unknown is not None:
        production["unknown"] = {"what": unknown.strip(), "declared": _declaration(by, when)}
    if none is not None:
        production["none"] = {"why": none.strip(), "declared": _declaration(by, when)}
    return production


def _production_from_json(json_text: str, person: str | None) -> dict:
    try:
        production = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ProvenanceError(f"--made-json is not JSON ({exc})") from None
    if not isinstance(production, dict):
        raise ProvenanceError(f"--made-json must be a production object with keys from {', '.join(EMPTY)}")
    if person:
        _declare_by_person(production, declarer(session=None, person=person))
    return production


def _declare_by_person(production: dict, by: dict) -> None:
    """Fill `declared.by` with the transported person where the object leaves it out."""
    made = production.get("made") if isinstance(production.get("made"), list) else []
    places = [(f"entry {n}", e) for n, e in enumerate(made, 1)]
    places += [(key, production.get(key)) for key in ("joint", "unknown", "none")]
    problems = []
    for label, holder in places:
        declared = holder.get("declared") if isinstance(holder, dict) else None
        if not isinstance(declared, dict):
            continue
        if "by" not in declared:
            declared["by"] = by
        elif declared["by"] != by:
            problems.append(f"{label}: declared.by is already {json.dumps(declared['by'])}; "
                            f"--made-declared-by {by['who']} would restate it")
    _raise_if(problems)


def production_from_args(*, made: str | None, jointly: str | None, unknown: str | None, none: str | None,
                         not_carried: str | None, kind: str | None, declared_by_person: str | None,
                         json_text: str | None, session: str | None, as_: str | None,
                         when: str, declared_by=UNSET) -> tuple[dict | None, list[dict] | None]:
    """(production, not-carried instructions) from `sill.py notice`'s --made* options.

    `production` is the full validated object, or None when nothing was declared;
    --made-json wins over the compact options. The instructions carry this
    command's declarer, for supersede_memory()'s production_not_carried.
    `declared_by` (a maker, or None for a pipeline) overrides the derived declarer;
    without it a route with no session refuses every compact declaration.
    """
    if declared_by_person and not json_text:
        raise ProvenanceError("--made-declared-by is for a §4f transport only: pass it with --made-json")
    compact = (made, jointly, unknown, none, not_carried) if not json_text else (not_carried,)
    _refuse_sessionless_compact(declared_by, session, declared_by_person, any(c is not None for c in compact))
    raw = parse_made(made, kind=kind) if made is not None and not json_text else []
    by = _command_declarer(declared_by, session=session, as_=as_, person=declared_by_person, raw=raw)
    if json_text:
        production = _production_from_json(json_text, declared_by_person)
    else:
        production = _compact_production(raw, jointly=jointly, unknown=unknown, none=none,
                                         by=by, when=when, session=session)
    if production is not None:
        validate(production)
    if not_carried is None:
        return production, None
    return production, [{**item, "declared": _declaration(by, when)} for item in parse_not_carried(not_carried)]


def production_from_value(value: str | dict | None, *, session: str | None, as_: str | None,
                          when: str, declared_by=UNSET) -> dict | None:
    """The MCP route: the compact grammar as a string, or a full object validated as-is.

    `declared_by` as in production_from_args; nothing is inferred for an object.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        validate(value)
        return value
    if not isinstance(value, str):
        raise ProvenanceError("production must be the compact grammar as a string, or an object")
    _refuse_sessionless_compact(declared_by, session, None, True)
    raw = parse_made(value)
    production = _compact_production(raw, jointly=None, unknown=None, none=None,
                                     by=_command_declarer(declared_by, session=session, as_=as_, person=None, raw=raw),
                                     when=when, session=session)
    validate(production)
    return production


# --- validation --------------------------------------------------------------

def validate(production: dict) -> None:
    """Raise ProvenanceError listing every violation of the write-time rules."""
    if not isinstance(production, dict):
        raise ProvenanceError("production must be an object")
    problems = []
    unknown_keys = [k for k in production if k not in EMPTY]
    if unknown_keys:
        problems.append(f"unknown key(s) {', '.join(map(str, unknown_keys))}; production takes {', '.join(EMPTY)}")
    made = production.get("made")
    if not isinstance(made, list):
        problems.append("made must be a list of entries (empty when none is listed)")
        made = []
    for n, entry in enumerate(made, 1):
        problems += [f"entry {n}: {p}" for p in _entry_problems(entry)]
    problems += [f"joint: {p}" for p in _joint_problems(production.get("joint"))]
    for key, field in (("unknown", "what"), ("none", "why")):
        problems += [f"{key}: {p}" for p in _state_problems(key, field, production.get(key))]
    if production.get("none") is not None and (made or production.get("joint") is not None
                                               or production.get("unknown") is not None):
        problems.append("none: a none declaration excludes made entries, joint and unknown")
    if not isinstance(production.get("superseded_declarations", []), list):
        problems.append("superseded_declarations must be a list of prior production objects")
    if not isinstance(production.get("note"), (str, type(None))):
        problems.append("note must be text or null")
    _raise_if(problems)


def _maker_problems(maker: dict) -> list[str]:
    """The shape shared by an entry's maker fields and a declarer."""
    kind, who, voice = maker.get("kind"), maker.get("who"), maker.get("as")
    problems = []
    session = maker.get("session")
    if session is not None and (not isinstance(session, str) or not session.strip() or session != session.strip()):
        problems.append("session must be a nonempty, trimmed string or null")
    if kind not in KINDS:
        problems.append(f"kind {json.dumps(kind)} is not one of {', '.join(KINDS)}")
    if who is not None and not isinstance(who, str):
        problems.append("who must be text, or null for a model instance whose id is not known")
        who = None
    if who is not None and who != who.strip():
        problems.append(f"who {json.dumps(who)} has leading or trailing whitespace; write the name trimmed")
    bare = who.strip() if who is not None else None   # every comparison below reads the trimmed name
    if bare == "self":
        problems.append('who "self" is grammar, never stored; give the maker\'s id, or null with the session')
    elif kind == "model" and bare is not None and bare.lower() in MODEL_FAMILIES:
        problems.append(f'who "{bare}" is a model family name; use the harness\'s model id (such as model-7) or self')
    elif kind == "model" and bare is not None and not _model_shaped(bare):
        problems.append(f'model who "{bare}" is not a precise model id (lowercase, no spaces, a digit); '
                        f"use the id the harness reports, or null with the session")
    if kind == "model" and who is None and not maker.get("session"):
        problems.append("a model entry needs session or who")
    if kind in ("person", "group") and not (isinstance(who, str) and who.strip()):
        problems.append(f"a {kind} needs who: the name")
    if voice is not None and kind != "model":
        problems.append(f'"as {voice}" is only for a model entry; a {who or kind} entry has no voice')
    elif voice is not None and (not isinstance(voice, str) or not voice.strip() or voice != voice.strip()):
        problems.append(f'"as {voice}": "as Voice" needs a nonempty, trimmed voice name')
    return problems


def _entry_problems(entry) -> list[str]:
    if not isinstance(entry, dict):
        return ["an entry must be an object {who, kind, work, at, declared}"]
    problems = _maker_problems(entry)
    work = entry.get("work")
    if not (isinstance(work, str) and work.strip()):
        problems.append("work is required: a phrase naming what this contributor supplied")
    at = entry.get("at")
    if not (isinstance(at, list) and all(isinstance(a, str) for a in at)):
        problems.append("at must be a list of address strings (empty when none was given)")
        at = []
    declared = entry.get("declared")
    problems += _declared_problems(declared, "declared", BASES)
    if isinstance(declared, dict):
        problems += _basis_problems(entry, declared, at)
    if "not_carried" in entry:
        problems += _not_carried_problems(entry["not_carried"])
    return problems


def _basis_problems(entry: dict, declared: dict, at: list[str]) -> list[str]:
    basis, by = declared.get("basis"), declared.get("by")
    if basis == "self-report":
        by_session = by.get("session") if isinstance(by, dict) else None
        if entry.get("kind") != "model" or not entry.get("session") or entry.get("session") != by_session:
            return [f'basis "self-report" is the declarer\'s own work only: it needs kind model and the entry\'s '
                    f"session ({entry.get('session')}) equal to declared.by.session ({by_session})"]
    if basis in ("witness", "read-back") and not any(not a.startswith("mem:") for a in at):
        return [f'basis "{basis}" needs an address in this entry\'s at that is not mem: '
                f"(a mem: row is a record of the act, not the act)"]
    if basis == "testimony" and not declared.get("from"):
        return ['basis "testimony" needs from: whose word was taken ([testimony from Name])']
    return []


def _declared_problems(declared, label: str, bases: tuple[str, ...]) -> list[str]:
    if not isinstance(declared, dict):
        return [f"{label} is required: {{by, when, basis}}"]
    problems = []
    if "by" not in declared:
        problems.append(f"{label}.by is required: the declarer, or null for a pipeline")
    by = declared.get("by")
    if isinstance(by, dict):
        problems += [f"{label}.by: {p}" for p in _maker_problems(by)]
    elif by is not None:
        problems.append(f"{label}.by must be a maker object or null")
    if not (isinstance(declared.get("when"), str) and declared["when"].strip()):
        problems.append(f"{label}.when must be the time of the declaring act, as text")
    if declared.get("basis") not in bases:
        problems.append(f"{label}.basis {json.dumps(declared.get('basis'))} is not one of {', '.join(bases)}")
    return problems


def _not_carried_problems(claim) -> list[str]:
    if not isinstance(claim, dict):
        return ["not_carried must be an object {because, declared}"]
    problems = []
    if not (isinstance(claim.get("because"), str) and claim["because"].strip()):
        problems.append("not_carried needs because: why the successor no longer carries this work")
    return problems + _declared_problems(claim.get("declared"), "not_carried.declared", BASES)


def _joint_problems(joint) -> list[str]:
    if joint is None:
        return []
    if not isinstance(joint, dict):
        return ["joint must be an object {how, at, declared} or null"]
    problems = []
    if not (isinstance(joint.get("how"), str) and joint["how"].strip()):
        problems.append("how is required: a phrase naming the interaction")
    if not (isinstance(joint.get("at"), list) and all(isinstance(a, str) for a in joint["at"])):
        problems.append("at must be a list of address strings (empty when none was given)")
    return problems + _declared_problems(joint.get("declared"), "declared", BASES)


def _state_problems(key: str, field: str, state) -> list[str]:
    if state is None:
        return []
    if not isinstance(state, dict):
        return [f"{key} must be an object {{{field}, declared}} or null"]
    problems = []
    if not (isinstance(state.get(field), str) and state[field].strip()):
        problems.append(f"{field} is required")
    return problems + _declared_problems(state.get("declared"), "declared", STATE_BASES)


# --- rendering ---------------------------------------------------------------

def resolve_model(entry: dict, stamps: list[dict]) -> str | None:
    """The model from the stamp of the act that declared this entry, or None.

    A same-act stamp shares the entry's session and its declared.when; no other
    stamp is ever used, so a copied entry cannot point at its copier.
    """
    session = entry.get("session")
    when = (entry.get("declared") or {}).get("when")
    if not session or not when:
        return None
    for stamp in stamps:
        if isinstance(stamp, dict) and stamp.get("session") == session and stamp.get("when") == when:
            return stamp.get("model")
    return None


def disagrees(entry: dict, stamps: list[dict]) -> bool:
    """A typed model id that the same-act stamp contradicts."""
    model = resolve_model(entry, stamps)
    return bool(entry.get("who")) and model is not None and model != entry["who"]


def _short_session(session: str) -> str:
    prefix = "beat:" if session.startswith("beat:") else ""
    return prefix + session[len(prefix):][:8]


def _instance_text(maker: dict, model: str | None, *, unknown_model: bool, full: bool) -> str:
    voice, session = maker.get("as"), maker.get("session")
    where = f", session {session if full else _short_session(session)}" if session else ""
    if model:
        return f"{model}{f' as {voice}' if voice else ''}{where}"
    name = f"a {voice} instance" if voice else "an instance"
    return f"{name}{where}{', model unknown' if unknown_model else ''}"


def _maker_text(entry: dict, model_for: Callable[[dict], str | None], full: bool) -> str:
    if entry.get("kind") != "model":
        return str(entry.get("who"))
    model = entry.get("who") or model_for(entry)
    return _instance_text(entry, model, unknown_model=True, full=full)


def _declarer_text(by: dict | None) -> str:
    """Only the detailed view names a declarer, so its session is always in full."""
    if by is None:
        return "a pipeline"
    if by.get("kind") != "model":
        return str(by.get("who"))
    return _instance_text(by, by.get("who"), unknown_model=False, full=True)


def _clock(when) -> str:
    m = re.search(r"[T ](\d{2}:\d{2})", when) if isinstance(when, str) else None
    return m.group(1) if m else str(when)


def _declared_line(declared: dict | None) -> str:
    declared = declared or {}
    return f"declared by {_declarer_text(declared.get('by'))}, {_clock(declared.get('when'))}"


def _entry_text(entry: dict, model_for: Callable[[dict], str | None], full: bool) -> str:
    declared = entry.get("declared") or {}
    basis = declared.get("basis") or "stated"
    if declared.get("from"):
        basis += f" from {declared['from']}"
    return f"{_maker_text(entry, model_for, full)} ({entry.get('work')}; {basis})"


def render(production: dict | None, *, model_for: Callable[[dict], str | None],
           detailed: bool = False) -> list[str]:
    """The `made:` lines for a read path; `detailed` adds the declarer of each declaration
    and prints session ids in full (a beat copies them into session=<id>), where the
    compact views (orient, check) show their first eight characters.

    `model_for(entry)` supplies a model id for an entry whose `who` is null, or
    None (shown as "model unknown"); this module never fills one itself.
    """
    if production is None:
        return ["made: undeclared"]
    entries = [e for e in production.get("made") or [] if isinstance(e, dict)]
    carried = [e for e in entries if not e.get("not_carried")]
    cut = [e for e in entries if e.get("not_carried")]
    joint, lines = production.get("joint"), []

    def add(line: str, declarations: list) -> None:
        lines.append(line)
        if detailed:
            lines.extend(_declared_line(d) for d in declarations)

    listed = " · ".join(_entry_text(e, model_for, detailed) for e in carried)
    if joint:
        add(f"made jointly ({joint.get('how')})" + (f": {listed}" if listed else ""),
            [e.get("declared") for e in carried] + [joint.get("declared")])
    elif carried:
        add(f"made: {listed}", [e.get("declared") for e in carried])
    if cut:
        add("not carried: " + " · ".join(f"{_maker_text(e, model_for, detailed)} ({e.get('work')}; {e['not_carried'].get('because')})"
                                          for e in cut),
            [e.get("declared") for e in cut])
    for key, field in (("unknown", "what"), ("none", "why")):
        state = production.get(key)
        if state:
            add(f"made: {key} ({state.get(field)})", [state.get("declared")])
    return lines or ["made: no entries"]


