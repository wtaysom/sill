"""Check leading datelines and clock readings against a completed attempt."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from scripts.beat_audit import AuditResult, Finding, Window, run_audit_cli


HEADER_LINES = 12
ESTIMATE_TOLERANCE = timedelta(minutes=30)
ROUNDING_TOLERANCE = timedelta(minutes=10)
MAX_RANGE = timedelta(minutes=60)

DATELINE_RE = re.compile(
    r"^\s*[*_]{0,2}(?:Date\b[^:\n]{0,40}|Bench clock receipt[^:\n]{0,20})"
    r"[*_]{0,2}:\s*[*_]{0,2}",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
DATED_TITLE_RE = re.compile(r"^\s{0,3}#{1,6}\s+.+?\s+[—–-]\s+(?=\d{4}-\d{2}-\d{2})")
LABEL_RE = re.compile(r"^\s*(?:[*_]{2}[^*_\n:]{1,60}[*_]{0,2}:|[A-Z][\w -]{0,32}:)")
STOP_RE = re.compile(r"^\s*(?:$|>|[`~]{3}|[-+*]\s|\d+[.)]\s)")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
CUE_RE = re.compile(
    r"(?:\bclock\b|`date[^`\n]{0,40}`)(?:\s+[a-z]+){0,2}?\s*"
    r"(?:\bread\b|\breads\b|\breading\b|\breturned\b|\bshowed\b|\bsaid\b|\bsays\b|:)"
    r"\s*(?:at\s+)?[`(]?\s*(?:(?:~|about|approx\.?|approximately|around|circa)\s*)?"
    r"(?=\d|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s)",
    re.IGNORECASE,
)
CLOCK_LEAD_RE = re.compile(r"^\s*[*_]{0,2}(?:(?:session|bench|wall)\s+)?(?:clock\b|`date\b)", re.IGNORECASE)
UNIX_DATE_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(?P<mon>[A-Z][a-z]{2})\s+(?P<d>\d{1,2})\s+"
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})\s+(?P<abbr>[A-Z]{2,6})\s+(?P<y>\d{4})"
)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
OFFSET = r"\d{2}:?\d{2}"
# An attached HH:MM-HH:MM is a range, except on an ISO or seconds reading.
TIME_RE = re.compile(
    r"(?:(?P<day>\d{4}-\d{2}-\d{2})(?:(?P<iso>T)|,?\s+))?"
    r"(?<![\d:])(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2})(?:\.(?P<fraction>\d{1,6}))?)?(?![\d:])"
    r"(?:\s*(?P<ampm>[AaPp]\.?[Mm]\.?)(?![A-Za-z]))?"
    rf"(?:\s*(?:UTC|GMT)\s*(?P<named_offset>[+-]{OFFSET})"
    rf"|\s*(?P<offset>\+{OFFSET}|-\d{{4}}|Z)"
    rf"|(?P<negative>\s+-{OFFSET}|(?(s)|(?(iso)|(?!)))-{OFFSET})"
    r"|\s*(?P<abbr>[A-Z]{2,6}))?(?![\w:])"
)
RANGE_RE = re.compile(r"\s*[–—-]\s*~?\s*")
APPROX_RE = re.compile(r"(?:~|\b(?:about|approx\.?|approximately|around|circa))\s*$", re.IGNORECASE)
ESTIMATE_BEFORE_RE = re.compile(
    r"\bestimat\w*\b[:\s]*(?:(?:at|as|about|around|approx\.?|approximately|circa)\s+)?~?\s*$",
    re.IGNORECASE,
)
ESTIMATE_AFTER_RE = re.compile(r"[\s,;:(—–-]*(?:(?:an?|the)\s+)?estimat", re.IGNORECASE)
UNMEASURED_RE = re.compile(
    r"\bno\s+(?:`date`?(?:\s+(?:tool|command))?(?:\s+(?:run|call|read)\w*)?|clock\s+(?:read|check)\w*)",
    re.IGNORECASE,
)
ATTRIBUTED_RE = re.compile(r"`date[^`\n]{0,40}`,?\s+(?:run|read)\b|\bclock\s+read\b", re.IGNORECASE)


@dataclass(frozen=True)
class Reading:
    day: date | None
    clock: time
    zone: timezone | str | None


@dataclass(frozen=True)
class Claim:
    first: Reading
    last: Reading | None
    approximate: bool
    estimate: bool
    day: date | None = None
    line: int = field(default=0, compare=False)
    text: str = field(default="", compare=False)

    def tolerance(self, grace: timedelta) -> timedelta:
        if self.estimate:
            return ESTIMATE_TOLERANCE
        return max(grace, ROUNDING_TOLERANCE) if self.approximate else grace


def _header(text: str) -> Iterator[tuple[int, str]]:
    """Read the leading title and metadata, stopping at the first body block."""
    lines = text.splitlines()[:HEADER_LINES]
    index = 0
    seen_content = False
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        title = not seen_content and HEADING_RE.match(line)
        dateline = DATELINE_RE.match(line)
        clock = CLOCK_LEAD_RE.match(line)
        if not (title or dateline or clock or LABEL_RE.match(line)):
            break
        seen_content = True
        start = index
        index += 1
        if dateline or clock:
            while index < min(len(lines), start + 3):
                following = lines[index]
                if STOP_RE.match(following) or HEADING_RE.match(following) or LABEL_RE.match(following):
                    break
                index += 1
        if title or dateline or clock:
            yield start + 1, "\n".join(lines[start:index])


def _date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _zone(match: re.Match) -> timezone | str | None:
    offset = (match["named_offset"] or match["offset"] or match["negative"] or "").strip()
    if offset == "Z" or match["abbr"] in {"UTC", "GMT"}:
        return timezone.utc
    if offset:
        digits = offset[1:].replace(":", "")
        hours, minutes = int(digits[:2]), int(digits[2:])
        if minutes > 59 or hours > 14 or (hours == 14 and minutes):
            return offset
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(delta if offset[0] == "+" else -delta)
    return match["abbr"]


def _reading(match: re.Match, *, header_date: bool = False) -> Reading | None:
    hour = int(match["h"])
    if match["ampm"]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if match["ampm"][0].lower() == "p" else 0)
    day = _date(match["day"])
    if match["day"] and day is None:
        return None
    if header_date and not match["iso"]:
        day = None
    try:
        microsecond = int((match["fraction"] or "").ljust(6, "0"))
        clock = time(hour, int(match["m"]), int(match["s"] or 0), microsecond)
    except ValueError:
        return None
    return Reading(day, clock, _zone(match))


def _parse_claim(
    match: re.Match, line: int, *, dateline: bool, attributed: bool,
) -> Claim | None:
    first = _reading(match, header_date=dateline)
    if first is None:
        return None
    span = match.string
    end = match.end()
    separator = RANGE_RE.match(span, end)
    partner = TIME_RE.match(span, separator.end()) if separator else None
    last = _reading(partner) if partner else None
    prefix = span[max(0, match.start() - 48):match.start()]
    approximate = bool(APPROX_RE.search(prefix))
    if last is not None:
        approximate = approximate or "~" in separator[0]
        end = partner.end()
    unmeasured = bool(ESTIMATE_BEFORE_RE.search(prefix) or ESTIMATE_AFTER_RE.match(span, end))
    unmeasured = unmeasured or (dateline and bool(UNMEASURED_RE.search(span)))
    return Claim(
        first, last, approximate, unmeasured or (approximate and not attributed),
        line=line + span.count("\n", 0, match.start()), text=" ".join(span[match.start():end].split()),
    )


def _unix_claim(match: re.Match, line: int) -> Claim | None:
    try:
        day = date(int(match["y"]), MONTHS.index(match["mon"]) + 1, int(match["d"]))
        clock = time(int(match["h"]), int(match["m"]), int(match["s"]))
    except ValueError:
        return None
    zone = timezone.utc if match["abbr"] in {"UTC", "GMT"} else match["abbr"]
    return Claim(Reading(day, clock, zone), None, False, False, line=line, text=match[0])


def _claims(text: str) -> Iterator[Claim]:
    header_day = None
    for line, span in _header(text):
        dateline = DATELINE_RE.match(span) or DATED_TITLE_RE.match(span)
        start = dateline.end() if dateline else 0
        date_token = DATE_RE.search(span, start) if dateline else None
        if date_token is not None:
            header_day = _date(date_token[0])
        cues = list(CUE_RE.finditer(span, start))
        claims: dict[int, Claim] = {}
        first_cue = cues[0].end() if cues else len(span)
        if dateline:
            for match in TIME_RE.finditer(span, start):
                # A clock cue may contain a whole dated reading or Unix date
                # output. Leave that reading to the cue parser.
                if match.start() >= first_cue:
                    break
                if claim := _parse_claim(
                    match, line, dateline=True, attributed=bool(ATTRIBUTED_RE.search(span)),
                ):
                    claims[match.start()] = claim
                    break
        for cue in cues:
            start = cue.end()
            if unix := UNIX_DATE_RE.match(span, start):
                claim = _unix_claim(unix, line + span.count("\n", 0, start))
            elif match := TIME_RE.match(span, start):
                claim = _parse_claim(match, line, dateline=False, attributed=True)
            else:
                continue
            if claim is not None:
                if dateline and UNMEASURED_RE.search(span):
                    claim = replace(claim, estimate=True)
                claims[start] = claim
        for claim in sorted(claims.values(), key=lambda item: item.line):
            yield replace(claim, day=None if claim.first.day is not None else header_day)


def _span_width(claim: Claim) -> timedelta | None:
    """Width independent of an attempt, when both readings share a clock."""
    if claim.last is None:
        return None
    first, last = claim.first, claim.last
    if first.zone is not None and last.zone is not None and first.zone != last.zone:
        return None
    if last.day is not None and first.day is None:
        return None
    anchor = first.day or date(2000, 1, 1)
    low, high = datetime.combine(anchor, first.clock), datetime.combine(last.day or anchor, last.clock)
    if last.day is None and high < low:
        high += timedelta(days=1)
    return high - low


def _candidate_starts(first: Reading, zone: timezone, day: date | None, window: Window) -> Iterator[datetime]:
    if first.day is not None:
        yield datetime.combine(first.day, first.clock, zone)
        return

    home = window.start.tzinfo
    if day is None:
        local_start = window.start.astimezone(zone).date()
        local_end = window.end.astimezone(zone).date()
        for base in dict.fromkeys((local_start, local_end)):
            for shift in (-1, 0, 1):
                yield datetime.combine(base + timedelta(days=shift), first.clock, zone)
        return

    # A header date is in the receipt's offset, even for a reading from
    # another offset. Borrow an adjacent date only at an actual midnight crossing.
    days = [day]
    began, ended = window.start.date(), window.end.astimezone(home).date()
    if began < day <= ended:
        days.append(day - timedelta(days=1))
    if began <= day < ended:
        days.append(day + timedelta(days=1))
    for local_day in days:
        near = datetime.combine(local_day, time(12), home).astimezone(zone).date()
        for shift in (-1, 0, 1):
            candidate = datetime.combine(near + timedelta(days=shift), first.clock, zone)
            if candidate.astimezone(home).date() == local_day:
                yield candidate


def _distance(low: datetime, high: datetime, window: Window) -> timedelta:
    return max(window.start - high, low - window.end, timedelta(0))


def _compare(
    claim: Claim, window: Window, grace: timedelta,
) -> tuple[str, str] | None:
    first, last = claim.first, claim.last
    first_zone = first.zone or (last and last.zone) or window.start.tzinfo
    last_zone = (last and last.zone) or first_zone
    spans = []
    for low in _candidate_starts(first, first_zone, claim.day, window):
        high = low
        if last is not None:
            day = last.day or low.astimezone(last_zone).date()
            high = datetime.combine(day, last.clock, last_zone)
            if last.day is None and high < low:
                high += timedelta(days=1)
        spans.append((low, high))
    low, high = min(spans, key=lambda span: _distance(*span, window))
    if high < low:
        return "invalid_range", f"{claim.text!r} ends before it starts"
    if _distance(low, high, window) <= claim.tolerance(grace):
        if high - low > MAX_RANGE:
            return "range_too_wide", f"{claim.text!r} spans more than 60 minutes"
        return None
    code = "estimate_outside_window" if claim.estimate else "outside_window"
    return code, (
        f"{claim.text!r} resolves to {low.isoformat()}"
        + (f" to {high.isoformat()}" if high != low else "")
        + f", outside the attempt ({window.start.isoformat()} to {window.end.isoformat()})"
    )


def audit(
    before: dict[Path, str] | None,
    after: dict[Path, str],
    window: Window | None,
    *,
    grace_seconds: float = 90,
) -> AuditResult:
    """Compare only attributable clocks, retaining independently known discrepancies."""
    reasons = []
    if before is None or window is None:
        reasons.append("Clock attribution requires a completed attempt and a baseline")
    unsupported = set()
    findings = []
    grace = timedelta(seconds=grace_seconds)
    for path, text in after.items():
        previous = Counter(_claims((before or {}).get(path, "")))
        for claim in _claims(text):
            seen = previous[claim] > 0
            previous[claim] -= 1
            origin = "unknown" if before is None else "preexisting" if seen else "new"
            zones = [reading.zone for reading in (claim.first, claim.last) if reading is not None]
            unknown_zone = next((zone for zone in zones if isinstance(zone, str)), None)
            width = _span_width(claim)
            if unknown_zone is not None:
                verdict = ("unsupported_timezone", f"{claim.text!r} uses unsupported timezone {unknown_zone}")
                if origin != "preexisting":
                    unsupported.add(unknown_zone)
            elif window is not None and origin == "new":
                verdict = _compare(claim, window, grace)
            elif width is not None and width > MAX_RANGE:
                verdict = ("range_too_wide", f"{claim.text!r} spans more than 60 minutes")
            elif width is not None and width < timedelta(0):
                verdict = ("invalid_range", f"{claim.text!r} ends before it starts")
            else:
                verdict = None
            if verdict is not None:
                code, detail = verdict
                findings.append(Finding(str(path), claim.line, code, detail, origin))
    if unsupported:
        reasons.append(f"Unsupported clock timezone: {', '.join(sorted(unsupported))}")
    return AuditResult("unchecked" if reasons else "checked", findings, ". ".join(reasons) or None)


def _audit_cli(before, after, window, *, grace_seconds: float) -> AuditResult:
    result = audit(before, after, window, grace_seconds=grace_seconds)
    if window is not None and any(next(_claims(text), None) is not None for text in before.values()):
        reason = "Clock attribution is unchecked for explicitly historical files"
        if result.reason:
            reason = f"{result.reason}. {reason}"
        return replace(result, status="unchecked", reason=reason)
    return result


def main(argv: list[str] | None = None) -> int:
    return run_audit_cli(_audit_cli, __doc__, argv)


if __name__ == "__main__":
    raise SystemExit(main())
