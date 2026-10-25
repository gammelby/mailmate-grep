"""Entry point for `python3 -m mmgrep` and bin/mmgrep.

SIGPIPE gets its default action before main runs (decision D12), so output
into a closed pipe ends the process quietly, as grep does. A Ctrl-C while
the package is still importing exits 130 with the same one line as main.
Pool workers import this module under another name and run nothing here.
"""

from __future__ import annotations

import signal
import sys


def _run() -> int:
    try:
        from mmgrep.cli import main
    except KeyboardInterrupt:
        sys.stderr.write("mmgrep: interrupted\n")
        return 130
    return main()


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(_run())
