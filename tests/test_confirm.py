"""Tests for mmgrep.confirm (milestone 3, Task 3).

Confirmation rechecks each group of copies on its `.eml` files: The
Message-ID, address, subject, header and date filters on a header-only
parse and the body on the decoded text parts (decisions D1 to D5 and D11,
rulings C2 to C4). Stores are synthetic and built with
tests.fixtures.build_store; tests may rewrite, delete or chmod a fixture
`.eml` inside their own temp store. RFC 2606 domains only.
"""

from __future__ import annotations

import builtins
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor
from unittest import mock

from mmgrep import confirm as confirm_module
from mmgrep.confirm import (Confirmation, body_texts, check_message,
                            confirm, confirm_all, read_message, run_pool)
from mmgrep.query import (Copy, Filters, Group, candidates, group_copies,
                          needed_indexes, parse_addr)
from mmgrep.store import Store
from tests.fixtures import FixtureMessage, FixturePart, build_store

ACCOUNT = "user%40example.com@imap.example.com"
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


class _ConfirmTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def build(self, messages: list[FixtureMessage], **kw) -> None:
        build_store(self.root, messages, **kw)
        self.store = Store(self.root)

    def groups(self, f: Filters, ids=None) -> list[Group]:
        snap = self.store.snapshot(needed_indexes(f), backoff=0)
        self.assertFalse(snap.unstable, snap.warnings)
        if ids is None:
            ids = candidates(snap, f).ids
        return group_copies(snap, ids, f, self.store.messages_dir).groups

    def one(self, f: Filters, ids=None) -> Confirmation:
        groups = self.groups(f, ids)
        self.assertEqual(len(groups), 1, groups)
        return confirm(groups[0], f)

    def status(self, f: Filters, ids=None) -> str:
        return self.one(f, ids).status

    def path(self, id: int, mailbox: str = "INBOX") -> pathlib.Path:
        return (self.root / "Messages.noindex" / "IMAP" / ACCOUNT
                / (mailbox + ".mailbox") / "Messages" / f"{id}.eml")


class HeaderTest(_ConfirmTest):
    def test_display_name_token_rejected(self) -> None:
        self.build([msg(1200, headers=hdr(
            1200, From='"alice@example.com" <mallory@example.org>'))])
        f = Filters(from_=[parse_addr("alice@example.com")])
        self.assertEqual(self.status(f), "rejected")
        f = Filters(from_=[parse_addr("mallory@example.org")])
        self.assertEqual(self.status(f), "confirmed")

    def test_encoded_comma_display_name_confirms(self) -> None:
        self.build([msg(1200, headers=hdr(
            1200, From="=?UTF-8?Q?Doe=2C_Jane?= <jane@example.com>"))])
        f = Filters(from_=[parse_addr("jane@example.com")])
        c = self.one(f)
        self.assertEqual(c.status, "confirmed")
        self.assertEqual(c.headers["from"], "Doe, Jane <jane@example.com>")

    def test_raw_utf8_header_confirms(self) -> None:
        self.build([msg(1200, headers=hdr(1200, Subject="Grüße aus Berlin"))])
        f = Filters(subject=["grüße"])
        c = self.one(f)
        self.assertEqual(c.status, "confirmed")
        self.assertEqual(c.headers["subject"], "Grüße aus Berlin")

    def test_raw_utf8_address_header_decides(self) -> None:
        self.build([msg(1200, headers=hdr(1200, From="Jörg <j@example.com>"))])
        self.assertIn("Jörg".encode("utf-8"), self.path(1200).read_bytes())
        f = Filters(from_=[parse_addr("b@example.com")])
        self.assertEqual(self.status(f, ids={1200}), "rejected")
        f = Filters(from_=[parse_addr("j@example.com")])
        c = self.one(f)
        self.assertEqual(c.status, "confirmed")
        self.assertEqual(c.headers["from"], "Jörg <j@example.com>")

    def test_message_id_mismatch_rejects(self) -> None:
        self.build([msg(1200, headers=hdr(1200),
                        index={"message-id": "<other@example.com>"})])
        self.assertEqual(self.status(Filters(subject=["Message"])), "rejected")

    def test_subject_rfc2047_confirms(self) -> None:
        self.build([msg(1200, headers=hdr(
            1200, Subject="=?UTF-8?Q?Gr=C3=BC=C3=9Fe?= aus Berlin"))])
        c = self.one(Filters(subject=["grüße"]))
        self.assertEqual(c.status, "confirmed")
        self.assertEqual(c.headers["subject"], "Grüße aus Berlin")
        self.assertEqual(c.headers["message-id"], "<1200@example.com>")
        self.assertEqual(c.headers["date"], "Tue, 05 Mar 2024 14:07:09 +0100")
        self.assertEqual(c.copy.id, 1200)
        self.assertEqual(c.date, datetime(2024, 3, 5, 13, 7, 9, tzinfo=UTC))

    def test_header_option_and_any_address(self) -> None:
        self.build([msg(1200, headers=hdr(1200, X_Mailer="Synthetic Mailer 1",
                                          Bcc="x@example.net"))])
        self.assertEqual(self.status(Filters(headers={"x-mailer": ["synthetic"]})),
                         "confirmed")
        self.assertEqual(self.status(Filters(headers={"x-mailer": ["other"]}),
                                     ids={1200}), "rejected")
        f = Filters(any_address=[parse_addr("@example.net")])
        self.assertEqual(self.status(f), "confirmed")
        f = Filters(to=[parse_addr("x@example.net")])
        self.assertEqual(self.status(f, ids={1200}), "rejected")

    def test_header_defect_blocks_rejection(self) -> None:
        self.build([msg(1200, headers=hdr(1200))])
        self.path(1200).write_bytes(
            b"Message-ID: <1200@example.com>\nSubject: Defect\n"
            b"not a header line\nFrom: a@example.com\n\nbody\n")
        f = Filters(from_=[parse_addr("a@example.com")])
        self.assertEqual(self.status(f, ids={1200}), "indeterminate")
        self.path(1200).write_bytes(
            b"Message-ID: <1200@example.com>\nSubject: Clean\n"
            b"From: b@example.com\n\nbody\n")
        self.assertEqual(self.status(f, ids={1200}), "rejected")


class BodyTest(_ConfirmTest):
    def test_body_anchor_paragraph(self) -> None:
        self.build([msg(1200, headers=hdr(1200),
                        body=["First line", "Second paragraph", "Third"])])
        self.assertEqual(self.status(Filters(body="^Second paragraph$")), "confirmed")
        self.assertEqual(self.status(Filters(body="^paragraph"), ids={1200}),
                         "rejected")

    def test_html_joined_blocks(self) -> None:
        part = FixturePart(content_type="text/html",
                           content="<div>Title</div><div>Body</div>",
                           body=["TitleBody"])
        self.build([msg(1200, headers=hdr(1200), parts=[part])])
        self.assertEqual(self.status(Filters(body="TitleBody")), "confirmed")
        self.assertEqual(self.status(Filters(body="^Body$"), ids={1200}), "confirmed")

    def test_all_alternatives(self) -> None:
        parts = [FixturePart(content="plain words only\n"),
                 FixturePart(content_type="text/html",
                             content="<p>htmlonlyterm &amp; more</p>",
                             body=["htmlonlyterm & more"])]
        self.build([msg(1200, headers=hdr(1200), parts=parts,
                        multipart="alternative")])
        self.assertEqual(self.status(Filters(body="htmlonlyterm & more")),
                         "confirmed")

    def test_unknown_charset_indeterminate(self) -> None:
        part = FixturePart(content=b"a known term here\n", charset="x-unknown-8",
                           body=["a known term here"])
        self.build([msg(1200, headers=hdr(1200), parts=[part])])
        self.assertEqual(self.status(Filters(body="term")), "indeterminate")

    def test_lossy_decode_match_confirms(self) -> None:
        lossy = FixturePart(content="café latte\n".encode("utf-8"),
                            charset="us-ascii", body=["café latte"])
        clean = FixturePart(content="café latte\n", charset="utf-8",
                            body=["café latte"])
        self.build([msg(1200, headers=hdr(1200), parts=[lossy]),
                    msg(1300, headers=hdr(1300), parts=[clean])])
        self.assertEqual(self.status(Filters(body="latte"), ids={1200}), "confirmed")
        self.assertEqual(self.status(Filters(body="zzz"), ids={1200}),
                         "indeterminate")
        self.assertEqual(self.status(Filters(body="zzz"), ids={1300}), "rejected")
        self.assertEqual(self.status(Filters(body="café"), ids={1300}), "confirmed")

    def test_quoted_markers_variant(self) -> None:
        part = FixturePart(content=b"Hi\r\n\r\n> > nested quoted line\r\n> second quoted\r\n",
                           body=["Hi"], quoted=["nested quoted line\nsecond quoted"])
        self.build([msg(1200, headers=hdr(1200), parts=[part])], eml_newline="\r\n")
        self.assertIn(b"\r\n> second quoted\r\n", self.path(1200).read_bytes())
        for pattern in ("^nested quoted line$", "line\nsecond"):
            with self.subTest(pattern=pattern):
                f = Filters(body=pattern, include_quoted=True)
                self.assertEqual(self.status(f), "confirmed")
                f = Filters(body=pattern)
                self.assertEqual(self.status(f, ids={1200}), "rejected")
        self.assertEqual(self.status(Filters(body="Hi$")), "confirmed")

    def test_body_texts_strip_quoted(self) -> None:
        parts = [FixturePart(content="kept line\n> quoted plain\n"),
                 FixturePart(content_type="text/html",
                             content="<p>kept html</p><blockquote>quoted html</blockquote>"
                                     "<script>hidden()</script>")]
        self.build([msg(1200, headers=hdr(1200), parts=parts)])
        m = read_message(str(self.path(1200)), body=True)
        texts, lossy = body_texts(m, strip_quoted=False)
        self.assertFalse(lossy)
        joined = "\n".join(texts)
        self.assertIn("> quoted plain", joined)
        self.assertIn("quoted html", joined)
        self.assertNotIn("hidden", joined)
        texts, lossy = body_texts(m, strip_quoted=True)
        joined = "\n".join(texts)
        self.assertIn("kept line", joined)
        self.assertIn("kept html", joined)
        self.assertNotIn("quoted", joined)


class DateTest(_ConfirmTest):
    def test_date_recheck(self) -> None:
        self.build([msg(1200, headers=hdr(1200),
                        index={"#date": b"2024-04-01 10:00:00 +0000"})])
        f = Filters(since=datetime(2024, 3, 20, tzinfo=UTC))
        self.assertEqual(self.status(f), "rejected")
        f = Filters(since=datetime(2024, 3, 1, tzinfo=UTC))
        self.assertEqual(self.status(f), "confirmed")

    def _two_received(self, early: str, late: str) -> None:
        headers = hdr(1300, Date=None)
        self.build([
            msg(1300, mailbox=early, headers=headers,
                index={"#date-received": b"2024-03-01 10:00:00"}),
            msg(1500, mailbox=late, headers=headers,
                index={"#date-received": b"2024-03-10 10:00:00"}),
        ])

    def test_dates_per_copy(self) -> None:
        f = Filters(since=datetime(2024, 3, 5, tzinfo=UTC))
        for early, late in (("A", "B"), ("B", "A")):
            with self.subTest(early=early):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                self.root = pathlib.Path(tmp.name)
                self._two_received(early, late)
                groups = self.groups(f)
                self.assertEqual(len(groups), 1)
                self.assertEqual(sorted(c.id for c in groups[0].copies), [1300, 1500])
                c = confirm(groups[0], f)
                self.assertEqual(c.status, "confirmed")
                self.assertEqual(c.copy.id, 1500)
                self.assertEqual(c.copy.mailbox, late)
                self.assertEqual(c.date, datetime(2024, 3, 10, 10, tzinfo=UTC))

    def test_naive_file_date_uses_received(self) -> None:
        self.build([msg(1200, headers=hdr(1200, Date="Tue, 05 Mar 2024 14:07:09"),
                        index={"#date-received": b"2024-03-05 13:07:12"})])
        f = Filters(since=datetime(2024, 3, 5, 13, tzinfo=UTC))
        c = self.one(f)
        self.assertEqual(c.status, "confirmed")
        self.assertEqual(c.date, datetime(2024, 3, 5, 13, 7, 12, tzinfo=UTC))
        f = Filters(since=datetime(2024, 3, 5, 14, tzinfo=UTC))
        self.assertEqual(self.status(f, ids={1200}), "rejected")

    def test_date_provenance_without_file_date(self) -> None:
        self.build([msg(1200, headers=hdr(1200, Date=None),
                        index={"#date": b"2024-03-05 14:07:09 +0100"})])
        f = Filters(since=datetime(2024, 3, 1, tzinfo=UTC))
        self.assertEqual(self.status(f), "indeterminate")


class CopyTest(_ConfirmTest):
    def _dup_store(self) -> None:
        self.build([msg(1300, headers=hdr(1300)),
                    msg(1500, mailbox="Sent Messages", duplicate_of=1300)])

    def test_first_copy_rejected_second_confirmed(self) -> None:
        self.build([msg(1300, headers=hdr(1300, Message_ID="<other@example.com>"),
                        index={"message-id": "<1300@example.com>"}),
                    msg(1500, mailbox="Sent Messages", headers=hdr(1300))])
        groups = self.groups(Filters(subject=["Message"]))
        self.assertEqual([c.id for c in groups[0].copies], [1300, 1500])
        c = confirm(groups[0], Filters(subject=["Message"]))
        self.assertEqual(c.status, "confirmed")
        self.assertEqual(c.copy.id, 1500)

    def test_indeterminate_only_without_confirming_copy(self) -> None:
        self.build([msg(1300, headers=hdr(1300)),
                    msg(1400, headers=hdr(1400, Message_ID="<1300@example.com>",
                                          Subject="Other"))])
        f = Filters(subject=["Message"])
        gone = Copy(1, str(self.root / "missing.eml"), "a", "INBOX")
        good = Copy(1300, str(self.path(1300)), "a", "INBOX")
        bad = Copy(1400, str(self.path(1400)), "a", "INBOX")
        c = confirm(Group("<1300@example.com>", [gone, good]), f)
        self.assertEqual((c.status, c.copy), ("confirmed", good))
        c = confirm(Group("<1300@example.com>", [gone, bad]), f)
        self.assertEqual((c.status, c.copy), ("indeterminate", None))
        c = confirm(Group("<1300@example.com>", [bad]), f)
        self.assertEqual((c.status, c.copy), ("rejected", None))

    def test_oserror_copy_indeterminate(self) -> None:
        self._dup_store()
        f = Filters(subject=["Message"])
        groups = self.groups(f)
        self.path(1300).unlink()
        c = confirm(groups[0], f)
        self.assertEqual((c.status, c.copy.id), ("confirmed", 1500))
        only = Group(groups[0].message_id, groups[0].copies[:1])
        self.assertEqual(confirm(only, f).status, "indeterminate")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads mode 0")
    def test_unreadable_copy_indeterminate(self) -> None:
        self.build([msg(1300, headers=hdr(1300))])
        f = Filters(subject=["Message"])
        groups = self.groups(f)
        os.chmod(self.path(1300), 0)
        self.addCleanup(os.chmod, self.path(1300), 0o644)
        self.assertEqual(confirm(groups[0], f).status, "indeterminate")

    def test_deep_nesting_indeterminate(self) -> None:
        self.build([msg(1200, headers=hdr(1200))])
        head = (b"Message-ID: <1200@example.com>\nFrom: a@example.com\n"
                b"Subject: Message 1200\n")
        self.path(1200).write_bytes(
            head + b"".join(b"Content-Type: message/rfc822\n\n" + head
                            for _ in range(1500)) + b"\nx\n")
        c = self.one(Filters(body="x"), ids={1200})
        self.assertEqual((c.status, c.copy), ("indeterminate", None))

    def test_hostile_html_gives_status(self) -> None:
        markup = b"<![if foo]>x<![endif]> <![x <!DOCTYPE y [ <!ELEMENT> ]> <! junk"
        self.build([msg(1200, headers=hdr(1200))])
        self.path(1200).write_bytes(
            b"Message-ID: <1200@example.com>\nSubject: Message 1200\n"
            b"Content-Type: text/html\n\n" + markup)
        for pattern in ("x", "zzz"):
            with self.subTest(pattern=pattern):
                status = self.status(Filters(body=pattern), ids={1200})
                self.assertIn(status, ("confirmed", "rejected", "indeterminate"))

    def test_keyboard_interrupt_propagates(self) -> None:
        self.build([msg(1200, headers=hdr(1200))])
        f = Filters(subject=["Message"])
        groups = self.groups(f)
        with mock.patch.object(confirm_module, "read_message",
                               side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                confirm(groups[0], f)

    def test_header_beyond_cap_indeterminate(self) -> None:
        self.build([msg(1200, headers=hdr(1200))])
        pad = b"".join(b"X-Pad-%06d: %s\n" % (i, b"p" * 100) for i in range(11000))
        self.assertGreater(len(pad), 1200 * 1024)
        self.path(1200).write_bytes(
            b"Message-ID: <1200@example.com>\n" + pad
            + b"Subject: Message 1200\n\nbody\n")
        self.assertEqual(self.status(Filters(subject=["Message"])), "indeterminate")

    def test_headers_only_reads_header_block(self) -> None:
        blob = bytes(range(256)) * (2 * 1024 * 1024 // 256)
        part = FixturePart(content_type="application/octet-stream",
                           content=blob, filename="blob.bin")
        self.build([msg(1200, headers=hdr(1200),
                        parts=[FixturePart(content="small text\n"), part])])
        self.assertGreater(self.path(1200).stat().st_size, 2 * 1024 * 1024)
        f = Filters(subject=["Message"])
        groups = self.groups(f)
        total = [0]
        real_open = builtins.open

        class Counting:
            def __init__(self, fh) -> None:
                self.fh = fh

            def read(self, *args):
                data = self.fh.read(*args)
                total[0] += len(data)
                return data

            def close(self) -> None:
                self.fh.close()

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                self.fh.close()

        def counting_open(*args, **kw):
            return Counting(real_open(*args, **kw))

        with mock.patch("builtins.open", counting_open):
            c = confirm(groups[0], f)
        self.assertEqual(c.status, "confirmed")
        self.assertGreater(total[0], 0)
        self.assertLess(total[0], 70000)


class ApiTest(_ConfirmTest):
    def test_check_message_direct(self) -> None:
        self.build([msg(1200, headers=hdr(1200))])
        m = read_message(str(self.path(1200)), body=False)
        f = Filters(subject=["Message 1200"])
        status, date = check_message(m, f, "<1200@example.com>", False)
        self.assertEqual(status, "confirmed")
        self.assertEqual(date, datetime(2024, 3, 5, 13, 7, 9, tzinfo=UTC))
        self.assertEqual(check_message(m, f, None, False)[0], "confirmed")
        self.assertEqual(check_message(m, f, "<x@example.com>", False)[0], "rejected")

    def test_confirm_all_pool(self) -> None:
        self.build([msg(1200 + 10 * i, headers=hdr(1200 + 10 * i, Subject=f"Pool {i}"))
                    for i in range(210)])
        f = Filters(subject=["^Pool 1"])
        snap = self.store.snapshot(needed_indexes(f), backoff=0)
        groups = group_copies(snap, snap.live, f, self.store.messages_dir).groups
        self.assertEqual(len(groups), 210)
        sequential = confirm_all(groups, f, 1)
        with mock.patch.object(confirm_module, "ProcessPoolExecutor",
                               wraps=ProcessPoolExecutor) as pool:
            pooled = confirm_all(groups, f, 2)
        pool.assert_called_once()
        self.assertEqual(pool.call_args.kwargs["max_workers"], 2)
        self.assertEqual([c.status for c in pooled], [c.status for c in sequential])
        self.assertEqual(pooled, sequential)
        statuses = {c.status for c in pooled}
        self.assertEqual(statuses, {"confirmed", "rejected"})

    def test_run_pool_order_and_threshold(self) -> None:
        items = [-i for i in range(250)]
        with mock.patch.object(confirm_module, "ProcessPoolExecutor",
                               wraps=ProcessPoolExecutor) as pool:
            self.assertEqual(run_pool(abs, items, 9), list(range(250)))
            self.assertEqual(pool.call_args.kwargs["max_workers"], 4)
            self.assertEqual(run_pool(abs, items[:199], 4), list(range(199)))
            self.assertEqual(run_pool(abs, items, 1), list(range(250)))
        self.assertEqual(pool.call_count, 1)


class StructuralDefectTest(unittest.TestCase):
    """Broken MIME structure makes extraction undecidable (Codex M3 review)."""

    def parse(self, raw: bytes):
        from email import policy
        from email.parser import BytesParser
        return BytesParser(policy=policy.default).parsebytes(raw)

    def test_missing_boundary_is_lossy(self) -> None:
        raw = (b"From: a@example.com\r\nMessage-ID: <x@example.com>\r\n"
               b"Content-Type: multipart/mixed; boundary=\"missing\"\r\n\r\n"
               b"an invoice without any boundary\r\n")
        texts, lossy = body_texts(self.parse(raw), False)
        self.assertTrue(lossy)

    def test_clean_multipart_is_not_lossy(self) -> None:
        raw = (b"From: a@example.com\r\nMessage-ID: <x@example.com>\r\n"
               b"Content-Type: multipart/mixed; boundary=\"b1\"\r\n\r\n"
               b"--b1\r\nContent-Type: text/plain\r\n\r\nan invoice\r\n--b1--\r\n")
        texts, lossy = body_texts(self.parse(raw), False)
        self.assertFalse(lossy)
        self.assertEqual(texts, ["an invoice"])


if __name__ == "__main__":
    unittest.main()
