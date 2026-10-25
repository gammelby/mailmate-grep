"""Privacy hygiene for every tracked and to-be-added file (C5).

Failures name the file and line only, never the matched text, so test logs
cannot leak a real address or a denylist term.

Set MMGREP_DENYLIST to another path to point the denylist check elsewhere
(a missing path skips it).
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DENYLIST = ROOT / "tools" / "privacy-denylist.local.txt"
ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
ALLOWED_DOMAINS = ("example.com", "example.org", "example.net")
# Grammar placeholders in docs such as "user%40domain@host" and
# "local@domain". Bare words only; they are not resolvable hosts.
PLACEHOLDER_HOSTS = frozenset({"domain", "host"})


def _files() -> list[pathlib.Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout
    names = [n for n in out.decode("utf-8", errors="surrogateescape").split("\0") if n]
    return [ROOT / n for n in names if (ROOT / n).is_file()]


def _lines(path: pathlib.Path) -> list[str] | None:
    """Text lines of path; None for a binary file."""
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace").splitlines()


def _host_allowed(host: str) -> bool:
    host = host.rstrip(".").lower()
    if host in PLACEHOLDER_HOSTS:
        return True
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


class HygieneTest(unittest.TestCase):
    def test_no_real_addresses(self) -> None:
        hits = []
        for path in _files():
            lines = _lines(path)
            if lines is None:
                continue
            for number, line in enumerate(lines, 1):
                for m in ADDRESS.finditer(line):
                    if not _host_allowed(m.group().rsplit("@", 1)[1]):
                        hits.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(hits, [], "non RFC 2606 address hosts at these lines")

    def test_no_denylist_terms(self) -> None:
        denylist = pathlib.Path(os.environ.get("MMGREP_DENYLIST", str(DENYLIST)))
        if not denylist.is_file():
            self.skipTest(f"denylist absent: {denylist}")
        terms = []
        for raw in denylist.read_text(encoding="utf-8").splitlines():
            term = raw.strip()
            if term and not term.startswith("#"):
                terms.append(term.lower())
        hits = []
        for path in _files():
            if path.resolve() == denylist.resolve():
                continue
            lines = _lines(path)
            if lines is None:
                continue
            for number, line in enumerate(lines, 1):
                lowered = line.lower()
                if any(term in lowered for term in terms):
                    hits.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(hits, [], "denylist terms at these lines")


if __name__ == "__main__":
    unittest.main()
