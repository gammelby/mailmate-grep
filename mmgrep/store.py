"""Consistent read-only snapshots of a MailMate store (rule R11).

Every file is read whole through read_file, which opens it with
open(path, "rb") and one read(). Nothing under the store is written, locked
or renamed (design section 4). The plist grammar is docs/FORMAT.md section
2; the live set follows rules R1 and R8; the retry loop follows R9 and R11.
"""

from __future__ import annotations

import os
import re
import sys
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from mmgrep.index import Index

DEFAULT_ROOT = Path.home() / "Library/Application Support/MailMate"

EXPECTED_VERSION = "1.1"
MAIN_KEYS = ("UUID", "revision", "version")
SET_FILES = ("Msgs.ids", "NewMsgs.ids")
SOURCE = "#source"

_KEY = re.compile(rb"[A-Za-z][A-Za-z0-9]*")
_VALUE = re.compile(rb'([0-9]+)|"([^"\\\n]*)"')
_DIGITS = re.compile(r"[0-9]+")
# Numeric fields are uint32 (FORMAT.md section 2). Checking the length
# before int() keeps Python 3.11+ from raising ValueError on huge inputs.
_MAX_DIGITS = 10
_UINT32_MAX = 2 ** 32 - 1
_SUFFIXES = (".plist", ".offsets", ".cache")


class FormatError(Exception):
    """Data that does not follow the observed format, or a root that is not a store."""


def _uint32(digits: bytes | str) -> int:
    """ASCII decimal digits as an int, FormatError unless it fits in uint32."""
    if len(digits) > _MAX_DIGITS:
        raise FormatError("plist integer has too many digits")
    value = int(digits)
    if value > _UINT32_MAX:
        raise FormatError("plist integer exceeds uint32")
    return value


def parse_openstep_plist(data: bytes) -> dict[str, int | str]:
    """Parse the OpenStep plist form of FORMAT.md section 2, strictly.

    The form is "{", then per key a tab, the key, " = ", a bare integer or a
    double-quoted string, ";" and a newline; then "}" and the end of data.
    Key order is not enforced. Anything else raises FormatError.
    """
    if data[:1] != b"{":
        raise FormatError("plist does not start with '{'")
    out: dict[str, int | str] = {}
    i = 1
    n = len(data)
    while True:
        if data[i:i + 1] == b"}":
            if i + 1 != n:
                raise FormatError("plist has data after '}'")
            return out
        if data[i:i + 1] != b"\t":
            raise FormatError("plist key is not preceded by a tab")
        m = _KEY.match(data, i + 1)
        if m is None:
            raise FormatError("plist key is malformed")
        key = m.group().decode("ascii")
        i = m.end()
        if data[i:i + 3] != b" = ":
            raise FormatError("plist key is not followed by ' = '")
        m = _VALUE.match(data, i + 3)
        if m is None:
            raise FormatError("plist value is neither an integer nor a quoted string")
        if m.group(1) is not None:
            value: int | str = _uint32(m.group(1))
        else:
            try:
                value = m.group(2).decode("ascii")
            except UnicodeDecodeError:
                raise FormatError("plist string is not ASCII") from None
        i = m.end()
        if data[i:i + 2] != b";\n":
            raise FormatError("plist value is not followed by ';' and a newline")
        i += 2
        if key in out:
            raise FormatError("plist has a duplicate key")
        out[key] = value


def read_file(path: Path) -> bytes:
    """The whole file, read with one read() on a read-only handle."""
    with open(path, "rb") as f:
        return f.read()


def _ids(data: bytes) -> array:
    arr = array("I")
    if arr.itemsize != 4:
        raise RuntimeError("array('I') is not 4 bytes on this platform")
    arr.frombytes(data)
    if sys.byteorder == "big":
        arr.byteswap()
    return arr


def _parse_main(data: bytes) -> dict[str, int | str] | None:
    try:
        main = parse_openstep_plist(data)
    except FormatError:
        return None
    if not all(isinstance(main.get(k), str) for k in MAIN_KEYS):
        return None
    return main


def _parse_sizes(data: bytes) -> tuple[int, int] | None:
    """(offsetsFileSize, stringsFileSize) of a header plist, else None."""
    try:
        plist = parse_openstep_plist(data)
    except FormatError:
        return None
    offsets = plist.get("offsetsFileSize")
    strings = plist.get("stringsFileSize")
    if not isinstance(offsets, int):
        return None
    if isinstance(strings, str):
        if not _DIGITS.fullmatch(strings):
            return None
        try:
            strings = _uint32(strings)
        except FormatError:
            return None
    if not isinstance(strings, int):
        return None
    return offsets, strings


@dataclass
class Snapshot:
    """The indexes and live set of one snapshot, with its consistency signals.

    unstable is True when the last attempt still failed a check; then
    warnings holds one line per failed check (names only, never values).
    An index with none of its three files at the start check and on every
    attempt is an empty Index in indexes with the one warning "index <name>
    not present in this store"; that alone is not instability. An index
    present (any of its files seen) at the start check or on one attempt
    and absent on another makes the snapshot unstable; it is in indexes
    when the last attempt read its .offsets and .cache, and missing from
    indexes otherwise. An index whose .offsets or .cache could not be read
    on the last attempt is missing from indexes, and the snapshot is then
    unstable.
    warnings can be non-empty on a stable snapshot: The Main.plist version
    warning does not make it unstable. retries is the number of retries
    made. skipped_records counts, per index, the ids without a value
    because of a malformed record plus one for a trailing partial record.
    A #source read only as the R8 live set fallback is never in indexes
    but counts in skipped_records. main is the last parsed Main.plist.
    """

    indexes: dict[str, Index]
    live: frozenset[int]
    unstable: bool
    retries: int
    warnings: list[str] = field(default_factory=list)
    skipped_records: dict[str, int] = field(default_factory=dict)
    main: dict[str, int | str] = field(default_factory=dict)


class _Attempt:
    """The data and failed checks of one pass over the R11 read sequence."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.mains: list[dict[str, int | str]] = []
        self.main_read = False
        self.sets: list[bytes] | None = None
        self.indexes: dict[str, Index] = {}
        self.absent: set[str] = set()
        self.fallback: Index | None = None

    def fail(self, message: str) -> None:
        if message not in self.failures:
            self.failures.append(message)


class Store:
    """A MailMate data root, read only."""

    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.database_dir = self.root / "Database.noindex"
        self.headers_dir = self.database_dir / "Headers"
        self.sets_dir = self.database_dir / "Sets"
        self.messages_dir = self.root / "Messages.noindex"
        self.main_plist = self.database_dir / "Main.plist"

    def index_names(self) -> set[str]:
        """Names that have all three files of a triple under Headers."""
        try:
            with os.scandir(self.headers_dir) as entries:
                files = {e.name for e in entries}
        except OSError:
            return set()
        names = {f[:-len(".plist")] for f in files if f.endswith(".plist")}
        return {n for n in names if all(n + s in files for s in _SUFFIXES)}

    def snapshot(self, names: Iterable[str], retries: int = 3,
                 backoff: float = 0.05) -> Snapshot:
        """Read the named indexes and the live set consistently (R11).

        Up to retries + 1 attempts; before retry n the reader sleeps
        backoff * 2 ** (n - 1) seconds. When the last attempt still fails a
        check, its whole files are used with per-id validation and the
        snapshot is unstable. Raises ValueError before any read for a name
        that is empty, "." or ".." or contains "/" or NUL. Raises
        FormatError when the root is not a store or Main.plist never
        parses.
        """
        wanted = list(dict.fromkeys(names))
        for name in wanted:
            if name in ("", ".", "..") or "/" in name or "\x00" in name:
                raise ValueError(f"invalid index name {name!r}")
        if not self.database_dir.is_dir():
            raise FormatError(
                f"not a MailMate store: {self.root} has no Database.noindex/Main.plist")
        last_main: dict[str, int | str] | None = None
        main_ever_read = False
        seen: dict[str, set[bool]] = {
            name: {self._triple_seen(name)} for name in wanted}
        attempt = _Attempt()
        made = 0
        for n in range(retries + 1):
            if n and backoff > 0:
                time.sleep(backoff * 2 ** (n - 1))
            made = n
            attempt = self._attempt(wanted)
            main_ever_read = main_ever_read or attempt.main_read
            if attempt.mains:
                last_main = attempt.mains[-1]
            for name in wanted:
                seen[name].add(name not in attempt.absent)
                if len(seen[name]) > 1:
                    attempt.fail(f"index {name}: present on one attempt and absent on another")
            if not attempt.failures:
                break
        if last_main is None:
            if main_ever_read:
                raise FormatError(f"{self.main_plist} never parsed (root {self.root})")
            raise FormatError(
                f"not a MailMate store: {self.main_plist} is absent (root {self.root})")

        warnings: list[str] = []
        version = last_main.get("version")
        if version != EXPECTED_VERSION:
            warnings.append(
                f"Main.plist version is {version}, not {EXPECTED_VERSION}; "
                "the index format may have changed")
        indexes = dict(attempt.indexes)
        for name in wanted:
            if seen[name] == {False}:
                indexes[name] = Index(name, b"", b"")
                warnings.append(f"index {name} not present in this store")
        warnings.extend(attempt.failures)

        if attempt.sets is not None:
            live: frozenset[int] = frozenset().union(*(_ids(d) for d in attempt.sets))
        else:
            source = attempt.indexes.get(SOURCE, attempt.fallback)
            if source is None:
                live = frozenset()
                warnings.append("live set is empty: Sets and #source both failed")
            else:
                live = frozenset(source.ids())
                warnings.append("live set taken from #source (R8 fallback)")

        skipped: dict[str, int] = {}
        used = dict(attempt.indexes)
        if attempt.sets is None and attempt.fallback is not None:
            used.setdefault(SOURCE, attempt.fallback)
        for name, index in used.items():
            count = len(index.invalid_ids) + (1 if index.partial_tail else 0)
            if count:
                skipped[name] = count

        return Snapshot(
            indexes=indexes,
            live=live,
            unstable=bool(attempt.failures),
            retries=made,
            warnings=warnings,
            skipped_records=skipped,
            main=last_main,
        )

    def _attempt(self, names: list[str]) -> _Attempt:
        a = _Attempt()
        first = self._read_main(a)
        sets: list[bytes] = []
        for set_name in SET_FILES:
            try:
                data = read_file(self.sets_dir / set_name)
            except OSError:
                a.fail(f"Sets/{set_name} could not be read")
                continue
            if len(data) % 4:
                a.fail(f"Sets/{set_name} length is not a multiple of 4")
                continue
            sets.append(data)
        sets_ok = len(sets) == len(SET_FILES)
        if sets_ok:
            a.sets = sets
        for name in names:
            index = self._read_index(name, a, track_absence=True)
            if index is not None:
                a.indexes[name] = index
        if not sets_ok and SOURCE not in names:
            a.fallback = self._read_index(SOURCE, a)
        second = self._read_main(a)
        if first is not None and second is not None:
            for key in ("UUID", "revision"):
                if first[key] != second[key]:
                    a.fail(f"Main.plist {key} changed during the read")
        return a

    def _read_main(self, a: _Attempt) -> dict[str, int | str] | None:
        try:
            data = read_file(self.main_plist)
        except OSError:
            a.fail("Main.plist could not be read")
            return None
        a.main_read = True
        main = _parse_main(data)
        if main is None:
            a.fail("Main.plist does not parse or lacks UUID, revision or version")
            return None
        a.mains.append(main)
        return main

    def _triple_seen(self, name: str) -> bool:
        """Whether any file of the triple exists (the start check)."""
        return any((self.headers_dir / f"{name}{s}").exists() for s in _SUFFIXES)

    def _read_index(self, name: str, a: _Attempt,
                    track_absence: bool = False) -> Index | None:
        """Read one triple in R11 order and record its failed checks.

        With track_absence, a triple whose every read raises
        FileNotFoundError is recorded in a.absent instead of failing.
        """
        base = self.headers_dir
        label = f"index {name}"
        data: dict[str, bytes | None] = {}
        missing: list[str] = []
        not_found = 0
        for key, path in (("plist1", base / f"{name}.plist"),
                          ("offsets", base / f"{name}.offsets"),
                          ("cache", base / f"{name}.cache"),
                          ("plist2", base / f"{name}.plist")):
            try:
                data[key] = read_file(path)
            except OSError as e:
                data[key] = None
                not_found += isinstance(e, FileNotFoundError)
                suffix = path.name[len(name):]
                missing.append(f"{label}: {suffix} could not be read (missing or vanished)")
        if track_absence and not_found == len(data):
            a.absent.add(name)
            return None
        for message in missing:
            a.fail(message)
        plist1, plist2 = data["plist1"], data["plist2"]
        offsets, cache = data["offsets"], data["cache"]
        if plist1 is not None and plist2 is not None and plist1 != plist2:
            a.fail(f"{label}: .plist changed during the read")
        sizes = None
        for raw in (plist1, plist2):
            if raw is not None:
                sizes = _parse_sizes(raw)
                if sizes is None:
                    a.fail(f"{label}: .plist does not parse or lacks its sizes")
                break
        if sizes is not None:
            if offsets is not None and len(offsets) != sizes[0]:
                a.fail(f"{label}: .offsets length differs from offsetsFileSize")
            if cache is not None and len(cache) != sizes[1]:
                a.fail(f"{label}: .cache length differs from stringsFileSize")
        if offsets is None or cache is None:
            return None
        index = Index(name, offsets, cache)
        if index.partial_tail:
            a.fail(f"{label}: .offsets length is not a multiple of 12")
        if index.bad_records:
            a.fail(f"{label}: malformed records")
        return index
