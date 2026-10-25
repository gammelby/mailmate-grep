"""The mmgrep command line (milestone 3 plan Task 6).

main() parses the options into Filters, runs the index pipeline (snapshot,
candidates, grouping and confirmation) or the --full scan, builds one Row
per message (decision D10), renders the rows and writes diagnostics to
stderr. Exit codes follow decision D9: 130 over 2 over 3 over 0 and 1.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

from mmgrep.confirm import CONFIRMED, INDETERMINATE, REJECTED, confirm_all
from mmgrep.output import Row, render, sort_rows
from mmgrep.query import (Copy, Filters, Group, group_copies, has_filter,
                          index_headers, needed_indexes, parse_addr,
                          parse_header_arg, parse_since, parse_until)
from mmgrep.query import candidates as find_candidates
from mmgrep.scan import full_scan
from mmgrep.store import DEFAULT_ROOT, FormatError, Store

MAX_JOBS = 4
EXIT_FOUND = 0
EXIT_NONE = 1
EXIT_USAGE = 2
EXIT_UNSTABLE = 3
EXIT_INTERRUPTED = 130

_DESCRIPTION = (
    "Search a local MailMate store read only, using MailMate's own index. "
    "Filters combine with AND; a repeated option ORs its values. Index hits "
    "are confirmed against the .eml files unless --no-confirm is given.")
_EPILOG = (
    "ADDR is @domain (that domain and its subdomains), local@domain (exact, "
    "case-insensitive) or re:PATTERN (a regex over the header value). "
    "PATTERN is a Python regex, case-insensitive unless -s is given. "
    "DATE is YYYY-MM-DD or an ISO datetime; local time unless it has an "
    "offset. Exit codes: 0 found, 1 none, 2 usage or format error, 3 "
    "results printed but the snapshot never stabilized, 130 interrupted.")


class _UsageError(Exception):
    """A usage error; main prints it as one line and returns 2."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageError(message)


_OPTION_PREFIX = re.compile(r"--[a-z-]+: ")


def _type(fn, option: str):
    """An argparse type that keeps fn's ValueError message.

    argparse already names the option, so a leading "--option: " in the
    message is dropped.
    """
    def convert(text: str):
        try:
            return fn(text)
        except ValueError as e:
            message = _OPTION_PREFIX.sub("", str(e), count=1)
            raise argparse.ArgumentTypeError(message) from None
    convert.__name__ = option
    return convert


def _count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"invalid number {text!r}") from None
    if value < 1:
        raise ValueError(f"must be 1 or more, got {value}")
    return value


def _default_jobs() -> int:
    return min(MAX_JOBS, os.cpu_count() or 1)


def build_parser() -> argparse.ArgumentParser:
    """The argument parser of mmgrep (design section 7)."""
    p = _Parser(prog="mmgrep", description=_DESCRIPTION, epilog=_EPILOG)
    flt = p.add_argument_group("filters")
    addr = _type(parse_addr, "ADDR")
    flt.add_argument("--from", dest="from_", action="append", default=[],
                     type=addr, metavar="ADDR", help="sender address")
    flt.add_argument("--to", action="append", default=[], type=addr,
                     metavar="ADDR", help="recipient address in To")
    flt.add_argument("--cc", action="append", default=[], type=addr,
                     metavar="ADDR", help="recipient address in Cc")
    flt.add_argument("--any-address", action="append", default=[], type=addr,
                     metavar="ADDR",
                     help="address in From, To, Cc or Bcc (Bcc where indexed)")
    flt.add_argument("--subject", action="append", default=[], metavar="PATTERN",
                     help="regex over the decoded subject")
    flt.add_argument("--header", action="append", default=[],
                     type=_type(parse_header_arg, "NAME=PATTERN"),
                     metavar="NAME=PATTERN",
                     help="regex over an indexed header (any header with --full)")
    flt.add_argument("--since", type=_type(parse_since, "DATE"), metavar="DATE",
                     help="date on or after DATE")
    flt.add_argument("--until", type=_type(parse_until, "DATE"), metavar="DATE",
                     help="date before DATE (a date only means before the next midnight)")
    flt.add_argument("--account", action="append", default=[], metavar="PATTERN",
                     help="regex over the account name")
    flt.add_argument("--mailbox", action="append", default=[], metavar="PATTERN",
                     help="regex over the mailbox path")
    flt.add_argument("--body", action="append", default=[], metavar="PATTERN",
                     help="regex over the unquoted body text")
    flt.add_argument("--include-quoted", action="store_true",
                     help="also search quoted body text")
    flt.add_argument("-s", "--case-sensitive", action="store_true",
                     help="match patterns case-sensitively")

    run = p.add_argument_group("search")
    run.add_argument("--full", action="store_true",
                     help="ignore the index and scan every .eml file")
    run.add_argument("--no-confirm", action="store_true",
                     help="trust the index and skip the .eml recheck")
    run.add_argument("--jobs", type=_type(_count, "N"), default=None, metavar="N",
                     help=f"worker processes, clamped to {MAX_JOBS} "
                          f"(default {_default_jobs()})")
    run.add_argument("--root", default=str(DEFAULT_ROOT), metavar="PATH",
                     help="MailMate data root (default %(default)s)")

    out = p.add_argument_group("output")
    fmt = out.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="JSON lines")
    fmt.add_argument("--paths-only", action="store_true",
                     help="the first .eml path of each message")
    out.add_argument("--all-paths", action="store_true",
                     help="with --paths-only: every copy's path")
    out.add_argument("--limit", type=_type(_count, "N"), metavar="N",
                     help="print at most N messages (after sorting by date)")
    out.add_argument("-v", "--verbose", action="store_true",
                     help="table: add to, cc and every path")
    return p


def _filters(args: argparse.Namespace) -> Filters:
    headers: dict[str, list[str]] = {}
    for name, pattern in args.header:
        headers.setdefault(name, []).append(pattern)
    return Filters(
        from_=args.from_, to=args.to, cc=args.cc, any_address=args.any_address,
        subject=args.subject, headers=headers, since=args.since,
        until=args.until, account=args.account, mailbox=args.mailbox,
        body=args.body[0] if args.body else None, include_quoted=args.include_quoted,
        case_sensitive=args.case_sensitive)


def _check(args: argparse.Namespace, f: Filters) -> None:
    """Raise _UsageError for option combinations and invalid patterns."""
    if len(args.body) > 1:
        raise _UsageError("--body given more than once")
    if not has_filter(f):
        raise _UsageError("no filter given; see mmgrep --help")
    if args.all_paths and not args.paths_only:
        raise _UsageError("--all-paths needs --paths-only")
    if args.full and args.no_confirm:
        raise _UsageError("--no-confirm cannot be used with --full")
    flags = 0 if f.case_sensitive else re.IGNORECASE
    patterns: list[tuple[str, str]] = [("--subject", p) for p in f.subject]
    patterns += [(f"--header {n}", p) for n, ps in f.headers.items() for p in ps]
    patterns += [("--account", p) for p in f.account]
    patterns += [("--mailbox", p) for p in f.mailbox]
    if f.body is not None:
        patterns.append(("--body", f.body))
    for option, pattern in patterns:
        try:
            re.compile(pattern, flags)
        except re.error as e:
            raise _UsageError(f"{option}: invalid pattern {pattern!r}: {e}") from None


def _err(line: str) -> None:
    sys.stderr.write("mmgrep: " + line + "\n")


def _paths(group: Group) -> tuple[list[str], list[int]]:
    return [c.path for c in group.copies], [c.id for c in group.copies]


def _confirmed_row(group: Group, conf) -> Row:
    h = conf.headers
    paths, ids = _paths(group)
    return Row(conf.date, h.get("from", ""), h.get("to", ""), h.get("cc", ""),
               h.get("subject", ""), group.message_id, paths, ids, True,
               conf.copy.path)


def _in_range(copy: Copy, f: Filters) -> bool:
    dt = copy.index_date
    if dt is None:
        return False
    if f.since is not None and dt < f.since:
        return False
    return f.until is None or dt < f.until


def _row_copy(group: Group, f: Filters) -> Copy:
    """The copy an unconfirmed row describes (decision D10).

    The first copy in path order whose index date passes --since and
    --until, else the first copy with an index date, else the first copy.
    """
    for copy in group.copies:
        if _in_range(copy, f):
            return copy
    for copy in group.copies:
        if copy.index_date is not None:
            return copy
    return group.copies[0]


def _index_row(snap, group: Group, f: Filters, confirmed) -> Row:
    first = _row_copy(group, f)
    h = index_headers(snap, first.id)
    paths, ids = _paths(group)
    return Row(first.index_date, h["from"], h["to"], h["cc"], h["subject"],
               group.message_id, paths, ids, confirmed, None)


class _Outcome:
    """Rows, diagnostic lines and the unstable flag of one run."""

    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.lines: list[str] = []
        self.unstable = False


def _run_index(store: Store, f: Filters, jobs: int, no_confirm: bool) -> _Outcome:
    if f.headers and store.database_dir.is_dir():
        known = store.index_names()
        for name in f.headers:
            if name not in known:
                raise _UsageError(
                    f"--header {name}: MailMate has no index for this header; "
                    "use --full to scan the .eml files")
    snap = store.snapshot(needed_indexes(f))
    cands = find_candidates(snap, f)
    grouped = group_copies(snap, cands.ids, f, store.messages_dir)
    o = _Outcome()
    rejected = indeterminate = 0
    if no_confirm:
        o.rows = [_index_row(snap, g, f, False) for g in grouped.groups]
    else:
        confs = confirm_all(grouped.groups, f, jobs)
        for group, conf in zip(grouped.groups, confs):
            if conf.status == CONFIRMED:
                o.rows.append(_confirmed_row(group, conf))
            elif conf.status == INDETERMINATE:
                indeterminate += 1
                o.rows.append(_index_row(snap, group, f, INDETERMINATE))
            elif conf.status == REJECTED:
                rejected += 1
    if rejected:
        o.lines.append(f"{rejected} index hits not confirmed")
    if indeterminate:
        o.lines.append(f"{indeterminate} indeterminate confirmations")
    for name in sorted(snap.skipped_records):
        count = snap.skipped_records[name]
        if count:
            o.lines.append(f"{count} skipped records in {name}")
    unmapped = cands.stats.get("unmapped_parts", 0)
    if unmapped:
        o.lines.append(f"{unmapped} body segments without a live message")
    if grouped.missing_eml:
        o.lines.append(f"{grouped.missing_eml} copies without an .eml file")
    if snap.retries:
        o.lines.append(f"snapshot retried {snap.retries} times")
    o.lines += [f"warning: {w}" for w in snap.warnings]
    if snap.unstable:
        o.unstable = True
        o.lines.append("snapshot never stabilized; results may be incomplete")
    return o


def _run_full(store: Store, f: Filters, jobs: int) -> _Outcome:
    if not store.messages_dir.is_dir():
        raise FormatError(
            f"not a MailMate store: {store.root} has no {store.messages_dir.name}")
    skipped: list[str] = []
    o = _Outcome()
    indeterminate = 0
    for group, conf in full_scan(store, f, jobs, skipped):
        if conf.status == CONFIRMED:
            o.rows.append(_confirmed_row(group, conf))
        else:
            indeterminate += 1
            paths, ids = _paths(group)
            # Decision D10 for --full: No index, so no values to show.
            o.rows.append(Row(None, "", "", "", "", group.message_id, paths,
                              ids, INDETERMINATE, None))
    if indeterminate:
        o.lines.append(f"{indeterminate} indeterminate confirmations")
    if skipped:
        o.lines.append(f"{len(skipped)} directories could not be read")
    return o


def _reconfigure_stdout() -> None:
    """Decision D13: Unencodable output characters become escapes."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            pass


def _main(argv: Optional[List[str]]) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EXIT_FOUND
    f = _filters(args)
    _check(args, f)
    jobs = min(MAX_JOBS, args.jobs) if args.jobs is not None else _default_jobs()
    store = Store(Path(args.root))
    if args.full:
        o = _run_full(store, f, jobs)
    else:
        o = _run_index(store, f, jobs, args.no_confirm)
    rows = sort_rows(o.rows)
    found = bool(rows)
    if args.limit is not None:
        rows = rows[:args.limit]
    fmt = "json" if args.json else "paths" if args.paths_only else "table"
    _reconfigure_stdout()
    render(rows, fmt, sys.stdout, verbose=args.verbose, all_paths=args.all_paths)
    sys.stdout.flush()
    for line in o.lines:
        _err(line)
    if o.unstable:
        return EXIT_UNSTABLE
    return EXIT_FOUND if found else EXIT_NONE


def main(argv: Optional[List[str]] = None) -> int:
    """Run mmgrep with argv (default sys.argv[1:]) and return the exit code.

    0: At least one message found. 1: None found. 2: Usage or format
    error. 3: Results printed but the snapshot never stabilized. 130:
    Interrupted; the one line "mmgrep: interrupted" goes to stderr.
    """
    try:
        return _main(argv)
    except KeyboardInterrupt:
        _err("interrupted")
        return EXIT_INTERRUPTED
    except (_UsageError, FormatError, ValueError, re.error) as e:
        _err(str(e))
        return EXIT_USAGE
    except OSError as e:
        # An unreadable root or store directory (SIGPIPE keeps its default
        # action, so a closed output pipe never arrives here).
        _err(f"cannot read the store: {e}")
        return EXIT_USAGE
