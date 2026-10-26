"""Show results in MailMate (mmgrep --open).

The one place mmgrep starts another program: It asks macOS (/usr/bin/open)
to open an mlmt:quicksearch URL, which makes MailMate run a search for the
given Message-IDs in its selected mailbox. Nothing is read from or written
to the MailMate folder here. mlmt:quicksearch is undocumented; it was found
in the MailMate 2.0 binary and verified by hand.
"""
from __future__ import annotations

import re
import subprocess
import urllib.parse
from typing import Callable, Iterable

MAX_IDS = 50
# Message-IDs MailMate's search syntax can take as one bare term.
_SAFE = re.compile(r"[^\s()\"'<>]+")


def search_url(message_ids: Iterable[str | None]) -> tuple[str | None, int, int]:
    """The quicksearch URL for up to MAX_IDS Message-IDs.

    Returns (url or None, ids used, ids skipped). Skipped are missing ids
    and ids with characters the search syntax would misread.
    """
    terms: list[str] = []
    skipped = 0
    for mid in message_ids:
        core = (mid or "").strip().strip("<>").strip()
        if not _SAFE.fullmatch(core):
            skipped += 1
            continue
        term = "message-id:" + core
        if term not in terms:
            terms.append(term)
    terms = terms[:MAX_IDS]
    if not terms:
        return None, 0, skipped
    query = " or ".join(terms)
    return "mlmt:quicksearch?string=" + urllib.parse.quote(query, safe=""), len(terms), skipped


def open_url(url: str, run: Callable = subprocess.run) -> int:
    """Hand an mlmt: URL to macOS; returns the exit code of /usr/bin/open."""
    if not url.startswith("mlmt:quicksearch?"):
        raise ValueError("only mlmt:quicksearch URLs are opened")
    return run(["/usr/bin/open", url], check=False).returncode
