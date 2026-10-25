"""Decoding and per-record search of one MailMate index triple.

The format is described in docs/FORMAT.md sections 3 and 8; the decoding
follows rule R1 and the search follows design section 8 step 2. This module
does no file I/O: The caller passes the whole `.offsets` and `.cache` bytes.
"""

from __future__ import annotations

import re
import sys
from array import array
from bisect import bisect_left, bisect_right
from itertools import compress, count, islice, repeat
from operator import gt, lt, ne
from typing import Iterator

MARKER = (1, 0)

_RECORD = 12
_LOW = 0xFFFFFFFF
# Index of the low 32-bit word inside a native 64-bit word.
_LO_WORD = 0 if sys.byteorder == "little" else 1


def _uint32_array(data: bytes) -> array:
    arr = array("I")
    if arr.itemsize != 4 or array("Q").itemsize != 8:
        raise RuntimeError("array('I') or array('Q') has an unexpected size")
    arr.frombytes(data)
    if sys.byteorder == "big":
        arr.byteswap()
    return arr


def _pack(high: array, low: array) -> array:
    """array("Q") of high << 32 | low, built with C-speed slice copies."""
    words = array("I", bytes(8 * len(low)))
    words[_LO_WORD::2] = low
    words[1 - _LO_WORD::2] = high
    return array("Q", words.tobytes())


def _unpack(packed: array) -> tuple[array, array]:
    """The high and low halves of an array("Q"), as two array("I")."""
    words = array("I", packed.tobytes())
    return words[1 - _LO_WORD::2], words[_LO_WORD::2]


class Index:
    """Current values per id of one index (rule R1).

    Validation of every record happens once at construction. The per-id
    and per-range views are built lazily on first use and cached (plan
    decision D4). They are flat arrays, so no Python object is kept per
    record.
    """

    def __init__(self, name: str, offsets: bytes, cache: bytes) -> None:
        self.name = name
        self.cache = cache
        self.partial_tail = len(offsets) % _RECORD
        self.record_count = len(offsets) // _RECORD
        arr = _uint32_array(offsets[: self.record_count * _RECORD])
        ids = arr[0::3]
        starts = arr[1::3]
        ends = arr[2::3]
        del arr
        size = len(cache)
        # Suspects: Start > end (markers included) or end beyond the cache.
        suspects = set(compress(range(self.record_count), map(gt, starts, ends)))
        if ends and max(ends) > size:
            suspects.update(compress(range(self.record_count), map(size.__lt__, ends)))
        last_marker: dict[int, int] = {}
        bad: list[int] = []
        for i in sorted(suspects):
            if starts[i] == MARKER[0] and ends[i] == MARKER[1]:
                last_marker[ids[i]] = i
            else:
                bad.append(i)
        self.bad_records = len(bad)
        self.invalid_ids = frozenset(
            ids[i] for i in bad if last_marker.get(ids[i], -1) < i
        )
        # A record is current when its index is above its id's cutoff: The
        # id's last marker, or record_count for an invalid id. Every marker
        # and bad record falls at or below its id's cutoff.
        last_marker.update(dict.fromkeys(self.invalid_ids, self.record_count))
        self._cutoff = last_marker
        self._columns = (ids, starts, ends)
        self._mask: bytes | None = None
        self._order: list[int] | None = None
        self._by_id: array | None = None
        self._by_range: tuple[array, array, array, array] | None = None
        self._overlap = False

    def _current(self) -> bytes | None:
        """One byte per record, nonzero when current; None when all are."""
        if self._mask is None and self._cutoff:
            keep = map(lt, map(self._cutoff.get, self._columns[0], repeat(-1)), count())
            self._mask = bytes(keep)
        return self._mask

    def _id_keys(self) -> array:
        """Sorted id << 32 | record index of every current record."""
        if self._by_id is None:
            keys = _pack(self._columns[0], array("I", range(self.record_count)))
            mask = self._current()
            self._by_id = array("Q", sorted(keys if mask is None else compress(keys, mask)))
        return self._by_id

    def _span(self, id: int) -> tuple[int, int]:
        keys = self._id_keys()
        lo = bisect_left(keys, id << 32)
        return lo, bisect_left(keys, (id + 1) << 32, lo)

    def values(self, id: int) -> list[bytes] | None:
        lo, hi = self._span(id)
        if lo == hi:
            return None
        keys = self._id_keys()
        _, starts, ends = self._columns
        cache = self.cache
        out = []
        for k in range(lo, hi):
            i = keys[k] & _LOW
            out.append(cache[starts[i]:ends[i]])
        return out

    def last(self, id: int) -> bytes | None:
        lo, hi = self._span(id)
        if lo == hi:
            return None
        i = self._id_keys()[hi - 1] & _LOW
        _, starts, ends = self._columns
        return self.cache[starts[i]:ends[i]]

    def ids(self) -> Iterator[int]:
        """Ids with a current record, in order of their first record."""
        if self._order is None:
            ids = self._columns[0]
            mask = self._current()
            if mask is None:
                self._order = list(dict.fromkeys(ids))
            else:
                live = set(compress(ids, mask))
                self._order = [i for i in dict.fromkeys(ids) if i in live]
        return iter(self._order)

    def _range_groups(self) -> tuple[array, array, array, array]:
        """The distinct current ranges and the ids that reference them.

        Returns (starts, ends, bounds, rids): Range k is starts[k] to
        ends[k], sorted by (start, end), and the ids rids[bounds[k]:
        bounds[k + 1]] reference it.
        """
        if self._by_range is None:
            ids, starts, ends = self._columns
            keys = _pack(starts, ends)
            mask = self._current()
            records = range(self.record_count)
            if mask is not None:
                records = compress(records, mask)
            # A stable sort by range keeps each range's records in order.
            order = sorted(records, key=keys.__getitem__)
            rids = array("I", map(ids.__getitem__, order))
            ranges = array("Q", map(keys.__getitem__, order))
            del order, keys
            n = len(ranges)
            fresh = bytearray(b"\x01" if n else b"")
            fresh.extend(map(ne, islice(ranges, 1, None), ranges))
            bounds = array("I", compress(range(n), fresh))
            rstarts, rends = _unpack(array("Q", compress(ranges, fresh)))
            del ranges, fresh
            bounds.append(n)
            self._by_range = (rstarts, rends, bounds, rids)
            # Sorted by (start, end), the ranges are disjoint exactly when
            # no start falls before the previous end.
            self._overlap = any(map(lt, islice(rstarts, 1, None), rends))
        return self._by_range

    def _disjoint_layout(self) -> tuple[array, array] | None:
        """Sorted starts and ends of the distinct current ranges.

        None when two ranges overlap; the literal prefilter then is not used.
        Checked once per index.
        """
        rstarts, rends, _, _ = self._range_groups()
        return None if self._overlap else (rstarts, rends)

    def search(self, pattern: re.Pattern[str], literal: bytes | None = None) -> set[int]:
        """Ids with a current record that pattern matches on its own.

        Each record is decoded with errors="replace" and matched alone, so a
        match never crosses records and anchors see only that record. Each
        distinct range is matched once and the result fans out to every id
        that references it. A literal (see required_literal) restricts the
        regex to ranges containing it; it must be required by the pattern.
        """
        starts, ends, bounds, rids = self._range_groups()
        cache = self.cache
        found: set[int] = set()
        if not literal or self._overlap:
            for s, e, lo, hi in zip(starts, ends, bounds, islice(bounds, 1, None)):
                if pattern.search(cache[s:e].decode("utf-8", "replace")):
                    found.update(rids[lo:hi])
            return found
        width = len(literal)
        find = cache.find
        pos = find(literal)
        while pos != -1:
            k = bisect_right(starts, pos) - 1
            if k >= 0 and pos + width <= ends[k]:
                s, e = starts[k], ends[k]
                if pattern.search(cache[s:e].decode("utf-8", "replace")):
                    found.update(rids[bounds[k]:bounds[k + 1]])
                nxt = e
            else:
                nxt = pos + 1
            pos = find(literal, nxt)
        return found


# required_literal: A hand tokenizer, not sre_parse (ruling C1).

_FLAG_GROUP = re.compile(r"\(\?[aiLmsux-]")
_QUANT = re.compile(r"\{(\d*)(?:(,)(\d*))?\}")
_SIMPLE_ESCAPES = {"a": "\a", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_CLASS_ESCAPES = frozenset("dDwWsS")
_ZERO_WIDTH_ESCAPES = frozenset("bBAZ")
_GROUP_KINDS = (":", "=", "!", "<=", "<!", "P<", "P=", ">")


class _Unknown(Exception):
    """A token the scanner does not fully understand."""


def _skip_class(p: str, i: int) -> int:
    """Index just past the character class that starts at p[i] == "["."""
    j = i + 1
    n = len(p)
    if j < n and p[j] == "^":
        j += 1
    if j < n and p[j] == "]":
        j += 1
    while j < n:
        c = p[j]
        if c == "\\":
            j += 2
        elif c == "]":
            return j + 1
        else:
            j += 1
    raise _Unknown("unterminated class")


def _skip_group(p: str, i: int) -> int:
    """Index just past the group that starts at p[i] == "("."""
    if p.startswith("(?", i) and not any(p.startswith(k, i + 2) for k in _GROUP_KINDS):
        raise _Unknown("group kind")
    depth = 0
    j = i
    n = len(p)
    while j < n:
        c = p[j]
        if c == "\\":
            j += 2
            continue
        if c == "[":
            j = _skip_class(p, j)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise _Unknown("unbalanced group")


def _quantifier(p: str, i: int) -> tuple[int, int] | None:
    """(minimum, index past it) for a quantifier at p[i], else None."""
    if i >= len(p):
        return None
    c = p[i]
    if c in "?*":
        low, j = 0, i + 1
    elif c == "+":
        low, j = 1, i + 1
    elif c == "{":
        m = _QUANT.match(p, i)
        if m is None or (m.group(1) == "" and not m.group(3)):
            # "{" that is not a quantifier, "{}" and "{,}" are not handled.
            raise _Unknown("brace")
        low, j = int(m.group(1) or "0"), m.end()
    else:
        return None
    if j < len(p) and p[j] in "?+":
        j += 1
    if j < len(p) and p[j] in "?*+{":
        raise _Unknown("multiple repeat")
    return low, j


def _scan(p: str) -> str:
    best = ""
    run: list[str] = []
    i = 0
    n = len(p)

    def close() -> None:
        nonlocal best, run
        if len(run) > len(best):
            best = "".join(run)
        run = []

    while i < n:
        c = p[i]
        literal: str | None = None
        if c == "\\":
            if i + 1 >= n:
                raise _Unknown("trailing backslash")
            d = p[i + 1]
            i += 2
            if d.isascii() and d.isalnum():
                if d in _SIMPLE_ESCAPES:
                    literal = _SIMPLE_ESCAPES[d]
                elif d in _ZERO_WIDTH_ESCAPES:
                    close()
                    if _quantifier(p, i) is not None:
                        raise _Unknown("repeated assertion")
                    continue
                elif d not in _CLASS_ESCAPES:
                    # \x \u \U \N, octal, backreferences and unknown escapes.
                    raise _Unknown("escape")
            else:
                literal = d
        elif c in "^$":
            close()
            i += 1
            if _quantifier(p, i) is not None:
                raise _Unknown("repeated assertion")
            continue
        elif c == "|":
            raise _Unknown("alternation")
        elif c == ")" or c in "?*+{":
            raise _Unknown("unexpected token")
        elif c == "[":
            i = _skip_class(p, i)
        elif c == "(":
            i = _skip_group(p, i)
        elif c == ".":
            i += 1
        else:
            literal = c
            i += 1
        quant = _quantifier(p, i)
        if quant is not None:
            low, i = quant
            if literal is not None and low >= 1:
                run.append(literal)
            close()
        elif literal is not None:
            run.append(literal)
        else:
            close()
    close()
    return best


def required_literal(pattern: str, flags: int) -> bytes | None:
    """A substring, as UTF-8, that every match of pattern contains.

    flags is the compiled pattern's flags. Returns None whenever soundness is
    not certain: Verbose mode, inline flags, top-level alternation, U+FFFD
    in the pattern, any token the scanner does not fully understand and
    ignore-case unless the literal is ASCII without letters. Groups end the
    current run and their content is skipped. The longest run wins (the
    first on a tie).
    """
    if flags & re.VERBOSE or "\ufffd" in pattern:
        return None
    if _FLAG_GROUP.search(pattern) or "(?#" in pattern or "(?(" in pattern:
        return None
    try:
        best = _scan(pattern)
    except _Unknown:
        return None
    if not best:
        return None
    if flags & re.IGNORECASE and not all(ch.isascii() and not ch.isalpha() for ch in best):
        return None
    try:
        return best.encode("utf-8")
    except UnicodeEncodeError:
        return None
