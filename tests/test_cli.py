"""Tests for mmgrep.cli, mmgrep.__main__ and bin/mmgrep (milestone 3, Task 6).

In-process tests call main() with stdout and stderr redirected to StringIO.
Wrapper tests run bin/mmgrep through sh in a subprocess (tests may use
subprocess; the package may not). Large stores are built once per class.
Every store is synthetic under a temp dir and every run passes --root.
RFC 2606 domains only.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from mmgrep.cli import main
from mmgrep.confirm import INDETERMINATE, Confirmation, display_body
from mmgrep.store import Store
from tests.fixtures import FixtureMessage, build_store

REPO = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO / "bin" / "mmgrep"
ACCOUNT = "user%40example.com@imap.example.com"
UNSTABLE = "mmgrep: snapshot never stabilized; results may be incomplete"


def msg(id: int, **headers: str) -> FixtureMessage:
    h = {"From": f"sender{id}@example.org", "To": "user@example.com",
         "Subject": f"Message {id}", "Message-ID": f"<{id}@example.com>",
         "Date": "Tue, 05 Mar 2024 14:07:09 +0100"}
    for key, value in headers.items():
        h[key.replace("_", "-")] = value
    return FixtureMessage(id=id, account=ACCOUNT, mailbox="INBOX", headers=h,
                          body=[f"Body text of message {id}"])


SMALL = [
    msg(1200, From="Alice <a@example.com>", Subject="Plan A",
        Date="Tue, 05 Mar 2024 14:07:09 +0100"),
    msg(1300, From="b@example.org", Subject="Plan B",
        Date="Fri, 01 Mar 2024 09:00:00 +0100"),
    msg(1400, From='"a@example.com" <mallory@example.org>', Subject="Other",
        Date="Wed, 06 Mar 2024 10:00:00 +0100"),
]
# Two copies of one Message-ID with different dates: AAA sorts first by path.
for _id, _box, _date in ((1500, "AAA", "Mon, 01 Jan 2024 10:00:00 +0000"),
                         (1600, "ZZZ", "Mon, 01 Jul 2024 10:00:00 +0000")):
    _m = msg(_id, Subject="Twin", Message_ID="<twin@example.com>", Date=_date)
    _m.mailbox = _box
    SMALL.append(_m)


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def sh(script: str, *args: str, cwd: str = "/", timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", script, "sh", *args], cwd=cwd,
                          capture_output=True, text=True, timeout=timeout)


class _StoreCase(unittest.TestCase):
    """A small store built once for the class, plus a torn copy."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(cls._tmp.name)
        cls.root = str(base / "store")
        cls.torn = str(base / "torn")
        cls.empty = str(base / "empty")
        build_store(pathlib.Path(cls.root), SMALL)
        build_store(pathlib.Path(cls.torn), SMALL, torn={"subject": b"x"})
        os.mkdir(cls.empty)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()


class TestMain(_StoreCase):
    def test_exit_0_and_1(self) -> None:
        code, out, err = run("--root", self.root, "--subject", "^Plan A$")
        self.assertEqual(code, 0, err)
        self.assertIn("Plan A", out)
        self.assertEqual(len(out.splitlines()), 1)
        code, out, err = run("--root", self.root, "--from", "nobody@example.com")
        self.assertEqual(code, 1, err)
        self.assertEqual(out, "")

    def test_exit_2_cases(self) -> None:
        cases = [
            (["--bogus"], None),
            (["--header", "x:y=a"], None),
            (["--header", "x-absent=a"], "--full"),
            ([], None),
            (["--subject", "Plan"], "not a MailMate store"),
            (["--subject", "("], None),
            (["--jobs", "0", "--subject", "Plan"], None),
            (["--limit", "0", "--subject", "Plan"], None),
            (["--all-paths", "--subject", "Plan"], None),
            (["--json", "--paths-only", "--subject", "Plan"], None),
            (["--since", "yesterday"], None),
            (["--from", "example.com"], None),
            (["--full", "--no-confirm", "--subject", "Plan"], None),
            (["--body", "a", "--body", "b"], "mmgrep: --body given more than once"),
            (["--flag", "a b"], "invalid flag"),
            (["--no-flag", "answered"], "no #flags index"),
            (["--full", "--flag", "seen", "--subject", "Plan"], "not with --full"),
        ]
        for argv, needle in cases:
            with self.subTest(argv=argv):
                root = self.empty if needle == "not a MailMate store" else self.root
                code, out, err = run("--root", root, *argv)
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertTrue(err.startswith("mmgrep: "), err)
                self.assertNotIn("Traceback", err)
                if needle:
                    self.assertIn(needle, err)

    def test_option_named_once(self) -> None:
        code, _, err = run("--root", self.root, "--since", "yesterday")
        self.assertEqual(code, 2)
        self.assertEqual(err.count("--since"), 1, err)

    def test_unconfirmed_row_uses_qualifying_copy(self) -> None:
        for extra in (["--no-confirm"], []):
            with self.subTest(extra=extra):
                with mock.patch("mmgrep.cli.confirm_all",
                                side_effect=lambda gs, f, j: [
                                    Confirmation(INDETERMINATE, None, {}, None) for _ in gs]):
                    code, out, err = run("--root", self.root, "--subject", "Twin",
                                         "--since", "2024-06-01T00:00:00+00:00",
                                         "--json", *extra)
                self.assertEqual(code, 0, err)
                obj = json.loads(out)
                self.assertEqual(obj["date"], "2024-07-01T10:00:00+00:00")
                self.assertEqual(obj["from"], "sender1600@example.org")
                self.assertEqual(obj["ids"], [1500, 1600])
        code, out, err = run("--root", self.root, "--subject", "Twin", "--json",
                             "--no-confirm")
        self.assertEqual(json.loads(out)["date"], "2024-01-01T10:00:00+00:00")

    def test_interrupt_during_import(self) -> None:
        class Broken(types.ModuleType):
            def __getattr__(self, name: str):
                raise KeyboardInterrupt

        err = io.StringIO()
        old = signal.getsignal(signal.SIGPIPE)
        self.addCleanup(signal.signal, signal.SIGPIPE, old)
        with mock.patch.dict(sys.modules, {"mmgrep.cli": Broken("mmgrep.cli")}):
            sys.modules.pop("mmgrep.__main__", None)
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                runpy.run_module("mmgrep", run_name="__main__")
        self.assertEqual(cm.exception.code, 130)
        self.assertEqual(err.getvalue(), "mmgrep: interrupted\n")

    def test_help_exits_0(self) -> None:
        code, out, _ = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("usage: mmgrep", out)

    def test_exit_3_torn(self) -> None:
        code, out, err = run("--root", self.torn, "--from", "b@example.org")
        self.assertEqual(code, 3, err)
        self.assertIn("Plan B", out)
        self.assertIn(UNSTABLE, err.splitlines())
        self.assertIn("mmgrep: snapshot retried 3 times", err.splitlines())

    def test_exit_3_unstable_zero_rows(self) -> None:
        code, out, err = run("--root", self.torn, "--from", "nobody@example.com")
        self.assertEqual(code, 3, err)
        self.assertEqual(out, "")
        self.assertIn(UNSTABLE, err.splitlines())

    def test_exit_precedence(self) -> None:
        code, out, err = run("--root", self.torn, "--jobs", "0", "--subject", "Plan")
        self.assertEqual(code, 2, err)
        with mock.patch("mmgrep.cli.confirm_all", side_effect=KeyboardInterrupt):
            code, out, err = run("--root", self.torn, "--subject", "Plan")
        self.assertEqual(code, 130)
        self.assertEqual(err, "mmgrep: interrupted\n")

    def test_no_confirm_json_false(self) -> None:
        code, out, err = run("--root", self.root, "--subject", "Plan", "--no-confirm", "--json")
        self.assertEqual(code, 0, err)
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(len(lines), 2)
        for obj in lines:
            self.assertIs(obj["confirmed"], False)
            self.assertIsNone(obj["confirmed_path"])
            self.assertIsNotNone(obj["date"])
        self.assertEqual([o["subject"] for o in lines], ["Plan B", "Plan A"])

    def test_confirmed_json(self) -> None:
        code, out, err = run("--root", self.root, "--subject", "^Plan A$", "--json")
        self.assertEqual(code, 0, err)
        obj = json.loads(out)
        self.assertIs(obj["confirmed"], True)
        self.assertEqual(obj["from"], "Alice <a@example.com>")
        self.assertEqual(obj["message_id"], "<1200@example.com>")
        self.assertEqual(obj["date"], "2024-03-05T14:07:09+01:00")
        self.assertEqual(obj["ids"], [1200])
        self.assertEqual(obj["confirmed_path"], obj["paths"][0])
        self.assertTrue(os.path.exists(obj["paths"][0]))

    def test_full_mode_rows(self) -> None:
        code, out, err = run("--root", self.root, "--full", "--subject", "Plan", "--json")
        self.assertEqual(code, 0, err)
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([o["message_id"] for o in lines],
                         ["<1300@example.com>", "<1200@example.com>"])
        self.assertTrue(all(o["confirmed"] is True for o in lines))

    def test_paths_only(self) -> None:
        code, out, err = run("--root", self.root, "--subject", "^Plan A$", "--paths-only")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("1200.eml\n"), out)
        code, out, err = run("--root", self.root, "--subject", "^Plan A$",
                             "--paths-only", "--all-paths")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 1)

    def test_display_name_rejected_counts(self) -> None:
        code, out, err = run("--root", self.root, "--from", "a@example.com",
                             "--subject", "Other")
        self.assertEqual(code, 1, err)
        self.assertEqual(out, "")
        self.assertIn("mmgrep: 1 index hits not confirmed", err.splitlines())

    def test_limit_after_sort(self) -> None:
        code, out, err = run("--root", self.root, "--subject", "Plan", "--json",
                             "--limit", "1")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["message_id"], "<1300@example.com>")

    def test_two_copies_one_row(self) -> None:
        code, out, err = run("--root", self.root, "--subject", "^Twin$", "--json")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        obj = json.loads(lines[0])
        self.assertEqual(obj["message_id"], "<twin@example.com>")
        self.assertEqual(obj["ids"], [1500, 1600])
        self.assertEqual(len(obj["paths"]), 2)
        self.assertTrue(obj["paths"][0].endswith("/AAA.mailbox/Messages/1500.eml"))
        self.assertTrue(obj["paths"][1].endswith("/ZZZ.mailbox/Messages/1600.eml"))
        code, out, err = run("--root", self.root, "--subject", "^Twin$", "--json",
                             "--mailbox", "^ZZZ$")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        obj = json.loads(lines[0])
        self.assertEqual(obj["ids"], [1600])
        self.assertEqual(len(obj["paths"]), 1)
        self.assertTrue(obj["paths"][0].endswith("/ZZZ.mailbox/Messages/1600.eml"))

    def test_interrupt_exits_130(self) -> None:
        with mock.patch("mmgrep.cli.confirm_all", side_effect=KeyboardInterrupt):
            code, out, err = run("--root", self.root, "--subject", "Plan")
        self.assertEqual(code, 130)
        self.assertEqual(out, "")
        self.assertEqual(err, "mmgrep: interrupted\n")


class TestWrapperHelp(unittest.TestCase):
    def test_wrapper_help_from_root_and_symlink(self) -> None:
        self.assertTrue(os.access(BIN, os.X_OK), "bin/mmgrep is not executable")
        result = sh('"$1" --help', str(BIN))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("usage: mmgrep"), result.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            link = os.path.join(tmp, "mm")
            os.symlink(str(BIN), link)
            result = sh('"$1" --help', link)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith("usage: mmgrep"), result.stdout)

    def test_wrapper_relative_with_cdpath(self) -> None:
        env = dict(os.environ, CDPATH="/tmp:.")
        result = subprocess.run(["sh", "-c", "bin/mmgrep --help"], cwd=str(REPO),
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("usage: mmgrep"), result.stdout)


def _pool_messages(n: int) -> list[FixtureMessage]:
    # A long To header makes each JSON line about 1 KB, so the output of
    # 210 rows is larger than a pipe buffer.
    to = ", ".join(f"Recipient {k} <recipient{k}@example.net>" for k in range(24))
    return [msg(2000 + 10 * i, Subject=f"Pool {i}", To=to,
                Date=f"Tue, 05 Mar 2024 14:{i // 60:02d}:{i % 60:02d} +0100")
            for i in range(n)]


class TestWrapperPool(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls._tmp.name, "pool")
        build_store(pathlib.Path(cls.root), _pool_messages(210))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_wrapper_pool_full(self) -> None:
        result = sh('"$1" --root "$2" --full --jobs 2 --subject Pool --json',
                    str(BIN), self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 210)
        self.assertTrue(all(json.loads(line)["confirmed"] is True for line in lines))
        self.assertEqual(result.stderr, "")

    def test_wrapper_broken_pipe_quiet(self) -> None:
        result = sh('"$1" --root "$2" --subject Pool --json | head -n 1',
                    str(BIN), self.root)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("BrokenPipeError", result.stderr)


# The SIGINT store: Many multipart files keep two workers busy for several
# seconds, so the signal at 1.5 s always lands mid-scan.
SIGINT_FILES = 3000
SIGINT_PARTS = 20
SIGINT_WAIT = 1.5


def _sigint_eml(n: int) -> bytes:
    line = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do.\n"
    text = (line * 16).encode("ascii")
    parts = b"".join(
        b"--b\nContent-Type: text/plain; charset=utf-8\n\n" + text for _ in range(SIGINT_PARTS))
    head = (f"From: sender{n}@example.org\nTo: user@example.com\n"
            f"Subject: Load {n}\nMessage-ID: <load{n}@example.com>\n"
            "Date: Tue, 05 Mar 2024 14:07:09 +0100\nMIME-Version: 1.0\n"
            'Content-Type: multipart/mixed; boundary="b"\n\n').encode("ascii")
    return head + parts + b"--b--\n"


def _kill_group(pgid: int) -> None:
    """Kill any survivor of a failed SIGINT test."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


class TestSigint(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls._tmp.name, "load")
        build_store(pathlib.Path(cls.root), [msg(1200)])
        messages = pathlib.Path(cls.root, "Messages.noindex", "IMAP", ACCOUNT,
                                "INBOX.mailbox", "Messages")
        for n in range(SIGINT_FILES):
            (messages / f"{100000 + n}.eml").write_bytes(_sigint_eml(n))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_sigint_pool_subprocess(self) -> None:
        proc = subprocess.Popen(
            [str(BIN), "--root", self.root, "--full", "--jobs", "2", "--body", "zzzq"],
            cwd="/", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        pgid = proc.pid
        self.addCleanup(_kill_group, pgid)
        try:
            time.sleep(SIGINT_WAIT)
            if proc.poll() is not None:
                out, err = proc.communicate()
                self.fail(f"run ended (exit {proc.returncode}) before SIGINT; "
                          f"enlarge SIGINT_FILES or SIGINT_PARTS: {err!r}")
            os.killpg(pgid, signal.SIGINT)
            out, err = proc.communicate(timeout=5)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
            proc.wait()
            raise
        self.assertEqual(proc.returncode, 130, err)
        self.assertEqual(err, b"mmgrep: interrupted\n")
        self.assertEqual(out, b"")
        time.sleep(1)
        with self.assertRaises(ProcessLookupError, msg="a child process survived"):
            os.killpg(pgid, 0)


class TestFlags(unittest.TestCase):
    """--flag, --no-flag and the flags of each row on a store with #flags."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = str(pathlib.Path(cls._tmp.name) / "store")
        answered = msg(1300, Subject="Asked")
        answered.index = {"#flags": "\\Seen"}
        archived = msg(1500, Subject="Asked", Message_ID="<1300@example.com>")
        archived.mailbox, archived.index = "Archive", {"#flags": "\\Answered \\Seen"}
        open_ = msg(1400, Subject="Open")
        open_.index = {"#flags": "\\Seen $MailFlagBit0"}
        build_store(pathlib.Path(cls.root), [answered, archived, open_])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_no_flag_uses_every_copy(self) -> None:
        code, out, err = run("--root", self.root, "--mailbox", "^INBOX$",
                             "--no-flag", "answered", "--json")
        self.assertEqual(code, 0, err)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([(r["subject"], r["flags"]) for r in rows],
                         [("Open", ["$MailFlagBit0", "\\Seen"])])

    def test_flag_row_lists_union(self) -> None:
        code, out, err = run("--root", self.root, "--flag", "\\answered", "--json")
        self.assertEqual(code, 0, err)
        obj = json.loads(out)
        self.assertEqual((obj["ids"], obj["flags"]), ([1500, 1300], ["\\Answered", "\\Seen"]))

    def test_verbose_table_flags_line(self) -> None:
        code, out, err = run("--root", self.root, "--flag", "$mailflagbit0", "-v")
        self.assertEqual(code, 0, err)
        self.assertIn("    flags: $MailFlagBit0 \\Seen\n", out)

    def test_flags_null_without_index(self) -> None:
        store =tempfile.TemporaryDirectory()
        self.addCleanup(store.cleanup)
        build_store(pathlib.Path(store.name), [msg(1200, Subject="Plain")])
        code, out, err = run("--root", store.name, "--subject", "^Plain$", "--json")
        self.assertEqual(code, 0, err)
        self.assertIsNone(json.loads(out)["flags"])
        self.assertNotIn("#flags", err)

    def test_flags_read_only_when_used(self) -> None:
        seen = []
        real = Store.snapshot

        def spy(store, names, *a, **kw):
            seen.append(set(names))
            return real(store, names, *a, **kw)
        with mock.patch.object(Store, "snapshot", spy):
            run("--root", self.root, "--subject", "Open")
            run("--root", self.root, "--subject", "Open", "--json")
        self.assertEqual(["#flags" in n for n in seen], [False, True])


class TestNotHeader(unittest.TestCase):
    """--not-header on the index, in confirmation and with --full."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = str(pathlib.Path(cls._tmp.name) / "store")
        news = msg(1200, Subject="News", List_Id="<news.example.com>")
        # The index has lost List-Id; only the .eml still carries it.
        stale = msg(1300, Subject="News", List_Id="<news.example.com>")
        stale.index = {"list-id": None}
        personal = msg(1400, Subject="News")
        # One message both direct (1500) and through a list (1600).
        direct = msg(1500, Subject="Both", Message_ID="<both@example.com>")
        listed = msg(1600, Subject="Both", Message_ID="<both@example.com>",
                     List_Id="<news.example.com>")
        listed.mailbox = "Lists"
        build_store(pathlib.Path(cls.root), [news, stale, personal, direct, listed])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def ids(self, *argv: str) -> tuple[list[list[int]], str]:
        code, out, err = run("--root", self.root, "--subject", "^News$", "--json", *argv)
        self.assertIn(code, (0, 1), err)
        return [json.loads(line)["ids"] for line in out.splitlines()], err

    def test_index_and_confirmation(self) -> None:
        rows, err = self.ids("--not-header", "list-id")
        self.assertEqual(rows, [[1400]])
        self.assertIn("mmgrep: 1 index hits not confirmed", err.splitlines())

    def test_pattern_and_full(self) -> None:
        self.assertEqual(self.ids("--not-header", "list-id=news")[0], [[1400]])
        self.assertEqual(self.ids("--not-header", "list-id=other")[0],
                         [[1200], [1300], [1400]])
        self.assertEqual(self.ids("--full", "--not-header", "list-id")[0], [[1400]])

    def test_copy_without_header_keeps_message(self) -> None:
        code, out, err = run("--root", self.root, "--subject", "^Both$",
                             "--not-header", "list-id", "--json")
        self.assertEqual(code, 0, err)
        obj = json.loads(out)
        self.assertEqual(obj["message_id"], "<both@example.com>")
        self.assertTrue(obj["confirmed_path"].endswith("/INBOX.mailbox/Messages/1500.eml"))

    def test_unknown_index(self) -> None:
        code, out, err = run("--root", self.root, "--not-header", "x-absent")
        self.assertEqual(code, 2)
        self.assertIn("--not-header x-absent", err)


class TestThreadFields(unittest.TestCase):
    """in_reply_to and references in JSON rows, from the .eml or the index."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = str(pathlib.Path(cls._tmp.name) / "store")
        root = msg(1200, Subject="Question")
        reply = msg(1300, Subject="Re: Question", In_Reply_To="<1200@example.com>",
                    References="<1000@example.com> <1200@example.com>")
        build_store(pathlib.Path(cls.root), [root, reply])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_confirmed_and_index_rows(self) -> None:
        for extra in ((), ("--no-confirm",)):
            with self.subTest(extra=extra):
                code, out, err = run("--root", self.root, "--subject", "Question",
                                     "--json", *extra)
                self.assertEqual(code, 0, err)
                rows = [json.loads(line) for line in out.splitlines()]
                self.assertEqual([(r["in_reply_to"], r["references"]) for r in rows], [
                    ([], []),
                    (["<1200@example.com>"], ["<1000@example.com>", "<1200@example.com>"]),
                ])

    def test_unknown_is_null(self) -> None:
        store = tempfile.TemporaryDirectory()
        self.addCleanup(store.cleanup)
        build_store(pathlib.Path(store.name), [msg(1200, Subject="Plain")])
        for extra, expected in (((), []), (("--no-confirm",), None)):
            with self.subTest(extra=extra):
                code, out, err = run("--root", store.name, "--subject", "^Plain$",
                                     "--json", *extra)
                self.assertEqual(code, 0, err)
                obj = json.loads(out)
                self.assertEqual((obj["in_reply_to"], obj["references"]),
                                 (expected, expected))

    def test_replies_to_one_message(self) -> None:
        code, out, err = run("--root", self.root, "--header",
                             "in-reply-to=<1200@example.com>", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual([json.loads(line)["ids"] for line in out.splitlines()], [[1300]])


class TestListMailboxes(_StoreCase):
    def test_table_and_json(self) -> None:
        code, out, err = run("--root", self.root, "--list-mailboxes")
        self.assertEqual(code, 0, err)
        account = "user@example.com@imap.example.com"
        self.assertEqual(out.splitlines(), [
            f"      1  {account}  AAA",
            f"      3  {account}  INBOX",
            f"      1  {account}  ZZZ",
        ])
        code, out, err = run("--root", self.root, "--list-mailboxes", "--json",
                             "--limit", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"account": account, "mailbox": "AAA",
                                           "messages": 1})

    def test_uncounted_messages_are_reported(self) -> None:
        store = tempfile.TemporaryDirectory()
        self.addCleanup(store.cleanup)
        bad = msg(1300)
        bad.raw = {"#source": [(999999, 1000000)]}  # out of the cache
        build_store(pathlib.Path(store.name), [msg(1200), bad])
        code, out, err = run("--root", store.name, "--list-mailboxes")
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("mmgrep: 1 messages without a known mailbox are not counted",
                      err.splitlines())
        self.assertIn("mmgrep: 1 skipped records in #source", err.splitlines())

    def test_account_filter_and_errors(self) -> None:
        code, out, _ = run("--root", self.root, "--list-mailboxes", "--account", "^nobody@")
        self.assertEqual((code, out), (1, ""))
        code, out, _ = run("--root", self.root, "--list-mailboxes", "-s", "--account", "^USER@")
        self.assertEqual((code, out), (1, ""))
        for argv in (["--subject", "x"], ["--account", "("], ["--who", "a"], ["-v"],
                     ["--include-quoted"], ["--flag", "seen"], ["--not-header", "subject"],
                     ["--paths-only"], ["--full"], ["--open"]):
            with self.subTest(argv=argv):
                code, out, err = run("--root", self.root, "--list-mailboxes", *argv)
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")


class AgentHelpTest(unittest.TestCase):
    def test_help_agents_prints_guide_and_exits_zero(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--help-agents"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        for key in ("message_id", "confirmed_path", "indeterminate", "--include-quoted",
                    "EXIT CODES", "130"):
            self.assertIn(key, text)

    def test_plain_help_points_to_agent_help(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("--help-agents", out.getvalue())


class RootAccessTest(unittest.TestCase):
    """An OSError reaching the root is a clean exit 2 (Codex M3 review)."""

    def test_permission_error_is_usage_exit(self) -> None:
        err = io.StringIO()
        with mock.patch("mmgrep.store.Store.snapshot",
                        side_effect=PermissionError(13, "Permission denied")), \
                contextlib.redirect_stderr(err):
            code = main(["--root", "/nonexistent-root", "--subject", "x"])
        self.assertEqual(code, 2)
        self.assertIn("Permission denied", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class TestShowBody(unittest.TestCase):
    """--show-body: The main body of each printed row, quoted text removed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = str(pathlib.Path(cls._tmp.name) / "store")
        plain = msg(1200, Subject="Plain")
        plain.body = ["First line", "Second line"]
        plain.quoted = ["Earlier text"]
        html = msg(1300, Subject="Html")
        html.content_type = "text/html"
        html.body = ["Hello <you>", "Bye"]
        html.quoted = ["Quoted html"]
        alt = msg(1400, Subject="Alternative")
        alt.multipart = "alternative"
        alt.body = []
        alt.parts = [FixtureMessage.Part(content_type="text/html", body=["Html part"]),
                     FixtureMessage.Part(content_type="text/plain", body=["Plain part"])]
        attach = msg(1500, Subject="Attachment only")
        attach.body = []
        attach.parts = [FixtureMessage.Part(content_type="application/pdf",
                                            content=b"%PDF", filename="a.pdf")]
        footer = msg(1700, Subject="Footer")
        footer.body = []
        footer.parts = [FixtureMessage.Part(content_type="text/html", body=["Real body"]),
                        FixtureMessage.Part(content_type="text/plain", body=["List footer"])]
        odd = msg(1800, Subject="Odd charset")
        odd.body = []
        odd.parts = [FixtureMessage.Part(content=b"caf\xc3\xa9", charset="x-unknown-8bit")]
        long = msg(1600, Subject="Long")
        long.body = ["x" * 20005]
        build_store(pathlib.Path(cls.root), [plain, html, alt, attach, footer, odd, long])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def rows(self, *argv: str) -> dict:
        code, out, err = run("--root", self.root, "--json", "--show-body", *argv)
        self.assertEqual(code, 0, err)
        return {r["subject"]: r for r in map(json.loads, out.splitlines())}

    def test_bodies(self) -> None:
        rows = self.rows("--any-address", "user@example.com")
        got = {s: (r["body"], r["body_truncated"]) for s, r in rows.items()
               if s != "Long"}
        self.assertEqual(got, {
            "Plain": ("First line\n\nSecond line", False),
            "Html": ("Hello <you>\n\nBye", False),
            "Alternative": ("Plain part", False),
            "Attachment only": (None, False),
            "Footer": ("Real body", False),
            "Odd charset": ("caf\u00e9", False),
        })
        self.assertEqual((len(rows["Long"]["body"]), rows["Long"]["body_truncated"]),
                         (20000, True))

    def test_include_quoted(self) -> None:
        rows = self.rows("--subject", "^(Plain|Html)$", "--include-quoted")
        self.assertEqual(rows["Plain"]["body"], "First line\n\nSecond line\n\n> Earlier text")
        self.assertEqual(rows["Html"]["body"], "Hello <you>\n\nBye\n\nQuoted html")

    def test_unreadable_file_is_null(self) -> None:
        self.assertEqual(display_body(str(pathlib.Path(self.root) / "absent.eml"), True),
                         (None, False))

    def test_keys_absent_without_option(self) -> None:
        code, out, err = run("--root", self.root, "--json", "--subject", "^Plain$")
        self.assertEqual(code, 0, err)
        self.assertNotIn("body", json.loads(out))

    def test_usage_errors(self) -> None:
        for argv in (["--subject", "x"], ["--subject", "x", "--paths-only"],
                     ["--json", "--who", "a"], ["--json", "--list-mailboxes"]):
            with self.subTest(argv=argv):
                code, out, err = run("--root", self.root, "--show-body", *argv)
                self.assertEqual((code, out), (2, ""), err)
                self.assertIn("--show-body", err)
