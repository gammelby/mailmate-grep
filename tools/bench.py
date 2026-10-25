"""Benchmark of the mmgrep index core (milestone 2, Task 6).

Not part of the package. Usage:

    python3 -I tools/bench.py synthetic --out DIR [--scale F]
    python3 -I tools/bench.py real [--root PATH]
    python3 -I tools/bench.py query --out DIR [--scale F]

The query mode (milestone 3, ruling RP2) builds the same synthetic store
and times Store.snapshot, query.candidates and query.group_copies for one
header query and one body query narrowed by an address. It is index only
and excludes confirmation, since the synthetic store has no .eml files.

The synthetic writer (plan decision D6) writes a store with its own fast
writer: One array("I") and one bytearray per index and plists in the
docs/FORMAT.md section 2 form. Every value is synthetic and uses RFC 2606
domains. It writes only under its out directory and refuses an out inside
DEFAULT_ROOT or an out that exists and is not an empty real directory. The real run reads the store only through mmgrep.store.Store.
Output lines carry names, seconds and counts, never values.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import resource
import sys
import time
from array import array
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    # python3 -I drops the script directory and the cwd from sys.path.
    sys.path.insert(0, str(REPO))

from mmgrep.index import required_literal  # noqa: E402
from mmgrep.store import DEFAULT_ROOT, Store  # noqa: E402

SYNTHETIC_UUID = "00000000-0000-4000-8000-0000000b3c00"
MESSAGES = 190_000
ROOT_PARTS = 380_000
BODY_RECORDS = 3_000_000
BODY_BYTES = 134_000_000
TOKEN = "zebrafinch"
HEADER_INDEXES = ("from", "to", "cc", "bcc", "subject", "message-id",
                  "#date", "#source", "#root-body-part")
BODY_INDEX = "#unquoted"
# Indexes whose plist writes stringsFileSize quoted (FORMAT.md section 2).
QUOTED_SIZE = frozenset({"subject", "#source"})
MAILBOXES = ("INBOX", "Archive/2024", "Sent Messages", "Lists/alpha")
ACCOUNT = "user%40example.com@imap.example.com"
SYLLABLES = ("ka", "lo", "mi", "nu", "pe", "ro", "sa", "ti", "vo", "du",
             "gel", "mar", "tos", "wen", "bri", "fal", "kø", "zu")
COMMON = ("thanks again", "see you soon", "best wishes from the team",
          "kind regards", "sent from a synthetic device")
REGEX = r"[0-9]{3}-[0-9]{4}"
# Like REGEX but required_literal finds no literal in it.
NOLIT = r"[0-9]{3}[-.][0-9]{4}"
REAL_LITERAL = "2024"
REAL_ICASE = "meeting"


def _mid(i: int) -> int:
    """Message id of synthetic message i; its parts are mid + 1 and mid + 2."""
    return 1000 + 3 * i


def _n(count: int, scale: float) -> int:
    return max(1, round(count * scale))


class _Writer:
    """One index: Records in an array("I") and values in a bytearray."""

    def __init__(self) -> None:
        self.offsets = array("I")
        self.cache = bytearray()
        self.seen: dict[bytes, tuple[int, int]] = {}

    def add(self, rid: int, value: bytes, share: bool = True) -> None:
        rng = self.seen.get(value) if share else None
        if rng is None:
            start = len(self.cache)
            self.cache += value
            self.cache += b"\n"
            rng = (start, start + len(value))
            if share:
                self.seen[value] = rng
        self.offsets.extend((rid, rng[0], rng[1]))

    def clear(self, rid: int) -> None:
        self.offsets.extend((rid, 1, 0))


def _inside(path: str, root: str) -> bool:
    path, root = path.lower(), root.lower()
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _check_out(out: Path) -> None:
    """Refuse an out inside DEFAULT_ROOT without touching DEFAULT_ROOT."""
    root = os.path.abspath(str(DEFAULT_ROOT))
    for candidate in (os.path.abspath(str(out)), os.path.realpath(str(out))):
        if _inside(candidate, root):
            raise ValueError("refusing to write a synthetic store inside DEFAULT_ROOT")
    # A symlink anywhere below out could lead the writes elsewhere, so out
    # must be new or an empty real directory. Everything below it is then
    # created by build_synthetic with mkdir(exist_ok=False) and mode "xb".
    if os.path.islink(out):
        raise ValueError("refusing a symlink as the synthetic store directory")
    if os.path.lexists(out):
        if not os.path.isdir(out):
            raise ValueError("synthetic store path exists and is not a directory")
        if os.listdir(out):
            raise ValueError("synthetic store directory is not empty")


def _write_new(path: Path, data: bytes) -> None:
    """Create path and write data; "xb" refuses any existing path or symlink."""
    with open(path, "xb") as f:
        f.write(data)


def _plist(name: str, offsets_size: int, strings_size: int) -> bytes:
    strings = f'"{strings_size}"' if name in QUOTED_SIZE else str(strings_size)
    return (f"{{\toffsetsFileSize = {offsets_size};\n"
            f"\tstringsFileSize = {strings};\n}}").encode("ascii")


def _main_plist(largest: int) -> bytes:
    return (f'{{\tUUID = "{SYNTHETIC_UUID}";\n'
            f"\tlargestUsedBodyPartID = {largest};\n"
            f'\trevision = "1";\n\tversion = "1.1";\n}}').encode("ascii")


def _ids_bytes(ids: list[int]) -> bytes:
    arr = array("I", ids)
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


def _headers(n: int, parts: int, rng: random.Random, vocab: list[str],
             seed: int) -> dict[str, _Writer]:
    w = {name: _Writer() for name in HEADER_INDEXES}
    base = 1_704_067_200  # 2024-01-01 00:00:00 UTC
    zones = ((0, "+0000"), (3600, "+0100"), (-18000, "-0500"), (19800, "+0530"))
    for i in range(n):
        m = _mid(i)
        w["from"].add(m, f"sender{(i // 3) % 9973}@example.com".encode())
        w["to"].add(m, f"Rcpt {i % 1999} <rcpt{i % 1999}@example.org>".encode())
        w["cc"].add(m, f"copy{i % 499}@example.net".encode())
        w["bcc"].add(m, f"blind{i % 97}@example.net".encode())
        subject = " ".join(rng.choices(vocab, k=rng.randint(2, 6))).encode()
        w["subject"].add(m, subject)
        if i % 1000 == 2:
            # A draft resave: Marker, then the new value (FORMAT.md section 3).
            w["subject"].clear(m)
            w["subject"].add(m, b"re " + subject)
        w["message-id"].add(m, f"<{i}.{seed}@example.net>".encode(), share=False)
        shift, zone = zones[i % len(zones)]
        stamp = time.gmtime(base + i * 613 + shift)
        w["#date"].add(m, (time.strftime("%Y-%m-%d %H:%M:%S ", stamp) + zone).encode())
        box = MAILBOXES[i % len(MAILBOXES)]
        w["#source"].add(m, f"imap://{ACCOUNT}/{box}".encode())
        if i % 1000 == 3:
            # A move: Marker, then the new mailbox URL.
            w["#source"].clear(m)
            box = MAILBOXES[(i + 1) % len(MAILBOXES)]
            w["#source"].add(m, f"imap://{ACCOUNT}/{box}".encode())
    for j in range(parts):
        i = (j // 2) % n
        m = _mid(i)
        w["#root-body-part"].add(m + 1 + j % 2, str(m).encode())
    return w


def _body(n: int, count: int, mean: float, rng: random.Random,
          vocab: list[str]) -> _Writer:
    w = _Writer()
    low = 8
    high = max(low, int(round(2 * mean)) - low)
    for k in range(count):
        # The drift k // 1000 spreads the planted records over many ids.
        i = (k + k // 1000) % n
        rid = _mid(i) + 1 + (k // n) % 2
        if k % 1000 == 41:
            # A body reindex clears the segments recorded so far for rid.
            w.clear(rid)
        if k % 30 == 0:
            # Shared ranges: One stored value referenced by many ids.
            w.add(rid, COMMON[(k // 60) % 2 + 2 * ((k // 1000) % 2)].encode())
            continue
        words = rng.choices(vocab, k=14)
        if k % 1000 == 7:
            words.insert(1, TOKEN)
        if k % 500 == 3:
            words.insert(2, f"ref {k % 1000:03d}-{k % 10000:04d}")
        text = " ".join(words)
        cut = rng.randint(low, high)
        if k % 1000 != 7 and k % 500 != 3:
            text = text[:cut].rstrip()
        if k % 4 == 1:
            text = text.replace(" ", "\n", 1)
        w.add(rid, text.encode("utf-8"), share=False)
    return w


def build_synthetic(out: Path, scale: float = 1.0, seed: int = 20261025) -> None:
    """Write a synthetic store under out, every size multiplied by scale."""
    out = Path(out)
    _check_out(out)
    rng = random.Random(seed)
    vocab = sorted({"".join(rng.choices(SYLLABLES, k=rng.randint(1, 4)))
                    for _ in range(3000)})
    vocab = [v for v in vocab if TOKEN not in v]
    n = _n(MESSAGES, scale)
    parts = _n(ROOT_PARTS, scale)
    count = _n(BODY_RECORDS, scale)
    writers = _headers(n, parts, rng, vocab, seed)
    writers[BODY_INDEX] = _body(n, count, BODY_BYTES / BODY_RECORDS, rng, vocab)

    if not os.path.lexists(out):
        out.mkdir(parents=True)
    db = out / "Database.noindex"
    headers = db / "Headers"
    sets = db / "Sets"
    for directory in (db, headers, sets):
        directory.mkdir(exist_ok=False)
    for name, w in writers.items():
        if sys.byteorder == "big":
            w.offsets.byteswap()
        offsets = w.offsets.tobytes()
        _write_new(headers / f"{name}.cache", w.cache)
        _write_new(headers / f"{name}.offsets", offsets)
        _write_new(headers / f"{name}.plist", _plist(name, len(offsets), len(w.cache)))
    ids = [_mid(i) for i in range(n)]
    _write_new(sets / "Msgs.ids", _ids_bytes(ids[:-1] if n > 1 else ids))
    _write_new(sets / "NewMsgs.ids", _ids_bytes(ids[-1:] if n > 1 else []))
    _write_new(db / "Main.plist", _main_plist(_mid(n - 1) + 2))


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Bytes on macOS, kilobytes on Linux.
    return peak / (2 ** 20 if sys.platform == "darwin" else 2 ** 10)


def _timed_search(index, pattern: re.Pattern[str]) -> tuple[float, float, int]:
    literal = required_literal(pattern.pattern, pattern.flags)
    t0 = time.perf_counter()
    cold = index.search(pattern, literal)
    t1 = time.perf_counter()
    warm = index.search(pattern, literal)
    t2 = time.perf_counter()
    if cold != warm:
        raise RuntimeError("cold and warm searches differ")
    return t1 - t0, t2 - t1, len(cold)


def run(root: Path) -> dict[str, float | int]:
    """Time the header snapshot and four body searches on the store at root.

    Patterns depend on the store: The synthetic store (its Main.plist UUID)
    gets the planted token, any other store the real-store words. Every
    literal comes from required_literal. grouping_s times the lazy grouping
    of the body Index (plan decision D4): The call index.ids() plus the
    range groups and range layout that search builds on first use. It runs
    once, right after the body snapshot and before any search. Each search then runs
    twice on the same Index: A *_cold_s key is the first run of that
    pattern after grouping and *_warm_s the second. Besides the Step 1 keys
    the result holds body_snapshot_s, grouping_s, unstable (0 or 1, both
    snapshots), retries and skipped_records (summed over both snapshots)
    and warnings (a count).
    """
    store = Store(Path(root))
    t0 = time.perf_counter()
    head = store.snapshot(HEADER_INDEXES)
    t1 = time.perf_counter()
    body = store.snapshot([BODY_INDEX])
    t2 = time.perf_counter()
    index = body.indexes.get(BODY_INDEX)
    if index is None:
        raise RuntimeError(f"index {BODY_INDEX} is missing")
    t3 = time.perf_counter()
    index.ids()
    index._range_groups()
    index._disjoint_layout()
    t4 = time.perf_counter()
    if head.main.get("UUID") == SYNTHETIC_UUID:
        words = (TOKEN, TOKEN)
    else:
        words = (REAL_LITERAL, REAL_ICASE)
    lit = re.compile(re.escape(words[0]), re.M)
    icase = re.compile(re.escape(words[1]), re.I | re.M)
    regex = re.compile(REGEX, re.M)
    nolit = re.compile(NOLIT, re.M)
    result: dict[str, float | int] = {
        "snapshot_s": t1 - t0,
        "body_snapshot_s": t2 - t1,
        "grouping_s": t4 - t3,
    }
    for key, pattern in (("literal", lit), ("icase", icase), ("regex", regex),
                         ("nolit", nolit)):
        cold, warm, hits = _timed_search(index, pattern)
        result[f"{key}_cold_s"] = cold
        result[f"{key}_warm_s"] = warm
        result[f"{key}_hits"] = hits
    result["records"] = index.record_count
    result["peak_rss_mb"] = _peak_rss_mb()
    result["unstable"] = int(head.unstable or body.unstable)
    result["retries"] = head.retries + body.retries
    result["skipped_records"] = (sum(head.skipped_records.values())
                                 + sum(body.skipped_records.values()))
    result["warnings"] = len(head.warnings) + len(body.warnings)
    return result


QUERY_LABEL = "index only, excludes confirmation"
# Header query: One recipient (about 1 in 1999 messages). Body query: The
# planted token narrowed by one cc address (about 1 in 499 messages).
HEADER_ADDRESS = "rcpt7@example.org"
NARROW_ADDRESS = "copy7@example.net"


def run_query(root: Path) -> dict[str, float | int]:
    """Time snapshot, candidates and group_copies for two queries.

    The synthetic store has no .eml files, so every group is dropped and
    confirmation never runs: Each total is index only. For each query q the
    result holds q_snapshot_s, q_candidates_s, q_group_s, q_total_s,
    q_candidates (the candidate count) and q_missing_eml.
    """
    from mmgrep import query

    store = Store(Path(root))
    queries = (
        ("header", query.Filters(any_address=[query.parse_addr(HEADER_ADDRESS)])),
        ("body", query.Filters(body=TOKEN,
                               any_address=[query.parse_addr(NARROW_ADDRESS)])),
    )
    result: dict[str, float | int] = {}
    for key, f in queries:
        t0 = time.perf_counter()
        snap = store.snapshot(query.needed_indexes(f))
        t1 = time.perf_counter()
        cands = query.candidates(snap, f)
        t2 = time.perf_counter()
        grouped = query.group_copies(snap, cands.ids, f, store.messages_dir)
        t3 = time.perf_counter()
        result[f"{key}_snapshot_s"] = t1 - t0
        result[f"{key}_candidates_s"] = t2 - t1
        result[f"{key}_group_s"] = t3 - t2
        result[f"{key}_total_s"] = t3 - t0
        result[f"{key}_candidates"] = len(cands.ids)
        result[f"{key}_missing_eml"] = grouped.missing_eml
    result["peak_rss_mb"] = _peak_rss_mb()
    return result


def _print(result: dict[str, float | int]) -> None:
    for key, value in result.items():
        if isinstance(value, float):
            print(f"{key} {value:.3f}")
        else:
            print(f"{key} {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.py")
    sub = parser.add_subparsers(dest="mode", required=True)
    syn = sub.add_parser("synthetic", help="build a synthetic store and time it")
    syn.add_argument("--out", type=Path, required=True)
    syn.add_argument("--scale", type=float, default=1.0)
    real = sub.add_parser("real", help="time the real store, read only")
    real.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    qry = sub.add_parser("query", help="build a synthetic store and time two "
                         f"queries ({QUERY_LABEL})")
    qry.add_argument("--out", type=Path, required=True)
    qry.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.mode == "query":
        t0 = time.perf_counter()
        build_synthetic(args.out, args.scale)
        print(f"build_s {time.perf_counter() - t0:.3f}")
        print(f"label {QUERY_LABEL}")
        _print(run_query(args.out))
        return 0
    if args.mode == "synthetic":
        t0 = time.perf_counter()
        build_synthetic(args.out, args.scale)
        print(f"build_s {time.perf_counter() - t0:.3f}")
        root = args.out
    else:
        root = args.root
    _print(run(root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
