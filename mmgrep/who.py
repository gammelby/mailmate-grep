"""Resolve a person's name to the addresses they use (mmgrep --who).

Reads only the address indexes (from, to, cc, bcc) and the date indexes
of live messages. A contact matches when every query word is a prefix of
some word in its display name or in its address, after folding case and
accents. Contacts are ranked by the number of messages, then by the most
recent date.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from mmgrep.query import _TOKEN, DATE, DATE_RECEIVED, MESSAGE_ID, effective_index_date
from mmgrep.store import Snapshot

FIELDS = ("from", "to", "cc", "bcc")
WHO_INDEXES = frozenset({*FIELDS, DATE, DATE_RECEIVED, MESSAGE_ID})
_WORD = re.compile(r"\w+")
_ANGLE = re.compile(r"<\s*([^<>\s]+@[^<>\s]+?)\s*>")
_COMMENT = re.compile(r"\s*\(([^()]*)\)")
# Letters NFKD does not decompose into a base letter plus accent.
_LETTERS = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "đ": "d",
                          "ł": "l", "þ": "th", "ð": "d", "ı": "i"})


def fold(text: str) -> str:
    """Casefold and strip accents ("José" becomes "jose", "Søren" "soren")."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return plain.translate(_LETTERS)


@dataclass
class Contact:
    address: str
    names: Counter = field(default_factory=Counter)
    ids: set = field(default_factory=set)
    keys: set = field(default_factory=set)
    last: datetime | None = None

    @property
    def messages(self) -> int:
        """Distinct messages: Copies sharing a Message-ID count once."""
        return len(self.keys)

    def top_names(self, n: int = 5) -> list[str]:
        return [name for name, _ in sorted(self.names.items(), key=lambda kv: (-kv[1], kv[0]))][:n]


def _mailboxes(value: str) -> list[tuple[str, str]]:
    """(display name, address) pairs of a decoded index value.

    Not getaddresses: MailMate stores decoded names without quotes, so
    "Doe, Jane <jane@example.com>" must stay one mailbox (FORMAT.md
    section 4). A name is the text before "<address>" back to the
    previous mailbox; a bare address may carry a "(Name)" comment.
    """
    out: list[tuple[str, str]] = []

    def bare(segment: str) -> str:
        end = 0
        for m in _TOKEN.finditer(segment):
            comment = _COMMENT.match(segment, m.end())
            out.append((comment.group(1) if comment else "", m.group()))
            end = comment.end() if comment else m.end()
        return segment[end:]

    pos = 0
    for m in _ANGLE.finditer(value):
        name = bare(value[pos:m.start()])
        out.append((name.strip(" \t,;\"'"), m.group(1)))
        pos = m.end()
    bare(value[pos:])
    return out


def _matches(words: list[str], text: str) -> bool:
    tokens = _WORD.findall(fold(text))
    return all(any(t.startswith(w) for t in tokens) for w in words)


def who(snap: Snapshot, query: str) -> list[Contact]:
    """Contacts whose display name or address matches every word of query."""
    words = _WORD.findall(fold(query))
    if not words:
        raise ValueError("--who needs at least one name word")
    parsed: dict[bytes, list[tuple[str, str]]] = {}
    contacts: dict[str, Contact] = {}
    mids = snap.indexes.get(MESSAGE_ID)

    def key(id: int) -> object:
        value = mids.last(id) if mids is not None else None
        text = value.decode("utf-8", "replace").strip() if value else ""
        return text or id
    for name in FIELDS:
        index = snap.indexes.get(name)
        if index is None:
            continue
        for id in index.ids():
            if id not in snap.live:
                continue
            for raw in index.values(id) or ():
                hits = parsed.get(raw)
                if hits is None:
                    hits = []
                    for display, addr in _mailboxes(raw.decode("utf-8", "replace")):
                        if _matches(words, display) or _matches(words, addr):
                            hits.append((display.strip(), addr.lower()))
                    parsed[raw] = hits
                for display, addr in hits:
                    c = contacts.setdefault(addr, Contact(addr))
                    if display:
                        c.names[display] += 1
                    c.ids.add(id)
                    c.keys.add(key(id))
    for c in contacts.values():
        dates = [d for d, _ in (effective_index_date(snap, i) for i in c.ids) if d is not None]
        c.last = max(dates) if dates else None
    return sorted(contacts.values(),
                  key=lambda c: (-c.messages, -(c.last.timestamp() if c.last else 0), c.address))
