"""Synthetic MailMate store writer for tests.

Writes a `Database.noindex` and `Messages.noindex` tree that follows
docs/FORMAT.md byte for byte: OpenStep ASCII plists, `<III` records, values
followed by `\\n`, the `(id, 1, 0)` clear marker, `Sets/*.ids` arrays, body
records keyed on part ids and `.eml` files at the section 6 path.

All data is synthetic. Use RFC 2606 domains only.
"""

from __future__ import annotations

import base64
import codecs
import datetime
import email.utils
import html
import os
import pathlib
import re
import struct
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple, Union

Value = Union[str, bytes]

UUID = "00000000-0000-4000-8000-000000000001"
REVISION = "1"
VERSION = "1.1"
MARKER = (1, 0)
SET_NAMES = ("Msgs.ids", "NewMsgs.ids", "Parts.ids", "NewParts.ids")
BODY_INDEXES = ("#unquoted", "#quoted")

# RFC header name to index name. Any other header indexes under its
# lowercased name (FORMAT.md section 1).
INDEX_NAMES: Dict[str, str] = {
    "From": "from",
    "To": "to",
    "Cc": "cc",
    "Bcc": "bcc",
    "Subject": "subject",
    "Message-ID": "message-id",
    "Date": "date",
    "In-Reply-To": "in-reply-to",
    "References": "references",
}

# Indexes stored as written (unfolded and stripped, no RFC 2047 decoding).
_AS_WRITTEN = frozenset({"message-id", "date"})


@dataclass
class FixturePart:
    """One MIME body part. Containers have a multipart content type."""

    content_type: str = "text/plain"
    body: List[Value] = field(default_factory=list)    # #unquoted segments
    quoted: List[Value] = field(default_factory=list)  # #quoted segments
    parts: List["FixturePart"] = field(default_factory=list)  # children
    id: Optional[int] = None         # explicit part id; None takes the next id
    content: Optional[Value] = None  # .eml payload; None builds it from segments
    filename: Optional[str] = None   # attachment file name
    charset: str = "utf-8"


@dataclass
class FixtureMessage:
    """One message of the synthetic store.

    `headers` maps an RFC header name to its raw value (a list gives one
    header line and one index record per item). `index` maps an index name
    to a value that replaces the derived one (a list gives several records;
    None suppresses the index). `stale` maps an index name to an older value
    (or a list of them), each written as a value record and a clear marker
    before the current records. With `parts` the message is multipart and
    its segments live on part ids; without it they live on the message id.
    `new` puts a live id in `NewMsgs.ids` instead of `Msgs.ids`. `orphan`
    gives an id with header records only: No `#source` record, no `.eml`
    and in no Sets file. `fresh` names the indexes where this message's
    values get a new cache range instead of reusing an identical one.
    `raw` maps an index name to `(start, end)` pairs written as records of
    this id after its other records in that index, unchecked (malformed
    records inside the committed range).
    """

    id: int
    account: str
    mailbox: str
    headers: Dict[str, Union[str, List[str]]] = field(default_factory=dict)
    body: List[Value] = field(default_factory=list)
    quoted: List[Value] = field(default_factory=list)
    parts: List[FixturePart] = field(default_factory=list)
    multipart: str = "mixed"
    index: Dict[str, Union[Value, List[Value], None]] = field(default_factory=dict)
    stale: Dict[str, Union[Value, List[Value]]] = field(default_factory=dict)
    duplicate_of: Optional[int] = None
    deleted: bool = False
    write_eml: bool = True
    new: bool = False
    charset: str = "utf-8"
    orphan: bool = False
    fresh: Set[str] = field(default_factory=set)
    content_type: str = "text/plain"  # root leaf of a single-part message
    raw: Dict[str, List[Tuple[int, int]]] = field(default_factory=dict)

    Part = FixturePart


# Append events (append_store)


@dataclass
class Arrival:
    """A new message, written like one `build_store` message."""

    message: FixtureMessage


@dataclass
class Move:
    """A marker and the new `#source` value; the `.eml` moves too."""

    id: int
    account: str
    mailbox: str


@dataclass
class Delete:
    """A marker in every message-keyed header index holding a value."""

    id: int


@dataclass
class Rewrite:
    """A marker and new values in one index; None leaves only the marker.

    The `.eml` is not changed.
    """

    id: int
    index: str
    values: Union[Value, List[Value], None]


# Value forms


def index_name(header: str) -> str:
    return INDEX_NAMES.get(header, header.lower())


_ENCODED_WORD = re.compile(r"=\?([^?\s]+)\?([BbQq])\?([^?\s]*)\?=")


def _decode_word(charset: str, enc: str, text: str) -> str:
    charset = charset.split("*", 1)[0]
    try:
        codecs.lookup(charset)
    except LookupError:
        raise ValueError(f"unknown charset in encoded word: {charset!r}") from None
    if enc in "Bb":
        raw = base64.b64decode(text + "=" * (-len(text) % 4))
    else:
        raw = re.sub(rb"=([0-9A-Fa-f]{2})",
                     lambda m: bytes([int(m.group(1), 16)]),
                     text.replace("_", " ").encode("ascii"))
    return raw.decode(charset, errors="replace")


def decode_rfc2047(s: str) -> str:
    """Decode encoded words; whitespace between two adjacent ones is dropped."""
    out: List[str] = []
    pos = 0
    prev_word = False
    for m in _ENCODED_WORD.finditer(s):
        gap = s[pos:m.start()]
        if not (prev_word and gap.strip() == ""):
            out.append(gap)
        out.append(_decode_word(m.group(1), m.group(2), m.group(3)))
        pos = m.end()
        prev_word = True
    out.append(s[pos:])
    return "".join(out)


def _unfold(raw: str) -> str:
    return re.sub(r"\r?\n(?=[ \t])", "", raw)


def index_value(index_name: str, raw: str) -> bytes:
    """Stored bytes of a header index value (FORMAT.md section 4)."""
    text = _unfold(raw)
    if index_name not in _AS_WRITTEN:
        text = decode_rfc2047(text)
    return text.strip().encode("utf-8")


def date_values(raw: str) -> Dict[str, bytes]:
    """`#date`, `#date-sent` and `#date-received` from a Date header.

    `#date-received` defaults to the `#date-sent` form. The zone `-0000`
    counts as `+0000`.
    """
    text = _unfold(raw).strip()
    try:
        dt = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return {}
    if dt is not None and dt.tzinfo is None and text.endswith("-0000"):
        # parsedate_to_datetime returns a naive datetime for -0000.
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    if dt is None or dt.tzinfo is None:
        # A Date with no zone at all gets no derived values. FORMAT.md does
        # not give the stored form for that case; use `index` to set one.
        return {}
    utc = dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "#date": dt.strftime("%Y-%m-%d %H:%M:%S %z").encode("ascii"),
        "#date-sent": utc.encode("ascii"),
        "#date-received": utc.encode("ascii"),
    }


def source_url(account: str, mailbox: str) -> str:
    """The `#source` value (FORMAT.md section 6)."""
    return f"imap://{account}/{mailbox}"


def eml_path(root: pathlib.Path, account: str, mailbox: str, id: int) -> pathlib.Path:
    """The `.eml` path for a message (FORMAT.md section 6, rule R7)."""
    path = pathlib.Path(root) / "Messages.noindex" / "IMAP" / account
    for segment in mailbox.split("/"):
        path = path / (segment + ".mailbox")
    return path / "Messages" / f"{id}.eml"


def _as_bytes(v: Value) -> bytes:
    return v if isinstance(v, bytes) else v.encode("utf-8")


def _as_list(v) -> list:
    return list(v) if isinstance(v, list) else [v]


def _lc_twin(segment: Value) -> bytes:
    """The `#lc` value: lower(), a leading U+FEFF dropped, empty if invalid."""
    if isinstance(segment, bytes):
        try:
            segment = segment.decode("utf-8")
        except UnicodeDecodeError:
            return b""
    if segment.startswith("\ufeff"):
        segment = segment[1:]
    return segment.lower().encode("utf-8")


# Store writer


class _Index:
    def __init__(self) -> None:
        self.cache = bytearray()
        self.offsets = bytearray()
        self._ranges: Dict[bytes, tuple] = {}

    def value(self, msg_id: int, data: bytes, fresh: bool = False) -> None:
        if fresh or data not in self._ranges:
            start = len(self.cache)
            self.cache += data + b"\n"
            self._ranges.setdefault(data, (start, start + len(data)))
            end = start + len(data)
        else:
            start, end = self._ranges[data]
        self.offsets += struct.pack("<III", msg_id, start, end)

    def marker(self, msg_id: int) -> None:
        self.offsets += struct.pack("<III", msg_id, *MARKER)

    def raw(self, msg_id: int, start: int, end: int) -> None:
        self.offsets += struct.pack("<III", msg_id, start, end)

    @classmethod
    def load(cls, cache: bytes, offsets: bytes) -> "_Index":
        """An index holding existing bytes; identical values reuse old ranges."""
        index = cls()
        index.cache += cache
        index.offsets += offsets
        for _, start, end in struct.iter_unpack("<III", offsets):
            if start <= end <= len(cache):
                index._ranges.setdefault(cache[start:end], (start, end))
        return index

    def current(self, msg_id: int) -> List[bytes]:
        """Values of msg_id after its last marker, in file order.

        All or nothing per id (design 6a): If any record after the last
        marker is malformed, the id has no value and the result is empty;
        an earlier value is never exposed. A `start == end` record is the
        empty value `b""`.
        """
        values: List[bytes] = []
        valid = True
        for rid, start, end in struct.iter_unpack("<III", bytes(self.offsets)):
            if rid != msg_id:
                continue
            if (start, end) == MARKER:
                values = []
                valid = True
            elif start <= end <= len(self.cache):
                values.append(bytes(self.cache[start:end]))
            else:
                valid = False
        return values if valid else []


def _strip_ids(part: FixturePart) -> FixturePart:
    return FixturePart(content_type=part.content_type, body=list(part.body),
                       quoted=list(part.quoted),
                       parts=[_strip_ids(p) for p in part.parts], id=None,
                       content=part.content, filename=part.filename,
                       charset=part.charset)


def _resolve(msg: FixtureMessage, by_id: Dict[int, FixtureMessage],
             seen: frozenset = frozenset()) -> FixtureMessage:
    """Copy headers, body, quoted and parts from `duplicate_of` where empty.

    A chain of duplicates resolves transitively; a cycle raises ValueError.
    """
    if msg.duplicate_of is None:
        return msg
    if msg.id in seen:
        raise ValueError(f"duplicate_of cycle at message {msg.id}")
    src = _resolve(by_id[msg.duplicate_of], by_id, seen | {msg.id})
    return FixtureMessage(
        id=msg.id, account=msg.account, mailbox=msg.mailbox,
        headers=msg.headers or dict(src.headers),
        body=msg.body or list(src.body), quoted=msg.quoted or list(src.quoted),
        parts=msg.parts or [_strip_ids(p) for p in src.parts],
        multipart=msg.multipart if msg.parts else src.multipart,
        index=msg.index, stale=msg.stale, duplicate_of=None,
        deleted=msg.deleted, write_eml=msg.write_eml, new=msg.new,
        charset=msg.charset if msg.body else src.charset,
        content_type=msg.content_type if msg.body else src.content_type,
        orphan=msg.orphan, fresh=set(msg.fresh), raw=dict(msg.raw))


def _layout(msg: FixtureMessage) -> list:
    """Depth-first (part_id, parent_id, subpart_number, part) tuples."""
    out: list = []
    counter = [msg.id + 1]

    def walk(parts: List[FixturePart], parent: int) -> None:
        for number, part in enumerate(parts):
            pid = part.id if part.id is not None else counter[0]
            counter[0] = pid + 1
            container = part.content_type.startswith("multipart/")
            if container and (part.body or part.quoted):
                raise ValueError(f"container part {pid} has segments")
            if part.parts and not container:
                raise ValueError(f"leaf part {pid} has children")
            out.append((pid, parent, number, part))
            walk(part.parts, pid)

    if msg.parts and (msg.body or msg.quoted):
        raise ValueError(f"message {msg.id} has both parts and segments")
    walk(msg.parts, msg.id)
    return out


def _header_values(msg: FixtureMessage) -> Dict[str, Optional[List[bytes]]]:
    values: Dict[str, Optional[List[bytes]]] = {}
    for name, raw in msg.headers.items():
        idx = index_name(name)
        raws = _as_list(raw)
        values[idx] = [index_value(idx, r) for r in raws]
        if idx == "date" and raws:
            for k, v in date_values(raws[0]).items():
                values[k] = [v]
    if not msg.orphan:
        values["#source"] = [source_url(msg.account, msg.mailbox).encode("utf-8")]
    for idx, override in msg.index.items():
        if override is None:
            values[idx] = None
        else:
            values[idx] = [_as_bytes(v) for v in _as_list(override)]
    return values


def _encode(v: Value, charset: str) -> bytes:
    """Str in the declared charset; bytes stay as given."""
    return v if isinstance(v, bytes) else v.encode(charset)


def _text_payload(body: List[Value], quoted: List[Value], content_type: str,
                  charset: str) -> bytes:
    if content_type == "text/html":
        def para(seg: Value) -> bytes:
            inner = _encode(html.escape(seg) if isinstance(seg, str) else seg, charset)
            return b"<p>" + inner + b"</p>"
        text = b"<html><body>" + b"".join(para(s) for s in body)
        if quoted:
            text += b"<blockquote>" + b"".join(para(s) for s in quoted) + b"</blockquote>"
        return text + b"</body></html>\n"
    pieces = [_encode(s, charset) for s in body]
    for seg in quoted:
        lines = _encode(seg, charset).split(b"\n")
        pieces.append(b"\n".join(b"> " + line for line in lines))
    return b"\n\n".join(pieces) + b"\n"


def _crlf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _leaf_bytes(part: FixturePart) -> bytes:
    """Headers, blank line and payload of a leaf part, ending in CRLF."""
    if part.content_type.startswith("text/"):
        if part.content is not None:
            payload = _encode(part.content, part.charset)
            if not payload.endswith(b"\n"):
                payload += b"\n"
        else:
            payload = _text_payload(part.body, part.quoted, part.content_type,
                                    part.charset)
        head = (f"Content-Type: {part.content_type}; charset={part.charset}\r\n"
                "Content-Transfer-Encoding: 8bit\r\n")
        if part.filename:
            head += f'Content-Disposition: attachment; filename="{part.filename}"\r\n'
        return head.encode("ascii") + b"\r\n" + _crlf(payload)
    data = _as_bytes(part.content) if part.content is not None else b""
    encoded = base64.encodebytes(data)
    name = f'; name="{part.filename}"' if part.filename else ""
    head = f"Content-Type: {part.content_type}{name}\r\nContent-Transfer-Encoding: base64\r\n"
    if part.filename:
        head += f'Content-Disposition: attachment; filename="{part.filename}"\r\n'
    return head.encode("ascii") + b"\r\n" + _crlf(encoded)


def _multipart_bytes(content_type: str, boundary: str, children: List[bytes]) -> bytes:
    out = (f'Content-Type: {content_type}; boundary="{boundary}"\r\n\r\n').encode("ascii")
    for child in children:
        out += f"--{boundary}\r\n".encode("ascii") + child + b"\r\n"
    return out + f"--{boundary}--\r\n".encode("ascii")


def _part_bytes(part: FixturePart, pid: int, ids: Dict[int, int]) -> bytes:
    if not part.content_type.startswith("multipart/"):
        return _leaf_bytes(part)
    children = [_part_bytes(c, ids[id(c)], ids) for c in part.parts]
    return _multipart_bytes(part.content_type, f"mmgrep-{pid}", children)


def _eml_bytes(msg: FixtureMessage, layout: list) -> bytes:
    out = bytearray()
    for name, raw in msg.headers.items():
        for r in _as_list(raw):
            out += _crlf(f"{name}: {r}\r\n".encode("utf-8"))
    out += b"MIME-Version: 1.0\r\n"
    if not msg.parts:
        root = FixturePart(content_type=msg.content_type, body=msg.body,
                           quoted=msg.quoted, charset=msg.charset)
        return bytes(out + _leaf_bytes(root))
    ids = {id(part): pid for pid, _, _, part in layout}
    children = [_part_bytes(p, ids[id(p)], ids) for p in msg.parts]
    return bytes(out + _multipart_bytes(f"multipart/{msg.multipart}",
                                        f"mmgrep-{msg.id}", children))


def _header_plist(offsets_size: int, strings_size: int, quoted: bool) -> bytes:
    strings = f'"{strings_size}"' if quoted else str(strings_size)
    return (f"{{\toffsetsFileSize = {offsets_size};\n"
            f"\tstringsFileSize = {strings};\n}}").encode("ascii")


def _main_plist(uuid: str, largest: int, revision: str) -> bytes:
    return (f'{{\tUUID = "{uuid}";\n'
            f"\tlargestUsedBodyPartID = {largest};\n"
            f'\trevision = "{revision}";\n'
            f'\tversion = "{VERSION}";\n}}').encode("ascii")


def _ids_bytes(ids: Iterable[int]) -> bytes:
    return b"".join(struct.pack("<I", i) for i in sorted(set(ids)))


def _write_message(msg: FixtureMessage, layout: list, ix, root: pathlib.Path,
                   eml_newline: str) -> None:
    """Append the records of one message to its indexes and write its `.eml`.

    `ix` maps an index name to its `_Index`. Live set bookkeeping is left to
    the caller.
    """
    current = _header_values(msg)
    names = list(current) + [k for k in msg.stale if k not in current]
    for name in names:
        values = current.get(name)
        if values is None and name not in msg.stale:
            continue
        index = ix(name)
        fresh = name in msg.fresh
        for old in _as_list(msg.stale.get(name, [])):
            index.value(msg.id, _as_bytes(old), fresh)
            index.marker(msg.id)
        for v in values or []:
            index.value(msg.id, v, fresh)
        if msg.deleted:
            index.marker(msg.id)
    for name, pairs in msg.raw.items():
        for start, end in pairs:
            ix(name).raw(msg.id, start, end)

    for pid, parent, number, _ in layout:
        ix("#root-body-part").value(pid, str(msg.id).encode("ascii"))
        ix("#parent-body-part").value(pid, str(parent).encode("ascii"))
        ix("#subpart-number").value(pid, str(number).encode("ascii"))
    carriers = [(msg.id, msg.body, msg.quoted)] if not msg.parts else [
        (pid, part.body, part.quoted) for pid, _, _, part in layout]
    for carrier, body, quoted in carriers:
        for name, segments in zip(BODY_INDEXES, (body, quoted)):
            for seg in segments:
                ix(name).value(carrier, _as_bytes(seg), name in msg.fresh)
                ix(name + "#lc").value(carrier, _lc_twin(seg),
                                       name + "#lc" in msg.fresh)

    if msg.write_eml and not (msg.deleted or msg.orphan):
        path = eml_path(root, msg.account, msg.mailbox, msg.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_with_newline(_eml_bytes(msg, layout), eml_newline))


def _with_newline(data: bytes, eml_newline: str) -> bytes:
    """Real `.eml` files use LF (0 CR bytes in 45 copies); CRLF is optional."""
    if eml_newline == "\r\n":
        return data
    if eml_newline == "\n":
        return data.replace(b"\r\n", b"\n")
    raise ValueError(f"eml_newline must be LF or CRLF, not {eml_newline!r}")


def _write_sets(db: pathlib.Path, msgs: Iterable[int], new: Iterable[int],
                parts: Iterable[int], new_parts: Iterable[int]) -> None:
    sets_dir = db / "Sets"
    sets_dir.mkdir(parents=True, exist_ok=True)
    contents = dict(zip(SET_NAMES, (msgs, new, parts, new_parts)))
    for name in SET_NAMES:
        (sets_dir / name).write_bytes(_ids_bytes(contents[name]))


def build_store(
    root: pathlib.Path,
    messages: List[FixtureMessage],
    *,
    torn: Optional[Dict[str, Union[bytes, Tuple[bytes, bytes]]]] = None,
    plist_sizes: Optional[Dict[str, Tuple[Optional[int], Optional[int]]]] = None,
    quoted_strings_size: Iterable[str] = (),
    revision: str = REVISION,
    uuid: str = UUID,
    largest_used_body_part_id: Optional[int] = None,
    eml_newline: str = "\n",
) -> None:
    """Write a synthetic store under `root`.

    `torn` maps an index name to bytes appended beyond the `.plist` sizes
    (RP1): Either one `bytes` value appended to both `.cache` and `.offsets`
    or a `(cache_tail, offsets_tail)` pair. `plist_sizes` maps an index name
    to `(offsetsFileSize, stringsFileSize)` written instead of the real
    lengths (None keeps a length), so a file can be shorter than its size.
    `quoted_strings_size` names the indexes whose `stringsFileSize` is
    written in the quoted form. `largestUsedBodyPartID` defaults to the
    largest message or part id. `eml_newline` is LF by default.
    """
    root = pathlib.Path(root)
    by_id: Dict[int, FixtureMessage] = {}
    for msg in messages:
        if msg.orphan and (msg.deleted or msg.new):
            raise ValueError(f"orphan {msg.id} cannot be deleted or new")
        if msg.id in by_id:
            raise ValueError(f"duplicate message id {msg.id}")
        by_id[msg.id] = msg
    resolved = [_resolve(m, by_id) for m in messages]
    layouts = [_layout(m) for m in resolved]

    all_parts: set = set()
    for layout in layouts:
        for pid, _, _, _ in layout:
            if pid in by_id or pid in all_parts:
                raise ValueError(f"part id {pid} collides with another id")
            all_parts.add(pid)

    indexes: Dict[str, _Index] = {"#source": _Index()}

    def ix(name: str) -> _Index:
        return indexes.setdefault(name, _Index())

    msgs_ids: List[int] = []
    new_ids: List[int] = []
    parts_ids: List[int] = []
    for msg, layout in zip(resolved, layouts):
        _write_message(msg, layout, ix, root, eml_newline)
        if msg.deleted or msg.orphan:
            continue
        if msg.new:
            new_ids.append(msg.id)
        else:
            # Parts.ids holds Msgs.ids ids and their parts, not new ids.
            msgs_ids.append(msg.id)
            parts_ids.append(msg.id)
            parts_ids.extend(pid for pid, _, _, _ in layout)

    db = root / "Database.noindex"
    headers_dir = db / "Headers"
    headers_dir.mkdir(parents=True, exist_ok=True)
    tails = {name: (t, t) if isinstance(t, bytes) else t
             for name, t in (torn or {}).items()}
    sizes = plist_sizes or {}
    quoted = set(quoted_strings_size)
    for name in list(tails) + list(sizes):
        ix(name)
    for name, index in indexes.items():
        cache_tail, offsets_tail = tails.get(name, (b"", b""))
        (headers_dir / f"{name}.cache").write_bytes(bytes(index.cache) + cache_tail)
        (headers_dir / f"{name}.offsets").write_bytes(bytes(index.offsets) + offsets_tail)
        offsets_size, strings_size = sizes.get(name, (None, None))
        if offsets_size is None:
            offsets_size = len(index.offsets)
        if strings_size is None:
            strings_size = len(index.cache)
        (headers_dir / f"{name}.plist").write_bytes(
            _header_plist(offsets_size, strings_size, name in quoted))

    _write_sets(db, msgs_ids, new_ids, parts_ids, new_ids)
    if largest_used_body_part_id is None:
        largest_used_body_part_id = max(list(by_id) + list(all_parts), default=0)
    (db / "Main.plist").write_bytes(_main_plist(uuid, largest_used_body_part_id, revision))


# Appends

_HEADER_PLIST = re.compile(
    rb'\A\{\toffsetsFileSize = (\d+);\n\tstringsFileSize = ("?)(\d+)\2;\n\}\Z')
_MAIN_PLIST = re.compile(
    rb'\A\{\tUUID = "([^"]*)";\n\tlargestUsedBodyPartID = (\d+);\n'
    rb'\trevision = "([^"]*)";\n\tversion = "([^"]*)";\n\}\Z')
# Indexes keyed on part ids or kept for deleted messages; Delete leaves them.
_PART_KEYED = frozenset(BODY_INDEXES + tuple(n + "#lc" for n in BODY_INDEXES)
                        + ("#root-body-part", "#parent-body-part",
                           "#subpart-number"))


def _read_ids(path: pathlib.Path) -> Set[int]:
    data = path.read_bytes() if path.exists() else b""
    if len(data) % 4:
        raise ValueError(f"{path.name} length is not a multiple of 4")
    return {i for (i,) in struct.iter_unpack("<I", data)}


def _source_path(root: pathlib.Path, url: bytes, msg_id: int) -> pathlib.Path:
    """The `.eml` path of an `imap://<account>/<mailbox>` value (R7)."""
    text = url.decode("utf-8")
    if not text.startswith("imap://") or "/" not in text[7:]:
        raise ValueError(f"unexpected #source value for {msg_id}")
    account, mailbox = text[7:].split("/", 1)
    return eml_path(root, account, mailbox, msg_id)


def append_store(
    root: pathlib.Path,
    events: List[Union[Arrival, Move, Delete, Rewrite]],
    *,
    revision: Optional[str] = None,
    largest_used_body_part_id: Optional[int] = None,
    eml_newline: str = "\n",
) -> None:
    """Apply events to a store written by `build_store`, in place.

    Records and cache bytes are appended (the old bytes stay an exact
    prefix and the inode is kept); `.plist` files of changed indexes,
    `Main.plist` and the Sets are replaced. The store must be quiescent:
    Every file length must equal its `.plist` size, else ValueError.
    `revision` defaults to the stored one; `largestUsedBodyPartID` grows to
    cover new ids.
    """
    root = pathlib.Path(root)
    db = root / "Database.noindex"
    headers_dir = db / "Headers"
    loaded: Dict[str, _Index] = {}
    original: Dict[str, Tuple[int, int, bool]] = {}

    def ix(name: str) -> _Index:
        if name in loaded:
            return loaded[name]
        plist_path = headers_dir / f"{name}.plist"
        if plist_path.exists():
            m = _HEADER_PLIST.match(plist_path.read_bytes())
            if m is None:
                raise ValueError(f"{name}.plist does not parse")
            cache = (headers_dir / f"{name}.cache").read_bytes()
            offsets = (headers_dir / f"{name}.offsets").read_bytes()
            if len(offsets) != int(m.group(1)) or len(cache) != int(m.group(3)):
                raise ValueError(f"index {name} is not quiescent")
            loaded[name] = _Index.load(cache, offsets)
            original[name] = (len(cache), len(offsets), m.group(2) == b'"')
        else:
            loaded[name] = _Index()
            original[name] = (0, 0, False)
        return loaded[name]

    sets = db / "Sets"
    msgs, new, parts, new_parts = (_read_ids(sets / n) for n in SET_NAMES)
    main = _MAIN_PLIST.match((db / "Main.plist").read_bytes())
    if main is None:
        raise ValueError("Main.plist does not parse")
    uuid = main.group(1).decode("ascii")
    largest = int(main.group(2))
    if revision is None:
        revision = main.group(3).decode("ascii")

    for event in events:
        if isinstance(event, Arrival):
            msg = event.message
            if msg.duplicate_of is not None:
                raise ValueError("an Arrival cannot use duplicate_of")
            layout = _layout(msg)
            taken = msgs | new | parts | new_parts
            for i in [msg.id] + [pid for pid, _, _, _ in layout]:
                if i in taken:
                    raise ValueError(f"id {i} is already in use")
            _write_message(msg, layout, ix, root, eml_newline)
            largest = max([largest, msg.id] + [pid for pid, _, _, _ in layout])
            if msg.deleted or msg.orphan:
                continue
            if msg.new:
                new.add(msg.id)
                new_parts.add(msg.id)
            else:
                msgs.add(msg.id)
                parts.add(msg.id)
                parts.update(pid for pid, _, _, _ in layout)
        elif isinstance(event, Move):
            source = ix("#source")
            old = source.current(event.id)
            if not old:
                raise ValueError(f"message {event.id} has no #source value")
            source.marker(event.id)
            source.value(event.id, source_url(event.account, event.mailbox).encode("utf-8"))
            old_path = _source_path(root, old[-1], event.id)
            if old_path.exists():
                new_path = eml_path(root, event.account, event.mailbox, event.id)
                new_path.parent.mkdir(parents=True, exist_ok=True)
                old_path.replace(new_path)
        elif isinstance(event, Delete):
            if event.id not in msgs | new:
                raise ValueError(f"message {event.id} is not live")
            old = ix("#source").current(event.id)
            names = sorted({p.name[:-len(".offsets")]
                            for p in headers_dir.glob("*.offsets")} | set(loaded))
            for name in names:
                if name in _PART_KEYED:
                    continue
                index = ix(name)
                if index.current(event.id):
                    index.marker(event.id)
            if old:
                _unlink(_source_path(root, old[-1], event.id))
            root_ids = ix("#root-body-part")
            own = str(event.id).encode("ascii")
            part_ids = {rid for rid, _, _ in struct.iter_unpack("<III", bytes(root_ids.offsets))
                        if root_ids.current(rid) == [own]}
            msgs.discard(event.id)
            new.discard(event.id)
            new_parts.discard(event.id)
            parts.discard(event.id)
            parts.difference_update(part_ids)
        elif isinstance(event, Rewrite):
            index = ix(event.index)
            index.marker(event.id)
            if event.values is not None:
                for v in _as_list(event.values):
                    index.value(event.id, _as_bytes(v))
        else:
            raise TypeError(f"unknown event {event!r}")

    # Observed order (FORMAT.md section 10): All data files (`.cache` before
    # `.offsets` per index), then all `.plist` files, then `Main.plist`.
    changed = [name for name, index in loaded.items()
               if (len(index.cache), len(index.offsets)) != original[name][:2]]
    if changed:
        headers_dir.mkdir(parents=True, exist_ok=True)
    for name in changed:
        index = loaded[name]
        cache_len, offsets_len, _ = original[name]
        with open(headers_dir / f"{name}.cache", "ab") as f:
            f.write(bytes(index.cache[cache_len:]))
        with open(headers_dir / f"{name}.offsets", "ab") as f:
            f.write(bytes(index.offsets[offsets_len:]))
    for name in changed:
        index = loaded[name]
        _replace(headers_dir / f"{name}.plist",
                 _header_plist(len(index.offsets), len(index.cache),
                               original[name][2]))

    _write_sets(db, msgs, new, parts, new_parts)
    if largest_used_body_part_id is not None:
        largest = largest_used_body_part_id
    _replace(db / "Main.plist", _main_plist(uuid, largest, revision))


def _unlink(path: pathlib.Path) -> None:
    if path.exists():
        path.unlink()


def _replace(path: pathlib.Path, data: bytes) -> None:
    """Write data to a new file and rename it over path (new inode)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
