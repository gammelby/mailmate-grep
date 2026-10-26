"""Filters, candidate evaluation on one snapshot and Message-ID grouping.

Candidates follow design section 8 steps 2 and 3 with the milestone 3 plan
decisions D6 to D8 and rulings C3 and C5: Location filters first, then
dates, then flags, then address and header filters, then the body. Every
step works on the current candidate set, which starts as the live set
(rule R1). Flags are per message, the union over its copies. Header
values are matched in the decoded form the index stores (rule R4), body
records one at a time (rule R5) and dates follow rule R6. Grouping
(design section 8 step 4, ruling RP3) folds live ids that share a
Message-ID and keeps the copies whose `.eml` exists. This module opens no
file; it only checks paths with os.path.exists.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from mmgrep.index import Index, required_literal
from mmgrep.source import (SourceRef, account_display, eml_path,
                           message_of_part, parse_source)
from mmgrep.store import Snapshot

SOURCE = "#source"
MESSAGE_ID = "message-id"
DATE = "#date"
DATE_RECEIVED = "#date-received"
UNQUOTED = "#unquoted"
QUOTED = "#quoted"
ROOT_BODY_PART = "#root-body-part"
FLAGS = "#flags"
BASE_INDEXES = frozenset({SOURCE, MESSAGE_ID, DATE, DATE_RECEIVED,
                          "from", "to", "cc", "subject"})
ADDRESS_FIELDS = {
    "from_": ("from",),
    "to": ("to",),
    "cc": ("cc",),
    "any_address": ("from", "to", "cc", "bcc"),
}
HEADER_KEYS = ("date", "from", "to", "cc", "subject", "message-id")

# Ruling C5: An address token is a maximal run of atext characters and
# dots, "@" and a maximal run of host characters.
_ATEXT = "A-Za-z0-9!#$%&'*+/=?^_`{|}~.-"
_TOKEN = re.compile(
    f"(?<![{_ATEXT}])[{_ATEXT}]+@[A-Za-z0-9.-]+(?![A-Za-z0-9.-])")
_LOCAL = re.compile(f"[{_ATEXT}]+")
_HOST = re.compile("[A-Za-z0-9.-]+")
_HEADER_NAME = re.compile("[!-9;-~]+")
_AT = b"@"

# --flag names for the IMAP system flags and the keywords MailMate sets.
# Any other name is one keyword (a MailMate tag), matched as written.
FLAG_ALIASES = {
    "seen": ("\\seen",),
    "answered": ("\\answered",),
    "flagged": ("\\flagged",),
    "draft": ("\\draft",),
    "deleted": ("\\deleted",),
    "forwarded": ("$forwarded", "forwarded"),
    "junk": ("$junk", "junk"),
}
_FLAG_TOKEN = re.compile("[!-~]+")

# Decision D6: The stored forms of FORMAT.md section 5. Anything else goes
# through strptime itself, so results always equal the strptime route.
_INDEX_DATE = re.compile(
    rb"([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})"
    rb"(?: ([+-])([0-9]{2})([0-5][0-9]))?")
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S %z"
_RECEIVED_FORMAT = "%Y-%m-%d %H:%M:%S"
_ZONES: dict[bytes, timezone] = {}

# --since and --until: ISO 8601 forms that every supported Python accepts.
_ISO = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(?P<time>[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{3}|\.[0-9]{6})?)?"
    r"(?:Z|z|[+-][0-9]{2}:[0-9]{2})?)?")


@dataclass(frozen=True)
class AddrSpec:
    """One address spec: kind is "domain", "exact" or "regex"."""

    kind: str
    value: str


@dataclass
class Filters:
    """The query. Repeated values of one option OR; different options AND.

    since is inclusive and until exclusive; both are aware datetimes.
    headers maps a lowercased index name to its patterns. flag and no_flag
    hold one lowercased token set per option (parse_flag): A message
    passes when its flags meet some flag set and no no_flag set.
    """

    from_: list[AddrSpec] = field(default_factory=list)
    to: list[AddrSpec] = field(default_factory=list)
    cc: list[AddrSpec] = field(default_factory=list)
    any_address: list[AddrSpec] = field(default_factory=list)
    subject: list[str] = field(default_factory=list)
    headers: dict[str, list[str]] = field(default_factory=dict)
    since: datetime | None = None
    until: datetime | None = None
    account: list[str] = field(default_factory=list)
    mailbox: list[str] = field(default_factory=list)
    flag: list[frozenset[str]] = field(default_factory=list)
    no_flag: list[frozenset[str]] = field(default_factory=list)
    body: str | None = None
    include_quoted: bool = False
    case_sensitive: bool = False


@dataclass
class CandidateSet:
    """Live message ids that pass the index checks, with step counts."""

    ids: set[int]
    stats: dict[str, int]


@dataclass(frozen=True)
class Copy:
    """One qualifying copy of a message (account with %40 decoded)."""

    id: int
    path: str
    account: str
    mailbox: str
    index_date: datetime | None = None
    date_source: str | None = None


@dataclass
class Group:
    """The qualifying copies of one Message-ID, sorted by path.

    flags lists the flags of every live copy, qualifying or not (None
    without the #flags index, as with --full).
    """

    message_id: str | None
    copies: list[Copy]
    flags: list[str] | None = None


@dataclass
class GroupResult:
    """Groups sorted by first copy path and the count of ids without an .eml."""

    groups: list[Group]
    missing_eml: int


# Specs and options


def parse_addr(spec: str) -> AddrSpec:
    """Parse "@domain", "local@domain" or "re:PATTERN"; ValueError otherwise."""
    if spec.startswith("re:"):
        pattern = spec[3:]
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"invalid address regex {pattern!r}: {e}") from None
        return AddrSpec("regex", pattern)
    local, sep, host = spec.rpartition("@")
    if not sep or not _HOST.fullmatch(host) or host.strip(".") == "":
        raise ValueError(
            f"invalid address {spec!r}: use @domain, local@domain or re:PATTERN")
    if not local:
        return AddrSpec("domain", host.lower())
    if not _LOCAL.fullmatch(local):
        raise ValueError(
            f"invalid address {spec!r}: use @domain, local@domain or re:PATTERN")
    return AddrSpec("exact", f"{local}@{host}".lower())


def parse_header_arg(text: str) -> tuple[str, str]:
    """Split "NAME=PATTERN" into the lowercased index name and the pattern.

    The name must be printable ASCII without space or ":", must not start
    with "#" and must pass the milestone 2 index name rule (decision D8).
    """
    name, sep, pattern = text.partition("=")
    name = name.lower()
    if not sep:
        raise ValueError(f"--header needs NAME=PATTERN, got {text!r}")
    if (not _HEADER_NAME.fullmatch(name) or name.startswith("#")
            or name in (".", "..") or "/" in name):
        raise ValueError(f"--header: invalid header name {name!r}")
    return name, pattern


def parse_flag(name: str) -> frozenset[str]:
    """The lowercased flag tokens a --flag NAME stands for.

    NAME is an alias of FLAG_ALIASES, a system flag such as "\\Answered"
    or a keyword such as "$MailFlagBit0" or a tag. "tag:NAME" is NAME as
    written, for a tag that shares an alias name. IMAP flags are
    case-insensitive, so the tokens are too.
    """
    token = name.lower()
    literal = token.startswith("tag:")
    if literal:
        token = token[4:]
    if not _FLAG_TOKEN.fullmatch(token):
        raise ValueError(f"invalid flag {name!r}: use one word such as answered or \\Seen")
    if literal:
        return frozenset({token})
    return frozenset(FLAG_ALIASES.get(token, (token,)))


def _parse_iso(text: str, option: str) -> tuple[datetime, bool]:
    """The aware instant of text and whether text was a date only."""
    m = _ISO.fullmatch(text)
    if m is None:
        raise ValueError(f"{option}: invalid date {text!r}; use YYYY-MM-DD or an ISO datetime")
    value = text
    if value[-1:] in ("Z", "z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{option}: invalid date {text!r}") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt, m.group("time") is None


def parse_since(text: str) -> datetime:
    """The inclusive lower bound; a naive value is local time."""
    return _parse_iso(text, "--since")[0]


def parse_until(text: str) -> datetime:
    """The exclusive upper bound; a date only means the next local midnight."""
    dt, date_only = _parse_iso(text, "--until")
    if date_only:
        day = datetime(dt.year, dt.month, dt.day) + timedelta(days=1)
        return day.astimezone()
    return dt


def has_filter(f: Filters) -> bool:
    """True when the query restricts anything."""
    return bool(f.from_ or f.to or f.cc or f.any_address or f.subject
                or f.headers or f.since is not None or f.until is not None
                or f.account or f.mailbox or f.flag or f.no_flag
                or f.body is not None)


def needed_indexes(f: Filters) -> set[str]:
    """The indexes the query and its output read."""
    names = set(BASE_INDEXES)
    if f.any_address:
        names.add("bcc")
    if f.body is not None:
        names.update((UNQUOTED, ROOT_BODY_PART))
    if f.include_quoted:
        names.add(QUOTED)
    if f.flag or f.no_flag:
        names.add(FLAGS)
    names.update(f.headers)
    return names


def _flags(f: Filters) -> int:
    return 0 if f.case_sensitive else re.IGNORECASE


# Addresses


def address_tokens(value: str) -> set[str]:
    """The lowercased address tokens of a header value (ruling C5)."""
    return {m.group().lower() for m in _TOKEN.finditer(value)}


def addr_matches(spec: AddrSpec, value: str, flags: int) -> bool:
    """Whether spec matches; value is one address unless spec is a regex.

    A regex spec searches the whole header value with flags. Domain and
    exact specs compare case-insensitively.
    """
    if spec.kind == "regex":
        return re.search(spec.value, value, flags) is not None
    address = value.lower()
    if spec.kind == "exact":
        return address == spec.value
    host = address.rpartition("@")[2]
    return host == spec.value or host.endswith("." + spec.value)


class _TokenMatcher:
    """A search() over the address tokens of a value, for Index.search."""

    def __init__(self, specs: list[AddrSpec]) -> None:
        self.specs = specs

    def search(self, text: str) -> bool:
        return any(addr_matches(s, t, 0) for t in address_tokens(text)
                   for s in self.specs)


# Dates


def _zone(sign: bytes, hours: bytes, minutes: bytes) -> timezone:
    key = sign + hours + minutes
    zone = _ZONES.get(key)
    if zone is None:
        delta = timedelta(hours=int(hours), minutes=int(minutes))
        zone = timezone(-delta if sign == b"-" else delta)
        _ZONES[key] = zone
    return zone


def parse_index_date(value: bytes, received: bool) -> datetime | None:
    """An aware datetime from a #date (received False) or #date-received value.

    Equal to datetime.strptime with "%Y-%m-%d %H:%M:%S %z" (or with
    "%Y-%m-%d %H:%M:%S" and UTC attached) and None where that raises
    (rule R6). Values that are not ASCII give None. The stored forms take
    a regex fast path; every other value goes through strptime itself.
    """
    m = _INDEX_DATE.fullmatch(value)
    try:
        if m is not None and (m.group(7) is None) == received:
            if received:
                zone = timezone.utc
            else:
                zone = _zone(m.group(7), m.group(8), m.group(9))
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5)), int(m.group(6)),
                            tzinfo=zone)
        text = value.decode("ascii")
        if received:
            return datetime.strptime(text, _RECEIVED_FORMAT).replace(tzinfo=timezone.utc)
        dt = datetime.strptime(text, _DATE_FORMAT)
        return dt if dt.tzinfo is not None else None
    except (ValueError, OverflowError, UnicodeDecodeError):
        return None


def effective_index_date(snap: Snapshot, id: int) -> tuple[datetime | None, str | None]:
    """The R6 date of id and its provenance, "date" or "received"."""
    index = snap.indexes.get(DATE)
    value = index.last(id) if index is not None else None
    if value is not None:
        dt = parse_index_date(value, False)
        if dt is not None:
            return dt, "date"
    index = snap.indexes.get(DATE_RECEIVED)
    value = index.last(id) if index is not None else None
    if value is not None:
        dt = parse_index_date(value, True)
        if dt is not None:
            return dt, "received"
    return None, None


# Flags


def mid_members(snap: Snapshot) -> dict[bytes, list[int]]:
    """The live ids of each non-empty last Message-ID value."""
    mid_index = snap.indexes.get(MESSAGE_ID)
    by_mid: dict[bytes, list[int]] = {}
    if mid_index is not None:
        for id in snap.live:
            value = mid_index.last(id)
            if value:
                by_mid.setdefault(value, []).append(id)
    return by_mid


def message_flags(snap: Snapshot, ids: Iterable[int]) -> dict[str, str]:
    """The flags of ids as {lowercased token: first spelling seen}.

    Flags are stored per copy, so a message's flags are the union over
    its copies. Every current #flags record counts (rule R1).
    """
    index = snap.indexes.get(FLAGS)
    out: dict[str, str] = {}
    if index is None:
        return out
    for id in ids:
        for value in index.values(id) or ():
            for token in _decode(value).split():
                out.setdefault(token.lower(), token)
    return out


def flags_ok(tokens: Iterable[str], f: Filters) -> bool:
    """Whether lowercased tokens meet some --flag set and no --no-flag set."""
    have = set(tokens)
    if f.flag and not any(have & s for s in f.flag):
        return False
    return not any(have & s for s in f.no_flag)


def _flag_step(snap: Snapshot, f: Filters, cands: set[int]) -> set[int]:
    """The ids of cands whose message (every copy by Message-ID) passes."""
    mid_index = snap.indexes.get(MESSAGE_ID)
    by_mid = mid_members(snap)
    seen: dict[bytes, bool] = {}
    kept: set[int] = set()
    for id in cands:
        value = mid_index.last(id) if mid_index is not None else None
        if value:
            hit = seen.get(value)
            if hit is None:
                hit = flags_ok(message_flags(snap, by_mid.get(value, [id])), f)
                seen[value] = hit
        else:
            hit = flags_ok(message_flags(snap, [id]), f)
        if hit:
            kept.add(id)
    return kept


# Candidates


class _Sources:
    """Cached #source parses per distinct value bytes."""

    def __init__(self, snap: Snapshot) -> None:
        self.index = snap.indexes.get(SOURCE)
        self.cache: dict[bytes, SourceRef | None] = {}

    def ref(self, id: int) -> SourceRef | None:
        value = self.index.last(id) if self.index is not None else None
        if value is None:
            return None
        if value not in self.cache:
            self.cache[value] = parse_source(value)
        return self.cache[value]


def _location_patterns(f: Filters) -> tuple[list[re.Pattern[str]], list[re.Pattern[str]]]:
    flags = _flags(f)
    return ([re.compile(p, flags) for p in f.account],
            [re.compile(p, flags) for p in f.mailbox])


def _location_ok(ref: SourceRef, accounts: list[re.Pattern[str]],
                 mailboxes: list[re.Pattern[str]]) -> bool:
    if accounts and not any(p.search(account_display(ref)) for p in accounts):
        return False
    if mailboxes and not any(p.search(ref.mailbox) for p in mailboxes):
        return False
    return True


def _matching(snap: Snapshot, name: str, pattern, literal: bytes | None,
              cands: set[int]) -> set[int]:
    """The ids of cands with a current value in index name that pattern matches.

    While cands is still the whole live set the index is searched once per
    distinct range (decision D7); otherwise each candidate's values are
    matched, with results cached per value bytes.
    """
    index: Index | None = snap.indexes.get(name)
    if index is None or not cands:
        return set()
    if len(cands) == len(snap.live):
        return index.search(pattern, literal) & cands
    seen: dict[bytes, bool] = {}
    out: set[int] = set()
    for id in cands:
        values = index.values(id)
        if not values:
            continue
        for value in values:
            hit = seen.get(value)
            if hit is None:
                hit = bool(pattern.search(value.decode("utf-8", "replace")))
                seen[value] = hit
            if hit:
                out.add(id)
                break
    return out


def _address_step(snap: Snapshot, specs: list[AddrSpec], names: tuple[str, ...],
                  flags: int, cands: set[int]) -> set[int]:
    found: set[int] = set()
    tokens = [s for s in specs if s.kind != "regex"]
    regexes = [re.compile(s.value, flags) for s in specs if s.kind == "regex"]
    for name in names:
        if tokens:
            found |= _matching(snap, name, _TokenMatcher(tokens), _AT, cands)
        for p in regexes:
            found |= _matching(snap, name, p, required_literal(p.pattern, p.flags), cands)
    return cands & found


def _pattern_step(snap: Snapshot, name: str, patterns: list[str], flags: int,
                  cands: set[int]) -> set[int]:
    found: set[int] = set()
    for text in patterns:
        p = re.compile(text, flags)
        found |= _matching(snap, name, p, required_literal(p.pattern, p.flags), cands)
    return cands & found


def candidates(snap: Snapshot, f: Filters) -> CandidateSet:
    """Live message ids that pass every filter on the index (cheapest first).

    stats counts the candidates after each step; unmapped_parts counts body
    record ids that did not map to a live message.
    """
    flags = _flags(f)
    cands = set(snap.live)
    stats = {"live": len(cands)}

    if f.account or f.mailbox:
        accounts, mailboxes = _location_patterns(f)
        sources = _Sources(snap)
        kept = set()
        for id in cands:
            ref = sources.ref(id)
            if ref is not None and _location_ok(ref, accounts, mailboxes):
                kept.add(id)
        cands = kept
    stats["after_location"] = len(cands)

    if f.since is not None or f.until is not None:
        kept = set()
        for id in cands:
            dt = effective_index_date(snap, id)[0]
            if dt is None:
                continue
            if f.since is not None and dt < f.since:
                continue
            if f.until is not None and dt >= f.until:
                continue
            kept.add(id)
        cands = kept
    stats["after_dates"] = len(cands)

    if f.flag or f.no_flag:
        cands = _flag_step(snap, f, cands)
    stats["after_flags"] = len(cands)

    for attr, names in ADDRESS_FIELDS.items():
        specs = getattr(f, attr)
        if specs:
            cands = _address_step(snap, specs, names, flags, cands)
    if f.subject:
        cands = _pattern_step(snap, "subject", f.subject, flags, cands)
    for name, patterns in f.headers.items():
        if patterns:
            cands = _pattern_step(snap, name, patterns, flags, cands)
    stats["after_headers"] = len(cands)

    unmapped = 0
    if f.body is not None:
        p = re.compile(f.body, re.MULTILINE | flags)
        literal = required_literal(p.pattern, p.flags)
        parts: set[int] = set()
        if cands:
            names = (UNQUOTED, QUOTED) if f.include_quoted else (UNQUOTED,)
            for name in names:
                index = snap.indexes.get(name)
                if index is not None:
                    parts |= index.search(p, literal)
        messages, unmapped = message_of_part(snap, parts)
        cands &= messages
    stats["after_body"] = len(cands)
    stats["unmapped_parts"] = unmapped
    return CandidateSet(ids=cands, stats=stats)


# Grouping


def _decode(value: bytes) -> str:
    return value.decode("utf-8", "replace")


def group_copies(snap: Snapshot, ids: Iterable[int], f: Filters,
                 messages_dir: Path) -> GroupResult:
    """Group ids by their last Message-ID and list the qualifying copies.

    Each group expands to every live id with the same Message-ID (one
    reverse map per call). A copy qualifies when its #source maps, its
    .eml under messages_dir exists and it passes --account and --mailbox.
    Ids without a Message-ID (or with an empty one) stand alone. Groups
    without a qualifying copy are dropped. missing_eml counts each
    considered live id once whose #source is absent or unmapped or whose
    .eml does not exist (ruling RP3, rule R10). A group's flags are the
    union over all its live ids, sorted by lowercased token.
    """
    mid_index = snap.indexes.get(MESSAGE_ID)
    by_mid = mid_members(snap)
    has_flags = FLAGS in snap.indexes
    accounts, mailboxes = _location_patterns(f)
    sources = _Sources(snap)
    considered: set[int] = set()
    done_mids: set[bytes] = set()
    missing = 0
    groups: list[Group] = []
    for id in sorted(ids):
        if id in considered:
            continue
        value = mid_index.last(id) if mid_index is not None else None
        if value:
            if value in done_mids:
                continue
            done_mids.add(value)
            members = sorted(set(by_mid.get(value, [])) | {id})
            message_id: str | None = _decode(value)
        else:
            members = [id]
            message_id = None
        copies: list[Copy] = []
        for member in members:
            if member in considered:
                continue
            considered.add(member)
            ref = sources.ref(member)
            if ref is None:
                missing += 1
                continue
            path = str(eml_path(messages_dir, ref, member))
            if not os.path.exists(path):
                missing += 1
                continue
            if not _location_ok(ref, accounts, mailboxes):
                continue
            dt, source = effective_index_date(snap, member)
            copies.append(Copy(member, path, account_display(ref), ref.mailbox,
                               dt, source))
        if copies:
            copies.sort(key=lambda c: c.path)
            flags = None
            if has_flags:
                found = message_flags(snap, members)
                flags = [found[k] for k in sorted(found)]
            groups.append(Group(message_id, copies, flags))
    groups.sort(key=lambda g: g.copies[0].path)
    return GroupResult(groups, missing)


def index_headers(snap: Snapshot, id: int) -> dict[str, str]:
    """The index values of date, from, to, cc, subject and message-id.

    Several values join with ", "; a missing value or index is "". The
    date key reads the raw `date` index only when the snapshot holds it.
    """
    out: dict[str, str] = {}
    for key in HEADER_KEYS:
        index = snap.indexes.get(key)
        values = index.values(id) if index is not None else None
        out[key] = ", ".join(_decode(v) for v in values) if values else ""
    return out
