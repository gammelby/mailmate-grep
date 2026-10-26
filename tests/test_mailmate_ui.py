"""Tests for mmgrep.mailmate_ui (mmgrep --open)."""
from __future__ import annotations

import contextlib
import io
import pathlib
import tempfile
import unittest
import urllib.parse
from unittest import mock

import mmgrep
from mmgrep import mailmate_ui
from mmgrep.cli import main
from mmgrep.mailmate_ui import MAX_IDS, open_url, search_url
from tests.fixtures import FixtureMessage, build_store

ACCOUNT = "user%40example.com@imap.example.com"


def query_of(url: str) -> str:
    prefix = "mlmt:quicksearch?string="
    assert url.startswith(prefix)
    return urllib.parse.unquote(url[len(prefix):])


class SearchUrlTest(unittest.TestCase):
    def test_or_of_message_ids_without_brackets(self) -> None:
        url, used, skipped = search_url(["<a1@example.com>", "b2@example.com"])
        self.assertEqual(query_of(url), "message-id:a1@example.com or message-id:b2@example.com")
        self.assertEqual((used, skipped), (2, 0))
        self.assertNotIn(" ", url)

    def test_skips_missing_and_unsafe_ids_and_duplicates(self) -> None:
        url, used, skipped = search_url(
            [None, "", "<has space@example.com>", "<x(y)@example.com>", "<ok@example.com>",
             "<ok@example.com>"])
        self.assertEqual(query_of(url), "message-id:ok@example.com")
        self.assertEqual((used, skipped), (1, 4))

    def test_none_usable(self) -> None:
        self.assertEqual(search_url([None]), (None, 0, 1))

    def test_caps_at_max_ids(self) -> None:
        url, used, _ = search_url(f"<m{i}@example.com>" for i in range(MAX_IDS + 5))
        self.assertEqual(used, MAX_IDS)
        self.assertEqual(query_of(url).count(" or "), MAX_IDS - 1)

    def test_open_url_runs_only_usr_bin_open(self) -> None:
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        self.assertEqual(open_url("mlmt:quicksearch?string=x", run=run), 0)
        run.assert_called_once_with(["/usr/bin/open", "mlmt:quicksearch?string=x"], check=False)
        with self.assertRaises(ValueError):
            open_url("file:///etc/passwd", run=run)

    def test_module_starts_nothing_else(self) -> None:
        path = pathlib.Path(mmgrep.__file__).resolve().parent / "mailmate_ui.py"
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("subprocess"), 2)  # the import and the default argument
        self.assertIn('run(["/usr/bin/open", url], check=False)', text)


class OpenCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        build_store(self.root, [FixtureMessage(
            id=1100, account=ACCOUNT, mailbox="INBOX",
            headers={"Message-ID": "<1100@example.com>", "Subject": "Lisbon trip",
                     "From": "a@example.org", "Date": "Tue, 05 Mar 2024 14:07:09 +0100"})])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str, list]:
        calls = []

        def fake(argv, check):
            calls.append(argv)
            return mock.Mock(returncode=0)

        err = io.StringIO()
        with mock.patch.object(mailmate_ui.subprocess, "run", fake), \
                mock.patch("mmgrep.cli.open_url",
                           lambda url: mailmate_ui.open_url(url, run=fake)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main(["--root", str(self.root), *args])
        return code, err.getvalue(), calls

    def test_open_hands_one_url_to_open(self) -> None:
        code, err, calls = self.run_cli("--subject", "lisbon", "--open")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "/usr/bin/open")
        self.assertEqual(query_of(calls[0][1]), "message-id:1100@example.com")
        self.assertIn("opened 1 messages", err)

    def test_no_results_opens_nothing(self) -> None:
        code, _, calls = self.run_cli("--subject", "nomatch", "--open")
        self.assertEqual((code, calls), (1, []))

    def test_open_with_who_is_usage_error(self) -> None:
        code, err, calls = self.run_cli("--who", "a", "--open")
        self.assertEqual((code, calls), (2, []))


if __name__ == "__main__":
    unittest.main()
