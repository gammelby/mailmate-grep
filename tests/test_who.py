"""Tests for mmgrep.who (name to address resolution, mmgrep --who)."""
from __future__ import annotations

import contextlib
import io
import json
import pathlib
import tempfile
import unittest

from mmgrep.cli import main
from mmgrep.store import Store
from mmgrep.who import WHO_INDEXES, fold, who
from tests.fixtures import FixtureMessage, build_store

ACCOUNT = "user%40example.com@imap.example.com"


def msg(id: int, date: str = "Tue, 05 Mar 2024 14:07:09 +0100", **headers) -> FixtureMessage:
    h = {"Message-ID": f"<{id}@example.com>", "Subject": f"Message {id}", "Date": date}
    h.update({k.replace("_", "-"): v for k, v in headers.items()})
    return FixtureMessage(id=id, account=ACCOUNT, mailbox="INBOX", headers=h)


class WhoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        build_store(self.root, [
            msg(1100, From="Carsten Rhod Gregersen <crg@example.com>"),
            msg(1200, From='"Gregersen, Carsten" <crg@example.com>',
                date="Thu, 10 Oct 2026 09:00:00 +0200"),
            msg(1300, From="Alice <alice@example.org>",
                To="Carsten R. Gregersen <carsten@example.net>, Bob <bob@example.org>"),
            msg(1400, From="Søren Ærø <soren@example.org>"),
            msg(1500, From="Carsten Other <cother@example.org>"),
            msg(1700, From="Doe, Jane <jane@example.com>",
                To="x@example.org, Roe, Richard <rr@example.net>, mo@example.org (Mo Ola)"),
            msg(1800, From="Alice Kay <alice@example.com>"),
            FixtureMessage(id=1900, account=ACCOUNT, mailbox="Archive",
                           headers={"Message-ID": "<1800@example.com>",
                                    "Subject": "Message 1800",
                                    "Date": "Tue, 05 Mar 2024 14:07:09 +0100",
                                    "From": "Alice Kay <alice@example.com>"}),
            FixtureMessage(id=1600, account=ACCOUNT, mailbox="INBOX", deleted=True,
                           headers={"Message-ID": "<1600@example.com>",
                                    "From": "Carsten Rhod Gregersen <old@example.com>"}),
        ])
        self.snap = Store(self.root).snapshot(WHO_INDEXES, backoff=0)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fold_removes_accents_and_case(self) -> None:
        self.assertEqual(fold("Søren ÆRØ Gregersen"), "soren aero gregersen")
        self.assertEqual(fold("Åse Straße"), "ase strasse")
        self.assertEqual(fold("José"), "jose")

    def test_all_words_in_any_order_ranked_by_count(self) -> None:
        got = who(self.snap, "gregersen carsten")
        self.assertEqual([c.address for c in got], ["crg@example.com", "carsten@example.net"])
        top = got[0]
        self.assertEqual(top.messages, 2)
        self.assertEqual(sorted(top.names),
                         ["Carsten Rhod Gregersen", "Gregersen, Carsten"])
        self.assertEqual(top.last.isoformat(), "2026-10-10T09:00:00+02:00")

    def test_word_prefixes_and_initial(self) -> None:
        self.assertEqual([c.address for c in who(self.snap, "carst greg")],
                         ["crg@example.com", "carsten@example.net"])

    def test_matches_address_tokens(self) -> None:
        self.assertEqual([c.address for c in who(self.snap, "crg")], ["crg@example.com"])

    def test_accent_insensitive(self) -> None:
        self.assertEqual([c.address for c in who(self.snap, "soren aero")],
                         ["soren@example.org"])

    def test_deleted_messages_do_not_count(self) -> None:
        self.assertNotIn("old@example.com", [c.address for c in who(self.snap, "carsten")])

    def test_unquoted_comma_names_and_comments(self) -> None:
        self.assertEqual([c.address for c in who(self.snap, "doe jane")], ["jane@example.com"])
        self.assertEqual([c.address for c in who(self.snap, "richard roe")], ["rr@example.net"])
        self.assertEqual([c.address for c in who(self.snap, "mo ola")], ["mo@example.org"])
        self.assertEqual(who(self.snap, "jane")[0].top_names(), ["Doe, Jane"])

    def test_copies_count_once(self) -> None:
        got = who(self.snap, "alice kay")
        self.assertEqual([(c.address, c.messages) for c in got], [("alice@example.com", 1)])
        self.assertEqual(got[0].ids, {1800, 1900})

    def test_no_match(self) -> None:
        self.assertEqual(who(self.snap, "nobody here"), [])

    def test_empty_query_raises(self) -> None:
        with self.assertRaises(ValueError):
            who(self.snap, "  ")

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--root", str(self.root), *args])
        return code, out.getvalue(), err.getvalue()

    def test_cli_json(self) -> None:
        code, out, _ = self.run_cli("--who", "carsten gregersen", "--json")
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(rows[0], {"address": "crg@example.com",
                                   "names": ["Carsten Rhod Gregersen", "Gregersen, Carsten"],
                                   "messages": 2, "last": "2026-10-10T09:00:00+02:00"})
        self.assertEqual(len(rows), 2)

    def test_cli_table_limit_and_none(self) -> None:
        code, out, _ = self.run_cli("--who", "carsten", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("crg@example.com", out)
        code, out, _ = self.run_cli("--who", "nobody")
        self.assertEqual((code, out), (1, ""))

    def test_cli_who_with_filters_is_usage_error(self) -> None:
        for extra in (["--subject", "x"], ["-s"], ["--include-quoted"], ["-v"],
                      ["--jobs", "2"], ["--paths-only"], ["--no-confirm"]):
            code, _, err = self.run_cli("--who", "carsten", *extra)
            self.assertEqual(code, 2, extra)
            self.assertIn("--who", err)


if __name__ == "__main__":
    unittest.main()
