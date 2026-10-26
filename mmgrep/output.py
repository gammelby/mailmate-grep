"""Rendering of result rows as a table, JSON lines or paths.

The CLI builds one Row per message (decision D10) and hands the sorted
rows to render() with a text stream. This module writes only to that
stream and imports nothing from the rest of the package.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Iterable, List, NamedTuple, Optional, TextIO, Union

FORMATS = ("table", "json", "paths")
DATE_WIDTH = 16
FROM_WIDTH = 30
SUBJECT_WIDTH = 60
SEP = "  "
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class Row(NamedTuple):
    """One message of the result with all its qualifying copies.

    `confirmed` is True, False (not checked, as with --no-confirm) or the
    string "indeterminate". `paths` and `ids` list the copies in path
    order; `confirmed_path` is the copy that confirmed, if any. `flags`
    lists the message's flags over all its copies (None when unknown, as
    with --full).
    """

    date: Optional[datetime]
    from_: str
    to: str
    cc: str
    subject: str
    message_id: Optional[str]
    paths: List[str]
    ids: List[int]
    confirmed: Union[bool, str]
    confirmed_path: Optional[str]
    flags: Optional[List[str]] = None


def _sort_key(row: Row) -> tuple:
    if row.date is None:
        return (1, _EPOCH, row.message_id or "")
    return (0, row.date, row.message_id or "")


def sort_rows(rows: Iterable[Row]) -> list[Row]:
    """Rows by date ascending, then Message-ID; undated rows come last.

    Dates must be aware. A missing Message-ID sorts as the empty string.
    """
    return sorted(rows, key=_sort_key)


def _units(text: str) -> list[str]:
    """Text as display units: A printable character or one whole escape."""
    return [
        ch if ch.isprintable()
        else ch.encode("unicode_escape", "backslashreplace").decode("ascii")
        for ch in text
    ]


def _escape(text: str) -> str:
    """Text with every non-printable character in unicode_escape form."""
    if text.isprintable():
        return text
    return "".join(_units(text))


def _fit(text: str, width: int) -> str:
    """Text escaped, then cut to width with "..." or padded to width.

    The cut drops whole units, so an escape such as \\x1b is never split.
    """
    units = _units(text)
    if sum(len(u) for u in units) <= width:
        return "".join(units).ljust(width)
    kept: list[str] = []
    used = 0
    for unit in units:
        if used + len(unit) > width - 3:
            break
        kept.append(unit)
        used += len(unit)
    return ("".join(kept) + "...").ljust(width)


def _table_date(date: Optional[datetime]) -> str:
    if date is None:
        return " " * DATE_WIDTH
    return date.astimezone().strftime("%Y-%m-%d %H:%M")


def _render_table(rows: list[Row], stream: TextIO, verbose: bool) -> None:
    width = max((len(str(len(r.paths))) for r in rows), default=1)
    for row in rows:
        stream.write(SEP.join((
            _table_date(row.date),
            _fit(row.from_, FROM_WIDTH),
            _fit(row.subject, SUBJECT_WIDTH),
            str(len(row.paths)).rjust(width),
        )) + "\n")
        if verbose:
            stream.write("    to: " + _escape(row.to) + "\n")
            stream.write("    cc: " + _escape(row.cc) + "\n")
            if row.flags is not None:
                stream.write("    flags: " + _escape(" ".join(row.flags)) + "\n")
            for path in row.paths:
                stream.write("    path: " + _escape(path) + "\n")


def _render_json(rows: list[Row], stream: TextIO) -> None:
    for row in rows:
        obj = {
            "date": None if row.date is None else row.date.isoformat(),
            "from": row.from_,
            "to": row.to,
            "cc": row.cc,
            "subject": row.subject,
            "message_id": row.message_id,
            "paths": list(row.paths),
            "ids": list(row.ids),
            "confirmed": row.confirmed,
            "confirmed_path": row.confirmed_path,
            "flags": None if row.flags is None else list(row.flags),
        }
        stream.write(json.dumps(obj, ensure_ascii=True) + "\n")


def _render_paths(rows: list[Row], stream: TextIO, all_paths: bool) -> None:
    """Paths one per line, as their real bytes when the stream has a buffer.

    A surrogate-escaped path then reaches a pipe such as xargs as the
    bytes the file system holds. A stream without a buffer gets the str.
    """
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        stream.flush()
    for row in rows:
        for path in (row.paths if all_paths else row.paths[:1]):
            if buffer is not None:
                buffer.write(os.fsencode(path) + b"\n")
            else:
                stream.write(path + "\n")


def render(rows: Iterable[Row], fmt: str, stream: TextIO,
           verbose: bool = False, all_paths: bool = False) -> None:
    """Write rows to stream in fmt ("table", "json" or "paths").

    Rows are written in the order given; sort them with sort_rows first.
    Table output escapes non-printable characters and shows the date in
    local time. JSON output is ASCII only (decision D13). Paths output
    writes the first path of each row, or every path with all_paths.
    Raises ValueError for an unknown fmt.
    """
    if fmt not in FORMATS:
        raise ValueError("unknown output format: " + repr(fmt))
    rows = list(rows)
    if fmt == "table":
        _render_table(rows, stream, verbose)
    elif fmt == "json":
        _render_json(rows, stream)
    else:
        _render_paths(rows, stream, all_paths)
