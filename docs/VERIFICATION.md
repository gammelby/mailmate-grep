# Verification and performance

Details behind the short "Accuracy" note in the [README](../README.md): How `mmgrep` was checked against an exhaustive scan of a real store, and what it costs.

## Checked against an exhaustive scan

`tools/groundtruth.py` is an independent second implementation: It reads every `.eml` file with the stdlib `email` package (retrying with the lenient `compat32` policy where the modern parser raises) and evaluates a set of queries without using any `mmgrep` code. `tools/compare.py` runs the same queries through `bin/mmgrep --json` and diffs the Message-ID sets. The queries live in a local JSON file: Copy `tools/acceptance.example.json` to `tools/acceptance.local.json` and edit it. Files named `tools/*.local.*` are ignored by git, so your queries and results (which hold real addresses and search terms) stay out of commits.

```
python3 -I tools/groundtruth.py tools/acceptance.local.json tools/truth.local.json
python3 -I tools/compare.py tools/acceptance.local.json tools/truth.local.json tools/diff.local.json
```

On the maintainer's store (179,059 `.eml` files) four queries were checked: Any address at three domains or From one of seven addresses (two `mmgrep` runs, unioned), one sender within one month, a non-ASCII RFC 2047 encoded subject, and a body word with the address set of the first query. The scan read every file once for all queries, in about 85 s with 4 workers. The table counts messages:

| Query | Messages found by the scan | Messages found by `mmgrep` | Missed by `mmgrep` | `mmgrep` time |
| --- | --- | --- | --- | --- |
| Addresses at domains or listed senders | 301 | 301 | none | 2.3 s (two runs) |
| One sender in one month | 24 | 24 | none | 1.4 s |
| Encoded non-ASCII subject | 4 | 4 | none | 0.8 s |
| Body word narrowed by addresses | 40 | 7 | 33 | 4.0 s |
| The same with `--include-quoted` | 40 | 40 | none | 8.1 s |

The 33 missed in the body row are by design: By default `mmgrep` searches only unquoted body text (MailMate's `#unquoted` index), while the scan searches all text. The 33 extra messages are found once MailMate's quoted-text index is included: `--include-quoted` matches the scan exactly. Every row `mmgrep` printed was confirmed against its `.eml` file (`"confirmed": true`), the scan read every file without error and `compare.py` exits 0 (PASS) only when runs succeed, all rows are confirmed and no difference is unexpected.

## Speed compared with a plain scan

One common body word (`invoice`) searched three ways on the same store and laptop. Counts differ because the methods look at different things; the notes say what each one counts.

| Method | Time | Found |
| --- | --- | --- |
| `grep -r -i -F invoice` over every `.eml` file | 593 s | 7,160 files. Raw bytes: Headers, HTML markup and every copy of a message count, while base64-encoded bodies are missed |
| `mmgrep --full --body invoice` (decodes every file) | 228 s | 4,328 messages, plus 536 it could not decide because their MIME structure is broken |
| `mmgrep --body invoice` (MailMate's index) | 13.7 s | 4,300 messages |
| The same with `--since 2026-10-01` | 5.7 s | 146 messages |

`grep` ran first and may have started with a colder disk cache than the runs after it. The index search and the full scan agree on 4,292 messages. 36 were found only by the full scan: The word is in the mail, mostly in HTML, but MailMate's index does not hold that text. 8 were found only through the index.

## Performance

Measured on an Apple M1 Pro MacBook Pro (10 cores) with Python 3.14.3 (`python3`) and the system Python 3.9.6 (`/usr/bin/python3`). The store holds about 160,000 dated messages. Each query ran with `--json` and the default `--jobs`. Times are wall seconds for the whole command. They include process start, the index snapshot and confirmation against the `.eml` files unless the query says `--no-confirm`:

| Query kind | Rows | 3.14 first run | 3.14 second run | 3.9 |
| --- | --- | --- | --- | --- |
| Address domain | 190 | 1.3 s | 1.0 s | 1.4 s |
| Subject word | 3,878 | 2.5 s | 1.3 s | 1.9 s |
| Body word narrowed by an address domain | 7 | 3.8 s | 3.7 s | 4.0 s |
| Address domain with `--no-confirm` | 190 | 1.0 s | 1.0 s | 1.2 s |
| No match | 0 | 0.6 s | 0.7 s | 0.9 s |

A header query with up to about 2,000 rows stays under 2 s and a narrowed body query under 10 s. Ctrl-C during `--full` exits 130 with one stderr line and leaves no worker behind.

`tools/bench.py query` times the index work alone on a synthetic store of 190,000 messages and 3,000,000 body segments. These figures are index only and exclude confirmation: `Store.snapshot`, `candidates` and `group_copies`.

| Query kind | Candidates | 3.14 | 3.9 |
| --- | --- | --- | --- |
| One recipient address | 96 | 0.67 s | 0.74 s |
| Body token narrowed by one address | 7 | 3.7 s | 3.9 s |
