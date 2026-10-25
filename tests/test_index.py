"""Unit tests for mmgrep.index (milestone 2, Tasks 1 and 2).

Records are built as literal bytes with recs() (plan decision D1) because
these tests need markers and malformed records that tests/fixtures.py does
not write. All values are synthetic.
"""

from __future__ import annotations

import random
import re
import struct
import unittest
import warnings

from mmgrep.index import MARKER, Index, required_literal

C = b"alice@example.com\nbob@example.org\n\n"
ALICE = b"alice@example.com"
BOB = b"bob@example.org"


def recs(*triples: tuple[int, int, int]) -> bytes:
    return b"".join(struct.pack("<III", *t) for t in triples)


# Task 1 cases over C, combined in test_ids_current_only.
CASES = {
    5: [(5, 0, 17), (5, 1, 0), (5, 0, 17), (5, 18, 33)],
    6: [(6, 0, 17), (6, 1, 0)],
    7: [(7, 0, 17), (7, 1, 0), (7, 18, 33)],
    8: [(8, 0, 17), (8, 1, 0), (8, 18, 33), (8, 0, 99)],
    9: [(9, 0, 99), (9, 1, 0), (9, 18, 33)],
    10: [(10, 34, 34)],
    11: [(11, 0, 17)],
}


def case(i: int) -> Index:
    return Index("from", recs(*CASES[i]), C)


class DecodeTest(unittest.TestCase):
    def test_marker_constant(self) -> None:
        self.assertEqual(MARKER, (1, 0))

    def test_format_example(self) -> None:
        cache = b"alice@example.com\nbob@example.org\n"
        offsets = recs((1200, 0, 17), (1205, 0, 17), (1207, 18, 33), (1207, 1, 0))
        self.assertEqual(len(cache), 34)
        self.assertEqual(len(offsets), 48)
        self.assertEqual(
            offsets.hex(" "),
            "b0 04 00 00 00 00 00 00 11 00 00 00 b5 04 00 00 00 00 00 00 11 00 00 00 "
            "b7 04 00 00 12 00 00 00 21 00 00 00 b7 04 00 00 01 00 00 00 00 00 00 00",
        )
        idx = Index("from", offsets, cache)
        self.assertEqual(idx.name, "from")
        self.assertEqual(idx.record_count, 4)
        self.assertEqual(idx.partial_tail, 0)
        self.assertEqual(idx.bad_records, 0)
        self.assertEqual(idx.invalid_ids, frozenset())
        self.assertEqual(idx.values(1200), [ALICE])
        self.assertEqual(idx.values(1205), [ALICE])
        self.assertIsNone(idx.values(1207))
        self.assertIsNone(idx.last(1207))

    def test_multi_record_after_marker(self) -> None:
        idx = case(5)
        self.assertEqual(idx.values(5), [ALICE, BOB])
        self.assertEqual(idx.last(5), BOB)

    def test_marker_only_tail(self) -> None:
        idx = case(6)
        self.assertIsNone(idx.values(6))
        self.assertIsNone(idx.last(6))

    def test_marker_then_replacement(self) -> None:
        self.assertEqual(case(7).values(7), [BOB])

    def test_malformed_after_marker(self) -> None:
        idx = case(8)
        self.assertIsNone(idx.values(8))
        self.assertIsNone(idx.last(8))
        self.assertIn(8, idx.invalid_ids)
        self.assertEqual(idx.bad_records, 1)

    def test_malformed_before_marker(self) -> None:
        idx = case(9)
        self.assertEqual(idx.values(9), [BOB])
        self.assertNotIn(9, idx.invalid_ids)
        self.assertEqual(idx.bad_records, 1)

    def test_start_after_end_is_malformed(self) -> None:
        # Start greater than end but not the marker form (1, 0).
        idx = Index("from", recs((12, 5, 2)), C)
        self.assertIsNone(idx.values(12))
        self.assertIn(12, idx.invalid_ids)
        self.assertEqual(idx.bad_records, 1)

    def test_empty_record(self) -> None:
        idx = case(10)
        self.assertEqual(idx.values(10), [b""])
        self.assertEqual(idx.last(10), b"")

    def test_partial_tail(self) -> None:
        idx = Index("from", recs((11, 0, 17)) + b"\x01\x02\x03\x04\x05", C)
        self.assertEqual(idx.partial_tail, 5)
        self.assertEqual(idx.record_count, 1)
        self.assertEqual(idx.values(11), [ALICE])

    def test_ids_current_only(self) -> None:
        triples = [t for i in sorted(CASES) for t in CASES[i]]
        idx = Index("from", recs(*triples) + b"\x00\x00\x00", C)
        self.assertEqual(idx.partial_tail, 3)
        self.assertEqual(idx.record_count, len(triples))
        self.assertEqual(idx.bad_records, 2)
        self.assertEqual(idx.invalid_ids, frozenset({8}))
        expected = sorted(i for i in CASES if idx.values(i) is not None)
        self.assertEqual(expected, [5, 7, 9, 10, 11])
        self.assertEqual(sorted(idx.ids()), expected)

    def test_unknown_id(self) -> None:
        self.assertIsNone(case(5).values(424242))
        self.assertIsNone(case(5).last(424242))

    def test_large_ids(self) -> None:
        idx = Index("from", recs((0xFFFFFFFF, 18, 33)), C)
        self.assertEqual(idx.values(0xFFFFFFFF), [BOB])
        self.assertEqual(list(idx.ids()), [0xFFFFFFFF])

    def test_empty_files(self) -> None:
        idx = Index("from", b"", b"")
        self.assertEqual(idx.record_count, 0)
        self.assertEqual(list(idx.ids()), [])
        self.assertEqual(idx.search(re.compile("x")), set())


B = b"Hi there\nSee you\none\ntwo\ncaf\xe9 ok\n"
B_RECS = [(1, 0, 8), (2, 9, 16), (3, 17, 24), (4, 25, 32)]


class CountingPattern:
    """A re.Pattern stand-in that counts search calls and delegates."""

    def __init__(self, pattern: re.Pattern[str]) -> None:
        self._p = pattern
        self.pattern = pattern.pattern
        self.flags = pattern.flags
        self.calls = 0

    def search(self, text: str):  # noqa: ANN201
        self.calls += 1
        return self._p.search(text)


class SearchTest(unittest.TestCase):
    def body(self, triples=B_RECS, cache: bytes = B) -> Index:
        return Index("#unquoted", recs(*triples), cache)

    def both(self, idx: Index, pattern: re.Pattern[str], expected: set[int]) -> None:
        lit = required_literal(pattern.pattern, pattern.flags)
        self.assertEqual(idx.search(pattern, None), expected, "no literal")
        self.assertEqual(idx.search(pattern, lit), expected, f"literal {lit!r}")

    def test_search_inside_record(self) -> None:
        self.both(self.body(), re.compile("there"), {1})

    def test_search_not_across_records(self) -> None:
        self.both(self.body(), re.compile(r"there\nSee"), set())

    def test_search_anchors_per_record(self) -> None:
        self.both(self.body(), re.compile("there$"), {1})
        self.both(self.body(), re.compile("^See"), {2})

    def test_search_multiline_inside_record(self) -> None:
        self.both(self.body(), re.compile("^two"), set())
        self.both(self.body(), re.compile("^two", re.M), {3})

    def test_search_greedy_does_not_hide(self) -> None:
        self.both(self.body(), re.compile("e.*"), {1, 2, 3})

    def test_search_invalid_utf8_record(self) -> None:
        self.both(self.body(), re.compile("ok"), {4})

    def test_search_shared_range_fans_out(self) -> None:
        idx = self.body([(20, 0, 8), (21, 0, 8)])
        for lit in (None, b"there"):
            p = CountingPattern(re.compile("there"))
            self.assertEqual(idx.search(p, lit), {20, 21})
            self.assertEqual(p.calls, 1)

    def test_search_stale_never_matches(self) -> None:
        idx = self.body([(30, 0, 8), (30, 1, 0), (30, 9, 16)])
        self.both(idx, re.compile("there"), set())
        self.both(idx, re.compile("See"), {30})

    def test_search_invalid_id_never_matches(self) -> None:
        idx = self.body([(31, 0, 8), (31, 0, 99)])
        self.both(idx, re.compile("there"), set())

    def test_search_literal_only_inside_record(self) -> None:
        p = re.compile(r"there\nSee")
        self.assertEqual(self.body().search(p, b"there\nSee"), set())

    def test_search_overlapping_ranges_fall_back(self) -> None:
        idx = self.body([(40, 0, 10), (41, 4, 10)], b"abcdefghij\n")
        self.both(idx, re.compile("efg"), {40, 41})

    def test_search_cursor_after_stale_hit(self) -> None:
        idx = self.body([(50, 0, 8), (50, 1, 0), (50, 9, 17)], b"needle x\nneedle y\n")
        self.both(idx, re.compile("needle"), {50})

    def test_search_cursor_after_gap_hit(self) -> None:
        idx = self.body([(51, 7, 17)], b"needle\nabc needle\n")
        self.both(idx, re.compile("needle"), {51})

    def test_search_cursor_overlapping_literal(self) -> None:
        # The first hit at 1 straddles the record start; the next hit at 2
        # overlaps it and is the only one inside (60, 2, 4).
        idx = self.body([(60, 2, 4)], b"xaaa\n")
        self.both(idx, re.compile("aa"), {60})

    def test_search_randomized_equivalence(self) -> None:
        rng = random.Random(20261025)
        words = ["alpha", "beta", "beda", "gamma1", "gamma", "delta", "epsilon",
                 "EPSILON", "zeta", "eta", "theta", "iota", "123", "45", "x"]
        cache = bytearray()
        ranges: list[tuple[int, int]] = []
        triples = []
        for _ in range(2000):
            rid = rng.randint(1, 300)
            roll = rng.random()
            if roll < 0.08:
                triples.append((rid, 1, 0))
                continue
            if roll < 0.25 and ranges:
                triples.append((rid,) + rng.choice(ranges))
                continue
            if roll < 0.30:
                value = b""
            else:
                text = " ".join(rng.choice(words) for _ in range(rng.randint(1, 5)))
                if rng.random() < 0.3:
                    text = text.replace(" ", "\n", 1)
                value = text.encode("utf-8")
                if rng.random() < 0.05:
                    value = value + b"\xe9"
            start = len(cache)
            cache += value + b"\n"
            ranges.append((start, start + len(value)))
            triples.append((rid, start, start + len(value)))
        idx = Index("#unquoted", recs(*triples), bytes(cache))
        patterns = ["alpha", "be.a", r"gamma\d", "ta$", "^delta", "(?i)EPSILON",
                    "zeta|eta", "[0-9]{3}"]
        useful = 0
        for text in patterns:
            p = re.compile(text, re.M)
            lit = required_literal(p.pattern, p.flags)
            brute = {
                i for i in idx.ids()
                if any(p.search(v.decode("utf-8", "replace")) for v in idx.values(i))
            }
            self.assertEqual(idx.search(p, None), brute, text)
            self.assertEqual(idx.search(p, lit), brute, text)
            if lit is not None and brute:
                useful += 1
        self.assertGreaterEqual(useful, 3)


FIXED_PATTERNS = [
    "foo.*bar", "a|b", "foo|bar", "Lisbon", "12345", "ab?c", "ab*c", "a+b",
    r"a\.b", "(?i)abc", "(?x)a b", "a b", "", "x(y|z)w", "a{0}b", r"\x41",
    "[]a]b", "(foo)?bar", "(?=foo)foo", "�",
]
CORPUS_BASE = "abAB.[]{}()|?*+ \n"


def corpus(extra: str) -> list[str]:
    rng = random.Random(20261025)
    alphabet = sorted(set(CORPUS_BASE + extra))
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
            for _ in range(2000)]


def compile_or_none(text: str, flags: int):  # noqa: ANN201
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return re.compile(text, flags)
        except (re.error, OverflowError, RecursionError):
            return None


class RequiredLiteralTest(unittest.TestCase):
    def test_required_literal_cases(self) -> None:
        rl = required_literal
        self.assertIn(rl("foo.*bar", 0), {b"foo", b"bar"})
        self.assertIsNone(rl("a|b", 0))
        self.assertIsNone(rl("foo|bar", 0))
        self.assertIsNone(rl("Lisbon", re.I))
        self.assertEqual(rl("12345", re.I), b"12345")
        self.assertEqual(rl("Lisbon", 0), b"Lisbon")
        self.assertIn(rl("ab?c", 0), {b"a", b"c"})
        self.assertIn(rl("ab*c", 0), {b"a", b"c"})
        self.assertIn(rl("a+b", 0), {b"a", b"b"})
        self.assertEqual(rl(r"a\.b", 0), b"a.b")
        self.assertIsNone(rl("(?i)abc", 0))
        self.assertIsNone(rl("(?x)a b", 0))
        self.assertIsNone(rl("a b", re.X))
        self.assertIsNone(rl("", 0))
        self.assertIn(rl("x(y|z)w", 0), {b"x", b"w"})
        lit = rl("a{0}b", 0)
        self.assertTrue(lit is None or b"a" not in lit)
        self.assertIn(rl(r"\x41", 0), {None, b"A"})
        self.assertIn(rl("[]a]b", 0), {None, b"b"})
        self.assertIn(rl("(foo)?bar", 0), {None, b"bar"})
        self.assertIn(rl("(?=foo)foo", 0), {None, b"foo"})

    def test_required_literal_more_cases(self) -> None:
        rl = required_literal
        self.assertIsNone(rl("abc|", 0))
        self.assertIsNone(rl("a(?i)b", 0))
        self.assertIsNone(rl("(?i:a)bc", 0))
        self.assertIsNone(rl("x(?#c)y", 0))
        self.assertIsNone(rl("a{", 0))
        self.assertIsNone(rl("abc\\", 0))
        self.assertIsNone(rl(r"(a)\1", 0))
        self.assertIsNone(rl("(abc", 0))
        self.assertIsNone(rl("abc)", 0))
        self.assertEqual(rl(r"ab\d+cde", 0), b"cde")
        self.assertEqual(rl(r"\bword\b", 0), b"word")
        self.assertEqual(rl("café", 0), "café".encode("utf-8"))
        self.assertIsNone(rl("café", re.I))
        self.assertEqual(rl("ab+c", 0), b"ab")
        self.assertEqual(rl("[a)]xyz", 0), b"xyz")
        self.assertEqual(rl("(a|b)xy(c|d)", 0), b"xy")

    def test_required_literal_replacement_char(self) -> None:
        self.assertIsNone(required_literal("�", 0))
        self.assertIsNone(required_literal("ab�cd", 0))

    def assert_sound(self, text: str, flags: int) -> bool:
        """True when a literal was returned; fails if it is unsound."""
        p = compile_or_none(text, flags)
        if p is None:
            return False
        lit = required_literal(p.pattern, p.flags)
        if lit is None:
            return False
        needle = lit.decode("utf-8")
        self.assertTrue(needle, f"empty literal for {text!r}")
        for s in corpus(text):
            for m in p.finditer(s):
                self.assertIn(needle, m.string[m.start():m.end()],
                              f"{text!r} flags {flags} on {s!r}")
        return True

    def test_required_literal_sound_fixed(self) -> None:
        for text in FIXED_PATTERNS:
            for flags in (0, re.I, re.X, re.M):
                self.assert_sound(text, flags)

    def test_required_literal_sound_random(self) -> None:
        rng = random.Random(20261025)
        tokens = ["a", "b", ".", "|", "?", "*", "+", "{0}", "{1,2}", "(", ")",
                  "[ab]", "\\.", "^", "$"]
        # About a quarter of uniform draws compile, so draw until 500 patterns
        # compile; otherwise the non-vacuity bound below cannot be reached.
        with_literal = 0
        compiled = 0
        draws = 0
        while compiled < 500 and draws < 20000:
            draws += 1
            text = "".join(rng.choice(tokens) for _ in range(rng.randint(1, 8)))
            if compile_or_none(text, 0) is None:
                continue
            compiled += 1
            hits = [self.assert_sound(text, flags) for flags in (0, re.I)]
            if any(hits):
                with_literal += 1
        self.assertEqual(compiled, 500)
        self.assertGreaterEqual(with_literal, 100)


if __name__ == "__main__":
    unittest.main()
