"""Tests for mmgrep.source (milestone 2, Task 4).

Covers the `#source` to `.eml` path rule (FORMAT.md section 6, rule R7),
the account display form (design 6a, RPF11) and the part to message
mapping (FORMAT.md section 8, rule R3, design 6a RPF7). Stores are
synthetic, built with tests.fixtures.build_store and kept clean (no
malformed records) so every snapshot is stable. RFC 2606 domains only.
"""

from __future__ import annotations

import pathlib
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mmgrep.index import required_literal
from mmgrep.source import (SourceRef, account_display, eml_path,
                           message_of_part, parse_source)
from mmgrep import store as store_mod
from mmgrep.store import Store
from tests import fixtures
from tests.fixtures import FixtureMessage, FixturePart, build_store

ACCOUNT = "user%40example.com@imap.example.com"
BOB = "bob@mail.example.org"
ALICE = "alice%40example.com@imap.example.com"
REAL_READ = store_mod.read_file


class _TempStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, messages: list[FixtureMessage], names: list[str]):
        build_store(self.root, messages)
        store = Store(self.root)
        snap = store.snapshot(names, backoff=0)
        self.assertFalse(snap.unstable, snap.warnings)
        self.assertEqual(snap.skipped_records, {})
        return store, snap


class ParseSourceTest(unittest.TestCase):
    def test_format_examples(self) -> None:
        m = Path("/m")
        cases = [
            (b"imap://user%40example.com@imap.example.com/Archive/2024",
             "/m/IMAP/user%40example.com@imap.example.com/Archive.mailbox/"
             "2024.mailbox/Messages/1200.eml"),
            (b"imap://bob@mail.example.org/Sent Messages",
             "/m/IMAP/bob@mail.example.org/Sent Messages.mailbox/Messages/1200.eml"),
            (b"imap://alice%40example.com@imap.example.com/[Gmail]/All Mail",
             "/m/IMAP/alice%40example.com@imap.example.com/[Gmail].mailbox/"
             "All Mail.mailbox/Messages/1200.eml"),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                ref = parse_source(url)
                self.assertIsNotNone(ref)
                self.assertEqual(eml_path(m, ref, 1200), Path(expected))

    def test_parse_source_fields(self) -> None:
        self.assertEqual(
            parse_source(b"imap://user%40example.com@imap.example.com/Archive/2024"),
            SourceRef("imap", "user%40example.com@imap.example.com", "Archive/2024"))

    def test_non_imap_is_none(self) -> None:
        for url in (b"pop://bob@mail.example.org/INBOX", b"file:///x",
                    b"IMAP://bob@mail.example.org/INBOX", b"bob@mail.example.org/INBOX"):
            with self.subTest(url=url):
                self.assertIsNone(parse_source(url))

    def test_parse_source_rejects_unsafe(self) -> None:
        for url in (b"imap://../INBOX", b"imap://./INBOX", b"imap:///INBOX",
                    b"imap://bob@mail.example.org",
                    b"imap://bob@mail.example.org/",
                    b"imap://bob@mail.example.org/a//b",
                    b"imap://bob@mail.example.org/a\x00b",
                    b"imap://bob@mail.example.org/\xff",
                    b"imap://bob@mail.example.org/INBOX/",
                    b"imap://bob@mail.example.org//INBOX",
                    b"imap://", b""):
            with self.subTest(url=url):
                self.assertIsNone(parse_source(url))

    def test_dot_segments_stay_inside(self) -> None:
        # A mailbox segment ".." becomes "...mailbox", a plain name.
        ref = parse_source(b"imap://bob@mail.example.org/../x")
        self.assertIsNotNone(ref)
        path = eml_path(Path("/m"), ref, 7)
        self.assertEqual(path, Path("/m/IMAP/bob@mail.example.org/...mailbox/"
                                    "x.mailbox/Messages/7.eml"))
        self.assertNotIn("..", path.parts)

    def test_eml_path_rejects_unsafe_ref(self) -> None:
        for ref in (SourceRef("imap", "..", "INBOX"), SourceRef("imap", "", "INBOX"),
                    SourceRef("imap", BOB, ""), SourceRef("imap", BOB, "a//b"),
                    SourceRef("imap", "a/b", "INBOX"), SourceRef("pop", BOB, "INBOX"),
                    SourceRef("imap", BOB, "a\x00b")):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    eml_path(Path("/m"), ref, 1)

    def test_account_display(self) -> None:
        self.assertEqual(account_display(SourceRef("imap", ACCOUNT, "INBOX")),
                         "user@example.com@imap.example.com")
        self.assertEqual(account_display(SourceRef("imap", BOB, "INBOX")), BOB)
        # Only %40 is decoded; other escapes stay as stored.
        self.assertEqual(
            account_display(SourceRef("imap", "a%41b%2Fc@imap.example.com", "INBOX")),
            "a%41b%2Fc@imap.example.com")


class EmlPathFixtureTest(_TempStore):
    def test_eml_path_matches_fixture(self) -> None:
        msgs = [
            FixtureMessage(id=1200, account=ACCOUNT, mailbox="Archive/2024",
                           body=["one"]),
            FixtureMessage(id=1300, account=BOB, mailbox="Sent Messages",
                           body=["two"]),
            FixtureMessage(id=1400, account=ALICE, mailbox="[Gmail]/All Mail",
                           parts=[FixturePart(body=["three"])]),
            FixtureMessage(id=1500, account=BOB, mailbox="INBOX", body=["four"],
                           new=True),
        ]
        by_id = {m.id: m for m in msgs}
        store, snap = self.build(msgs, ["#source"])
        self.assertEqual(snap.live, frozenset(by_id))
        source = snap.indexes["#source"]
        for msg_id in sorted(snap.live):
            with self.subTest(id=msg_id):
                ref = parse_source(source.last(msg_id))
                self.assertIsNotNone(ref)
                path = eml_path(store.messages_dir, ref, msg_id)
                m = by_id[msg_id]
                self.assertEqual(path, fixtures.eml_path(self.root, m.account,
                                                         m.mailbox, msg_id))
                self.assertTrue(path.is_file())


class MessageOfPartTest(_TempStore):
    def test_message_of_part(self) -> None:
        msgs = [
            FixtureMessage(id=1200, account=ACCOUNT, mailbox="INBOX",
                           multipart="alternative",
                           parts=[FixturePart(body=["Hi there"]),
                                  FixturePart(content_type="text/html",
                                              body=["See you"])]),
            FixtureMessage(id=1300, account=ACCOUNT, mailbox="INBOX",
                           body=["single"]),
            FixtureMessage(id=1900, account=ACCOUNT, mailbox="INBOX",
                           parts=[FixturePart(id=1999, body=["gone"])],
                           deleted=True),
        ]
        _, snap = self.build(msgs, ["#source", "#root-body-part"])
        root = snap.indexes["#root-body-part"]
        self.assertEqual(root.last(1201), b"1200")
        self.assertEqual(root.last(1202), b"1200")
        self.assertEqual(root.last(1999), b"1900")
        self.assertEqual(snap.live, frozenset({1200, 1300}))
        self.assertEqual(message_of_part(snap, {1201, 1202, 1300, 1999, 5555}),
                         ({1200, 1300}, 2))
        self.assertEqual(message_of_part(snap, set()), (set(), 0))

    def test_message_of_part_without_root_index(self) -> None:
        msgs = [FixtureMessage(id=1300, account=ACCOUNT, mailbox="INBOX",
                               body=["single"])]
        _, snap = self.build(msgs, ["#source"])
        self.assertNotIn("#root-body-part", snap.indexes)
        self.assertEqual(message_of_part(snap, {1300, 1301, 5555}), ({1300}, 2))
        self.assertEqual(message_of_part(snap, set()), (set(), 0))


class _FakeIndex:
    def __init__(self, values: dict[int, bytes]) -> None:
        self._values = values

    def last(self, id: int) -> bytes | None:
        return self._values.get(id)


class _FakeSnapshot:
    def __init__(self, live: set[int], roots: dict[int, bytes]) -> None:
        self.live = frozenset(live)
        self.indexes = {"#root-body-part": _FakeIndex(roots)}


class MessageOfPartValuesTest(unittest.TestCase):
    def test_root_value_must_be_ascii_decimal(self) -> None:
        roots = {1: b"100", 2: b"", 3: b" 100", 4: b"100\n", 5: b"-100",
                 6: b"1e2", 7: b"\xd9\xa1", 8: b"0100", 9: b"200"}
        snap = _FakeSnapshot({100}, roots)
        self.assertEqual(message_of_part(snap, set(roots)), ({100}, 7))

    def test_oversized_root_value_is_unmapped(self) -> None:
        snap = _FakeSnapshot({100}, {1: b"9" * 5000})
        self.assertEqual(message_of_part(snap, {1}), (set(), 1))
        bounds = {1: b"4294967296", 2: b"00000000100", 3: b"4294967295"}
        snap = _FakeSnapshot({100, 4294967295}, bounds)
        self.assertEqual(message_of_part(snap, set(bounds)), ({4294967295}, 2))

    def test_live_id_maps_to_itself_before_root(self) -> None:
        snap = _FakeSnapshot({100, 300}, {300: b"100"})
        self.assertEqual(message_of_part(snap, {300}), ({300}, 0))


class BodySearchEndToEndTest(_TempStore):
    def test_body_search_end_to_end(self) -> None:
        msgs = [
            FixtureMessage(id=1200, account=ACCOUNT, mailbox="INBOX",
                           multipart="alternative",
                           parts=[FixturePart(body=["Hi there"]),
                                  FixturePart(content_type="text/html",
                                              body=["See you"])]),
            FixtureMessage(id=1300, account=ACCOUNT, mailbox="INBOX",
                           body=["there and back"]),
            FixtureMessage(id=1400, account=ACCOUNT, mailbox="INBOX",
                           body=["there too"], deleted=True),
        ]
        _, snap = self.build(msgs, ["#source", "#root-body-part", "#unquoted"])
        body = snap.indexes["#unquoted"]

        icase = re.compile("there", re.I | re.M)
        self.assertIsNone(required_literal(icase.pattern, icase.flags))
        hits = body.search(icase, None)
        self.assertEqual(hits, {1201, 1300, 1400})
        self.assertEqual(message_of_part(snap, hits), ({1200, 1300}, 1))

        exact = re.compile("there", re.M)
        literal = required_literal(exact.pattern, exact.flags)
        self.assertEqual(literal, b"there")
        hits = body.search(exact, literal)
        self.assertEqual(hits, {1201, 1300, 1400})
        self.assertEqual(message_of_part(snap, hits), ({1200, 1300}, 1))

    def test_body_search_unstable_without_root_index(self) -> None:
        msgs = [
            FixtureMessage(id=1200, account=ACCOUNT, mailbox="INBOX",
                           multipart="alternative",
                           parts=[FixturePart(body=["Hi there"]),
                                  FixturePart(content_type="text/html",
                                              body=["See you"])]),
            FixtureMessage(id=1300, account=ACCOUNT, mailbox="INBOX",
                           body=["there and back"]),
            FixtureMessage(id=1400, account=ACCOUNT, mailbox="INBOX",
                           body=["there too"], deleted=True),
        ]
        build_store(self.root, msgs)

        def no_root_cache(path: Path) -> bytes:
            if Path(path).name == "#root-body-part.cache":
                raise FileNotFoundError(path)
            return REAL_READ(path)

        with mock.patch("mmgrep.store.read_file", no_root_cache):
            snap = Store(self.root).snapshot(
                ["#source", "#root-body-part", "#unquoted"], backoff=0)
        self.assertTrue(snap.unstable)
        self.assertNotIn("#root-body-part", snap.indexes)
        hits = snap.indexes["#unquoted"].search(re.compile("there", re.M), b"there")
        self.assertEqual(hits, {1201, 1300, 1400})
        self.assertEqual(message_of_part(snap, hits), ({1300}, 2))


if __name__ == "__main__":
    unittest.main()
