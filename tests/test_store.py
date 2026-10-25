"""Tests for mmgrep.store (milestone 2, Task 3).

Stores are synthetic, built with tests.fixtures.build_store in temporary
directories. File reads are observed and altered through a double that
replaces mmgrep.store.read_file (plan decision D2). All values are
synthetic and use RFC 2606 domains.
"""

from __future__ import annotations

import pathlib
import re
import struct
import tempfile
import unittest
from typing import Callable, Optional
from unittest import mock

from mmgrep import store as store_mod
from mmgrep.store import FormatError, Store, parse_openstep_plist
from tests.fixtures import FixtureMessage, FixturePart, build_store

ACCOUNT = "user%40example.com@imap.example.com"
MAIN_EXAMPLE = (b'{\tUUID = "00000000-0000-4000-8000-000000000001";\n'
                b"\tlargestUsedBodyPartID = 1206;\n"
                b'\trevision = "1";\n\tversion = "1.1";\n}')
REAL_READ = store_mod.read_file

# A transform gets (file name, 1-based read count of that name, bytes) and
# returns the bytes to hand back, or raises OSError.
Transform = Callable[[str, int, bytes], bytes]


class Reads:
    """A read_file double that records file names and can alter results."""

    def __init__(self, transform: Optional[Transform] = None) -> None:
        self.names: list[str] = []
        self.counts: dict[str, int] = {}
        self.transform = transform

    def __call__(self, path: pathlib.Path) -> bytes:
        name = pathlib.Path(path).name
        self.names.append(name)
        count = self.counts[name] = self.counts.get(name, 0) + 1
        if self.transform is None:
            return REAL_READ(path)
        return self.transform(name, count, REAL_READ(path))

    def attempts(self) -> list[list[str]]:
        """The recorded names split into attempts at each closing Main.plist."""
        out: list[list[str]] = []
        current: list[str] = []
        mains = 0
        for name in self.names:
            current.append(name)
            if name == "Main.plist":
                mains += 1
                if mains % 2 == 0:
                    out.append(current)
                    current = []
        if current:
            out.append(current)
        return out


def msg(id: int, subject: str, **kw) -> FixtureMessage:
    headers = {"From": f"sender{id}@example.org", "Subject": subject,
               "Message-ID": f"<{id}@example.com>",
               "Date": "Tue, 05 Mar 2024 14:07:09 +0100"}
    headers.update(kw.pop("headers", {}))
    return FixtureMessage(id=id, account=ACCOUNT, mailbox="INBOX",
                          headers=headers, **kw)


THREE = [
    msg(1200, "Hello", parts=[FixturePart(body=["Hi there"]),
                              FixturePart(content_type="text/html", body=["See you"])],
        multipart="alternative"),
    msg(1300, "Second", body=["there and back"]),
    msg(1400, "Third", body=["one"]),
]


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.root = self.tmp / "store"

    def build(self, messages=None, **kw) -> Store:
        build_store(self.root, THREE if messages is None else messages, **kw)
        return Store(self.root)

    def snap(self, store: Store, names, reads: Optional[Reads] = None, **kw):
        kw.setdefault("backoff", 0)
        if reads is None:
            return store.snapshot(names, **kw)
        with mock.patch("mmgrep.store.read_file", reads):
            return store.snapshot(names, **kw)


class PlistTest(unittest.TestCase):
    def test_plist_examples(self) -> None:
        self.assertEqual(parse_openstep_plist(MAIN_EXAMPLE), {
            "UUID": "00000000-0000-4000-8000-000000000001",
            "largestUsedBodyPartID": 1206, "revision": "1", "version": "1.1"})
        self.assertEqual(
            parse_openstep_plist(b'{\toffsetsFileSize = 48;\n\tstringsFileSize = 34;\n}'),
            {"offsetsFileSize": 48, "stringsFileSize": 34})
        self.assertEqual(
            parse_openstep_plist(b'{\toffsetsFileSize = 48;\n\tstringsFileSize = "34";\n}'),
            {"offsetsFileSize": 48, "stringsFileSize": "34"})

    def test_plist_key_order_free(self) -> None:
        self.assertEqual(parse_openstep_plist(b'{\tb = 2;\n\ta = "x";\n}'),
                         {"a": "x", "b": 2})
        self.assertEqual(parse_openstep_plist(b"{}"), {})

    def test_plist_rejects(self) -> None:
        bad = [
            b"{\toffsetsFileSize = 48\n}",
            b"{\tversion = 1.1;\n}",
            b'{\tversion = "1.1";\n}x',
            b'{\tversion = "1.1";\n}\n',
            b'{\tversion = "1.1";\n\tversion = "1.2";\n}',
            b'{\tversion = "1\\.1";\n}',
            b"{\toffsetsFileSize = -48;\n}",
            b"",
            b'{\nversion = "1.1";\n}',
            b'{\tversion="1.1";\n}',
            b'{\tversion = "1.1"\n;}',
            b'{\t1version = "1.1";\n}',
            b'{\tversion = "1\n1";\n}',
            b'{\tversion = "1.1";\n',
            b'\t{version = "1.1";\n}',
        ]
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(FormatError):
                    parse_openstep_plist(data)

    def test_plist_integer_bounds(self) -> None:
        # Python 3.11+ refuses int() on more than 4300 digits with ValueError;
        # the parser must turn every oversized integer into FormatError.
        self.assertEqual(parse_openstep_plist(b"{\ta = 4294967295;\n}"), {"a": 4294967295})
        self.assertEqual(parse_openstep_plist(b"{\ta = 0000000001;\n}"), {"a": 1})
        for digits in (b"4294967296", b"00000000001", b"9" * 5000):
            with self.subTest(digits=digits[:12]):
                with self.assertRaises(FormatError):
                    parse_openstep_plist(b"{\ta = " + digits + b";\n}")


class SnapshotTest(StoreCase):
    def test_snapshot_oversized_sizes_retry(self) -> None:
        store = self.build()
        huge = b"9" * 5000
        cases = (
            (b"offsetsFileSize", huge),
            (b"stringsFileSize", huge),
            (b"stringsFileSize", b'"' + huge + b'"'),
            (b"stringsFileSize", b'"4294967296"'),
        )
        for key, bad in cases:
            with self.subTest(key=key, bad=bad[:12]):
                def oversized(name: str, count: int, data: bytes) -> bytes:
                    if name == "from.plist":
                        return re.sub(key + rb" = \S+;", key + b" = " + bad + b";", data)
                    return data

                snap = self.snap(store, ["from"], Reads(oversized))
                self.assertTrue(snap.unstable)
                self.assertEqual(snap.retries, 3)
                self.assertTrue(any("index from" in w for w in snap.warnings))

    def test_snapshot_clean(self) -> None:
        store = self.build()
        snap = self.snap(store, ["from", "subject", "#source", "#root-body-part"])
        self.assertFalse(snap.unstable)
        self.assertEqual(snap.retries, 0)
        self.assertEqual(snap.warnings, [])
        self.assertEqual(snap.skipped_records, {})
        self.assertEqual(snap.live, frozenset({1200, 1300, 1400}))
        self.assertEqual(sorted(snap.indexes),
                         ["#root-body-part", "#source", "from", "subject"])
        self.assertEqual(snap.indexes["subject"].values(1300), [b"Second"])
        self.assertEqual(snap.indexes["#root-body-part"].last(1201), b"1200")
        self.assertEqual(snap.main["UUID"], "00000000-0000-4000-8000-000000000001")
        self.assertEqual(snap.main["largestUsedBodyPartID"], 1400)

    def test_snapshot_read_order(self) -> None:
        store = self.build()
        reads = Reads()
        snap = self.snap(store, ["from"], reads)
        self.assertFalse(snap.unstable)
        self.assertEqual(reads.names, [
            "Main.plist", "Msgs.ids", "NewMsgs.ids", "from.plist", "from.offsets",
            "from.cache", "from.plist", "Main.plist"])

    def test_snapshot_torn_then_heals(self) -> None:
        store = self.build()

        def torn_once(name: str, count: int, data: bytes) -> bytes:
            return data[:-12] if name == "from.offsets" and count == 1 else data

        snap = self.snap(store, ["from"], Reads(torn_once))
        self.assertEqual(snap.retries, 1)
        self.assertFalse(snap.unstable)
        self.assertEqual(snap.warnings, [])
        self.assertEqual(snap.indexes["from"].values(1400), [b"sender1400@example.org"])

    def test_snapshot_persistent_torn(self) -> None:
        tail = struct.pack("<III", 1200, 0, 9999) + b"\x01\x02"
        store = self.build(torn={"subject": (b"", tail)})
        reads = Reads()
        snap = self.snap(store, ["subject"], reads)
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.retries, 3)
        self.assertEqual(reads.names.count("Main.plist"), 8)
        self.assertEqual(len(reads.attempts()), 4)
        self.assertEqual(snap.skipped_records, {"subject": 2})
        self.assertIsNone(snap.indexes["subject"].values(1200))
        self.assertEqual(snap.indexes["subject"].values(1300), [b"Second"])
        self.assertTrue(snap.warnings)
        self.assertTrue(any("subject" in w for w in snap.warnings))
        for w in snap.warnings:
            for value in ("Hello", "Second", "Third", "example"):
                self.assertNotIn(value, w)

    def test_snapshot_uuid_change(self) -> None:
        store = self.build()
        for key, old, new in (("UUID", b"000000000001", b"000000000002"),
                              ("revision", b'revision = "1"', b'revision = "2"')):
            with self.subTest(key=key):
                def change(name: str, count: int, data: bytes) -> bytes:
                    if name == "Main.plist" and count == 2:
                        changed = data.replace(old, new)
                        assert changed != data
                        return changed
                    return data

                snap = self.snap(store, ["from"], Reads(change))
                self.assertEqual(snap.retries, 1)
                self.assertFalse(snap.unstable)
                self.assertEqual(snap.warnings, [])

    def test_snapshot_plist_changes_between_reads(self) -> None:
        store = self.build()

        def requote(name: str, count: int, data: bytes) -> bytes:
            # Same sizes in the quoted form: Only the bytes differ.
            if name == "from.plist" and count == 2:
                head, size = data[:-3].rsplit(b"= ", 1)
                return head + b'= "' + size + b'";\n}'
            return data

        snap = self.snap(store, ["from"], Reads(requote))
        self.assertEqual(snap.retries, 1)
        self.assertFalse(snap.unstable)

    def test_snapshot_quoted_strings_size_is_stable(self) -> None:
        store = self.build(quoted_strings_size=["from"])
        snap = self.snap(store, ["from"])
        self.assertFalse(snap.unstable)
        self.assertEqual(snap.retries, 0)

    def test_snapshot_size_mismatch(self) -> None:
        store = self.build(plist_sizes={"from": (None, 1)})
        snap = self.snap(store, ["from"])
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.retries, 3)
        self.assertTrue(any("from" in w for w in snap.warnings))

    def test_snapshot_version_warning(self) -> None:
        store = self.build()

        def version(name: str, count: int, data: bytes) -> bytes:
            if name == "Main.plist":
                return data.replace(b'version = "1.1"', b'version = "1.2"')
            return data

        snap = self.snap(store, ["from"], Reads(version))
        self.assertFalse(snap.unstable)
        self.assertEqual(snap.retries, 0)
        self.assertEqual(len(snap.warnings), 1)
        self.assertIn("1.2", snap.warnings[0])

        def version_torn(name: str, count: int, data: bytes) -> bytes:
            if name == "from.offsets":
                return data[:-1]
            return version(name, count, data)

        snap = self.snap(store, ["from"], Reads(version_torn))
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.retries, 3)
        self.assertEqual(len([w for w in snap.warnings if "1.2" in w]), 1)

    def test_snapshot_partial_triple_is_unstable(self) -> None:
        store = self.build()

        def vanish(name: str, count: int, data: bytes) -> bytes:
            if name == "from.cache":
                raise FileNotFoundError(name)
            return data

        snap = self.snap(store, ["from", "subject"], Reads(vanish))
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.retries, 3)
        self.assertNotIn("from", snap.indexes)
        self.assertIn("subject", snap.indexes)
        self.assertTrue(any("from" in w for w in snap.warnings))

    def test_snapshot_absent_index_is_empty(self) -> None:
        store = self.build()
        snap = self.snap(store, ["from", "no-such-header"])
        self.assertFalse(snap.unstable)
        self.assertEqual(snap.retries, 0)
        absent = snap.indexes["no-such-header"]
        self.assertEqual(list(absent.ids()), [])
        for id in (0, 1200, 1300):
            self.assertIsNone(absent.values(id))
        self.assertEqual(snap.warnings, ["index no-such-header not present in this store"])
        self.assertEqual(snap.skipped_records, {})
        self.assertIn("from", snap.indexes)

    def test_snapshot_index_vanishes_is_unstable(self) -> None:
        store = self.build()
        reads = Reads()

        def gone(name: str, count: int, data: bytes) -> bytes:
            attempt = (reads.counts.get("Main.plist", 0) + 1) // 2
            if name.startswith("from.") and attempt >= 2:
                raise FileNotFoundError(name)
            if name == "subject.offsets" and count == 1:
                return data[:-12]
            return data

        reads.transform = gone
        snap = self.snap(store, ["from", "subject"], reads)
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.retries, 3)
        self.assertNotIn("from", snap.indexes)
        self.assertTrue(any("index from" in w for w in snap.warnings))
        self.assertNotIn("index from not present in this store", snap.warnings)

    def test_snapshot_index_appears_is_unstable(self) -> None:
        store = self.build()
        mains = [0]

        def appear(path: pathlib.Path) -> bytes:
            path = pathlib.Path(path)
            name = path.name
            if name == "Main.plist":
                mains[0] += 1
            attempt = (mains[0] + 1) // 2
            if name.startswith("late.") and attempt >= 2:
                return REAL_READ(path.with_name("from" + name[len("late"):]))
            data = REAL_READ(path)
            if name == "subject.offsets" and attempt == 1:
                return data[:-12]
            return data

        self.assertFalse((store.headers_dir / "late.plist").exists())
        with mock.patch("mmgrep.store.read_file", appear):
            snap = store.snapshot(["late", "subject"], backoff=0)
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.retries, 3)
        self.assertTrue(any("index late" in w for w in snap.warnings))
        self.assertNotIn("index late not present in this store", snap.warnings)
        # The last attempt read the triple, so its whole files are used (R11).
        self.assertIn("late", snap.indexes)

    def test_snapshot_index_seen_at_start_then_gone_is_unstable(self) -> None:
        store = self.build()
        real_seen = Store._triple_seen

        def seen(self_: Store, name: str) -> bool:
            return True if name == "ghost" else real_seen(self_, name)

        def gone(name: str, count: int, data: bytes) -> bytes:
            return data

        self.assertFalse((store.headers_dir / "ghost.plist").exists())
        with mock.patch.object(Store, "_triple_seen", seen):
            snap = self.snap(store, ["ghost", "from"], Reads(gone))
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.retries, 3)
        self.assertNotIn("ghost", snap.indexes)
        self.assertNotIn("index ghost not present in this store", snap.warnings)
        self.assertTrue(any("index ghost" in w for w in snap.warnings))

    def test_snapshot_backoff_schedule(self) -> None:
        store = self.build(plist_sizes={"from": (None, 1)})
        with mock.patch("mmgrep.store.time.sleep") as sleep:
            snap = store.snapshot(["from"], backoff=0.5)
        self.assertEqual(snap.retries, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.5, 1.0, 2.0])
        with mock.patch("mmgrep.store.time.sleep") as sleep:
            snap = store.snapshot(["from"], retries=1, backoff=0)
        self.assertEqual(snap.retries, 1)
        sleep.assert_not_called()

    def test_index_names_lists_triples(self) -> None:
        store = self.build()
        names = store.index_names()
        for name in ("from", "subject", "#source", "#unquoted", "#unquoted#lc",
                     "#root-body-part"):
            self.assertIn(name, names)
        (store.headers_dir / "x.plist").write_bytes(b"{}")
        (store.headers_dir / "y.cache").write_bytes(b"")
        (store.headers_dir / "y.offsets").write_bytes(b"")
        later = store.index_names()
        self.assertNotIn("x", later)
        self.assertNotIn("y", later)
        self.assertEqual(later, names)

        headers = self.tmp / "hand" / "Database.noindex" / "Headers"
        headers.mkdir(parents=True)
        for f in ("a.plist", "a.offsets", "a.cache", "b.plist", "c.cache", "c.offsets",
                  "d.e.plist", "d.e.offsets", "d.e.cache"):
            (headers / f).write_bytes(b"")
        self.assertEqual(Store(self.tmp / "hand").index_names(), {"a", "d.e"})
        self.assertEqual(Store(self.tmp / "nothing").index_names(), set())

    def test_snapshot_not_a_store_raises(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(FormatError) as cm:
            Store(empty).snapshot(["from"], backoff=0)
        self.assertIn("Main.plist", str(cm.exception))
        self.assertIn(str(empty), str(cm.exception))

        store = self.build()

        def absent(name: str, count: int, data: bytes) -> bytes:
            if name == "Main.plist":
                raise FileNotFoundError(name)
            return data

        reads = Reads(absent)
        with self.assertRaises(FormatError) as cm:
            self.snap(store, ["from"], reads)
        self.assertIn("Main.plist", str(cm.exception))
        self.assertIn(str(self.root), str(cm.exception))
        self.assertEqual(reads.names.count("Main.plist"), 8)

    def test_snapshot_rejects_bad_index_names(self) -> None:
        store = self.build()
        for bad in ("", ".", "..", "a/b", "/from", "../from", "fr\x00om"):
            with self.subTest(name=bad):
                reads = Reads()
                with self.assertRaises(ValueError):
                    self.snap(store, ["from", bad], reads)
                self.assertEqual(reads.names, [])
        self.assertIn("from", self.snap(store, ["from", "d.e"]).indexes)

    def test_snapshot_main_never_parses(self) -> None:
        store = self.build()

        def garbage(name: str, count: int, data: bytes) -> bytes:
            return b"garbage" if name == "Main.plist" else data

        with self.assertRaises(FormatError) as cm:
            self.snap(store, ["from"], Reads(garbage))
        self.assertIn("Main.plist", str(cm.exception))
        self.assertIn(str(self.root), str(cm.exception))

    def test_snapshot_main_lacks_keys(self) -> None:
        store = self.build()

        def no_uuid(name: str, count: int, data: bytes) -> bytes:
            return b'{\trevision = "1";\n\tversion = "1.1";\n}' if name == "Main.plist" else data

        with self.assertRaises(FormatError):
            self.snap(store, ["from"], Reads(no_uuid))

    def test_snapshot_main_parsed_once(self) -> None:
        store = self.build()

        def later_garbage(name: str, count: int, data: bytes) -> bytes:
            if name == "Main.plist" and count > 2:
                return b"garbage"
            if name == "from.offsets" and count == 1:
                return data[:-12]
            return data

        snap = self.snap(store, ["from"], Reads(later_garbage))
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.retries, 3)
        self.assertTrue(any("Main.plist" in w for w in snap.warnings))
        self.assertEqual(snap.main["revision"], "1")
        self.assertEqual(snap.indexes["from"].values(1200), [b"sender1200@example.org"])


class ValuesAndLiveTest(StoreCase):
    def test_index_values_from_fixture_store(self) -> None:
        messages = [
            msg(1200, "Current", stale={"subject": "Old"},
                headers={"To": ["bob@example.org", "carol@example.net"]}),
            msg(1300, "Second", raw={"subject": [(0, 9999)]}),
        ]
        store = self.build(messages)
        snap = self.snap(store, ["subject", "to", "from"])
        subject = snap.indexes["subject"]
        self.assertEqual(subject.values(1200), [b"Current"])
        self.assertEqual(subject.last(1200), b"Current")
        self.assertIsNone(subject.values(1300))
        self.assertEqual(snap.indexes["to"].values(1200),
                         [b"bob@example.org", b"carol@example.net"])
        self.assertEqual(snap.indexes["from"].values(1300), [b"sender1300@example.org"])
        self.assertEqual(snap.skipped_records, {"subject": 1})
        # The malformed record is an R11 retry trigger in every attempt.
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.retries, 3)
        self.assertTrue(any("subject" in w for w in snap.warnings))

    LIVE = [
        msg(1200, "One"),
        msg(1300, "Two", new=True),
        msg(1400, "Gone", deleted=True),
        msg(1500, "Orphan", orphan=True),
    ]

    def test_live_union(self) -> None:
        store = self.build(self.LIVE)
        snap = self.snap(store, ["from"])
        self.assertFalse(snap.unstable)
        self.assertEqual(snap.live, frozenset({1200, 1300}))
        # The orphan still has a current header value; it is not live.
        self.assertEqual(snap.indexes["from"].values(1500), [b"sender1500@example.org"])

    def test_live_fallback(self) -> None:
        store = self.build(self.LIVE)

        def extra_byte(name: str, count: int, data: bytes) -> bytes:
            return data + b"\x00" if name == "Msgs.ids" else data

        reads = Reads(extra_byte)
        snap = self.snap(store, ["from"], reads)
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.retries, 3)
        self.assertEqual(snap.live, frozenset({1200, 1300}))
        self.assertTrue(any("Msgs.ids" in w for w in snap.warnings))
        self.assertNotIn("#source", snap.indexes)
        self.assertEqual(reads.attempts()[-1], [
            "Main.plist", "Msgs.ids", "NewMsgs.ids",
            "from.plist", "from.offsets", "from.cache", "from.plist",
            "#source.plist", "#source.offsets", "#source.cache", "#source.plist",
            "Main.plist"])

    def test_live_fallback_requested_source_not_read_twice(self) -> None:
        store = self.build(self.LIVE)

        def extra_byte(name: str, count: int, data: bytes) -> bytes:
            return data + b"\x00" if name == "NewMsgs.ids" else data

        reads = Reads(extra_byte)
        snap = self.snap(store, ["#source"], reads, retries=0)
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.live, frozenset({1200, 1300}))
        self.assertEqual(reads.names.count("#source.offsets"), 1)

    def test_live_fallback_change_detected(self) -> None:
        store = self.build(self.LIVE)

        def bad_sets_last_change(name: str, count: int, data: bytes) -> bytes:
            if name == "Msgs.ids":
                return data + b"\x00"
            if name == "#source.plist" and count == 8:
                return data.replace(b"stringsFileSize = ", b"stringsFileSize = 0")
            return data

        snap = self.snap(store, ["from"], Reads(bad_sets_last_change))
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.retries, 3)
        self.assertTrue(any("Msgs.ids" in w for w in snap.warnings))
        self.assertTrue(any("#source" in w and "changed" in w for w in snap.warnings))

        def bad_once(name: str, count: int, data: bytes) -> bytes:
            if name == "Msgs.ids" and count == 1:
                return data + b"\x00"
            if name == "#source.plist" and count == 2:
                return data.replace(b"stringsFileSize = ", b"stringsFileSize = 0")
            return data

        reads = Reads(bad_once)
        snap = self.snap(store, ["from"], reads)
        self.assertEqual(snap.retries, 1)
        self.assertFalse(snap.unstable)
        self.assertEqual(snap.warnings, [])
        self.assertEqual(snap.live, frozenset({1200, 1300}))
        first = reads.attempts()[0]
        self.assertEqual(first[-5:], ["#source.plist", "#source.offsets",
                                      "#source.cache", "#source.plist", "Main.plist"])
        self.assertNotIn("#source.plist", reads.attempts()[1])

    def test_snapshot_never_uses_default_root(self) -> None:
        store = self.build()
        self.assertNotEqual(store.root, store_mod.DEFAULT_ROOT)
        self.assertEqual(Store().root, store_mod.DEFAULT_ROOT)

    def test_snapshot_partial_tail_alone(self) -> None:
        store = self.build()
        size = len((store.headers_dir / "from.offsets").read_bytes()) + 1

        def tail(name: str, count: int, data: bytes) -> bytes:
            if name == "from.offsets":
                return data + b"\x01"
            if name == "from.plist":
                return re.sub(rb"offsetsFileSize = \d+", b"offsetsFileSize = %d" % size, data)
            return data

        snap = self.snap(store, ["from"], Reads(tail))
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.retries, 3)
        self.assertEqual(snap.skipped_records, {"from": 1})
        self.assertTrue(any("from" in w for w in snap.warnings))

    def test_snapshot_non_digit_strings_size(self) -> None:
        store = self.build()
        size = len((store.headers_dir / "from.cache").read_bytes())
        # " <size>" would pass int() and the size check; only the digit rule rejects it.
        for bad in (b'"3a"', b'" %d"' % size):
            with self.subTest(bad=bad):
                def quoted(name: str, count: int, data: bytes) -> bytes:
                    if name == "from.plist":
                        return re.sub(rb"stringsFileSize = \S+;", b"stringsFileSize = " + bad + b";", data)
                    return data

                snap = self.snap(store, ["from"], Reads(quoted))
                self.assertTrue(snap.unstable)
                self.assertTrue(any("index from" in w for w in snap.warnings))

    def test_live_fallback_counts_source_records(self) -> None:
        messages = [msg(1200, "One", raw={"#source": [(0, 9999)]}), msg(1300, "Two")]
        store = self.build(messages)

        def extra_byte(name: str, count: int, data: bytes) -> bytes:
            return data + b"\x00" if name == "Msgs.ids" else data

        snap = self.snap(store, ["from"], Reads(extra_byte))
        self.assertTrue(snap.unstable)
        self.assertNotIn("#source", snap.indexes)
        self.assertEqual(snap.skipped_records, {"#source": 1})
        self.assertEqual(snap.live, frozenset({1300}))


if __name__ == "__main__":
    unittest.main()
