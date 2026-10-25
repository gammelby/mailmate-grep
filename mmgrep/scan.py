"""The exhaustive --full scan over every `.eml` file, without the index.

Milestone 3 plan Task 4 with decisions D4, D5 and D11 and ruling C4: The
walk visits `<scheme>/<account>/<name>.mailbox/.../Messages/<digits>.eml`
under the messages directory with os.scandir and never follows a
symlink. Each file is checked with the confirmation predicates of
mmgrep.confirm: A header-only parse unless the query has a body pattern,
no index date (so date filters use the file's aware Date) and quoted text
removed from the body unless --include-quoted. Files are then grouped by
their own last Message-ID. An unreadable or unparsable file is
indeterminate and stands alone. Files are opened only through
confirm.read_message.
"""

from __future__ import annotations

import os
import re
from email.message import Message
from pathlib import Path
from typing import Iterator

from mmgrep.confirm import (CONFIRMED, INDETERMINATE, Confirmation,
                            check_message, decoded_headers, read_message,
                            run_pool)
from mmgrep.query import Copy, Filters, Group

_EML = re.compile(r"[0-9]+\.eml")
_MAILBOX_SUFFIX = ".mailbox"
_FOLD = re.compile(r"\r?\n(?=[ \t])")

# One file's result: Copy, file Message-ID, status, headers and date.
_Result = tuple


class _Lister:
    """Lists directories and records those that could not be read."""

    def __init__(self, root: str, skipped: list[str] | None) -> None:
        self.root = root
        self.skipped = skipped

    def entries(self, path: str) -> list[os.DirEntry]:
        """The entries of path sorted by name; empty when unreadable.

        A directory that vanished (FileNotFoundError) is skipped silently.
        Any other OSError (permission denied, a name too long) skips the
        directory and appends its path relative to root (foreman F2).
        """
        try:
            with os.scandir(path) as it:
                return sorted(it, key=lambda e: e.name)
        except FileNotFoundError:
            return []
        except OSError:
            if self.skipped is not None:
                self.skipped.append(os.path.relpath(path, self.root))
            return []


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _is_file(entry: os.DirEntry) -> bool:
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False


def _walk_mailbox(lister: _Lister, path: str, account: str,
                  segments: list[str]) -> Iterator[Copy]:
    """Copies in one `.mailbox` directory and its submailboxes."""
    mailbox = "/".join(segments)
    for entry in lister.entries(path):
        if not _is_dir(entry):
            continue
        if entry.name == "Messages":
            for item in lister.entries(entry.path):
                if _EML.fullmatch(item.name) and _is_file(item):
                    yield Copy(int(item.name[:-4]), item.path, account, mailbox)
        elif entry.name.endswith(_MAILBOX_SUFFIX) and len(entry.name) > len(_MAILBOX_SUFFIX):
            yield from _walk_mailbox(lister, entry.path, account,
                                     segments + [entry.name[:-len(_MAILBOX_SUFFIX)]])


def walk_emls(messages_dir: Path, skipped: list[str] | None = None) -> Iterator[Copy]:
    """One Copy per `<digits>.eml` directly under a mailbox's Messages dir.

    The layout is FORMAT.md section 6: The account is the directory below
    the scheme directory with `%40` decoded and the mailbox is the
    `.mailbox` components without the suffix, joined by "/". Symlinked
    files and directories are skipped. A directory that vanishes during
    the walk is skipped silently. A directory that cannot be listed for
    any other OSError is skipped and its path relative to messages_dir is
    appended to skipped when given (foreman F2). Copies carry no index
    date.
    """
    root = str(messages_dir)
    lister = _Lister(root, skipped)
    for scheme in lister.entries(root):
        if not _is_dir(scheme):
            continue
        for account in lister.entries(scheme.path):
            if not _is_dir(account):
                continue
            display = account.name.replace("%40", "@")
            for entry in lister.entries(account.path):
                name = entry.name
                if (_is_dir(entry) and name.endswith(_MAILBOX_SUFFIX)
                        and len(name) > len(_MAILBOX_SUFFIX)):
                    yield from _walk_mailbox(lister, entry.path, display,
                                             [name[:-len(_MAILBOX_SUFFIX)]])


def _message_id(msg: Message) -> str | None:
    """The last Message-ID of msg, unfolded and stripped (decision D1)."""
    values = [v for k, v in msg.raw_items() if k.lower() == "message-id"]
    if not values:
        return None
    text = _FOLD.sub("", str(values[-1])).strip()
    try:
        text = text.encode("ascii", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:
        pass
    return text or None


def _scan_item(item: tuple[Copy, Filters]) -> _Result:
    """Check one file; module level so pool workers can run it."""
    copy, f = item
    try:
        msg = read_message(copy.path, body=f.body is not None)
        message_id = _message_id(msg)
        status, date = check_message(msg, f, None, not f.include_quoted)
        headers = decoded_headers(msg) if status == CONFIRMED else {}
    except Exception:
        # Ruling C4: Any per-file failure (OSError, a parse error or hostile
        # MIME raising RecursionError and the like) makes only this file
        # indeterminate. KeyboardInterrupt is not an Exception and propagates.
        return copy, None, INDETERMINATE, {}, None
    return copy, message_id, status, headers, date


def _location_ok(copy: Copy, f: Filters) -> bool:
    flags = 0 if f.case_sensitive else re.IGNORECASE
    if f.account and not any(re.search(p, copy.account, flags) for p in f.account):
        return False
    if f.mailbox and not any(re.search(p, copy.mailbox, flags) for p in f.mailbox):
        return False
    return True


def _group_result(copies: list[Copy], results: list[_Result],
                  message_id: str | None) -> tuple[Group, Confirmation] | None:
    """The group of results and its outcome; None when every copy failed."""
    indeterminate = False
    for copy, _, status, headers, date in results:
        if status == CONFIRMED:
            return Group(message_id, copies), Confirmation(CONFIRMED, copy, headers, date)
        if status == INDETERMINATE:
            indeterminate = True
    if not indeterminate:
        return None
    return Group(message_id, copies), Confirmation(INDETERMINATE, None, {}, None)


def full_scan(store, f: Filters, jobs: int,
              skipped: list[str] | None = None) -> list[tuple[Group, Confirmation]]:
    """Check every `.eml` under store.messages_dir against f.

    --account and --mailbox select files before they are read. Files
    group by their own last Message-ID; a file without one, or one that
    could not be read or parsed, stands alone. A group is confirmed by
    its first confirming copy in path order, else indeterminate when any
    copy was; rejected groups are left out. Groups are sorted by first
    copy path. The pool follows confirm.run_pool, counting files.
    Directories that could not be listed go to skipped as in walk_emls.
    """
    copies = [c for c in walk_emls(store.messages_dir, skipped) if _location_ok(c, f)]
    results = run_pool(_scan_item, [(c, f) for c in copies], jobs)
    by_mid: dict[str, list[_Result]] = {}
    alone: list[list[_Result]] = []
    for result in results:
        message_id = result[1]
        if message_id is None:
            alone.append([result])
        else:
            by_mid.setdefault(message_id, []).append(result)
    out: list[tuple[Group, Confirmation]] = []
    buckets = [(mid, rs) for mid, rs in by_mid.items()] + [(None, rs) for rs in alone]
    for message_id, rs in buckets:
        rs.sort(key=lambda r: r[0].path)
        group = _group_result([r[0] for r in rs], rs, message_id)
        if group is not None:
            out.append(group)
    out.sort(key=lambda gc: gc[0].copies[0].path)
    return out
