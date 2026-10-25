"""Tests for mmgrep.scan (milestone 3, Task 4).

The full scan walks every `<digits>.eml` under the messages directory
without the index and checks each file with the confirmation predicates
(decisions D4, D5 and D11, ruling C4). Stores are synthetic and built with
tests.fixtures.build_store; tests may add `.eml` files and symlinks inside
their own temp store. RFC 2606 domains only.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from mmgrep import scan as scan_module
from mmgrep.confirm import confirm_all
from mmgrep.query import (Copy, Filters, candidates, group_copies,
                          needed_indexes, parse_addr)
from mmgrep.scan import full_scan, walk_emls
from mmgrep.store import Store
from tests.fixtures import FixtureMessage, FixturePart, build_store

ACCOUNT = "user%40example.com@imap.example.com"
DISPLAY = "user@example.com@imap.example.com"
UTC = timezone.utc


def msg(id: int, mailbox: str = "INBOX", **kw) -> FixtureMessage:
    return FixtureMessage(id=id, account=ACCOUNT, mailbox=mailbox, **kw)


def hdr(id: int, **extra: object) -> dict:
    headers = {"From": f"sender{id}@example.org", "Subject": f"Message {id}",
               "Message-ID": f"<{id}@example.com>",
               "Date": "Tue, 05 Mar 2024 14:07:09 +0100"}
    for key, value in extra.items():
        name = key.replace("_", "-")
        if value is None:
            headers.pop(name, None)
        else:
            headers[name] = value
    return headers


class _ScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def build(self, messages: list[FixtureMessage]) -> None:
        build_store(self.root, messages)
        self.store = Store(self.root)

    def mailbox_dir(self, mailbox: str = "INBOX") -> pathlib.Path:
        path = self.root / "Messages.noindex" / "IMAP" / ACCOUNT
        for segment in mailbox.split("/"):
            path = path / (segment + ".mailbox")
        return path / "Messages"

    def scan(self, f: Filters, jobs: int = 1):
        return full_scan(self.store, f, jobs)

    def mids(self, f: Filters, jobs: int = 1) -> set:
        return {g.message_id for g, _ in self.scan(f, jobs)}

    def index_mids(self, f: Filters) -> set:
        snap = self.store.snapshot(needed_indexes(f), backoff=0)
        self.assertFalse(snap.unstable, snap.warnings)
        ids = candidates(snap, f).ids
        groups = group_copies(snap, ids, f, self.store.messages_dir).groups
        results = confirm_all(groups, f, 1)
        return {g.message_id for g, c in zip(groups, results)
                if c.status in ("confirmed", "indeterminate")}


class WalkTest(_ScanTest):
    def test_walk_emls(self) -> None:
        self.build([msg(1200, "Archive/2024", headers=hdr(1200), body=["one"])])
        messages = self.mailbox_dir("Archive/2024")
        (messages / "State.plist").write_bytes(b"{}")
        (messages.parent / "State.plist").write_bytes(b"{}")
        (messages / "notes.eml").write_bytes(b"Subject: x\n\nx\n")
        os.symlink(messages / "1200.eml", messages / "1300.eml")
        account = self.root / "Messages.noindex" / "IMAP" / ACCOUNT
        os.symlink(account / "Archive.mailbox", account / "Linked.mailbox")
        os.symlink(messages, messages.parent / "Other.mailbox")
        copies = list(walk_emls(self.store.messages_dir))
        self.assertEqual(copies, [Copy(1200, str(messages / "1200.eml"), DISPLAY,
                                       "Archive/2024")])
        self.assertIsNone(copies[0].date_source)
        self.assertIsNone(copies[0].index_date)

    def test_walk_missing_dir_empty(self) -> None:
        self.assertEqual(list(walk_emls(self.root / "absent")), [])


def six() -> list[FixtureMessage]:
    return [
        msg(1200, headers=hdr(1200, Subject="Plan A", To="x@example.net"),
            body=["alpha one"]),
        msg(1300, headers=hdr(1300, From="b@example.com", Subject="Plan B"),
            body=["beta"], quoted=["alpha quoted"]),
        msg(1400, headers=hdr(1400, Subject="Other", Cc="y@example.net"),
            parts=[FixturePart(body=["gamma"]),
                   FixturePart(content_type="text/html", body=["alpha html"])],
            multipart="alternative"),
        msg(1500, "Sent Messages", duplicate_of=1300),
        msg(1600, headers=hdr(1600, From="c@example.com", Bcc="x@example.net"),
            body=["delta"]),
        msg(1700, "Archive/2024", headers=hdr(1700, Subject="Plan C"),
            body=["epsilon alpha"]),
        # Raw UTF-8 display name: policy.default flags UndecodableBytesDefect.
        msg(1800, headers=hdr(1800, From="Jörg <j@example.net>", Subject="Other 8"),
            body=["theta"]),
    ]


class FullScanTest(_ScanTest):
    def test_full_equals_index_mode(self) -> None:
        self.build(six())
        cases = {
            "from": (Filters(from_=[parse_addr("@example.org")]),
                     {"<1200@example.com>", "<1400@example.com>", "<1700@example.com>"}),
            "subject": (Filters(subject=["^Plan"]),
                        {"<1200@example.com>", "<1300@example.com>", "<1700@example.com>"}),
            "body": (Filters(body="alpha"),
                     {"<1200@example.com>", "<1400@example.com>", "<1700@example.com>"}),
            "any_address": (Filters(any_address=[parse_addr("x@example.net")]),
                            {"<1200@example.com>", "<1600@example.com>"}),
            "raw_utf8_from": (Filters(from_=[parse_addr("@example.net")]),
                              {"<1800@example.com>"}),
            "exact_from": (Filters(from_=[parse_addr("b@example.com")]),
                           {"<1300@example.com>"}),
        }
        for name, (f, expected) in cases.items():
            with self.subTest(query=name):
                self.assertEqual(self.mids(f), expected)
                self.assertEqual(self.index_mids(f), expected)

    def test_full_raw_utf8_from_rejected(self) -> None:
        self.build([msg(1800, headers=hdr(1800, From="Jörg <j@example.net>"),
                        body=["theta"])])
        self.assertEqual(self.scan(Filters(from_=[parse_addr("b@example.com")])), [])
        self.assertEqual(self.mids(Filters(from_=[parse_addr("j@example.net")])),
                         {"<1800@example.com>"})

    def test_full_finds_eml_only(self) -> None:
        self.build(six())
        (self.mailbox_dir() / "1900.eml").write_bytes(
            b"From: z@example.org\nSubject: Plan Z\nMessage-ID: <1900@example.com>\n"
            b"Date: Tue, 05 Mar 2024 14:07:09 +0100\n\nzeta\n")
        f = Filters(subject=["^Plan Z$"])
        result = self.scan(f)
        self.assertEqual(len(result), 1)
        group, conf = result[0]
        self.assertEqual(group.message_id, "<1900@example.com>")
        self.assertEqual(conf.status, "confirmed")
        self.assertEqual(conf.copy.id, 1900)
        self.assertEqual(conf.headers["subject"], "Plan Z")
        self.assertEqual(conf.date, datetime(2024, 3, 5, 13, 7, 9, tzinfo=UTC))
        self.assertEqual(self.index_mids(f), set())

    def test_full_quoted(self) -> None:
        self.build([msg(1200, headers=hdr(1200), body=["hello"],
                        quoted=["only quoted gamma"])])
        self.assertEqual(self.mids(Filters(body="gamma")), set())
        self.assertEqual(self.mids(Filters(body="gamma", include_quoted=True)),
                         {"<1200@example.com>"})
        self.assertEqual(self.mids(Filters(body="^only quoted gamma$",
                                           include_quoted=True)),
                         {"<1200@example.com>"})
        self.assertEqual(self.mids(Filters(body="hello")), {"<1200@example.com>"})

    def test_full_dedup_paths(self) -> None:
        self.build([msg(1300, headers=hdr(1300), body=["x"]),
                    msg(1500, "Sent Messages", duplicate_of=1300)])
        result = self.scan(Filters(subject=["Message 1300"]))
        self.assertEqual(len(result), 1)
        group, conf = result[0]
        self.assertEqual(group.message_id, "<1300@example.com>")
        self.assertEqual([c.id for c in group.copies], [1300, 1500])
        self.assertEqual([c.path for c in group.copies],
                         sorted(c.path for c in group.copies))
        self.assertEqual(conf.copy.id, 1300)
        result = self.scan(Filters(subject=["Message 1300"], mailbox=["^Sent"]))
        self.assertEqual([[c.id for c in g.copies] for g, _ in result], [[1500]])
        self.assertEqual(result[0][1].copy.id, 1500)

    def test_full_no_message_id_stands_alone(self) -> None:
        self.build([msg(1200, headers=hdr(1200, Message_ID=None), body=["x"]),
                    msg(1300, headers=hdr(1300, Message_ID=None), body=["x"])])
        result = self.scan(Filters(from_=[parse_addr("@example.org")]))
        self.assertEqual([(g.message_id, [c.id for c in g.copies]) for g, _ in result],
                         [(None, [1200]), (None, [1300])])

    def test_full_rejected_dropped(self) -> None:
        self.build(six())
        self.assertEqual(self.scan(Filters(subject=["^nothing here$"])), [])

    def test_full_dates_use_file_date(self) -> None:
        self.build([msg(1200, headers=hdr(1200), body=["x"]),
                    msg(1300, headers=hdr(1300, Date=None), body=["x"]),
                    msg(1400, headers=hdr(1400, Date="Tue, 05 Mar 2024 14:07:09"),
                        body=["x"])])
        f = Filters(since=datetime(2024, 3, 1, tzinfo=UTC))
        self.assertEqual(self.mids(f), {"<1200@example.com>"})
        f = Filters(since=datetime(2024, 3, 6, tzinfo=UTC))
        self.assertEqual(self.mids(f), set())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads mode 0")
    def test_full_unreadable_indeterminate(self) -> None:
        self.build([msg(1200, headers=hdr(1200), body=["x"]),
                    msg(1300, headers=hdr(1300), body=["x"])])
        path = self.mailbox_dir() / "1300.eml"
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o644)
        result = self.scan(Filters(subject=["Message"]))
        statuses = [(c.status, [x.id for x in g.copies]) for g, c in result]
        self.assertEqual(statuses, [("confirmed", [1200]), ("indeterminate", [1300])])
        self.assertIsNone(result[1][0].message_id)

    def test_full_header_beyond_cap_indeterminate(self) -> None:
        self.build([msg(1200, headers=hdr(1200), body=["x"])])
        filler = b"".join(b"X-Filler-%06d: %s\n" % (i, b"a" * 60)
                          for i in range(17000))
        (self.mailbox_dir() / "1200.eml").write_bytes(
            filler + b"Subject: Message 1200\n\nx\n")
        result = self.scan(Filters(subject=["Message 1200"]))
        self.assertEqual([c.status for _, c in result], ["indeterminate"])

    def _unreadable_dir(self, kind: str) -> str:
        """Two mailboxes; make one mailbox dir or its Messages dir mode 0."""
        self.build([msg(1200, headers=hdr(1200), body=["x"]),
                    msg(1300, "Locked", headers=hdr(1300), body=["x"])])
        path = self.mailbox_dir("Locked")
        if kind == "mailbox":
            path = path.parent
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o755)
        return os.path.relpath(path, self.store.messages_dir)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads mode 0")
    def test_full_unreadable_dir_skipped(self) -> None:
        for kind in ("mailbox", "messages"):
            with self.subTest(kind=kind):
                self.setUp()
                rel = self._unreadable_dir(kind)
                skipped: list[str] = []
                result = full_scan(self.store, Filters(subject=["Message"]), 1, skipped)
                self.assertEqual([[c.id for c in g.copies] for g, _ in result], [[1200]])
                self.assertEqual(skipped, [rel])
                self.assertEqual(len(list(walk_emls(self.store.messages_dir))), 1)

    def test_full_missing_dir_not_skipped(self) -> None:
        skipped: list[str] = []
        self.assertEqual(list(walk_emls(self.root / "absent", skipped)), [])
        self.assertEqual(skipped, [])

    def test_full_hostile_file_indeterminate(self) -> None:
        self.build([msg(1200, headers=hdr(1200), body=["x"]),
                    msg(1300, headers=hdr(1300), body=["x"])])
        hostile = str(self.mailbox_dir() / "1300.eml")
        real = scan_module.read_message

        def fake(path, body):
            if path == hostile:
                raise RecursionError("hostile MIME")
            return real(path, body)

        with mock.patch.object(scan_module, "read_message", fake):
            result = self.scan(Filters(subject=["Message"]))
        statuses = [(c.status, [x.id for x in g.copies]) for g, c in result]
        self.assertEqual(statuses, [("confirmed", [1200]), ("indeterminate", [1300])])

    def test_full_interrupt_propagates(self) -> None:
        self.build([msg(1200, headers=hdr(1200), body=["x"])])
        with mock.patch.object(scan_module, "read_message",
                               side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.scan(Filters(subject=["Message"]))

    def test_full_pool(self) -> None:
        self.build([msg(2000 + i * 10, headers=hdr(2000 + i * 10), body=["x"])
                    for i in range(210)])
        f = Filters(subject=["^Message [0-9]+$"])
        one = [(g.message_id, c.status) for g, c in self.scan(f, 1)]
        two = [(g.message_id, c.status) for g, c in self.scan(f, 2)]
        self.assertEqual(len(one), 210)
        self.assertEqual(one, two)


if __name__ == "__main__":
    unittest.main()
