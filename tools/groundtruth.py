"""Exhaustive ground truth for the acceptance queries.

Reads every .eml under <root>/Messages.noindex read-only with the stdlib
email package and evaluates the four acceptance queries independently of
the mmgrep package (no imports from it). The queries come from a local,
gitignored JSON config so no real values live in the repo (see
tools/acceptance.example.json for the shape):

    python3 -I tools/groundtruth.py CONFIG OUT.json [--root PATH] [--jobs N]

OUT.json maps each query name to {message_id_or_path_key: [paths]} plus
"errors" for files or directories that could not be read. Rules mirror
the mmgrep README: Addresses from From, To, Cc and Bcc; "@domain" covers
subdomains; the Message-ID is the last such header, unfolded and
stripped; dates come from the Date header (a "-0000" zone counts as UTC)
and the date-only bounds are local midnights; patterns are
case-insensitive with MULTILINE; body text is each decoded text/plain
part and the visible text of each text/html part, searched part by part
(quoted text included). Exits 1 when any file or directory failed.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

DEFAULT_ROOT = os.path.expanduser("~/Library/Application Support/MailMate")
FIELDS = ("from", "to", "cc", "bcc")
_CFG: dict = {}


class _Visible(HTMLParser):
    """Visible text of an HTML part (character references decoded)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li", "blockquote"):
            self.out.append("\n")

    def handle_endtag(self, tag) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data) -> None:
        if not self._skip:
            self.out.append(data)


def _addrs(msg, field: str) -> list[str]:
    values = [str(v) for v in msg.get_all(field, [])]
    return [a.lower() for _, a in getaddresses(values) if "@" in a]


def _domain_hit(addr: str, domains: list[str]) -> bool:
    host = addr.rsplit("@", 1)[1]
    return any(host == d or host.endswith("." + d) for d in domains)


def _part_texts(msg) -> list[str]:
    out = []
    for part in msg.walk():
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "us-ascii", "replace")
        except Exception:
            continue
        text = text.replace("\r\n", "\n")
        if kind == "text/html":
            p = _Visible()
            p.feed(text)
            p.close()
            text = "".join(p.out)
        out.append(text)
    return out


def _decoded(msg, name: str) -> str:
    """A header as text under either policy (compat32 leaves RFC 2047 encoded)."""
    value = msg.get(name)
    if value is None:
        return ""
    if isinstance(value, str) and "=?" in value:
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value
    return str(value)


def _message_id(msg) -> str | None:
    values = msg.get_all("message-id") or []
    if not values:
        return None
    return re.sub(r"\s*\r?\n\s*", " ", str(values[-1])).strip() or None


def _init(cfg: dict) -> None:
    _CFG.update(cfg)


def _evaluate(path: str) -> tuple[str, str | None, list[str], str | None]:
    """Return (path, message_id, matching query names, error).

    The modern policy raises on some malformed headers (an IndexError in
    the stdlib header parser); such files are retried with compat32.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return path, None, [], f"{type(e).__name__}: {e}"
    try:
        return _evaluate_with(path, data, policy.default)
    except Exception:
        try:
            return _evaluate_with(path, data, policy.compat32)
        except Exception as e:
            return path, None, [], f"{type(e).__name__}: {e}"


def _evaluate_with(path: str, data: bytes, pol) -> tuple[str, str | None, list[str], None]:
    c = _CFG
    flags = re.IGNORECASE | re.MULTILINE
    msg = BytesParser(policy=pol).parsebytes(data, headersonly=True)
    mid = _message_id(msg)
    by_field = {fld: _addrs(msg, fld) for fld in FIELDS}
    every = [a for fld in FIELDS for a in by_field[fld]]
    hits = []
    domain_any = any(_domain_hit(a, c["domains"]) for a in every)
    if domain_any or any(a in c["from_addresses"] for a in by_field["from"]):
        hits.append("q1")
    if c["q2_from"] in by_field["from"]:
        try:
            d = parsedate_to_datetime(_decoded(msg, "date"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            if c["since"] <= d < c["until"]:
                hits.append("q2")
        except Exception:
            pass
    if re.search(c["q3_subject"], _decoded(msg, "subject"), flags):
        hits.append("q3")
    if domain_any or any(a in c["from_addresses"] for a in every):
        full = BytesParser(policy=pol).parsebytes(data)
        if any(re.search(c["q4_body"], t, flags) for t in _part_texts(full)):
            hits.append("q4")
    return path, mid, hits, None


def _local_midnight(day: str) -> datetime:
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day).astimezone()


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("out")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    for root in {a.root, DEFAULT_ROOT}:
        if _inside(a.out, root):
            sys.exit(f"refusing to write {a.out} inside the MailMate data root")
    with open(a.config, "rb") as f:
        raw = json.load(f)
    cfg = {
        "domains": [d.lower() for d in raw["domains"]],
        "from_addresses": [x.lower() for x in raw["from_addresses"]],
        "q2_from": raw["q2_from"].lower(),
        "since": _local_midnight(raw["q2_since"]),
        "until": _local_midnight((date.fromisoformat(raw["q2_until"])
                                  + timedelta(days=1)).isoformat()),
        "q3_subject": raw["q3_subject"],
        "q4_body": raw["q4_body"],
    }
    start = time.monotonic()
    errors: dict[str, str] = {}

    def walk_error(e: OSError) -> None:
        errors[e.filename or "?"] = f"{type(e).__name__}: {e}"

    paths = []
    for dirpath, _dirs, files in os.walk(os.path.join(a.root, "Messages.noindex"),
                                         onerror=walk_error):
        paths.extend(os.path.join(dirpath, n) for n in files if n.endswith(".eml"))
    result: dict[str, dict] = {q: {} for q in ("q1", "q2", "q3", "q4")}
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(min(a.jobs, 4), initializer=_init, initargs=(cfg,)) as pool:
        for path, mid, hits, err in pool.imap_unordered(_evaluate, paths, chunksize=256):
            if err:
                errors[path] = err
            for q in hits:
                result[q].setdefault(mid or "path:" + path, []).append(path)
    result["errors"] = errors
    with open(a.out, "x", encoding="utf-8") as f:
        json.dump(result, f)
    took = time.monotonic() - start
    counts = ", ".join(f"{q}={len(v)}" for q, v in result.items() if q != "errors")
    print(f"scanned {len(paths)} files in {took:.0f} s, errors {len(errors)}: {counts}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
