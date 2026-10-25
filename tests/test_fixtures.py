"""Tests for the synthetic store writer in tests/fixtures.py.

Every expected value is a literal (C4). The decoder below is independent of
the writer: It slices bytes with struct and never calls a fixtures helper.
"""

from __future__ import annotations

import email.parser
import email.policy
import os
import pathlib
import struct
import tempfile
import unittest
from typing import Optional
from unittest import mock

from tests import fixtures
from tests.fixtures import (Arrival, Delete, FixtureMessage, Move, Rewrite,
                            append_store, build_store)

ACCOUNT = "user@imap.example.com"
INBOX_EML = "Messages.noindex/IMAP/user@imap.example.com/INBOX.mailbox/Messages"
MAIN_PLIST_5 = (
    b'{\tUUID = "00000000-0000-4000-8000-000000000001";\n'
    b"\tlargestUsedBodyPartID = 5;\n"
    b'\trevision = "1";\n'
    b'\tversion = "1.1";\n}'
)


def _records(b: bytes) -> list[tuple[int, int, int]]:
    return list(struct.iter_unpack("<III", b))


def _value(cache: bytes, start: int, end: int) -> bytes:
    """cache[start:end], or ValueError when the bounds are invalid.

    Python slicing past the end returns short bytes instead of failing, so
    the bounds are checked first (FORMAT.md section 3).
    """
    if not start <= end <= len(cache):
        raise ValueError(f"record bounds ({start}, {end}) outside {len(cache)} bytes")
    return cache[start:end]


def _current(offsets: bytes, cache: bytes, msg_id: int) -> Optional[list[bytes]]:
    """Values of msg_id after its last (id, 1, 0) marker, in file order.

    None means no value: No record follows the last marker, the id has no
    records or any record after the last marker is malformed (all or
    nothing per id, design 6a). An earlier value is never exposed.
    """
    tail: list[tuple[int, int]] = []
    for rid, start, end in _records(offsets):
        if rid != msg_id:
            continue
        if (start, end) == (1, 0):
            tail = []
        else:
            tail.append((start, end))
    try:
        values = [_value(cache, start, end) for start, end in tail]
    except ValueError:
        return None
    return values or None


def _msg(msg_id: int, **kw) -> FixtureMessage:
    kw.setdefault("account", ACCOUNT)
    kw.setdefault("mailbox", "INBOX")
    kw.setdefault("headers", {})
    return FixtureMessage(id=msg_id, **kw)


def _parse(path: pathlib.Path):
    with open(path, "rb") as f:
        return email.parser.BytesParser(policy=email.policy.default).parse(f)


class FixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, messages, **kw) -> None:
        build_store(self.root, messages, **kw)

    def header(self, name: str, ext: str) -> bytes:
        path = self.root / "Database.noindex" / "Headers" / f"{name}.{ext}"
        return path.read_bytes()

    def header_exists(self, name: str) -> bool:
        path = self.root / "Database.noindex" / "Headers" / f"{name}.offsets"
        return path.exists()

    def ids_file(self, name: str) -> bytes:
        return (self.root / "Database.noindex" / "Sets" / name).read_bytes()

    def main_plist(self) -> bytes:
        return (self.root / "Database.noindex" / "Main.plist").read_bytes()

    # Records and plists

    def test_record_layout(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"})])
        self.assertEqual(self.header("subject", "offsets"),
                         bytes.fromhex("05000000 00000000 05000000"))
        self.assertEqual(self.header("subject", "cache"), b"Hello\n")
        self.assertEqual(self.header("subject", "plist"),
                         b"{\toffsetsFileSize = 12;\n\tstringsFileSize = 6;\n}")
        eml = (self.root / INBOX_EML / "5.eml").read_bytes()
        self.assertIn(b"Subject: Hello\n", eml)
        self.assertNotIn(b"\r", eml)

    def test_plist_quoted_strings_size(self) -> None:
        self.build([_msg(5, headers={"From": "alice@example.com",
                                     "Subject": "Hello"})],
                   quoted_strings_size={"subject"})
        self.assertEqual(self.header("subject", "plist"),
                         b'{\toffsetsFileSize = 12;\n\tstringsFileSize = "6";\n}')
        self.assertEqual(self.header("from", "plist"),
                         b"{\toffsetsFileSize = 12;\n\tstringsFileSize = 18;\n}")

    def test_shared_byte_range(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"}),
                    _msg(7, headers={"Subject": "Hello"}),
                    _msg(8, headers={"Subject": "Bye"})])
        self.assertEqual(self.header("subject", "offsets"), bytes.fromhex(
            "05000000 00000000 05000000"
            "07000000 00000000 05000000"
            "08000000 06000000 09000000"))
        self.assertEqual(self.header("subject", "cache"), b"Hello\nBye\n")

    def test_stale_record_precedes_current(self) -> None:
        self.build([_msg(5, headers={"Subject": "New"},
                         stale={"subject": "Old"})])
        offsets = self.header("subject", "offsets")
        cache = self.header("subject", "cache")
        self.assertEqual(offsets, bytes.fromhex(
            "05000000 00000000 03000000"
            "05000000 01000000 00000000"
            "05000000 04000000 07000000"))
        self.assertEqual(cache, b"Old\nNew\n")
        self.assertEqual(_current(offsets, cache, 5), [b"New"])

    def test_moved_message_source(self) -> None:
        self.build([_msg(5, mailbox="Archive", headers={"Subject": "Hi"},
                         stale={"#source": "imap://user@imap.example.com/INBOX"})])
        self.assertEqual(self.header("#source", "offsets"), bytes.fromhex(
            "05000000 00000000 22000000"
            "05000000 01000000 00000000"
            "05000000 23000000 47000000"))
        self.assertEqual(self.header("#source", "cache"),
                         b"imap://user@imap.example.com/INBOX\n"
                         b"imap://user@imap.example.com/Archive\n")

    def test_deleted_last_record_marker(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"}),
                    _msg(9, headers={"Subject": "Gone"}, deleted=True)])
        self.assertEqual(self.header("subject", "offsets"), bytes.fromhex(
            "05000000 00000000 05000000"
            "09000000 06000000 0a000000"
            "09000000 01000000 00000000"))
        self.assertEqual(self.header("subject", "cache"), b"Hello\nGone\n")
        self.assertEqual(self.header("#source", "offsets"), bytes.fromhex(
            "05000000 00000000 22000000"
            "09000000 00000000 22000000"
            "09000000 01000000 00000000"))
        self.assertEqual(self.header("#source", "cache"),
                         b"imap://user@imap.example.com/INBOX\n")
        self.assertTrue((self.root / INBOX_EML / "5.eml").exists())
        self.assertFalse((self.root / INBOX_EML / "9.eml").exists())
        self.assertEqual(self.ids_file("Msgs.ids"), bytes.fromhex("05000000"))

    def test_multi_valued_header(self) -> None:
        self.build([_msg(5, headers={"Message-ID": ["<a@example.com>",
                                                    "<b@example.com>"]},
                         stale={"message-id": "<old@example.com>"})])
        offsets = self.header("message-id", "offsets")
        cache = self.header("message-id", "cache")
        self.assertEqual(offsets, bytes.fromhex(
            "05000000 00000000 11000000"
            "05000000 01000000 00000000"
            "05000000 12000000 21000000"
            "05000000 22000000 31000000"))
        self.assertEqual(cache, b"<old@example.com>\n<a@example.com>\n"
                                b"<b@example.com>\n")
        self.assertEqual(_current(offsets, cache, 5),
                         [b"<a@example.com>", b"<b@example.com>"])
        msg = _parse(self.root / INBOX_EML / "5.eml")
        self.assertEqual([str(v) for v in msg.get_all("Message-ID")],
                         ["<a@example.com>", "<b@example.com>"])

    def test_fresh_range_skips_dedup(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"}),
                    _msg(7, headers={"Subject": "Hello"}, fresh={"subject"}),
                    _msg(8, headers={"Subject": "Hello"})])
        self.assertEqual(self.header("subject", "offsets"), bytes.fromhex(
            "05000000 00000000 05000000"
            "07000000 06000000 0b000000"
            "08000000 00000000 05000000"))
        self.assertEqual(self.header("subject", "cache"), b"Hello\nHello\n")

    def test_orphan_id_has_headers_only(self) -> None:
        self.build([_msg(5, headers={"From": "alice@example.com", "Subject": "Hi"}),
                    _msg(30, headers={"From": "bob@example.org",
                                      "Subject": "Inner"}, orphan=True)])
        self.assertEqual(self.header("from", "offsets"), bytes.fromhex(
            "05000000 00000000 11000000"
            "1e000000 12000000 21000000"))
        self.assertEqual(self.header("from", "cache"),
                         b"alice@example.com\nbob@example.org\n")
        self.assertEqual(self.header("subject", "offsets"), bytes.fromhex(
            "05000000 00000000 02000000"
            "1e000000 03000000 08000000"))
        self.assertEqual(self.header("subject", "cache"), b"Hi\nInner\n")
        self.assertEqual(self.header("#source", "offsets"),
                         bytes.fromhex("05000000 00000000 22000000"))
        self.assertEqual(self.ids_file("Msgs.ids"), bytes.fromhex("05000000"))
        self.assertEqual(self.ids_file("NewMsgs.ids"), b"")
        self.assertEqual(self.ids_file("Parts.ids"), bytes.fromhex("05000000"))
        self.assertEqual(self.ids_file("NewParts.ids"), b"")
        self.assertFalse((self.root / INBOX_EML / "30.eml").exists())

    def test_torn_tail_beyond_plist_sizes(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"})],
                   torn={"subject": b"\x05\x00\x00\x00XX"})
        offsets = self.header("subject", "offsets")
        self.assertEqual(offsets, bytes.fromhex(
            "05000000 00000000 05000000 05000000 5858"))
        self.assertEqual(len(offsets) % 12, 6)
        self.assertEqual(self.header("subject", "cache"),
                         b"Hello\n\x05\x00\x00\x00XX")
        self.assertEqual(self.header("subject", "plist"),
                         b"{\toffsetsFileSize = 12;\n\tstringsFileSize = 6;\n}")

    def test_torn_cache_grown_offsets_unchanged(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"})],
                   torn={"subject": (b"Later\n", b"")})
        self.assertEqual(self.header("subject", "cache"), b"Hello\nLater\n")
        self.assertEqual(self.header("subject", "offsets"),
                         bytes.fromhex("05000000 00000000 05000000"))
        self.assertEqual(self.header("subject", "plist"),
                         b"{\toffsetsFileSize = 12;\n\tstringsFileSize = 6;\n}")

    def test_torn_record_past_fence_into_cache_tail(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"})],
                   torn={"subject": (b"Later\n",
                                     bytes.fromhex("07000000 06000000 0b000000"))})
        self.assertEqual(self.header("subject", "cache"), b"Hello\nLater\n")
        self.assertEqual(self.header("subject", "offsets"), bytes.fromhex(
            "05000000 00000000 05000000"
            "07000000 06000000 0b000000"))
        self.assertEqual(self.header("subject", "plist"),
                         b"{\toffsetsFileSize = 12;\n\tstringsFileSize = 6;\n}")

    def test_file_shorter_than_plist_size(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"})],
                   plist_sizes={"subject": (24, 10)})
        self.assertEqual(self.header("subject", "plist"),
                         b"{\toffsetsFileSize = 24;\n\tstringsFileSize = 10;\n}")
        self.assertEqual(self.header("subject", "offsets"),
                         bytes.fromhex("05000000 00000000 05000000"))
        self.assertEqual(self.header("subject", "cache"), b"Hello\n")

    def test_malformed_record_in_committed_range(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"},
                         raw={"subject": [(3, 99)]})])
        self.assertEqual(self.header("subject", "offsets"), bytes.fromhex(
            "05000000 00000000 05000000"
            "05000000 03000000 63000000"))
        self.assertEqual(self.header("subject", "cache"), b"Hello\n")
        self.assertEqual(self.header("subject", "plist"),
                         b"{\toffsetsFileSize = 24;\n\tstringsFileSize = 6;\n}")

    def test_main_plist(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"})])
        self.assertEqual(self.main_plist(), MAIN_PLIST_5)

    def test_empty_store(self) -> None:
        self.build([])
        self.assertEqual(self.header("#source", "offsets"), b"")
        self.assertEqual(self.header("#source", "cache"), b"")
        self.assertEqual(self.header("#source", "plist"),
                         b"{\toffsetsFileSize = 0;\n\tstringsFileSize = 0;\n}")
        for name in ("Msgs.ids", "NewMsgs.ids", "Parts.ids", "NewParts.ids"):
            self.assertEqual(self.ids_file(name), b"")
        self.assertIn(b"\tlargestUsedBodyPartID = 0;\n", self.main_plist())

    # Value forms

    def test_non_ascii_offsets_are_bytes(self) -> None:
        raw = "=?UTF-8?Q?Gr=C3=BC=C3=9Fe?= aus Berlin"
        self.build([_msg(5, headers={"Subject": raw})])
        self.assertEqual(self.header("subject", "offsets"),
                         bytes.fromhex("05000000 00000000 12000000"))
        self.assertEqual(self.header("subject", "cache"),
                         b"Gr\xc3\xbc\xc3\x9fe aus Berlin\n")
        eml = (self.root / INBOX_EML / "5.eml").read_bytes()
        self.assertIn(b"Subject: =?UTF-8?Q?Gr=C3=BC=C3=9Fe?= aus Berlin\n", eml)

    def test_folded_subject(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello\r\n world"})])
        self.assertEqual(self.header("subject", "cache"), b"Hello world\n")
        eml = (self.root / INBOX_EML / "5.eml").read_bytes()
        self.assertIn(b"Subject: Hello\n world\n", eml)

    def test_from_value_form(self) -> None:
        self.build([_msg(5, headers={
            "From": '"Alice Example" <alice@example.com>',
            "To": "=?UTF-8?Q?J=C3=B6rg?= <jorg@example.org>, "
                  "bob@example.net (Bob)"})])
        self.assertEqual(self.header("from", "cache"),
                         b'"Alice Example" <alice@example.com>\n')
        self.assertEqual(self.header("to", "cache"),
                         b"J\xc3\xb6rg <jorg@example.org>, bob@example.net (Bob)\n")

    def test_cc_and_bcc_indexes(self) -> None:
        self.build([_msg(5, headers={"Cc": "carol@example.org",
                                     "Bcc": "dave@example.net"})])
        self.assertEqual(self.header("cc", "cache"), b"carol@example.org\n")
        self.assertEqual(self.header("bcc", "cache"), b"dave@example.net\n")
        self.assertFalse(self.header_exists("#cc"))

    def test_message_id_as_written(self) -> None:
        self.build([_msg(5, headers={"Message-ID": "<m1@example.com>"})])
        self.assertEqual(self.header("message-id", "offsets"),
                         bytes.fromhex("05000000 00000000 10000000"))
        self.assertEqual(self.header("message-id", "cache"),
                         b"<m1@example.com>\n")

    def test_date_form(self) -> None:
        self.build([_msg(5, headers={"Date": "Tue, 01 Oct 2024 12:00:00 +0200"})])
        self.assertEqual(self.header("date", "cache"),
                         b"Tue, 01 Oct 2024 12:00:00 +0200\n")
        self.assertEqual(self.header("#date", "offsets"),
                         bytes.fromhex("05000000 00000000 19000000"))
        self.assertEqual(self.header("#date", "cache"),
                         b"2024-10-01 12:00:00 +0200\n")
        self.assertEqual(self.header("#date-sent", "offsets"),
                         bytes.fromhex("05000000 00000000 13000000"))
        self.assertEqual(self.header("#date-sent", "cache"),
                         b"2024-10-01 10:00:00\n")
        self.assertEqual(self.header("#date-received", "cache"),
                         b"2024-10-01 10:00:00\n")

    def test_index_override_and_suppress(self) -> None:
        self.build([_msg(5, headers={"Date": "Tue, 01 Oct 2024 12:00:00 +0200",
                                     "Subject": "Written"},
                         index={"subject": "Indexed", "#date": None,
                                "#date-received": "2024-10-01 10:00:03"})])
        self.assertEqual(self.header("subject", "cache"), b"Indexed\n")
        self.assertFalse(self.header_exists("#date"))
        self.assertEqual(self.header("#date-received", "cache"),
                         b"2024-10-01 10:00:03\n")
        msg = _parse(self.root / INBOX_EML / "5.eml")
        self.assertEqual(str(msg["Subject"]), "Written")

    def test_date_minus_zero_zone(self) -> None:
        self.build([_msg(5, headers={"Date": "Tue, 01 Oct 2024 12:00:00 -0000"})])
        self.assertEqual(self.header("#date", "cache"),
                         b"2024-10-01 12:00:00 +0000\n")
        self.assertEqual(self.header("#date-sent", "cache"),
                         b"2024-10-01 12:00:00\n")

    def test_unknown_charset_word_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.build([_msg(5, headers={"Subject": "=?x-unknown?Q?a?="})])

    # Sources and paths

    def test_source_url_and_eml_path(self) -> None:
        self.build([
            _msg(42, account="user%40example.com@imap.example.com",
                 mailbox="Archive/2024", headers={"Subject": "Report"}),
            _msg(43, account="alice%40example.com@imap.example.com",
                 mailbox="[Gmail]/All Mail", headers={"Subject": "Other"}),
            _msg(44, account="user%40example.com@imap.example.com",
                 mailbox="Archive/2024", headers={"Subject": "Third"}),
        ])
        self.assertEqual(self.header("#source", "cache"),
                         b"imap://user%40example.com@imap.example.com/Archive/2024\n"
                         b"imap://alice%40example.com@imap.example.com/[Gmail]/All Mail\n")
        self.assertEqual(self.header("#source", "offsets"), bytes.fromhex(
            "2a000000 00000000 37000000"
            "2b000000 38000000 74000000"
            "2c000000 00000000 37000000"))
        p42 = self.root / ("Messages.noindex/IMAP/user%40example.com@imap.example.com/"
                           "Archive.mailbox/2024.mailbox/Messages/42.eml")
        p43 = self.root / ("Messages.noindex/IMAP/alice%40example.com@imap.example.com/"
                           "[Gmail].mailbox/All Mail.mailbox/Messages/43.eml")
        self.assertEqual(str(_parse(p42)["Subject"]), "Report")
        self.assertEqual(str(_parse(p43)["Subject"]), "Other")

    def test_write_eml_false(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hi"}, write_eml=False)])
        self.assertFalse((self.root / INBOX_EML / "5.eml").exists())
        self.assertEqual(self.ids_file("Msgs.ids"), bytes.fromhex("05000000"))
        self.assertEqual(self.header("#source", "offsets"),
                         bytes.fromhex("05000000 00000000 22000000"))

    def test_duplicate_copy(self) -> None:
        self.build([_msg(5, headers={"Message-ID": "<d@example.com>",
                                     "Subject": "Dup"}, body=["Text"]),
                    _msg(8, mailbox="Archive", duplicate_of=5)])
        self.assertEqual(self.header("message-id", "offsets"), bytes.fromhex(
            "05000000 00000000 0f000000"
            "08000000 00000000 0f000000"))
        self.assertEqual(self.header("#unquoted", "offsets"), bytes.fromhex(
            "05000000 00000000 04000000"
            "08000000 00000000 04000000"))
        e5 = (self.root / INBOX_EML / "5.eml").read_bytes()
        e8 = (self.root / "Messages.noindex/IMAP/user@imap.example.com/"
              "Archive.mailbox/Messages/8.eml").read_bytes()
        self.assertEqual(e5, e8)
        self.assertEqual(str(_parse(self.root / INBOX_EML / "5.eml")["Subject"]),
                         "Dup")

    def test_duplicate_chain_resolves(self) -> None:
        self.build([_msg(5, headers={"Subject": "Dup"}),
                    _msg(6, mailbox="Archive", duplicate_of=5),
                    _msg(7, mailbox="Sent", duplicate_of=6)])
        self.assertEqual(self.header("subject", "offsets"), bytes.fromhex(
            "05000000 00000000 03000000"
            "06000000 00000000 03000000"
            "07000000 00000000 03000000"))
        self.assertEqual(self.header("subject", "cache"), b"Dup\n")

    def test_eml_lf_default(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"}, body=["Hi"])])
        self.assertEqual((self.root / INBOX_EML / "5.eml").read_bytes(),
                         b"Subject: Hello\nMIME-Version: 1.0\n"
                         b"Content-Type: text/plain; charset=utf-8\n"
                         b"Content-Transfer-Encoding: 8bit\n\nHi\n")

    def test_eml_crlf_option(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"}, body=["Hi"])],
                   eml_newline="\r\n")
        self.assertEqual((self.root / INBOX_EML / "5.eml").read_bytes(),
                         b"Subject: Hello\r\nMIME-Version: 1.0\r\n"
                         b"Content-Type: text/plain; charset=utf-8\r\n"
                         b"Content-Transfer-Encoding: 8bit\r\n\r\nHi\r\n")

    # Appends

    def test_append_events_extend_files_in_place(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"}),
                    _msg(7, headers={"Subject": "World"})])
        old_offsets = self.header("subject", "offsets")
        old_cache = self.header("subject", "cache")
        old_source = self.header("#source", "offsets")
        headers_dir = self.root / "Database.noindex" / "Headers"
        inodes = [os.stat(headers_dir / n).st_ino
                  for n in ("subject.cache", "subject.offsets")]

        append_store(self.root, [Arrival(_msg(9, headers={"Subject": "Hello"})),
                                 Rewrite(7, "subject", "Again"),
                                 Delete(5)])

        offsets = self.header("subject", "offsets")
        cache = self.header("subject", "cache")
        self.assertEqual(old_offsets, bytes.fromhex(
            "05000000 00000000 05000000"
            "07000000 06000000 0b000000"))
        self.assertEqual(old_cache, b"Hello\nWorld\n")
        self.assertEqual(offsets, bytes.fromhex(
            "05000000 00000000 05000000"
            "07000000 06000000 0b000000"
            "09000000 00000000 05000000"
            "07000000 01000000 00000000"
            "07000000 0c000000 11000000"
            "05000000 01000000 00000000"))
        self.assertEqual(cache, b"Hello\nWorld\nAgain\n")
        self.assertTrue(offsets.startswith(old_offsets))
        self.assertTrue(cache.startswith(old_cache))
        self.assertEqual(_current(offsets, cache, 7), [b"Again"])
        self.assertIsNone(_current(offsets, cache, 5))
        self.assertEqual(self.header("subject", "plist"),
                         b"{\toffsetsFileSize = 72;\n\tstringsFileSize = 18;\n}")
        source = self.header("#source", "offsets")
        self.assertTrue(source.startswith(old_source))
        self.assertEqual(source, bytes.fromhex(
            "05000000 00000000 22000000"
            "07000000 00000000 22000000"
            "09000000 00000000 22000000"
            "05000000 01000000 00000000"))
        self.assertEqual(self.header("#source", "cache"),
                         b"imap://user@imap.example.com/INBOX\n")
        self.assertEqual([os.stat(headers_dir / n).st_ino
                          for n in ("subject.cache", "subject.offsets")], inodes)
        self.assertEqual(self.ids_file("Msgs.ids"),
                         bytes.fromhex("07000000 09000000"))
        self.assertEqual(self.ids_file("Parts.ids"),
                         bytes.fromhex("07000000 09000000"))
        self.assertEqual(self.ids_file("NewMsgs.ids"), b"")
        self.assertFalse((self.root / INBOX_EML / "5.eml").exists())
        self.assertEqual(str(_parse(self.root / INBOX_EML / "9.eml")["Subject"]),
                         "Hello")
        self.assertEqual(self.main_plist(),
                         b'{\tUUID = "00000000-0000-4000-8000-000000000001";\n'
                         b"\tlargestUsedBodyPartID = 9;\n"
                         b'\trevision = "1";\n'
                         b'\tversion = "1.1";\n}')

    def test_append_move(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hi"})])
        append_store(self.root, [Move(5, ACCOUNT, "Archive")])
        self.assertEqual(self.header("#source", "offsets"), bytes.fromhex(
            "05000000 00000000 22000000"
            "05000000 01000000 00000000"
            "05000000 23000000 47000000"))
        self.assertEqual(self.header("#source", "cache"),
                         b"imap://user@imap.example.com/INBOX\n"
                         b"imap://user@imap.example.com/Archive\n")
        self.assertFalse((self.root / INBOX_EML / "5.eml").exists())
        moved = self.root / ("Messages.noindex/IMAP/user@imap.example.com/"
                             "Archive.mailbox/Messages/5.eml")
        self.assertEqual(str(_parse(moved)["Subject"]), "Hi")
        self.assertEqual(self.ids_file("Msgs.ids"), bytes.fromhex("05000000"))

    def test_append_new_arrival_keeps_quoted_plist(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"})],
                   quoted_strings_size={"subject"})
        append_store(self.root, [Arrival(_msg(11, headers={"Subject": "World"},
                                              new=True))])
        self.assertEqual(self.header("subject", "offsets"), bytes.fromhex(
            "05000000 00000000 05000000"
            "0b000000 06000000 0b000000"))
        self.assertEqual(self.header("subject", "cache"), b"Hello\nWorld\n")
        self.assertEqual(self.header("subject", "plist"),
                         b'{\toffsetsFileSize = 24;\n\tstringsFileSize = "12";\n}')
        self.assertEqual(self.ids_file("Msgs.ids"), bytes.fromhex("05000000"))
        self.assertEqual(self.ids_file("Parts.ids"), bytes.fromhex("05000000"))
        self.assertEqual(self.ids_file("NewMsgs.ids"), bytes.fromhex("0b000000"))
        self.assertEqual(self.ids_file("NewParts.ids"), bytes.fromhex("0b000000"))

    def test_append_delete_drops_parts(self) -> None:
        part = FixtureMessage.Part
        self.build([_msg(5, parts=[part(id=1005, body=["Hi"])]),
                    _msg(7, headers={"Subject": "X"})])
        append_store(self.root, [Delete(5)])
        self.assertEqual(self.ids_file("Msgs.ids"), bytes.fromhex("07000000"))
        self.assertEqual(self.ids_file("Parts.ids"), bytes.fromhex("07000000"))
        self.assertEqual(self.header("#unquoted", "offsets"),
                         bytes.fromhex("ed030000 00000000 02000000"))
        self.assertEqual(self.header("#source", "offsets"), bytes.fromhex(
            "05000000 00000000 22000000"
            "07000000 00000000 22000000"
            "05000000 01000000 00000000"))

    def test_append_to_torn_store_rejected(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"})],
                   torn={"subject": b"\x05\x00\x00\x00XX"})
        with self.assertRaises(ValueError):
            append_store(self.root, [Delete(5)])

    def test_append_order_data_then_plists_then_main(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello",
                                     "From": "alice@example.com"})])
        calls: list[tuple[str, str]] = []
        real_open = open
        real_replace = os.replace

        def record_open(file, mode="r", *args, **kwargs):
            calls.append((mode, pathlib.Path(file).name))
            return real_open(file, mode, *args, **kwargs)

        def record_replace(src, dst, *args, **kwargs):
            calls.append(("replace", pathlib.Path(dst).name))
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch.object(fixtures, "open", record_open, create=True), \
                mock.patch.object(fixtures.os, "replace", record_replace):
            append_store(self.root, [Rewrite(5, "subject", "Hi"),
                                     Rewrite(5, "from", "bob@example.org")])
        self.assertEqual(calls, [
            ("ab", "subject.cache"), ("ab", "subject.offsets"),
            ("ab", "from.cache"), ("ab", "from.offsets"),
            ("replace", "subject.plist"), ("replace", "from.plist"),
            ("replace", "Main.plist")])
        self.assertEqual(self.header("from", "cache"),
                         b"alice@example.com\nbob@example.org\n")

    # Current values (all or nothing per id, design 6a)

    def test_malformed_current_record_hides_earlier_value(self) -> None:
        self.build([_msg(5, stale={"subject": "Old"},
                         raw={"subject": [(3, 99)]})])
        offsets = self.header("subject", "offsets")
        cache = self.header("subject", "cache")
        self.assertEqual(offsets, bytes.fromhex(
            "05000000 00000000 03000000"
            "05000000 01000000 00000000"
            "05000000 03000000 63000000"))
        self.assertEqual(cache, b"Old\n")
        # Plain slicing would hide the bad bounds behind a short value.
        self.assertEqual(cache[3:99], b"\n")
        with self.assertRaises(ValueError):
            _value(cache, 3, 99)
        self.assertIsNone(_current(offsets, cache, 5))

    def test_malformed_source_record_blocks_move(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hi"},
                         raw={"#source": [(0, 99)]})])
        before = self.header("#source", "offsets")
        self.assertEqual(before, bytes.fromhex(
            "05000000 00000000 22000000"
            "05000000 00000000 63000000"))
        self.assertIsNone(_current(before, self.header("#source", "cache"), 5))
        with self.assertRaises(ValueError):
            append_store(self.root, [Move(5, ACCOUNT, "Archive")])
        self.assertEqual(self.header("#source", "offsets"), before)
        self.assertTrue((self.root / INBOX_EML / "5.eml").exists())

    def test_malformed_record_before_marker_keeps_replacement(self) -> None:
        url = "imap://user@imap.example.com/INBOX"
        self.build([_msg(5, headers={"Subject": "Old"},
                         raw={"subject": [(3, 99)], "#source": [(0, 99)]})])
        append_store(self.root, [Rewrite(5, "subject", "New"),
                                 Rewrite(5, "#source", url)])
        offsets = self.header("subject", "offsets")
        cache = self.header("subject", "cache")
        self.assertEqual(offsets, bytes.fromhex(
            "05000000 00000000 03000000"
            "05000000 03000000 63000000"
            "05000000 01000000 00000000"
            "05000000 04000000 07000000"))
        self.assertEqual(cache, b"Old\nNew\n")
        self.assertEqual(_current(offsets, cache, 5), [b"New"])
        self.assertEqual(self.header("#source", "offsets"), bytes.fromhex(
            "05000000 00000000 22000000"
            "05000000 00000000 63000000"
            "05000000 01000000 00000000"
            "05000000 00000000 22000000"))
        # Move sees the valid #source replacement.
        append_store(self.root, [Move(5, ACCOUNT, "Archive")])
        self.assertEqual(self.header("#source", "offsets"), bytes.fromhex(
            "05000000 00000000 22000000"
            "05000000 00000000 63000000"
            "05000000 01000000 00000000"
            "05000000 00000000 22000000"
            "05000000 01000000 00000000"
            "05000000 23000000 47000000"))
        self.assertFalse((self.root / INBOX_EML / "5.eml").exists())
        # Delete sees the valid subject replacement and clears it.
        append_store(self.root, [Delete(5)])
        self.assertEqual(self.header("subject", "offsets"), bytes.fromhex(
            "05000000 00000000 03000000"
            "05000000 03000000 63000000"
            "05000000 01000000 00000000"
            "05000000 04000000 07000000"
            "05000000 01000000 00000000"))

    def test_marker_only_tail_has_no_value(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"})])
        append_store(self.root, [Rewrite(5, "subject", None),
                                 Rewrite(5, "#source", None)])
        offsets = self.header("subject", "offsets")
        cache = self.header("subject", "cache")
        self.assertEqual(offsets, bytes.fromhex(
            "05000000 00000000 05000000"
            "05000000 01000000 00000000"))
        self.assertEqual(cache, b"Hello\n")
        self.assertIsNone(_current(offsets, cache, 5))
        with self.assertRaises(ValueError):
            append_store(self.root, [Move(5, ACCOUNT, "Archive")])

    def test_marker_plus_replacement_tail(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hello"})])
        append_store(self.root, [Rewrite(5, "subject", "New")])
        offsets = self.header("subject", "offsets")
        cache = self.header("subject", "cache")
        self.assertEqual(offsets, bytes.fromhex(
            "05000000 00000000 05000000"
            "05000000 01000000 00000000"
            "05000000 06000000 09000000"))
        self.assertEqual(cache, b"Hello\nNew\n")
        self.assertEqual(_current(offsets, cache, 5), [b"New"])

    def test_empty_current_record_is_empty_value(self) -> None:
        self.build([_msg(5, stale={"subject": "Old"}, index={"subject": ""})])
        offsets = self.header("subject", "offsets")
        cache = self.header("subject", "cache")
        self.assertEqual(offsets, bytes.fromhex(
            "05000000 00000000 03000000"
            "05000000 01000000 00000000"
            "05000000 04000000 04000000"))
        self.assertEqual(cache, b"Old\n\n")
        self.assertEqual(_current(offsets, cache, 5), [b""])
        # The empty value is a value, so Delete clears it with a marker.
        append_store(self.root, [Delete(5)])
        offsets = self.header("subject", "offsets")
        self.assertEqual(offsets, bytes.fromhex(
            "05000000 00000000 03000000"
            "05000000 01000000 00000000"
            "05000000 04000000 04000000"
            "05000000 01000000 00000000"))
        self.assertIsNone(_current(offsets, cache, 5))

    # Sets

    def test_live_set_ids(self) -> None:
        self.build([_msg(7, headers={"Subject": "A"}),
                    _msg(5, headers={"Subject": "B"}),
                    _msg(9, headers={"Subject": "C"}, deleted=True),
                    _msg(11, headers={"Subject": "D"}, new=True)])
        self.assertEqual(self.ids_file("Msgs.ids"),
                         bytes.fromhex("05000000 07000000"))
        self.assertEqual(self.ids_file("NewMsgs.ids"), bytes.fromhex("0b000000"))
        self.assertEqual(self.ids_file("NewParts.ids"), bytes.fromhex("0b000000"))
        self.assertEqual(self.ids_file("Parts.ids"),
                         bytes.fromhex("05000000 07000000"))

    # Bodies and parts

    def test_body_segments(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hi"},
                         body=["Hi there", "See you"], quoted=["earlier"])])
        self.assertEqual(self.header("#unquoted", "offsets"), bytes.fromhex(
            "05000000 00000000 08000000"
            "05000000 09000000 10000000"))
        self.assertEqual(self.header("#unquoted", "cache"), b"Hi there\nSee you\n")
        self.assertEqual(self.header("#unquoted#lc", "offsets"), bytes.fromhex(
            "05000000 00000000 08000000"
            "05000000 09000000 10000000"))
        self.assertEqual(self.header("#unquoted#lc", "cache"),
                         b"hi there\nsee you\n")
        self.assertEqual(self.header("#quoted", "offsets"),
                         bytes.fromhex("05000000 00000000 07000000"))
        self.assertEqual(self.header("#quoted", "cache"), b"earlier\n")
        self.assertEqual(self.header("#quoted#lc", "cache"), b"earlier\n")
        self.assertFalse(self.header_exists("#root-body-part"))
        self.assertEqual(self.ids_file("Parts.ids"), bytes.fromhex("05000000"))
        msg = _parse(self.root / INBOX_EML / "5.eml")
        self.assertEqual(msg.get_content_type(), "text/plain")
        self.assertEqual(msg.get_content().replace("\r\n", "\n"),
                         "Hi there\n\nSee you\n\n> earlier\n")

    def test_single_part_html(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hi"}, content_type="text/html",
                         body=["Hi there"])])
        self.assertEqual(self.header("#unquoted", "offsets"),
                         bytes.fromhex("05000000 00000000 08000000"))
        self.assertEqual(self.header("#unquoted", "cache"), b"Hi there\n")
        self.assertFalse(self.header_exists("content-type"))
        eml = (self.root / INBOX_EML / "5.eml").read_bytes()
        self.assertEqual(eml.count(b"Content-Type:"), 1)
        msg = _parse(self.root / INBOX_EML / "5.eml")
        self.assertEqual(msg.get_content_type(), "text/html")
        self.assertEqual(msg.get_content().replace("\r\n", "\n"),
                         "<html><body><p>Hi there</p></body></html>\n")

    def test_declared_charset_encodes_text(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hi"}, charset="iso-8859-1",
                         body=["café"])])
        self.assertEqual(self.header("#unquoted", "cache"), b"caf\xc3\xa9\n")
        self.assertEqual(self.header("#unquoted", "offsets"),
                         bytes.fromhex("05000000 00000000 05000000"))
        eml = (self.root / INBOX_EML / "5.eml").read_bytes()
        self.assertIn(b"\n\ncaf\xe9\n", eml)
        msg = _parse(self.root / INBOX_EML / "5.eml")
        self.assertEqual(msg.get_content_charset(), "iso-8859-1")
        self.assertEqual(msg.get_content().replace("\r\n", "\n"), "café\n")

    def test_multipart_explicit_part_ids(self) -> None:
        part = FixtureMessage.Part
        self.build([_msg(5, headers={"Subject": "Hi"}, multipart="alternative",
                         parts=[part(id=1005, body=["Hi there", "See you"]),
                                part(id=1006, content_type="text/html",
                                     content="<p>Hi there</p><p>See you</p>")])])
        self.assertEqual(self.header("#unquoted", "offsets"), bytes.fromhex(
            "ed030000 00000000 08000000"
            "ed030000 09000000 10000000"))
        self.assertEqual(self.header("#unquoted", "cache"), b"Hi there\nSee you\n")
        self.assertEqual(self.header("#root-body-part", "offsets"), bytes.fromhex(
            "ed030000 00000000 01000000"
            "ee030000 00000000 01000000"))
        self.assertEqual(self.header("#root-body-part", "cache"), b"5\n")
        self.assertEqual(self.header("#parent-body-part", "offsets"), bytes.fromhex(
            "ed030000 00000000 01000000"
            "ee030000 00000000 01000000"))
        self.assertEqual(self.header("#parent-body-part", "cache"), b"5\n")
        self.assertEqual(self.header("#subpart-number", "offsets"), bytes.fromhex(
            "ed030000 00000000 01000000"
            "ee030000 02000000 03000000"))
        self.assertEqual(self.header("#subpart-number", "cache"), b"0\n1\n")
        self.assertEqual(self.ids_file("Parts.ids"),
                         bytes.fromhex("05000000 ed030000 ee030000"))
        self.assertIn(b"\tlargestUsedBodyPartID = 1006;\n", self.main_plist())

    def test_nested_multipart_mime_structure(self) -> None:
        part = FixtureMessage.Part
        self.build([_msg(20, headers={"Subject": "Nested",
                                      "From": "alice@example.com"},
                         parts=[part(content_type="multipart/alternative", parts=[
                                    part(body=["Hello"]),
                                    part(content_type="text/html",
                                         content="<p>Hello</p>")]),
                                part(content_type="application/pdf",
                                     content=b"%PDF-1.4 synthetic\n",
                                     filename="a.pdf")])])
        self.assertEqual(self.header("#root-body-part", "offsets"), bytes.fromhex(
            "15000000 00000000 02000000"
            "16000000 00000000 02000000"
            "17000000 00000000 02000000"
            "18000000 00000000 02000000"))
        self.assertEqual(self.header("#root-body-part", "cache"), b"20\n")
        self.assertEqual(self.header("#parent-body-part", "offsets"), bytes.fromhex(
            "15000000 00000000 02000000"
            "16000000 03000000 05000000"
            "17000000 03000000 05000000"
            "18000000 00000000 02000000"))
        self.assertEqual(self.header("#parent-body-part", "cache"), b"20\n21\n")
        self.assertEqual(self.header("#subpart-number", "offsets"), bytes.fromhex(
            "15000000 00000000 01000000"
            "16000000 00000000 01000000"
            "17000000 02000000 03000000"
            "18000000 02000000 03000000"))
        self.assertEqual(self.header("#subpart-number", "cache"), b"0\n1\n")
        self.assertEqual(self.header("#unquoted", "offsets"),
                         bytes.fromhex("16000000 00000000 05000000"))
        self.assertEqual(self.header("#unquoted", "cache"), b"Hello\n")
        self.assertEqual(self.ids_file("Parts.ids"), bytes.fromhex(
            "14000000 15000000 16000000 17000000 18000000"))

        msg = _parse(self.root / INBOX_EML / "20.eml")
        self.assertEqual(str(msg["Subject"]), "Nested")
        self.assertEqual(str(msg["From"]), "alice@example.com")
        self.assertEqual(msg.get_content_type(), "multipart/mixed")
        top = list(msg.iter_parts())
        self.assertEqual([p.get_content_type() for p in top],
                         ["multipart/alternative", "application/pdf"])
        alt = list(top[0].iter_parts())
        self.assertEqual([p.get_content_type() for p in alt],
                         ["text/plain", "text/html"])
        self.assertEqual(alt[0].get_content().replace("\r\n", "\n"), "Hello\n")
        self.assertEqual(alt[1].get_content().replace("\r\n", "\n"),
                         "<p>Hello</p>\n")
        self.assertEqual(top[1].get_filename(), "a.pdf")
        self.assertEqual(top[1].get_content(), b"%PDF-1.4 synthetic\n")

    def test_part_id_collision_rejected(self) -> None:
        part = FixtureMessage.Part
        with self.assertRaises(ValueError):
            self.build([_msg(5, parts=[part(id=7, body=["A"])]),
                        _msg(7, headers={"Subject": "B"})])

    def test_invalid_utf8_body_segment(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hi"}, body=[b"caf\xe9"])])
        self.assertEqual(self.header("#unquoted", "offsets"),
                         bytes.fromhex("05000000 00000000 04000000"))
        self.assertEqual(self.header("#unquoted", "cache"), b"caf\xe9\n")
        self.assertEqual(self.header("#unquoted#lc", "offsets"),
                         bytes.fromhex("05000000 00000000 00000000"))
        self.assertEqual(self.header("#unquoted#lc", "cache"), b"\n")
        eml = (self.root / INBOX_EML / "5.eml").read_bytes()
        self.assertIn(b"\n\ncaf\xe9\n", eml)

    def test_lc_twin_drops_bom(self) -> None:
        self.build([_msg(5, headers={"Subject": "Hi"}, body=["\ufeffHello"])])
        self.assertEqual(self.header("#unquoted", "cache"),
                         b"\xef\xbb\xbfHello\n")
        self.assertEqual(self.header("#unquoted", "offsets"),
                         bytes.fromhex("05000000 00000000 08000000"))
        self.assertEqual(self.header("#unquoted#lc", "cache"), b"hello\n")
        self.assertEqual(self.header("#unquoted#lc", "offsets"),
                         bytes.fromhex("05000000 00000000 05000000"))


if __name__ == "__main__":
    unittest.main()
