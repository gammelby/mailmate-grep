"""`#source` URLs, `.eml` paths and the part to message mapping.

The path rule is docs/FORMAT.md section 6 and rule R7: Only the `imap`
scheme maps; the account and every mailbox segment are kept as stored
(no percent decoding). The part to message mapping follows section 8,
rule R3 and design section 6a (RPF7). This module does no file I/O: It
only builds paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from mmgrep.store import Snapshot

ROOT_BODY_PART = "#root-body-part"
_SCHEMES = frozenset({"imap"})
_DECIMAL = re.compile(rb"[0-9]+")
_MAX_DIGITS = 10
_UINT32_MAX = 2 ** 32 - 1


@dataclass(frozen=True)
class SourceRef:
    """One parsed `#source` value: Scheme, URL authority and mailbox path."""

    scheme: str
    account: str
    mailbox: str


def _safe(ref: SourceRef) -> bool:
    """True when ref maps to a path inside the messages directory.

    The account must be one non-empty path component other than "." and
    "..". The mailbox must be non-empty with no empty segment. Segments get
    ".mailbox" appended, so "." and ".." there become plain names. NUL is
    refused everywhere.
    """
    if ref.scheme not in _SCHEMES:
        return False
    if ref.account in ("", ".", "..") or "/" in ref.account:
        return False
    if "\x00" in ref.account or "\x00" in ref.mailbox:
        return False
    if not ref.mailbox:
        return False
    return all(ref.mailbox.split("/"))


def parse_source(value: bytes) -> SourceRef | None:
    """Parse a `#source` value; None when it is unmapped (R7, R10).

    The value must be strict UTF-8 of the form
    `imap://<account>/<mailbox>[/<submailbox>...]`. Any other scheme, an
    empty, "." or ".." account, a missing or empty mailbox, an empty
    mailbox segment or a NUL byte gives None.
    """
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    scheme, sep, rest = text.partition("://")
    if not sep:
        return None
    account, sep, mailbox = rest.partition("/")
    if not sep:
        return None
    ref = SourceRef(scheme, account, mailbox)
    return ref if _safe(ref) else None


def eml_path(messages_dir: Path, ref: SourceRef, msg_id: int) -> Path:
    """The `.eml` path of msg_id under messages_dir (FORMAT.md section 6).

    Raises ValueError for a ref that parse_source would not return.
    """
    if not _safe(ref):
        raise ValueError("unsafe or unmapped source reference")
    path = Path(messages_dir) / ref.scheme.upper() / ref.account
    for segment in ref.mailbox.split("/"):
        path = path / (segment + ".mailbox")
    return path / "Messages" / f"{int(msg_id)}.eml"


def account_display(ref: SourceRef) -> str:
    """The account with `%40` decoded to `@` (design 6a, RPF11)."""
    return ref.account.replace("%40", "@")


def message_of_part(snapshot: Snapshot, part_ids: set[int]) -> tuple[set[int], int]:
    """Map body part ids to live message ids (R1, R3).

    An id in the live set maps to itself. Any other id maps through the
    last current `#root-body-part` value, parsed as ASCII decimal, and is
    kept only when that message is live. Returns the live message ids and
    the number of input ids that did not map. When the snapshot lacks
    `#root-body-part` (an unstable snapshot that could not read it), live
    ids still map to themselves and every other id counts as unmapped.
    """
    root = snapshot.indexes.get(ROOT_BODY_PART)
    live = snapshot.live
    found: set[int] = set()
    unmapped = 0
    for pid in part_ids:
        if pid in live:
            found.add(pid)
            continue
        value = root.last(pid) if root is not None else None
        # Ids are uint32: The length check before int() keeps Python 3.11+
        # from raising ValueError on a huge value; it then counts as unmapped.
        if (value is not None and len(value) <= _MAX_DIGITS
                and _DECIMAL.fullmatch(value)):
            msg_id = int(value)
            if msg_id <= _UINT32_MAX and msg_id in live:
                found.add(msg_id)
                continue
        unmapped += 1
    return found, unmapped
