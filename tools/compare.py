"""Compare mmgrep results with tools/groundtruth.py output.

    python3 -I tools/compare.py CONFIG GROUNDTRUTH.json DIFF_OUT.json

Runs bin/mmgrep --json for each acceptance query, prints counts, set
differences, confirmation statuses and timings (no mail values) and
writes the differing keys with their paths to DIFF_OUT.json (a new file)
for local inspection. Query 1 is "any address at the domains OR from the
listed addresses", so it is the union of two mmgrep runs. Query 4 runs
twice: Default (unquoted body text only, so it may miss messages the
scan finds) and with --include-quoted (expected to match exactly).

Exits 1 when a run fails (exit code other than 0 or 1), the ground truth
recorded errors, a row is not confirmed, or a difference is unexpected.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MMGREP = os.path.join(REPO, "bin", "mmgrep")
DEFAULT_ROOT = os.path.expanduser("~/Library/Application Support/MailMate")


def run(args: list[str]) -> tuple[dict[str, list[str]], dict[str, int], float, int, str]:
    start = time.monotonic()
    p = subprocess.run([MMGREP, "--json", *args], capture_output=True, text=True)
    took = time.monotonic() - start
    rows: dict[str, list[str]] = {}
    status: dict[str, int] = {}
    for line in p.stdout.splitlines():
        r = json.loads(line)
        key = r["message_id"] or "path:" + r["paths"][0]
        rows[key] = r["paths"]
        s = str(r["confirmed"])
        status[s] = status.get(s, 0) + 1
    return rows, status, took, p.returncode, p.stderr.strip()


def main() -> int:
    cfg_path, gt_path, out_path = sys.argv[1:4]
    real_root = os.path.realpath(DEFAULT_ROOT)
    if os.path.realpath(out_path).startswith(real_root + os.sep):
        sys.exit(f"refusing to write {out_path} inside the MailMate data root")
    with open(cfg_path, "rb") as f:
        c = json.load(f)
    with open(gt_path, "rb") as f:
        gt = json.load(f)
    ok = True
    if gt.get("errors"):
        print(f"ground truth recorded {len(gt['errors'])} errors")
        ok = False
    doms = [x for d in c["domains"] for x in ("--any-address", "@" + d)]
    froms = [x for a in c["from_addresses"] for x in ("--from", a)]
    anys = doms + [x for a in c["from_addresses"] for x in ("--any-address", a)]
    queries = {
        "q1": [doms, froms],
        "q2": [["--from", c["q2_from"], "--since", c["q2_since"], "--until", c["q2_until"]]],
        "q3": [["--subject", c["q3_subject"]]],
        "q4": [anys + ["--body", c["q4_body"]]],
        "q4-quoted": [anys + ["--body", c["q4_body"], "--include-quoted"]],
    }
    diffs = {}
    for name, runs in queries.items():
        got: dict[str, list[str]] = {}
        status: dict[str, int] = {}
        took = 0.0
        codes = []
        for args in runs:
            rows, st, t, code, err = run(args)
            got.update(rows)
            for k, v in st.items():
                status[k] = status.get(k, 0) + v
            took += t
            codes.append(code)
            if code not in (0, 1):
                print(f"{name}: mmgrep exited {code}: {err}")
                ok = False
            elif err:
                print(f"{name}: mmgrep diagnostics: {err}")
        truth = gt[name.split("-")[0]]
        only_gt = sorted(set(truth) - set(got))
        only_mm = sorted(set(got) - set(truth))
        if set(status) - {"True"}:
            ok = False
        if only_mm or (only_gt and name != "q4"):
            ok = False
        print(f"{name}: truth {len(truth)}, mmgrep {len(got)} {status}, "
              f"only truth {len(only_gt)}, only mmgrep {len(only_mm)}, "
              f"{took:.2f} s, exit {codes}")
        diffs[name] = {"only_truth": {k: truth[k] for k in only_gt},
                       "only_mmgrep": {k: got[k] for k in only_mm}}
    with open(out_path, "x", encoding="utf-8") as f:
        json.dump(diffs, f, indent=1)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
