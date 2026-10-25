"""Tests for mmgrep.output (milestone 3, Task 5; decisions D10 and D13).

Rows are built by hand with synthetic values and RFC 2606 domains only.
"""

from __future__ import annotations

import io
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone

from mmgrep.output import Row, render, sort_rows

PLUS_ONE = timezone(timedelta(hours=1))
KEYS = {
    "date", "from", "to", "cc", "subject", "message_id", "paths", "ids",
    "confirmed", "confirmed_path",
}


def _row(**kw) -> Row:
    """A Row with defaults for every field not given."""
    values = dict(
        date=datetime(2024, 3, 5, 14, 7, 9, tzinfo=PLUS_ONE),
        from_="Alice <alice@example.com>",
        to="bob@example.org",
        cc="carol@example.net",
        subject="Hello",
        message_id="<m1@example.com>",
        paths=["/store/a/1.eml"],
        ids=[1],
        confirmed=True,
        confirmed_path="/store/a/1.eml",
    )
    values.update(kw)
    return Row(**values)


def _render(rows, fmt, **kw) -> str:
    out = io.StringIO()
    render(rows, fmt, out, **kw)
    return out.getvalue()


class OutputTests(unittest.TestCase):
    def setUp(self) -> None:
        old = os.environ.get("TZ")

        def restore() -> None:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = "Asia/Tokyo"
        time.tzset()

    def test_table_cut_keeps_escapes_whole(self) -> None:
        for n in range(50, 61):
            subject = "s" * n + "\x1b" * 10
            with self.subTest(n=n):
                line = _render([_row(subject=subject)], "table")
                cell = line[16 + 2 + 30 + 2:][:60].rstrip()
                self.assertTrue(cell.endswith("..."), cell)
                body = cell[:-3]
                self.assertRegex(body, r"^s*(?:\\x1b)*$")
                self.assertLessEqual(len(cell), 60)

    def test_paths_real_bytes(self) -> None:
        path = "/s/x\udcff.eml"
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8")
        stream.write("pre\n")
        render([_row(paths=[path], ids=[1])], "paths", stream)
        stream.flush()
        self.assertEqual(raw.getvalue(), b"pre\n" + os.fsencode(path) + b"\n")
        stream.detach()
    def test_json_keys_and_types(self) -> None:
        rows = [
            _row(),
            _row(date=None, confirmed=False, confirmed_path=None,
                 message_id=None, paths=["/s/2.eml", "/s/3.eml"],
                 ids=[2, 3]),
            _row(confirmed="indeterminate", confirmed_path=None),
        ]
        lines = _render(rows, "json").splitlines()
        self.assertEqual(len(lines), 3)
        objs = [json.loads(line) for line in lines]
        for obj in objs:
            self.assertEqual(set(obj), KEYS)
            self.assertIsInstance(obj["paths"], list)
            self.assertIsInstance(obj["ids"], list)
        self.assertEqual(objs[0]["date"], "2024-03-05T14:07:09+01:00")
        self.assertIs(objs[0]["confirmed"], True)
        self.assertEqual(objs[0]["from"], "Alice <alice@example.com>")
        self.assertEqual(objs[0]["confirmed_path"], "/store/a/1.eml")
        self.assertIsNone(objs[1]["date"])
        self.assertIs(objs[1]["confirmed"], False)
        self.assertIsNone(objs[1]["confirmed_path"])
        self.assertIsNone(objs[1]["message_id"])
        self.assertEqual(objs[1]["ids"], [2, 3])
        self.assertEqual(objs[2]["confirmed"], "indeterminate")

    def test_json_ascii_and_surrogates(self) -> None:
        text = _render([_row(subject="café",
                             paths=["/s/x\udcff.eml"])], "json")
        self.assertTrue(text.isascii())
        obj = json.loads(text)
        self.assertEqual(obj["subject"], "café")
        self.assertEqual(obj["paths"], ["/s/x\udcff.eml"])

    def test_table_escapes_controls(self) -> None:
        row = _row(subject="a\x1b[31mb\n", paths=["/s/1.eml", "/s/2.eml"],
                   ids=[1, 2])
        text = _render([row], "table")
        self.assertNotIn("\x1b", text)
        self.assertIn("a\\x1b[31mb\\n", text)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertTrue(text.startswith("2024-03-05 22:07  "))
        self.assertTrue(text.rstrip("\n").endswith("2"))
        self.assertNotIn("to:", text)

        verbose = _render([row], "table", verbose=True)
        self.assertNotIn("\x1b", verbose)
        lines = verbose.splitlines()
        self.assertEqual(lines[0], text.rstrip("\n"))
        self.assertTrue(any(ln.strip() == "to: bob@example.org"
                            for ln in lines))
        self.assertTrue(any(ln.strip() == "cc: carol@example.net"
                            for ln in lines))
        path_lines = [ln.strip() for ln in lines
                      if ln.strip().startswith("path:")]
        self.assertEqual(path_lines, ["path: /s/1.eml", "path: /s/2.eml"])

    def test_table_columns_cut_and_pad(self) -> None:
        long_from = "x" * 40 + "@example.com"
        long_subject = "s" * 80
        text = _render([_row(from_=long_from, subject=long_subject),
                        _row(date=None, from_="a@example.com",
                             subject="short", paths=["/a", "/b", "/c"] * 4,
                             ids=list(range(12)))], "table")
        first, second = text.splitlines()
        self.assertIn("x" * 27 + "...", first)
        self.assertNotIn("x" * 28, first)
        self.assertIn("s" * 57 + "...", first)
        self.assertNotIn("s" * 58, first)
        self.assertEqual(second[:18], " " * 18)
        self.assertEqual(len(first), len(second))
        self.assertTrue(first.endswith(" 1"))
        self.assertTrue(second.endswith("12"))

    def test_paths_and_all_paths(self) -> None:
        rows = [_row(paths=["/s/1.eml", "/s/2.eml"], ids=[1, 2]),
                _row(paths=["/s/3.eml"], ids=[3])]
        self.assertEqual(_render(rows, "paths"), "/s/1.eml\n/s/3.eml\n")
        self.assertEqual(_render(rows, "paths", all_paths=True),
                         "/s/1.eml\n/s/2.eml\n/s/3.eml\n")

    def test_sort_order(self) -> None:
        early = datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc)
        late = datetime(2024, 1, 1, 9, 30, tzinfo=PLUS_ONE)
        rows = [
            _row(date=None, message_id="<b@example.com>"),
            _row(date=early, message_id="<z@example.com>"),
            _row(date=None, message_id="<a@example.com>"),
            _row(date=early, message_id="<c@example.com>"),
            _row(date=late, message_id="<a@example.org>"),
            _row(date=early, message_id=None),
        ]
        got = [(r.date, r.message_id) for r in sort_rows(rows)]
        self.assertEqual(got, [
            (late, "<a@example.org>"),
            (early, None),
            (early, "<c@example.com>"),
            (early, "<z@example.com>"),
            (None, "<a@example.com>"),
            (None, "<b@example.com>"),
        ])
        self.assertEqual(len(rows), 6)

    def test_render_unencodable_stream(self) -> None:
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii",
                                  errors="backslashreplace")
        row = _row(subject="Grüße ☃", from_="Jörg <j@example.com>")
        render([row], "table", stream, verbose=True)
        render([row], "json", stream)
        render([row], "paths", stream)
        stream.flush()
        data = raw.getvalue()
        self.assertIn(b"Gr\\xfc\\xdfe \\u2603", data)
        stream.detach()

    def test_unknown_format(self) -> None:
        with self.assertRaises(ValueError):
            _render([_row()], "csv")


if __name__ == "__main__":
    unittest.main()
