"""Safety tests for the package (milestone 2 Task 5, milestone 3 Task 7).

The read-only tests build a synthetic store, record the bytes, size, mode
and mtime of every path under its root plus the path list, run every read
operation of the package and every CLI mode and assert that nothing
changed. The static scan
checks the package source for forbidden calls and for any open() other
than open(path, "rb") (ruling C4). Failures name file and line only.
Synthetic data with RFC 2606 domains only.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import os
import pathlib
import re
import tempfile
import tokenize
import unittest
from unittest import mock

import mmgrep
from mmgrep import confirm
from mmgrep.cli import main
from mmgrep.index import required_literal
from mmgrep.source import eml_path, message_of_part, parse_source
from mmgrep.store import Store
from tests.fixtures import FixtureMessage, FixturePart, build_store

ACCOUNT = "user%40example.com@imap.example.com"
OTHER = "bob@mail.example.org"
BODY_INDEXES = ("#unquoted", "#quoted")
PACKAGE_DIR = pathlib.Path(mmgrep.__file__).resolve().parent

# Substrings that must not occur in package code once comments and
# docstrings are gone (design section 4, plan Task 5 and decision D5).
FORBIDDEN = (
    "flock", "fcntl", "mmap", "os.remove", "os.unlink", "os.rename",
    "os.replace", "utime", "chmod", "setxattr", "shutil", "tempfile",
    "write_bytes", "write_text", ".touch(", "mkdir", "makedirs",
    "subprocess", "ctypes",
)

# Synthetic sources the scanner must flag, and sources it must pass.
BAD_SOURCES = (
    'open(p, "wb")\n',
    'open(p, "r")\n',
    'open(p, mode="r")\n',
    "open(p)\n",
    "open(p, m)\n",
    "os.open(p, 0)\n",
    "os.unlink(p)\n",
    "from os import remove\n",
    "from os import unlink as u\n",
    "import os as o\n",
    "Path(p).unlink()\n",
    "os.rmdir(p)\n",
    "os.truncate(p, 0)\n",
    'io.FileIO(p, "w")\n',
    'os.fdopen(fd, "w")\n',
    "f = open\n",
    "os.open(p, os.O_RDONLY)\n",
    "os.lockf(fd, 1, 0)\n",
    "os.chflags(p, 0)\n",
    "os.lchflags(p, 0)\n",
    "Path(p).read_bytes()\n",
    "Path(p).read_text()\n",
    "os.system(c)\n",
    "os.popen(c)\n",
    "os.posix_spawn(c, a, e)\n",
    "os.posix_spawnp(c, a, e)\n",
    "os.execv(c, a)\n",
    "os.execve(c, a, e)\n",
    "os.execl(c, a)\n",
    "os.execlp(c, a)\n",
    "os.execvp(c, a)\n",
    "os.execvpe(c, a, e)\n",
    "os.spawnv(0, c, a)\n",
    "os.spawnve(0, c, a, e)\n",
    "os.spawnl(0, c, a)\n",
    "os.spawnlp(0, c, a)\n",
    "os.spawnvp(0, c, a)\n",
    "os.spawnvpe(0, c, a, e)\n",
)
GOOD_SOURCES = (
    'open(p, "rb")\n',
    'open(p, mode="rb")\n',
    '"""Never call os.unlink or flock here."""\n# open(p, "w") and mmap\nx = 1\n',
    "os.scandir(p)\n",
    "os.path.exists(p)\n",
)
# Every module of the package after milestone 3; the scan must see them all.
MODULE_STEMS = frozenset({"__init__", "__main__", "cli", "confirm", "index",
                          "output", "query", "scan", "source", "store",
                          "mailmate_ui", "who", "agent_help"})
UI_MODULE = "mailmate_ui.py"


def _strip(text: str) -> list[str]:
    """The source lines with comments and docstrings blanked by spaces.

    A docstring here is any string statement: STRING tokens that start a
    logical line and are followed only by its NEWLINE. Positions and line
    numbers are kept, so later findings point at the original lines.
    """
    lines = [list(line) for line in text.splitlines(keepends=True)]
    blank: list[tuple[tuple[int, int], tuple[int, int]]] = []
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    starters = {tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                tokenize.DEDENT, tokenize.ENCODING}
    prev_type = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == tokenize.COMMENT:
            blank.append((tok.start, tok.end))
        elif tok.type == tokenize.STRING and (prev_type is None or prev_type in starters):
            j = i
            while j < len(tokens) and tokens[j].type in (tokenize.STRING, tokenize.NL,
                                                         tokenize.COMMENT):
                j += 1
            if j == len(tokens) or tokens[j].type in (tokenize.NEWLINE,
                                                      tokenize.ENDMARKER):
                blank.append((tok.start, tokens[j - 1].end))
                prev_type = tokenize.NEWLINE
                i = j
                continue
        if tok.type not in (tokenize.NL, tokenize.COMMENT):
            prev_type = tok.type
        i += 1
    for (srow, scol), (erow, ecol) in blank:
        for row in range(srow, erow + 1):
            chars = lines[row - 1]
            start = scol if row == srow else 0
            end = ecol if row == erow else len(chars)
            for c in range(start, end):
                if chars[c] != "\n":
                    chars[c] = " "
    return ["".join(chars) for chars in lines]


def _open_ok(call: ast.Call) -> bool:
    """True only for the builtin open(path, "rb") or open(path, mode="rb")."""
    if not isinstance(call.func, ast.Name):
        return False  # os.open, io.open, Path.open and the like
    if len(call.args) == 2 and not call.keywords:
        mode = call.args[1]
    elif len(call.args) == 1 and len(call.keywords) == 1 and call.keywords[0].arg == "mode":
        mode = call.keywords[0].value
    else:
        return False
    return isinstance(mode, ast.Constant) and mode.value == "rb"


# Names that mutate, remove or reopen files, flagged as any attribute or name.
# Path.replace cannot be told apart from str.replace by name, so it is a
# known gap; os.replace is caught by the substring scan.
MUTATING_NAMES = frozenset({
    "unlink", "rmdir", "rename", "renames", "removedirs", "symlink",
    "symlink_to", "hardlink_to", "link", "link_to", "truncate", "ftruncate",
    "FileIO", "fdopen", "chown", "lchown", "mkfifo", "mknod", "__import__",
    "lockf", "chflags", "lchflags", "read_bytes", "read_text",
    "system", "popen", "posix_spawn", "posix_spawnp",
    "execv", "execve", "execl", "execlp", "execvp", "execvpe",
    "spawnv", "spawnve", "spawnl", "spawnlp", "spawnvp", "spawnvpe",
})
IMPORT_MODULES = frozenset({"os", "io", "builtins", "pathlib"})
ALIAS_MODULES = frozenset({"os", "io", "pathlib"})
# The one honest from-import of these modules that the package uses. Path
# methods stay visible to the attribute check, so it hides nothing.
ALLOWED_FROM = frozenset({("pathlib", "Path")})


def _ast_extra(tree: ast.AST) -> set[int]:
    """Lines with aliased imports, mutating names or a bare open reference."""
    found: set[int] = set()
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in IMPORT_MODULES and any(
                    (node.module, a.name) not in ALLOWED_FROM or a.asname
                    for a in node.names):
                found.add(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name in ALIAS_MODULES and a.asname for a in node.names):
                found.add(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr in MUTATING_NAMES:
            found.add(node.lineno)
        elif isinstance(node, ast.Name):
            if node.id in MUTATING_NAMES:
                found.add(node.lineno)
            elif (node.id == "open" and isinstance(node.ctx, ast.Load)
                  and id(node) not in called):
                found.add(node.lineno)
    return found


def scan_source(text: str, name: str) -> list[str]:
    """Findings as "name:line" strings, sorted; empty when the source is clean."""
    found: set[int] = set()
    for number, line in enumerate(_strip(text), 1):
        if any(term in line for term in FORBIDDEN):
            found.add(number)
    tree = ast.parse(text, filename=name)
    found |= _ast_extra(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_open = ((isinstance(func, ast.Name) and func.id == "open")
                   or (isinstance(func, ast.Attribute) and func.attr == "open"))
        if is_open and not _open_ok(node):
            found.add(node.lineno)
    return [f"{name}:{n}" for n in sorted(found)]


def _fingerprint(root: pathlib.Path) -> dict[str, tuple]:
    """Per path under root (root included): Kind, SHA-256, size, mode, mtime."""
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for entry in [dirpath] + [os.path.join(dirpath, n) for n in dirnames + filenames]:
            st = os.lstat(entry)
            digest = None
            if os.path.isfile(entry) and not os.path.islink(entry):
                with open(entry, "rb") as f:
                    digest = hashlib.sha256(f.read()).hexdigest()
            out[os.path.relpath(entry, root)] = (digest, st.st_size, st.st_mode,
                                                 st.st_mtime_ns)
    return out


def _msg(id: int, subject: str, **kw) -> FixtureMessage:
    headers = {"From": f"sender{id}@example.org", "Subject": subject,
               "Message-ID": f"<{id}@example.com>",
               "Date": "Tue, 05 Mar 2024 14:07:09 +0100"}
    kw.setdefault("account", ACCOUNT)
    kw.setdefault("mailbox", "INBOX")
    return FixtureMessage(id=id, headers=headers, **kw)


FIVE = [
    _msg(1200, "Hello", parts=[FixturePart(body=["Hi there"], quoted=["older there"]),
                               FixturePart(content_type="text/html", body=["See you"])],
         multipart="alternative"),
    _msg(1300, "Second", body=["there and back"], quoted=["quoted there"]),
    _msg(1400, "Third", body=["one"], account=OTHER, mailbox="Archive/2024"),
    _msg(1500, "Second", mailbox="Sent Messages", duplicate_of=1300),
    _msg(1600, "Fifth", body=["fresh there"], new=True),
]


class ReadOnlyTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name) / "store"

    def _exercise(self, store: Store):
        """Run every read operation of the package once."""
        snap = store.snapshot(store.index_names(), backoff=0)
        pattern = re.compile("there", re.M)
        literal = required_literal(pattern.pattern, pattern.flags)
        self.assertEqual(literal, b"there")
        hits: set[int] = set()
        for name in BODY_INDEXES:
            index = snap.indexes[name]
            with_literal = index.search(pattern, literal)
            without = index.search(pattern)
            self.assertEqual(with_literal, without)
            hits |= with_literal
        self.assertTrue(hits)
        found, _ = message_of_part(snap, hits)
        self.assertTrue(found)
        paths = []
        source = snap.indexes["#source"]
        for msg_id in sorted(snap.live):
            value = source.last(msg_id)
            self.assertIsNotNone(value)
            ref = parse_source(value)
            self.assertIsNotNone(ref)
            paths.append(eml_path(store.messages_dir, ref, msg_id))
        self.assertEqual(len(paths), 5)
        return snap

    def _check(self, **kw):
        build_store(self.root, FIVE, **kw)
        before = _fingerprint(self.root)
        self.assertGreater(len(before), 20)
        snap = self._exercise(Store(self.root))
        after = _fingerprint(self.root)
        self.assertEqual(sorted(after), sorted(before), "paths added or removed")
        changed = [p for p in before if before[p] != after[p]]
        self.assertEqual(changed, [], "paths changed")
        return snap

    def test_read_only_clean_store(self) -> None:
        snap = self._check()
        self.assertFalse(snap.unstable)
        self.assertEqual(snap.retries, 0)

    def test_read_only_torn_store(self) -> None:
        # A torn record is unstable on every attempt by design (R11), so
        # every retry path runs.
        snap = self._check(torn={"subject": b"x"})
        self.assertTrue(snap.unstable)
        self.assertEqual(snap.retries, 3)

    def test_read_only_sets_fallback(self) -> None:
        # Without Msgs.ids the live set comes from #source (R8), read as a
        # fallback because #source is not among the requested names.
        build_store(self.root, FIVE)
        (self.root / "Database.noindex" / "Sets" / "Msgs.ids").unlink()
        before = _fingerprint(self.root)
        store = Store(self.root)
        names = sorted(store.index_names() - {"#source"})
        snap = store.snapshot(names, backoff=0)
        after = _fingerprint(self.root)
        self.assertEqual(sorted(after), sorted(before), "paths added or removed")
        changed = [p for p in before if before[p] != after[p]]
        self.assertEqual(changed, [], "paths changed")
        self.assertTrue(snap.unstable)
        self.assertNotIn("#source", snap.indexes)
        self.assertIn("live set taken from #source (R8 fallback)", snap.warnings)
        self.assertEqual(snap.live, frozenset({1200, 1300, 1400, 1500, 1600}))


def _run_main(*argv: str) -> tuple[int, str, str]:
    """main(argv) in process with stdout and stderr captured."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


# 210 messages: Above confirm.POOL_THRESHOLD, so --jobs 2 starts a pool.
POOL = [_msg(2000 + n, f"Pool {n}", body=[f"pool body {n}"]) for n in range(210)]


class CliReadOnlyTest(unittest.TestCase):
    """Every CLI mode leaves the store byte for byte as it was (Task 7)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)

    def _assert_unchanged(self, root: pathlib.Path, before: dict) -> None:
        after = _fingerprint(root)
        self.assertEqual(sorted(after), sorted(before), "paths added or removed")
        changed = [p for p in before if before[p] != after[p]]
        self.assertEqual(changed, [], "paths changed")

    def test_cli_modes_read_only(self) -> None:
        root = self.tmp / "five"
        build_store(root, FIVE)
        before = _fingerprint(root)
        self.assertGreater(len(before), 20)
        modes = {
            "a": ["--subject", "Second"],
            "b": ["--body", "there"],
            "c": ["--body", "there", "--include-quoted", "--full"],
            "d": ["--subject", "Second", "--no-confirm"],
        }
        for label, args in modes.items():
            with self.subTest(mode=label):
                code, out, err = _run_main(*args, "--root", str(root))
                self.assertEqual(code, 0, err)
                self.assertTrue(out)
                self._assert_unchanged(root, before)

        pool_root = self.tmp / "pool"
        build_store(pool_root, POOL)
        before = _fingerprint(pool_root)
        real = confirm.ProcessPoolExecutor
        with self.subTest(mode="e"), mock.patch.object(
                confirm, "ProcessPoolExecutor", side_effect=real) as pool:
            code, out, err = _run_main("--full", "--subject", "Pool", "--jobs", "2",
                                       "--paths-only", "--root", str(pool_root))
            self.assertEqual(code, 0, err)
            self.assertEqual(pool.call_count, 1, "pool path not taken")
            self.assertEqual(
                pool.call_args.kwargs["mp_context"].get_start_method(), "spawn")
            self.assertEqual(len(out.splitlines()), 210)
            self._assert_unchanged(pool_root, before)


class StaticScanTest(unittest.TestCase):
    def test_static_scan(self) -> None:
        files = sorted(PACKAGE_DIR.glob("*.py"))
        stems = {path.stem for path in files}
        self.assertEqual(sorted(MODULE_STEMS - stems), [], "modules missing")
        findings: list[str] = []
        for path in files:
            with open(path, "rb") as f:
                text = f.read().decode("utf-8")
            found = scan_source(text, f"mmgrep/{path.name}")
            if path.name == UI_MODULE:
                # The one sanctioned exception: mailmate_ui starts
                # /usr/bin/open (pinned by MailMateUITest); only lines that
                # name subprocess may be flagged there.
                lines = text.splitlines()
                found = [x for x in found
                         if "subprocess" not in lines[int(x.rsplit(":", 1)[1]) - 1]]
            findings += found
        self.assertEqual(findings, [])

    def test_static_scan_detects(self) -> None:
        for text in BAD_SOURCES:
            with self.subTest(source=text):
                self.assertEqual(scan_source(text, "bad.py"), ["bad.py:1"])
        for text in GOOD_SOURCES:
            with self.subTest(source=text):
                self.assertEqual(scan_source(text, "good.py"), [])


if __name__ == "__main__":
    unittest.main()
