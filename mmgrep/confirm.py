"""Confirmation of index hits against the `.eml` files.

Design section 8 step 4 with the milestone 3 plan decisions D1 to D5 and
D11 and rulings C1 to C4: Each group tries its qualifying copies in order
until one passes every filter. A copy's Message-ID header must equal the
indexed one; address, subject, header and date filters are rechecked on a
header-only parse; the body is matched on the decoded text parts (rule R5)
only when no header check failed. Every check is three-valued: True, False
or None (undecidable). Files are opened with open(path, "rb") only, one at
a time, and closed before parsing. confirm_all and run_pool spread the
work over a bounded spawn pool.
"""

from __future__ import annotations

import multiprocessing
import re
import signal
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email import errors, policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Callable

from mmgrep.query import Copy, Filters, Group, addr_matches

CONFIRMED = "confirmed"
REJECTED = "rejected"
INDETERMINATE = "indeterminate"
HEADER_KEYS = ("date", "from", "to", "cc", "subject", "message-id")
ADDRESS_FIELDS = (
    ("from_", ("from",)),
    ("to", ("to",)),
    ("cc", ("cc",)),
    ("any_address", ("from", "to", "cc", "bcc")),
)

# Decision D11: Header-only reads stop at the first blank line.
CHUNK = 64 * 1024
HEADER_CAP = 1024 * 1024
POOL_THRESHOLD = 200
MAX_WORKERS = 4

# Decision D2: Exceptions that make one check undecidable.
_UNDECIDABLE = (LookupError, UnicodeError, errors.MessageError, ValueError,
                TypeError, IndexError)
_FOLD = re.compile(r"\r?\n(?=[ \t])")
_ENCODED_WORD = re.compile(r"=\?[^?\s]+\?[BbQq]\?[^?\s]*\?=")
_QUOTED_LINE = re.compile(r"[ \t]*>")
_QUOTE_MARKERS = re.compile(r"(?m)^[ \t]*>(?:[ \t]*>)*[ \t]?")
_BLOCKS = frozenset("p div li tr td th h1 h2 h3 h4 h5 h6 blockquote table "
                    "ul ol pre hr".split())
_HIDDEN = frozenset({"script", "style"})
_TEXT_TYPES = frozenset({"text/plain", "text/html"})


@dataclass
class Confirmation:
    """The outcome for one group.

    status is "confirmed", "rejected" or "indeterminate". copy, headers
    (decoded date, from, to, cc, subject and message-id) and date (the
    copy's aware effective date or None) describe the confirming copy;
    they are None, empty and None for any other status (rulings RP4, C3).
    """

    status: str
    copy: Copy | None
    headers: dict[str, str]
    date: datetime | None


class _HeaderTooLong(Exception):
    """No header separator within HEADER_CAP bytes (ruling C4)."""


# Reading


def _read_head(path: str) -> bytes:
    """The bytes up to and including the first blank line (decision D11)."""
    buf = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            start = max(0, len(buf) - 3)
            buf += chunk
            ends = [i for i in (buf.find(b"\n\n", start), buf.find(b"\r\n\r\n", start))
                    if i >= 0]
            if ends:
                end = min(ends)
                return buf[:end + (2 if buf[end] == 0x0A else 4)]
            if not chunk:
                return buf
            if len(buf) >= HEADER_CAP:
                raise _HeaderTooLong(path)


def read_message(path: str, body: bool) -> Message:
    """Parse one `.eml`: The whole file when body, else its header block.

    The file is closed before parsing. OSError propagates; a header block
    longer than HEADER_CAP raises an email.errors.MessageError subclass.
    """
    if body:
        with open(path, "rb") as fh:
            data = fh.read()
        return BytesParser(policy=policy.default).parsebytes(data)
    try:
        head = _read_head(path)
    except _HeaderTooLong:
        raise errors.HeaderParseError(f"no header end within {HEADER_CAP} bytes") from None
    return BytesParser(policy=policy.default).parsebytes(head, headersonly=True)


# Header values (decision D1)


def _utf8(raw: str) -> tuple[str, bool]:
    """Raw header text as UTF-8 and whether the decode was lossy."""
    try:
        data = raw.encode("ascii", "surrogateescape")
    except UnicodeEncodeError:
        return raw, False
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace"), True


def _decode_words(text: str) -> str:
    """RFC 2047 decoded text; LookupError or UnicodeError when undecodable."""
    if "=?" not in text:
        return text
    try:
        return str(make_header(decode_header(text)))
    except UnicodeDecodeError:
        if text.isascii():
            raise
    # Raw 8-bit text next to encoded words: Decode each word on its own.
    out: list[str] = []
    pos = 0
    for m in _ENCODED_WORD.finditer(text):
        gap = text[pos:m.start()]
        if not (out and pos and gap.strip() == ""):
            out.append(gap)
        for data, charset in decode_header(m.group()):
            if isinstance(data, str):
                out.append(data)
            else:
                out.append(data.decode(charset or "us-ascii"))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


class _Headers:
    """The unfolded raw header values of one message by lowercased name."""

    def __init__(self, msg: Message) -> None:
        self.msg = msg
        self.raw: dict[str, list[tuple[str, bool]]] = {}
        for name, value in msg.raw_items():
            text = _FOLD.sub("", str(value)).strip()
            self.raw.setdefault(name.lower(), []).append(_utf8(text))
        self.global_defect = any(
            isinstance(d, (errors.MissingHeaderBodySeparatorDefect, errors.HeaderDefect))
            for d in msg.defects)

    def values(self, name: str) -> list[tuple[str, bool]]:
        return self.raw.get(name, [])

    def defective(self, name: str) -> bool:
        """True when a defect may hide or change header name (ruling C4).

        UndecodableBytesDefect is ignored: policy.default attaches it to
        any header with raw 8-bit bytes (a raw UTF-8 display name, for
        one), and the D1 lossy flag already covers bytes that really do
        not decode.
        """
        if self.global_defect:
            return True
        try:
            return any(not isinstance(d, errors.UndecodableBytesDefect)
                       for h in self.msg.get_all(name, [])
                       for d in getattr(h, "defects", ()))
        except _UNDECIDABLE:
            return True

    def decoded(self, name: str) -> list[str]:
        """Decoded values of name, undecodable ones as the UTF-8 raw text."""
        out = []
        for text, _ in self.values(name):
            try:
                out.append(_decode_words(text))
            except _UNDECIDABLE:
                out.append(text)
        return out


def _verdict(hit: bool, lossy: bool, defective: bool) -> bool | None:
    if hit:
        return True
    return None if lossy or defective else False


def _check_patterns(h: _Headers, name: str, patterns: list[re.Pattern[str]]) -> bool | None:
    lossy = False
    try:
        for text, was_lossy in h.values(name):
            lossy = lossy or was_lossy
            value = _decode_words(text)
            if any(p.search(value) for p in patterns):
                return True
    except _UNDECIDABLE:
        return None
    return _verdict(False, lossy, h.defective(name))


def _check_addresses(h: _Headers, names: tuple[str, ...], specs: list,
                     flags: int) -> bool | None:
    lossy = False
    defective = False
    undecidable = False
    plain = [s for s in specs if s.kind != "regex"]
    regexes = [re.compile(s.value, flags) for s in specs if s.kind == "regex"]
    for name in names:
        values = h.values(name)
        defective = defective or h.defective(name)
        for text, was_lossy in values:
            lossy = lossy or was_lossy
            try:
                if plain:
                    for _, addr in getaddresses([text]):
                        if addr and any(addr_matches(s, addr.lower(), flags) for s in plain):
                            return True
                if regexes:
                    value = _decode_words(text)
                    if any(p.search(value) for p in regexes):
                        return True
            except _UNDECIDABLE:
                undecidable = True
    if undecidable:
        return None
    return _verdict(False, lossy, defective)


def _file_date(h: _Headers) -> tuple[datetime | None, bool]:
    """The aware instant of the first Date header and whether one was usable."""
    values = h.values("date")
    if not values:
        return None, False
    text = values[0][0]
    try:
        dt = parsedate_to_datetime(text)
    except _UNDECIDABLE:
        return None, False
    if dt is None:
        return None, False
    if dt.tzinfo is None:
        if text.rstrip().endswith("-0000"):
            # parsedate_to_datetime leaves -0000 naive; RFC 5322 means UTC.
            return dt.replace(tzinfo=timezone.utc), True
        return None, False
    return dt, True


def _in_range(dt: datetime, f: Filters) -> bool:
    if f.since is not None and dt < f.since:
        return False
    if f.until is not None and dt >= f.until:
        return False
    return True


# Body text (decisions D3 and D4)


class _HTMLText(HTMLParser):
    """Two text variants: Block boundaries as "\\n" (a) and as "" (b)."""

    def __init__(self, strip_quoted: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.strip_quoted = strip_quoted
        self.a: list[str] = []
        self.b: list[str] = []
        self.hidden = 0
        self.quote = 0

    def _shown(self) -> bool:
        return self.hidden == 0 and not (self.strip_quoted and self.quote)

    def _boundary(self, tag: str) -> None:
        if not self._shown():
            return
        if tag == "br":
            self.a.append("\n")
            self.b.append("\n")
        elif tag in _BLOCKS:
            self.a.append("\n")

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _HIDDEN:
            self.hidden += 1
            return
        self._boundary(tag)
        if tag == "blockquote":
            self.quote += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN:
            self.hidden = max(0, self.hidden - 1)
            return
        if tag == "blockquote":
            self.quote = max(0, self.quote - 1)
        if tag != "br":
            self._boundary(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag not in _HIDDEN:
            self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._shown():
            self.a.append(data)
            self.b.append(data)


def _html_variants(text: str, strip_quoted: bool) -> list[str]:
    parser = _HTMLText(strip_quoted)
    parser.feed(text)
    parser.close()
    a, b = "".join(parser.a), "".join(parser.b)
    return [a] if a == b else [a, b]


def _strip_quoted_lines(text: str) -> str:
    return "\n".join(line for line in text.split("\n") if not _QUOTED_LINE.match(line))


_STRUCTURAL_DEFECTS = (errors.StartBoundaryNotFoundDefect,
                       errors.CloseBoundaryNotFoundDefect,
                       errors.MultipartInvariantViolationDefect,
                       errors.NoBoundaryInMultipartDefect)


def body_texts(msg: Message, strip_quoted: bool) -> tuple[list[str], bool]:
    """The decoded text of every text/plain and text/html leaf.

    Attachments and embedded messages count. HTML gives two variants
    (decision D3). With strip_quoted, plain lines starting with ">" and
    HTML blockquote content are dropped (decision D4). CRLF becomes LF in
    every text (ruling C2). The flag is True when any decode was lossy or
    any leaf could not be decoded at all (unknown charset).
    """
    texts: list[str] = []
    lossy = False
    for part in msg.walk():
        # Broken MIME structure (a missing or unclosed boundary) can hide
        # text parts, so a miss on such a message is undecidable.
        if any(isinstance(d, _STRUCTURAL_DEFECTS) for d in part.defects):
            lossy = True
        if part.is_multipart() or part.get_content_type() not in _TEXT_TYPES:
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "us-ascii"
            try:
                text = payload.decode(charset)
            except UnicodeDecodeError:
                text = payload.decode(charset, "replace")
                lossy = True
            text = text.replace("\r\n", "\n")
            if part.get_content_type() == "text/html":
                texts.extend(_html_variants(text, strip_quoted))
            else:
                texts.append(_strip_quoted_lines(text) if strip_quoted else text)
        except Exception:
            # Any decode or HTML parser failure (NotImplementedError from
            # HTMLParser on 3.9, for one) leaves this leaf undecidable.
            lossy = True
    return texts, lossy


def _check_body(msg: Message, f: Filters, strip_quoted: bool) -> bool | None:
    flags = re.MULTILINE | (0 if f.case_sensitive else re.IGNORECASE)
    pattern = re.compile(f.body, flags)
    try:
        texts, lossy = body_texts(msg, strip_quoted)
    except Exception:
        return None  # RecursionError from a deeply nested part tree, for one
    variants = list(texts)
    if f.include_quoted:
        variants += [_QUOTE_MARKERS.sub("", t) for t in texts]
    return _verdict(any(pattern.search(t) for t in variants), lossy, False)


# Checks


def check_message(msg: Message, f: Filters, message_id: str | None,
                  strip_quoted: bool, index_date: datetime | None = None,
                  date_source: str | None = None) -> tuple[str, datetime | None]:
    """The status of one parsed message and its effective date.

    message_id None skips the Message-ID check. With date_source
    "received" the date filter uses index_date; otherwise it uses the
    file Date (aware only), which is None (undecidable) when the index
    took it from "date" and False when there is no index date at all
    (decision D5, ruling C3). The date returned is index_date when given,
    else the aware file Date or None. Any Exception raised while checking
    (RecursionError, AttributeError from a mutated header and the like)
    makes the message indeterminate; KeyboardInterrupt propagates.
    """
    try:
        return _check_message(msg, f, message_id, strip_quoted, index_date,
                              date_source)
    except Exception:
        return INDETERMINATE, index_date


def _check_message(msg: Message, f: Filters, message_id: str | None,
                   strip_quoted: bool, index_date: datetime | None,
                   date_source: str | None) -> tuple[str, datetime | None]:
    h = _Headers(msg)
    flags = 0 if f.case_sensitive else re.IGNORECASE
    checks: list[bool | None] = []

    if message_id is not None:
        values = h.values("message-id")
        hit = bool(values) and values[-1][0] == message_id
        checks.append(_verdict(hit, False, h.defective("message-id")))

    file_date, has_file_date = _file_date(h)
    if f.since is not None or f.until is not None:
        if date_source == "received" and index_date is not None:
            checks.append(_in_range(index_date, f))
        elif has_file_date:
            checks.append(_in_range(file_date, f))
        elif date_source == "date":
            checks.append(None)
        else:
            checks.append(_verdict(False, False, h.defective("date")))

    for attr, names in ADDRESS_FIELDS:
        specs = getattr(f, attr)
        if specs:
            checks.append(_check_addresses(h, names, specs, flags))
    if f.subject:
        checks.append(_check_patterns(h, "subject", [re.compile(p, flags) for p in f.subject]))
    for name, patterns in f.headers.items():
        if patterns:
            checks.append(_check_patterns(h, name, [re.compile(p, flags) for p in patterns]))

    if f.body is not None and False not in checks:
        checks.append(_check_body(msg, f, strip_quoted))

    date = index_date if index_date is not None else file_date
    if False in checks:
        return REJECTED, date
    if None in checks:
        return INDETERMINATE, date
    return CONFIRMED, date


def decoded_headers(msg: Message) -> dict[str, str]:
    """The date, from, to, cc, subject and message-id values of msg.

    Several values join with ", " and a missing header is "". Date and
    Message-ID are the unfolded raw text; the others are RFC 2047 decoded
    (decision D1), falling back to the raw UTF-8 text when undecodable.
    """
    h = _Headers(msg)
    out: dict[str, str] = {}
    for key in HEADER_KEYS:
        if key in ("date", "message-id"):
            values = [text for text, _ in h.values(key)]
        else:
            values = h.decoded(key)
        out[key] = ", ".join(values)
    return out


def _check_copy(copy: Copy, f: Filters, message_id: str | None) -> tuple[str, Confirmation | None]:
    if (f.since is not None or f.until is not None) and copy.date_source is None:
        return REJECTED, None  # Decision D5: No index date fails a date filter.
    try:
        msg = read_message(copy.path, body=f.body is not None)
        status, date = check_message(msg, f, message_id, False,
                                     copy.index_date, copy.date_source)
        if status != CONFIRMED:
            return status, None
        return status, Confirmation(CONFIRMED, copy, decoded_headers(msg), date)
    except Exception:
        # OSError (ruling C4), parser errors and anything else the email
        # package raises on hostile input, RecursionError included. Never
        # BaseException, so KeyboardInterrupt still reaches main.
        return INDETERMINATE, None


def confirm(group: Group, f: Filters) -> Confirmation:
    """Try the copies of group in order until one confirms.

    The group is indeterminate when no copy confirms and at least one copy
    was indeterminate (an OSError included, ruling C4); else rejected.
    """
    indeterminate = False
    for copy in group.copies:
        status, result = _check_copy(copy, f, group.message_id)
        if result is not None:
            return result
        if status == INDETERMINATE:
            indeterminate = True
    return Confirmation(INDETERMINATE if indeterminate else REJECTED, None, {}, None)


# Pool (ruling C1)


def _ignore_sigint() -> None:
    """Pool worker initializer: Only the parent handles Ctrl-C."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _confirm_item(item: tuple[Group, Filters]) -> Confirmation:
    return confirm(item[0], item[1])


def _stop(executor: ProcessPoolExecutor) -> None:
    """Cancel pending work and terminate the workers (ruling C1).

    The worker list is taken before shutdown, which sets
    executor._processes to None on Python 3.9 and 3.14.
    """
    procs = list((getattr(executor, "_processes", None) or {}).values())
    executor.shutdown(wait=False, cancel_futures=True)
    for proc in procs:
        proc.terminate()
    for proc in procs:
        proc.join(timeout=1)


def run_pool(fn: Callable, items: list, jobs: int,
             threshold: int = POOL_THRESHOLD) -> list:
    """fn over items in order; a spawn pool of min(jobs, 4) when it pays.

    Sequential when jobs is 1 or less or there are fewer than threshold
    items. fn must be a module-level function. KeyboardInterrupt stops the
    pool and is re-raised for main.
    """
    if jobs <= 1 or len(items) < threshold:
        return [fn(item) for item in items]
    executor = ProcessPoolExecutor(
        max_workers=min(jobs, MAX_WORKERS),
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_ignore_sigint)
    try:
        results = list(executor.map(fn, items, chunksize=16))
    except BaseException:
        _stop(executor)
        raise
    executor.shutdown(wait=True)
    return results


def confirm_all(groups: list[Group], f: Filters, jobs: int) -> list[Confirmation]:
    """confirm for every group, in input order (run_pool rules)."""
    return run_pool(_confirm_item, [(g, f) for g in groups], jobs)
