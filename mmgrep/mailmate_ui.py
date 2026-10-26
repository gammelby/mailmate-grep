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
from dataclasses import dataclass
from typing import Callable, Iterable

MAX_IDS = 50
MAX_ID_LENGTH = 250
MAX_URL_BYTES = 16000
PREFIX = "mlmt:quicksearch?string="
# Message-IDs MailMate's search syntax can take as one bare term.
_SAFE = re.compile(r"[^\s()\"'<>]+")


@dataclass
class SearchPlan:
    url: str | None
    used: int = 0
    skipped: int = 0      # missing, unsafe or overlong Message-IDs
    duplicates: int = 0   # rows sharing a Message-ID already used
    capped: int = 0       # usable ids left out by MAX_IDS or MAX_URL_BYTES


def search_url(message_ids: Iterable[str | None]) -> SearchPlan:
    """The quicksearch URL for up to MAX_IDS Message-IDs within MAX_URL_BYTES."""
    terms: list[str] = []
    plan = SearchPlan(url=None)
    length = len(PREFIX)
    for mid in message_ids:
        core = (mid or "").strip().strip("<>").strip()
        if len(core) > MAX_ID_LENGTH or not _SAFE.fullmatch(core):
            plan.skipped += 1
            continue
        term = "message-id:" + core
        if term in terms:
            plan.duplicates += 1
            continue
        piece = len(urllib.parse.quote((" or " if terms else "") + term, safe=""))
        if len(terms) >= MAX_IDS or length + piece > MAX_URL_BYTES:
            plan.capped += 1
            continue
        terms.append(term)
        length += piece
    if terms:
        plan.url = PREFIX + urllib.parse.quote(" or ".join(terms), safe="")
        plan.used = len(terms)
    return plan


def open_url(url: str, run: Callable = subprocess.run) -> int:
    """Hand an mlmt: URL to macOS; returns the exit code of /usr/bin/open."""
    if not url.startswith(PREFIX):
        raise ValueError("only mlmt:quicksearch URLs are opened")
    return run(["/usr/bin/open", url], check=False).returncode
