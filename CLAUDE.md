# mmgrep: Notes for agents

Read-only command-line search over a local MailMate store using MailMate's own index.

## What to read

- The code in `mmgrep/` and its tests in `tests/` are the source of truth.
- `docs/FORMAT.md`: The reverse-engineered index format and the reader rules R1 to R11.
- `README.md`: Usage, caveats and known limits.
- `docs/archive/` holds stale planning records (including the original design, which "design 6a" style citations in comments refer to). Do not read it unless you need the history of one decision.

## Hard rules

- MailMate's data (`~/Library/Application Support/MailMate/`) is read-only: No writes, locks, renames, `mmap` or temp files there. Package code opens files only with `open(path, "rb")`; `tests/test_safety.py` enforces this.
- No real mail values (addresses, names, subjects, body terms, account names) in any tracked file or commit message. Use RFC 2606 domains (`example.com`, `example.org`, `example.net`). `tests/test_hygiene.py` checks this; it also reads the gitignored `tools/privacy-denylist.local.txt` when present.
- No shell heredocs over 512 bytes (they hang the shell; a project hook blocks them). Write files with the Write or Edit tool; commit with `git commit -F <file>`.
- Python 3 standard library only. Download, install or run nothing new without the owner's explicit consent.

## Tests

```
/usr/bin/python3 -I -m unittest discover -s tests -t .
```

Run from the repo root. Isolated mode (`-I`) drops the current directory from the import path, which is why the `-t .` flag is needed; `bin/mmgrep` handles this for the CLI.

## Workflow

This project uses a light process: The session implements directly or with at most one implementer subagent per change, then one review and one Codex review at the end of a change. No foreman layer and no plan or ledger documents for routine work.
