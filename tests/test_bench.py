"""Tests for tools/bench.py (milestone 2, Task 6).

The bench writer (plan decision D6, ruling RPM2) is checked against explicit
records decoded here with struct.iter_unpack, independent of mmgrep.index.
The expected records were printed once from a scratchpad run and checked by
hand against docs/FORMAT.md section 3. All values are synthetic and use RFC
2606 domains.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import struct
import tempfile
import unittest
from unittest import mock

from mmgrep.index import required_literal
from mmgrep.store import Store

ROOT = pathlib.Path(__file__).resolve().parent.parent
BENCH_PATH = ROOT / "tools" / "bench.py"
TINY_SCALE = 0.00002
SEED = 20261025


def _load_bench():
    spec = importlib.util.spec_from_file_location("mmgrep_tools_bench", BENCH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load_bench()

# Per index: (plist bytes, [(id, start, end, value or None for a marker)]).
EXPECTED = {
    '#date': (
        b'{\toffsetsFileSize = 48;\n\tstringsFileSize = 104;\n}',
        [
            (1000, 0, 25, b'2024-01-01 00:00:00 +0000'),
            (1003, 26, 51, b'2024-01-01 01:10:13 +0100'),
            (1006, 52, 77, b'2023-12-31 19:20:26 -0500'),
            (1009, 78, 103, b'2024-01-01 06:00:39 +0530'),
        ],
    ),
    '#root-body-part': (
        b'{\toffsetsFileSize = 96;\n\tstringsFileSize = 20;\n}',
        [
            (1001, 0, 4, b'1000'),
            (1002, 0, 4, b'1000'),
            (1004, 5, 9, b'1003'),
            (1005, 5, 9, b'1003'),
            (1007, 10, 14, b'1006'),
            (1008, 10, 14, b'1006'),
            (1010, 15, 19, b'1009'),
            (1011, 15, 19, b'1009'),
        ],
    ),
    '#source': (
        b'{\toffsetsFileSize = 72;\n\tstringsFileSize = "217";\n}',
        [
            (1000, 0, 48, b'imap://user%40example.com@imap.example.com/INBOX'),
            (1003, 49, 104, b'imap://user%40example.com@imap.example.com/Archive/2024'),
            (1006, 105, 161, b'imap://user%40example.com@imap.example.com/Sent Messages'),
            (1009, 162, 216, b'imap://user%40example.com@imap.example.com/Lists/alpha'),
            (1009, 1, 0, None),
            (1009, 0, 48, b'imap://user%40example.com@imap.example.com/INBOX'),
        ],
    ),
    '#unquoted': (
        b'{\toffsetsFileSize = 732;\n\tstringsFileSize = 2872;\n}',
        [
            (1001, 0, 12, b'thanks again'),
            (1004, 13, 80, b'bridulosa\nk\xc3\xb8tisasa sanu kasa britigelti gelzugel gelpepe k\xc3\xb8wenbri'),
            (1007, 81, 140, b'tosgelnulo wenfalnumar marbriwenbri mifalmi tigellovo k\xc3\xb8we'),
            (1010, 141, 277, b'votosbri vologelwen ref 003-0003 wensaka toszuvolo brilokamar titosvo nuka zumarro nubrimi mimarfal brigelzupe lok\xc3\xb8 kamarbrimar tirok\xc3\xb8'),
            (1002, 278, 298, b'bripetosfal sawenvok'),
            (1005, 299, 332, b'sak\xc3\xb8dumar\nzumarmarmar dulofalfal'),
            (1008, 333, 375, b'fallonuk\xc3\xb8 mi dugelwen vomarmar tivo gelro'),
            (1011, 376, 526, b'k\xc3\xb8rovozu zebrafinch timikaro sabrigel k\xc3\xb8titimi mibrisa misaka dutoszuro wenk\xc3\xb8k\xc3\xb8vo brimimar falpenudu gelbribrigel wentosk\xc3\xb8 tosgelpelo sak\xc3\xb8brigel'),
            (1001, 527, 579, b'brilodubri titosnufal dugelbrika gelmigel vofalmardu'),
            (1004, 580, 624, b'geldufal\nzusamarfal zulomi zuka dugelnuvo ka'),
            (1007, 625, 666, b'tosgelnulo zutosrovo votosroka zuduti rop'),
            (1010, 667, 745, b'nuromarbri sadu falmiwen brimarwenbri perotiti nuzu falmi vozulo titosnufal zu'),
            (1002, 746, 814, b'briwenk\xc3\xb8 briwenlo toswenrozu zugelvo k\xc3\xb8britoska satoswenmi gelbriz'),
            (1005, 815, 865, b'romibri\nzusa mark\xc3\xb8 brisazusa tosfalk\xc3\xb8 tostosmige'),
            (1008, 866, 946, b'robrilotos karoka rovokasa briwen vonubrimi zuvo romarfalfal sawenbri zumarmi ro'),
            (1011, 947, 983, b'lonunuwen tosfalk\xc3\xb8 falpepesa falgel'),
            (1001, 984, 1066, b'brisavosa mivofaldu lotibrik\xc3\xb8 savovosa mitosti duk\xc3\xb8duka brifalbrimi gelfalro bri'),
            (1004, 1067, 1143, b'sazugelro\nsak\xc3\xb8brigel falmiwen kawenk\xc3\xb8fal wenmarzuti peti nu martisa wenlof'),
            (1007, 1144, 1181, b'pewensafal mifalmi markamarmi pesa br'),
            (1010, 1182, 1208, b'wenkabrizu vovotos tospemi'),
            (1002, 1209, 1285, b'vok\xc3\xb8 k\xc3\xb8titisa robrivo midunu k\xc3\xb8pe martisa gelnufalti falsakami karomarbri'),
            (1005, 1286, 1339, b'kakamidu\nvomik\xc3\xb8 wentiwenpe tostospe falmidu falsakam'),
            (1008, 1340, 1356, b'zufalvo falmarlo'),
            (1011, 1357, 1428, b'votik\xc3\xb8 vomi tosfalsavo marnupe rodubriwen pegelzu lomarfalmi nufalka g'),
            (1001, 1429, 1457, b'rosalo votosbri gelzugel pek'),
            (1004, 1458, 1480, b'brisadu\nbrik\xc3\xb8falmi to'),
            (1007, 1481, 1504, b'mibrivogel toslo tosbri'),
            (1010, 1505, 1581, b'sazutika tik\xc3\xb8brinu vodumi brilosa toskanu brisafal pefalpe zusafalfal mipet'),
            (1002, 1582, 1635, b'pewenkape sawenzuk\xc3\xb8 mivo pero tossak\xc3\xb8wen timark\xc3\xb8ma'),
            (1005, 1636, 1693, b'dukavomar\ntosfalvope vobrilo kaduka wendutos mivo tostosp'),
            (1008, 0, 12, b'thanks again'),
            (1011, 1694, 1748, b'brimiti k\xc3\xb8nudu tiwenduka mik\xc3\xb8bri britipe vok\xc3\xb8pevo d'),
            (1001, 1749, 1797, b'ronulomi brizugelpe petosk\xc3\xb8 tosdumar petosdu pe'),
            (1004, 1798, 1851, b'penuka\nnusazu vomardu karomarbri tik\xc3\xb8 katospero mimi'),
            (1007, 1852, 1899, b'wenbrikawen k\xc3\xb8katos kanugel faltosfal safalfal'),
            (1010, 1900, 1917, b'k\xc3\xb8lozu votik\xc3\xb8 k'),
            (1002, 1918, 1948, b'tizu tik\xc3\xb8lo zupemilo tostosdu'),
            (1005, 1949, 1987, b'wenmargel\npepenuka tosduk\xc3\xb8 martosfalg'),
            (1008, 1988, 2034, b'nutosfal zumarmarmar falk\xc3\xb8k\xc3\xb8nu falronu kamar'),
            (1011, 2035, 2075, b'wensaka vokalozu wenvokamar wenmarmargel'),
            (1001, 2076, 2109, b'k\xc3\xb8brika nubri falgelmitos zukaka'),
            (1004, 1, 0, None),
            (1004, 2110, 2131, b'mitosti\nwenvokamar ka'),
            (1007, 2132, 2148, b'marnuro dunupe s'),
            (1010, 2149, 2177, b'durope zumarwenka britosvoge'),
            (1002, 2178, 2188, b'sabriwen t'),
            (1005, 2189, 2201, b'dubri\nbritos'),
            (1008, 2202, 2242, b'kape mikazupe zulok\xc3\xb8 gellozutos zutilok'),
            (1011, 2243, 2273, b'vomarsagel tizuro kabrifal lon'),
            (1001, 2274, 2349, b'timimarbri sagelsami k\xc3\xb8faltosdu satika k\xc3\xb8 nusasazu wenmizuk\xc3\xb8 brigeldugel'),
            (1004, 2350, 2421, b'vozu\ndurozu wengelbridu wenpewenvo nufalduwen mimi wenmizuk\xc3\xb8 marrolo g'),
            (1007, 2422, 2477, b'britipe sabrik\xc3\xb8 marmarmar gelgelmar vorotos mipe tiwen'),
            (1010, 2478, 2540, b'lok\xc3\xb8toslo toskanu tiwenduka gelrorolo kawenk\xc3\xb8 pemimi kanu lo'),
            (1002, 2541, 2565, b'mimi lodutosmi gelgelfal'),
            (1005, 2566, 2588, b'nutilomi\ntikaro mimilo'),
            (1008, 2589, 2650, b'brifallolo marsavo tosgeltik\xc3\xb8 wentoszu gelk\xc3\xb8minu rororo nud'),
            (1011, 2651, 2732, b'nulosatos marvo wenmilodu romilo dumarmi peduzu nuduk\xc3\xb8sa sasazu dunupe gelpefalw'),
            (1001, 2733, 2781, b'vofalfalro pebrigel tostipe gelmar gelbriti bril'),
            (1004, 2782, 2793, b'votosroka\nz'),
            (1007, 2794, 2844, b'tifalsa numarfalbri tipek\xc3\xb8 mimarfal dumarmi gelka'),
            (1010, 2845, 2871, b'karotosfal mifalka gelgelm'),
        ],
    ),
    'bcc': (
        b'{\toffsetsFileSize = 48;\n\tstringsFileSize = 76;\n}',
        [
            (1000, 0, 18, b'blind0@example.net'),
            (1003, 19, 37, b'blind1@example.net'),
            (1006, 38, 56, b'blind2@example.net'),
            (1009, 57, 75, b'blind3@example.net'),
        ],
    ),
    'cc': (
        b'{\toffsetsFileSize = 48;\n\tstringsFileSize = 72;\n}',
        [
            (1000, 0, 17, b'copy0@example.net'),
            (1003, 18, 35, b'copy1@example.net'),
            (1006, 36, 53, b'copy2@example.net'),
            (1009, 54, 71, b'copy3@example.net'),
        ],
    ),
    'from': (
        b'{\toffsetsFileSize = 48;\n\tstringsFileSize = 40;\n}',
        [
            (1000, 0, 19, b'sender0@example.com'),
            (1003, 0, 19, b'sender0@example.com'),
            (1006, 0, 19, b'sender0@example.com'),
            (1009, 20, 39, b'sender1@example.com'),
        ],
    ),
    'message-id': (
        b'{\toffsetsFileSize = 48;\n\tstringsFileSize = 100;\n}',
        [
            (1000, 0, 24, b'<0.20261025@example.net>'),
            (1003, 25, 49, b'<1.20261025@example.net>'),
            (1006, 50, 74, b'<2.20261025@example.net>'),
            (1009, 75, 99, b'<3.20261025@example.net>'),
        ],
    ),
    'subject': (
        b'{\toffsetsFileSize = 72;\n\tstringsFileSize = "234";\n}',
        [
            (1000, 0, 54, b'k\xc3\xb8k\xc3\xb8wengel nulotiwen britosmibri lovolo kasavo brivo'),
            (1003, 55, 97, b'marzurosa fallonuk\xc3\xb8 rotosgelsa kageltifal'),
            (1006, 98, 140, b'nulomartos wenpe tosfalk\xc3\xb8mi tipek\xc3\xb8 brika'),
            (1006, 1, 0, None),
            (1006, 141, 186, b're nulomartos wenpe tosfalk\xc3\xb8mi tipek\xc3\xb8 brika'),
            (1009, 187, 233, b'kak\xc3\xb8tossa kawenk\xc3\xb8fal gelpe penuvo titifalk\xc3\xb8'),
        ],
    ),
    'to': (
        b'{\toffsetsFileSize = 48;\n\tstringsFileSize = 108;\n}',
        [
            (1000, 0, 26, b'Rcpt 0 <rcpt0@example.org>'),
            (1003, 27, 53, b'Rcpt 1 <rcpt1@example.org>'),
            (1006, 54, 80, b'Rcpt 2 <rcpt2@example.org>'),
            (1009, 81, 107, b'Rcpt 3 <rcpt3@example.org>'),
        ],
    ),
}
MAIN_PLIST = b'{\tUUID = "00000000-0000-4000-8000-0000000b3c00";\n\tlargestUsedBodyPartID = 1011;\n\trevision = "1";\n\tversion = "1.1";\n}'
MSGS_IDS = b'\xe8\x03\x00\x00\xeb\x03\x00\x00\xee\x03\x00\x00'
NEW_MSGS_IDS = b'\xf1\x03\x00\x00'

RUN_KEYS = {"snapshot_s", "body_snapshot_s", "grouping_s", "literal_cold_s",
            "literal_warm_s", "icase_cold_s",
            "icase_warm_s", "regex_cold_s", "regex_warm_s", "literal_hits",
            "icase_hits", "regex_hits", "nolit_cold_s", "nolit_warm_s",
            "nolit_hits", "records", "peak_rss_mb"}


def _decode(headers: pathlib.Path, name: str) -> list[tuple[int, int, int, bytes | None]]:
    offsets = (headers / f"{name}.offsets").read_bytes()
    cache = (headers / f"{name}.cache").read_bytes()
    out = []
    for rid, start, end in struct.iter_unpack("<III", offsets):
        value = None if (start, end) == (1, 0) else cache[start:end]
        out.append((rid, start, end, value))
    return out


class TinyStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = pathlib.Path(cls._tmp.name) / "store"
        bench.build_synthetic(cls.root, scale=TINY_SCALE, seed=SEED)
        cls.db = cls.root / "Database.noindex"
        cls.headers = cls.db / "Headers"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_bench_writer_exact(self) -> None:
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["Database.noindex"])
        self.assertEqual(sorted(p.name for p in self.db.iterdir()),
                         ["Headers", "Main.plist", "Sets"])
        self.assertEqual(sorted(p.name for p in self.headers.iterdir()),
                         sorted(f"{n}{s}" for n in EXPECTED
                                for s in (".cache", ".offsets", ".plist")))
        self.assertEqual(set(EXPECTED), set(bench.HEADER_INDEXES) | {bench.BODY_INDEX})
        for name, (plist, records) in EXPECTED.items():
            with self.subTest(index=name):
                self.assertEqual((self.headers / f"{name}.plist").read_bytes(), plist)
                self.assertEqual(_decode(self.headers, name), records)
                cache = (self.headers / f"{name}.cache").read_bytes()
                offsets = (self.headers / f"{name}.offsets").read_bytes()
                # FORMAT.md sections 2 and 3: Sizes, the trailing newline and
                # the newline after every value.
                sizes = re.fullmatch(
                    rb'\{\toffsetsFileSize = (\d+);\n\tstringsFileSize = "?(\d+)"?;\n\}',
                    plist)
                self.assertIsNotNone(sizes)
                self.assertEqual(int(sizes.group(1)), len(offsets))
                self.assertEqual(int(sizes.group(2)), len(cache))
                self.assertTrue(cache.endswith(b"\n"))
                for _, start, end, value in records:
                    if value is not None:
                        self.assertEqual(cache[end:end + 1], b"\n")
                        self.assertTrue(start == 0 or cache[start - 1:start] == b"\n")
        self.assertEqual((self.db / "Main.plist").read_bytes(), MAIN_PLIST)
        self.assertEqual((self.db / "Sets" / "Msgs.ids").read_bytes(), MSGS_IDS)
        self.assertEqual((self.db / "Sets" / "NewMsgs.ids").read_bytes(), NEW_MSGS_IDS)
        self.assertEqual(sorted(p.name for p in (self.db / "Sets").iterdir()),
                         ["Msgs.ids", "NewMsgs.ids"])

    def test_bench_expected_hits(self) -> None:
        snap = Store(self.root).snapshot([bench.BODY_INDEX], backoff=0)
        self.assertFalse(snap.unstable)
        self.assertEqual(snap.skipped_records, {})
        self.assertEqual(snap.live, frozenset({1000, 1003, 1006, 1009}))
        index = snap.indexes[bench.BODY_INDEX]
        cases = (
            (re.compile("zebrafinch", re.M), b"zebrafinch", {1011}),
            (re.compile("zebrafinch", re.I | re.M), None, {1011}),
            (re.compile(bench.REGEX, re.M), b"-", {1010}),
            (re.compile(bench.NOLIT, re.M), None, {1010}),
            # Only in a record of 1004 before its marker, so not current.
            (re.compile("bridulosa", re.M), b"bridulosa", set()),
            # One shared range, two ids.
            (re.compile("^thanks again$", re.M), b"thanks again", {1001, 1008}),
        )
        for pattern, literal, ids in cases:
            with self.subTest(pattern=pattern.pattern, flags=pattern.flags):
                found = required_literal(pattern.pattern, pattern.flags)
                self.assertEqual(found, literal)
                self.assertEqual(index.search(pattern, found), ids)
                self.assertEqual(index.search(pattern, None), ids)


class SmallRunTest(unittest.TestCase):
    def test_bench_small(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "store"
            bench.build_synthetic(root, scale=0.001)
            result = bench.run(root)
        self.assertLessEqual(RUN_KEYS, set(result))
        self.assertGreater(result["literal_hits"], 0)
        self.assertEqual(result["literal_hits"], result["icase_hits"])
        self.assertGreater(result["regex_hits"], 0)
        self.assertEqual(result["nolit_hits"], result["regex_hits"])
        self.assertGreater(result["records"], 0)
        self.assertEqual(result["unstable"], 0)
        self.assertEqual(result["skipped_records"], 0)
        self.assertEqual(result["warnings"], 0)

    def test_bench_refuses_default_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp) / "MailMate"
            fake.mkdir()
            with mock.patch.object(bench, "DEFAULT_ROOT", fake):
                for out in (fake, fake / "bench", fake / "x" / ".." / "y"):
                    with self.subTest(out=str(out)):
                        with self.assertRaises(ValueError):
                            bench.build_synthetic(out, scale=TINY_SCALE)
            self.assertEqual(list(fake.iterdir()), [])

    def test_bench_refuses_symlink_below_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            # Stands in for a protected store outside DEFAULT_ROOT's check.
            protected = tmp_path / "protected"
            (protected / "Headers").mkdir(parents=True)
            (protected / "Main.plist").write_bytes(b"keep")
            before = sorted(str(p.relative_to(protected)) for p in protected.rglob("*"))
            out = tmp_path / "out"
            out.mkdir()
            (out / "Database.noindex").symlink_to(protected, target_is_directory=True)
            with self.assertRaises((ValueError, OSError)):
                bench.build_synthetic(out, scale=TINY_SCALE)
            after = sorted(str(p.relative_to(protected)) for p in protected.rglob("*"))
            self.assertEqual(after, before)
            self.assertEqual((protected / "Main.plist").read_bytes(), b"keep")
            self.assertEqual(list((protected / "Headers").iterdir()), [])

    def test_bench_refuses_unsafe_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            full = tmp_path / "full"
            full.mkdir()
            (full / "other").write_bytes(b"x")
            real = tmp_path / "real"
            real.mkdir()
            link = tmp_path / "link"
            link.symlink_to(real, target_is_directory=True)
            plain = tmp_path / "plain"
            plain.write_bytes(b"x")
            for out in (full, link, plain):
                with self.subTest(out=out.name):
                    with self.assertRaises(ValueError):
                        bench.build_synthetic(out, scale=TINY_SCALE)
            self.assertEqual(list(real.iterdir()), [])
            self.assertEqual(sorted(p.name for p in full.iterdir()), ["other"])

    def test_bench_writes_into_empty_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "empty"
            out.mkdir()
            bench.build_synthetic(out, scale=TINY_SCALE, seed=SEED)
            self.assertEqual((out / "Database.noindex" / "Main.plist").read_bytes(), MAIN_PLIST)


if __name__ == "__main__":
    unittest.main()
