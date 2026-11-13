"""Audit quotations against explicitly configured source files.

Check straight and curly double quotations outside Markdown code. An inline
or reference citation uses its exact configured target. A quote needs at least
three words unless its block has an unambiguous mapped citation. Plain text
proves wording. Timed quotes need a matched caption word within 90 seconds
of the citation, and a match cannot cross a caption gap greater than 90 seconds.
Sentence boundaries are lexical: comma-introduced e.g./i.e. continues the
sentence, even before a capitalized name. Without that comma, lowercase prose
or a directly introduced quote continues; capitalized prose starts a sentence.
Markdown emphasis is transparent to this rule. Ambiguous prose should put the
timestamp next to the quote rather than rely on natural-language inference.
Equally near distinct timestamps leave timing unchecked; wording is still checked.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
import html
import json
from pathlib import Path
import re
import unicodedata

from .beat_audit import AuditResult, Finding
from .link_check import FENCE_RE, INLINE_RE, REFERENCE_RE

WINDOW_SECONDS = 90
MAX_FRAGMENT_GAP = 200
FALLBACK_MIN_WORDS = 3
FILLERS = frozenset("uh um uhm umm uhh mm mhm hmm erm".split())
FILLER_PHRASES = frozenset({("you", "know"), ("i", "mean"), ("sort", "of"), ("kind", "of"), ("you", "see")})
DROPPABLE = frozenset("like so well right okay ok oh yeah yes now the a an that it and".split())
PROTECTED_WORDS = frozenset((
    "not no never nothing none nobody nor neither without only all most some every each any both either "
    "other another few fewer many more less least several enough cannot cant dont "
    "doesnt didnt wont wouldnt couldnt shouldnt isnt arent wasnt werent havent hasnt hadnt aint mustnt neednt"
).split())
WORD_RE = re.compile(r"[^\W_]+")
TAG_RE = re.compile(r"<[^>\n]*>")
ANNOTATION_RE = re.compile(r"\[(?:music|applause|laughter|inaudible|silence|_+)\]", re.IGNORECASE)
ELLIPSIS_RE = re.compile(r"\[?\s*(?:…|\.\s?\.\s?\.)\s*\]?")
QUOTE_RE = re.compile(r'(?<!\\)“([^”]+)”|(?<![\w\\])"((?:\\.|[^"\\])+)"')
REFERENCE_USE_RE = re.compile(r"(?:!|(?<![\\!\w]))\[([^\[\]\n]+)\](?:\[([^\[\]\n]*)\])?(?!\(|:)")
REFERENCE_LABEL_RE = re.compile(r"^ {0,3}\[([^\]\n]+)\]:")
BLOCKQUOTE_RE = re.compile(r"^ {0,3}(?:> ?)+")
LIST_RE = re.compile(r"^ {0,3}(?:[-+*]|\d+[.)])[ \t]+")
THEMATIC_RE = re.compile(r"^ {0,3}([*_-])(?:[ \t]*\1){2,}[ \t]*$")
HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:\s|$)")
BACKTICKS_RE = re.compile(r"`+")
STAMP = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?"
STAMP_RE = re.compile(
    rf"(?<![\w:.,/+])(?P<first>{STAMP})(?:\s*(?:–|—|-|to)\s*(?P<last>{STAMP}))?"
    r"(?![\w]|:\d|[.,]\d|\s*(?:[AaPp]\.?[Mm]\b|[A-Z]{3,4}\b|[+-]\d{2}:?\d{2}))"
)
CUE_CLOCK = r"(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}"
CUE_RE = re.compile(rf"^\s*({CUE_CLOCK})\s*-->\s*({CUE_CLOCK})(?:\s|$)")
CLEAN_CUE_RE = re.compile(r"^\[(\d+:\d{2}:\d{2}(?:[.,]\d{1,3})?)\][ \t]*(.*)$", re.MULTILINE)
PAREN_CUE = r"\(\s*(?:(?i:about|around|at|ca\.?|~)\s*)?"
PAREN_RE = re.compile(PAREN_CUE + r"$")
ABBREVIATION_RE = re.compile(r"(?i)(?<![^\W_])(?:e\.g|i\.e)$")
SENTENCE_RE = re.compile(
    r"[.!?][\"”’)\]*_]*\s+(?=[\w“\"*\[>])"
    rf"(?!(?:\[[^\]\n]*\]\s*)+{PAREN_CUE}{STAMP})"
)
DURATION_BEFORE_RE = re.compile(r"\b(?:duration|runtime|running time|length|runs|lasts)\W{0,3}(?:of\s+)?$", re.I)
DURATION_AFTER_RE = re.compile(r"^\s*(?:long\b|in length\b|runtime\b|total\b)", re.I)


@dataclass(frozen=True)
class Stamp:
    text: str
    spans: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class Quote:
    line: int
    text: str
    parts: tuple[tuple[str, ...], ...]
    target: str | None
    stamp: Stamp | None
    ambiguous: bool
    tied_stamps: tuple[Stamp, ...] = ()

    @property
    def fingerprint(self) -> tuple[
        str, str | None, tuple[tuple[float, float], ...], tuple[tuple[tuple[float, float], ...], ...]
    ]:
        return (
            " ".join(self.text.split()), self.target, self.stamp.spans if self.stamp else (),
            tuple(sorted(stamp.spans for stamp in self.tied_stamps)),
        )


@dataclass(frozen=True)
class Source:
    tokens: tuple[str, ...]
    positions: dict[str, list[int]]
    times: tuple[tuple[float, float], ...] = ()
    passage_breaks: tuple[int, ...] = ()


def _words(text: str, *, caption: bool = False) -> tuple[str, ...]:
    text = html.unescape(text)
    if caption:
        text = ANNOTATION_RE.sub(" ", TAG_RE.sub("", text))
    text = re.sub(r"(?<=\w)['’‘ʼ](?=\w)", "", text)
    normalized = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    words: list[str] = []
    for word in WORD_RE.findall(plain):
        if word not in FILLERS and (not words or word != words[-1] or word in PROTECTED_WORDS):
            words.append(word)
    return tuple(words)


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _escaped(text: str, index: int) -> bool:
    start = index
    while start and text[start - 1] == "\\":
        start -= 1
    return (index - start) % 2 == 1


def _mask_code(text: str) -> str:
    # Pair whole runs of the same length, including across wrapped lines.
    runs = list(BACKTICKS_RE.finditer(text))
    next_run: dict[int, int] = {}
    closing = {}
    for index in range(len(runs) - 1, -1, -1):
        size = len(runs[index][0])
        closing[index] = next_run.get(size)
        next_run[size] = index
    masked = list(text)
    index = 0
    while index < len(runs):
        start = runs[index].start()
        end = closing[index]
        if end is not None and not _escaped(text, start):
            stop = runs[end].end()
            masked[start:stop] = _blank(text[start:stop])
            index = end
        index += 1
    return "".join(masked)


def _blocks(text: str) -> Iterator[tuple[int, list[str]]]:
    """Keep citations local to paragraphs, headings, list items and blockquotes."""
    lines: list[str] = []
    start = 0
    fence = ""
    fence_indent = 0
    fence_quoted = False
    quoted = False
    list_indent = 0
    list_quoted = False
    for number, raw in enumerate(text.splitlines(), 1):
        quote_prefix = BLOCKQUOTE_RE.match(raw)
        in_quote = quote_prefix is not None
        line = raw[quote_prefix.end():] if quote_prefix else raw
        expanded = line.expandtabs(4)
        indent = len(expanded) - len(expanded.lstrip(" "))
        blank = not line.strip()

        if fence:
            # Only strip a blockquote prefix when the opening fence had one.
            # Otherwise `>` and its preceding spaces are literal code content.
            fence_line = expanded if fence_quoted else raw.expandtabs(4)
            fence_line_indent = len(fence_line) - len(fence_line.lstrip(" "))
            fence_blank = not fence_line.strip()
            if (fence_quoted and not in_quote) or (not fence_blank and fence_line_indent < fence_indent):
                fence = ""  # The containing block ended before the fence closed.
            else:
                closing = FENCE_RE.match(fence_line[fence_indent:])
                if closing and closing[1].startswith(fence) and not closing[2].strip():
                    fence = ""
                continue

        # A list's content column survives blank lines. Test its block syntax
        # at that column so loose list paragraphs and their fences agree.
        content_indent = list_indent if in_quote == list_quoted and indent >= list_indent else 0
        thematic = THEMATIC_RE.match(expanded[content_indent:])
        item = None if thematic else LIST_RE.match(line)
        if item:
            content_indent = len(item[0].expandtabs(4))
        marker = FENCE_RE.match(expanded[content_indent:]) if not thematic else None
        heading = HEADING_RE.match(line)
        if lines and (marker or thematic or blank or item or heading or quoted != in_quote):
            yield start, lines
            lines = []

        if item:
            list_indent, list_quoted = content_indent, in_quote
        elif not blank and not content_indent and (not lines or list_quoted != in_quote):
            list_indent = 0

        if thematic:
            continue
        if marker and (marker[1].startswith("~") or "`" not in marker[2]):
            fence, fence_indent, fence_quoted = marker[1], content_indent, in_quote
        elif heading:
            yield number, [line]
        elif not blank:
            if not lines:
                if indent >= content_indent + 4:
                    continue  # Indented code cannot interrupt an open paragraph.
                start = number
                quoted = in_quote
            lines.append(line)
    if lines:
        yield start, lines


def _label(text: str) -> str:
    return " ".join(text.casefold().split())


def _citation_blocks(text: str, sources: dict[str, list[Path]]) -> Iterator[tuple[int, str, str, set[str]]]:
    references = {}
    blocks = []
    for line, lines in _blocks(text):
        # A definition declares a target; only an actual reference uses it.
        for index, value in enumerate(lines):
            definition = REFERENCE_RE.match(value)
            if definition is None:
                break
            label = REFERENCE_LABEL_RE.match(value)[1]
            if label.startswith("^"):
                break
            target = definition["angle"] if definition["angle"] is not None else definition["bare"]
            references.setdefault(_label(label), target)
            lines[index] = _blank(value)
        blocks.append((line, "\n".join(lines)))

    for line, original in blocks:
        text = _mask_code(original)
        targets = set()
        visible = list(text)
        quoted_text = list(original)
        reference_text = list(text)
        for match in INLINE_RE.finditer(text):
            if _escaped(text, match.start()):
                continue
            name = "angle" if match["angle"] is not None else "bare"
            target = match[name]
            if target in sources and not match[0].startswith("!"):
                targets.add(target)
            # Keep visible link labels for quotation detection, not URL/title text.
            start = text.rfind("](", match.start(), match.start(name)) + 1
            visible[start:match.end()] = quoted_text[start:match.end()] = _blank(text[start:match.end()])
            reference_text[match.start():match.end()] = _blank(match[0])
        for match in REFERENCE_USE_RE.finditer("".join(reference_text)):
            if match[0].startswith("!") or _escaped(text, match.start()):
                continue
            target = references.get(_label(match[2] or match[1]))
            if target in sources:
                targets.add(target)
            if target is not None and match[2] is not None:
                start = match.start(2) - 1
                visible[start:match.end()] = quoted_text[start:match.end()] = _blank(text[start:match.end()])
        yield line, "".join(visible), "".join(quoted_text), targets


def _seconds(value: str) -> float:
    result = 0.0
    for part in value.replace(",", ".").split(":"):
        result = 60 * result + float(part)
    return result


def _stamp_spans(first: str, last: str | None) -> tuple[tuple[float, float], ...]:
    def readings(value: str) -> list[tuple[str, float]]:
        parts = [float(part) for part in value.replace(",", ".").split(":")]
        if parts[-1] >= 60 or (len(parts) == 3 and parts[-2] >= 60):
            return []
        if len(parts) == 3:
            return [("hms", _seconds(value))]
        return [("ms", _seconds(value)), ("hm", 60 * _seconds(value))]

    # As in the house notes, two-part readings may mean MM:SS or HH:MM.
    return tuple(
        (lo, hi + (59 if end == "hm" else 0))
        for start, lo in readings(first)
        for end, hi in readings(last or first)
        if (start == end or "hms" in (start, end)) and hi >= lo
    )


def _stamps(text: str) -> Iterator[tuple[int, int, bool, bool, Stamp]]:
    for match in STAMP_RE.finditer(text):
        prefix = text[max(0, match.start() - 32):match.start()]
        suffix = text[match.end():match.end() + 20]
        if DURATION_BEFORE_RE.search(prefix) or DURATION_AFTER_RE.match(suffix):
            continue
        if spans := _stamp_spans(match["first"], match["last"]):
            yield match.start(), match.end(), bool(PAREN_RE.search(prefix)), suffix.lstrip().startswith(":"), Stamp(match[0], spans)


def _quotes(text: str, sources: dict[str, list[Path]]) -> Iterator[Quote]:
    blocks = list(_citation_blocks(text, sources))
    file_targets = set().union(*(targets for _, _, _, targets in blocks))
    for line, block, quoted_text, direct in blocks:
        targets = direct or file_targets
        target = next(iter(targets)) if len(targets) == 1 else None
        quoted = list(QUOTE_RE.finditer(block))
        masked = list(block)
        for match in quoted:
            # Only terminal punctuation can separate a quote from following prose.
            start, end = match.span(1 if match[1] is not None else 2)
            last = start + len(block[start:end].rstrip())
            stop = last - 1 if block[last - 1:last] in {".", "!", "?"} else end
            masked[start:stop] = re.sub(r"[^\n]", "x", block[start:stop])
        stamp_text = "".join(masked)
        stamps = list(_stamps(stamp_text))
        boundaries = [0]
        for boundary in SENTENCE_RE.finditer(stamp_text):
            # Three abbreviation characters plus their word-boundary context.
            # Never scan a growing paragraph prefix for each sentence boundary.
            tail_start = max(0, boundary.start() - 4)
            before_period = stamp_text[tail_start:boundary.start()]
            if abbreviation := ABBREVIATION_RE.search(before_period):
                # A comma introduces an example/explanation, including names.
                # Ignore emphasis on either side of the abbreviation; otherwise
                # retain the existing capitalized-prose boundary convention.
                preceding = tail_start + abbreviation.start() - 1
                while preceding >= 0 and stamp_text[preceding] in " \t\n*_":
                    preceding -= 1
                introduced = preceding >= 0 and stamp_text[preceding] == ","
                following = boundary.end()
                while following < len(stamp_text) and stamp_text[following] in "*_":
                    following += 1
                next_char = stamp_text[following:following + 1]
                if introduced or next_char.islower() or next_char in {"“", '"'}:
                    continue
            boundaries.append(boundary.end())
        boundaries.append(len(block))
        for match in quoted:
            start, end = match.span(1 if match[1] is not None else 2)
            quote = quoted_text[start:end]
            parts = tuple(part for fragment in ELLIPSIS_RE.split(quote) if (part := _words(fragment)))
            minimum = 1 if len(direct) == 1 else FALLBACK_MIN_WORDS
            if sum(map(len, parts)) < minimum:
                continue
            lo, hi = next((lo, hi) for lo, hi in zip(boundaries, boundaries[1:]) if lo <= match.start() < hi)
            nearby = []
            for start, end, backward, forward, stamp in stamps:
                if lo <= start < hi:
                    if end <= match.start() and not backward:
                        nearby.append((match.start() - end, stamp))
                    elif start >= match.end() and not forward:
                        nearby.append((start - match.end(), stamp))
            stamp = None
            tied_stamps = ()
            if nearby:
                distance = min(distance for distance, _ in nearby)
                nearest = {item.spans: item for gap, item in nearby if gap == distance}
                if len(nearest) == 1:
                    stamp = next(iter(nearest.values()))
                else:
                    tied_stamps = tuple(nearest.values())
            yield Quote(
                line + block.count("\n", 0, match.start()), quote, parts, target, stamp,
                len(targets) > 1, tied_stamps,
            )


def _cue_words(text: str) -> Iterator[tuple[tuple[float, float], tuple[str, ...]]]:
    """Read SRT/VTT cues, or a plain caption file with bracketed cue starts."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if match := CUE_RE.match(line):
            end = index + 1
            while end < len(lines) and lines[end].strip() and not CUE_RE.match(lines[end]):
                end += 1
            body = lines[index + 1:end]
            if len(body) > 1 and end < len(lines) and CUE_RE.match(lines[end]):
                # A cue ID directly before the next time line is not speech.
                body.pop()
            span = _seconds(match[1]), _seconds(match[2])
            if span[0] <= span[1]:
                yield span, _words("\n".join(body), caption=True)
        elif match := CLEAN_CUE_RE.match(line):
            instant = _seconds(match[1])
            yield (instant, instant), _words(match[2], caption=True)


def _read_source(path: Path) -> Source | None:
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return None
    times: list[tuple[float, float]] = []
    passage_breaks: list[int] = []
    if path.suffix.lower() in {".srt", ".vtt"} or CLEAN_CUE_RE.search(text):
        caption_words: list[str] = []
        previous: tuple[str, ...] = ()
        previous_end = -float("inf")
        for span, cue in _cue_words(text):
            if not cue:
                continue
            # A large silence separates passages even when the words align.
            if times and span[0] > previous_end + WINDOW_SECONDS:
                passage_breaks.append(len(caption_words))
            overlap = 0
            if span[0] <= previous_end + 1:
                for size in range(min(len(previous), len(cue)), 0, -1):
                    if previous[-size:] == cue[:size]:
                        overlap = size
                        break
            caption_words.extend(cue[overlap:])
            times.extend([span] * (len(cue) - overlap))
            previous, previous_end = cue, span[1]
        if not times:
            return None
        tokens = tuple(caption_words)
    else:
        tokens = _words(text)
    positions: dict[str, list[int]] = {}
    for index, word in enumerate(tokens):
        positions.setdefault(word, []).append(index)
    return Source(tokens, positions, tuple(times), tuple(passage_breaks))


def _harmless(gap: tuple[str, ...], words: tuple[str, ...], index: int) -> bool:
    if any(word in PROTECTED_WORDS for word in gap):
        return False
    nearby = words[max(0, index - 2):index + 2]
    cursor = 0
    while cursor < len(gap):
        if gap[cursor:cursor + 2] in FILLER_PHRASES:
            cursor += 2
            continue
        word = gap[cursor]
        cursor += 1
        false_start = cursor == len(gap) and len(word) < len(words[index]) and words[index].startswith(word)
        if word not in DROPPABLE and word not in nearby and not false_start:
            return False
    return True


def _matches(source: Source, quote: Quote, allowed_drops: int = 0) -> Iterator[tuple[int, bool]]:
    """Yield match starts and whether a matched caption word is near the cited time."""
    words = tuple(word for part in quote.parts for word in part)
    boundaries = set()
    offset = 0
    for part in quote.parts[:-1]:
        offset += len(part)
        boundaries.add(offset)

    def near(position: int) -> bool:
        if quote.stamp is None or not source.times:
            return False
        cue_start, cue_end = source.times[position]
        return any(
            cue_end >= lo - WINDOW_SECONDS and cue_start <= hi + WINDOW_SECONDS
            for lo, hi in quote.stamp.spans
        )

    for start in source.positions.get(words[0], ()):
        break_index = bisect_right(source.passage_breaks, start)
        passage_end = (
            source.passage_breaks[break_index] if break_index < len(source.passage_breaks) else len(source.tokens)
        )
        # Keep alternative alignments: the first repeated fragment after an
        # ellipsis may dead-end while the next one matches the rest.
        ends = {(start + 1, near(start)): 0}
        for index, word in enumerate(words[1:], 1):
            following: dict[tuple[int, bool], int] = {}
            positions = source.positions.get(word, ())
            for (end, near_citation), dropped in ends.items():
                declared = index in boundaries
                width = MAX_FRAGMENT_GAP if declared else allowed_drops - dropped
                for pos_index in range(bisect_left(positions, end), len(positions)):
                    position = positions[pos_index]
                    if position >= passage_end or position > end + width:
                        break
                    gap = source.tokens[end:position]
                    if declared:
                        if any(token in PROTECTED_WORDS for token in gap):
                            break
                        count = dropped
                    elif not _harmless(gap, words, index):
                        continue
                    else:
                        count = dropped + len(gap)
                    state = (position + 1, near_citation or near(position))
                    following[state] = min(count, following.get(state, count))
            if not following:
                ends = {}
                break
            ends = following
        yield from ((start, near_citation) for _, near_citation in ends)


def _format_time(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def _check(quote: Quote, evidence: dict[Path, Source | None]) -> tuple[str, str]:
    if not evidence:
        return "unchecked", "configured source unavailable (no evidence files)"
    word_match = False
    other_time = None
    for path, source in evidence.items():
        if source is None:
            continue
        for start, near_citation in _matches(source, quote):
            if quote.stamp is None or near_citation:
                return "verified", ""
            if not source.times:
                word_match = True
                break
            if other_time is None:
                other_time = f"{path.name} at {_format_time(source.times[start][0])}"

    missing = [path.name for path, source in evidence.items() if source is None]
    if other_time and not missing:
        return "wrong_timestamp", f"cites {quote.stamp.text}; wording found in {other_time}"

    # Preserve the distinction between a literal match and harmless drops.
    # Only caption words have the documented allowance for such omissions.
    allowed_drops = max(3, sum(map(len, quote.parts)) // 4)
    for source in evidence.values():
        if source is not None and source.times and any(
            quote.stamp is None or near_citation
            for _, near_citation in _matches(source, quote, allowed_drops)
        ):
            return "harmless_elision", ""
    if missing:
        return "unchecked", "configured source unavailable: " + ", ".join(missing)
    if word_match:
        return "unchecked", f"wording found in text; no caption match establishes {quote.stamp.text}"
    cited = f" at {quote.stamp.text}" if quote.stamp else ""
    return "text_mismatch", f"wording not found in the configured source{cited}"


def audit(before: dict[Path, str] | None, after: dict[Path, str], sources: dict[str, list[Path]]) -> AuditResult:
    """Compare quotations without inferring an origin when the baseline is missing."""
    if not after:
        return AuditResult("unchecked", [], "No Markdown files in the quote audit scope")
    if not sources:
        return AuditResult("unchecked", [], "No quote sources configured")
    cache: dict[Path, Source | None] = {}
    findings = []
    unchecked: Counter[str] = Counter()
    harmless = 0
    for path, text in sorted(after.items()):
        previous = Counter(quote.fingerprint for quote in _quotes((before or {}).get(path, ""), sources))
        for quote in _quotes(text, sources):
            fingerprint = quote.fingerprint
            seen = previous[fingerprint] > 0
            previous[fingerprint] -= 1
            if quote.target is None:
                unchecked["ambiguous mapped citations" if quote.ambiguous else "no mapped citation"] += 1
                continue
            evidence = {}
            for source in sources[quote.target]:
                if source not in cache:
                    cache[source] = _read_source(source)
                evidence[source] = cache[source]
            if quote.tied_stamps:
                unchecked["ambiguous equally near timestamps: " + ", ".join(
                    stamp.text for stamp in quote.tied_stamps
                )] += 1
            code, detail = _check(quote, evidence)
            if code == "unchecked":
                unchecked[detail] += 1
            elif code == "harmless_elision":
                harmless += 1
            elif code != "verified":
                origin = "unknown" if before is None else "preexisting" if seen else "new"
                shown = " ".join(quote.text.split())
                findings.append(Finding(str(path), quote.line, code, f"{shown!r} [{quote.target}]: {detail}", origin))
    reasons = [f"{count} quote(s) unchecked: {reason}" for reason, count in unchecked.items()]
    if harmless:
        reasons.append(f"{harmless} quote(s) with harmless elision")
    return AuditResult("unchecked" if unchecked else "checked", findings, "; ".join(reasons) or None)


def _load_sources(path: Path) -> dict[str, list[Path]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Quote sources must map citation targets to lists of paths")
    if any(
        not target or "\0" in target or not isinstance(paths, list)
        or any(not isinstance(item, str) or not item or "\0" in item for item in paths)
        for target, paths in payload.items()
    ):
        raise ValueError("Quote sources must map nonempty citation targets to lists of paths")
    return {target: [path.parent / item for item in paths] for target, paths in payload.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources", type=Path, required=True, help="JSON mapping of exact citation targets to evidence paths")
    parser.add_argument("--json", action="store_true", help="emit the audit result as JSON")
    parser.add_argument("files", nargs="+", type=Path, help="Markdown files to check")
    args = parser.parse_args(argv)
    try:
        sources = _load_sources(args.sources)
        notes = {path: path.read_text(encoding="utf-8") for path in dict.fromkeys(args.files)}
        result = audit(None, notes, sources)
    except (OSError, ValueError, RecursionError) as exc:
        result = AuditResult("error", [], str(exc))
    if args.json:
        print(result.to_json())
    else:
        print(result.status + (f" - {result.reason}" if result.reason else ""))
        for finding in result.findings:
            print(f"{finding.file}:{finding.line}: {finding.code} ({finding.origin}): {finding.detail}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
