# mmgrep

`mmgrep` is a read-only command-line search over a local [MailMate](https://freron.com) mail store. It answers questions such as "every message from anyone at example.com since March" or "every message whose body mentions an invoice number" from MailMate's own index files in seconds and then confirms each hit against the message's `.eml` file.

It needs only the Python 3 standard library (3.9 or later) and runs straight from a checkout. Nothing is installed.

## Before you use it

**Not affiliated.** `mmgrep` is an independent project with no affiliation with or endorsement by MailMate or Freron Software. MailMate is their product and name.

**Credit.** The first description of the index file format came from the format notes in Brian Murphy-Dye's `mailmate` Ruby gem (https://github.com/brianmd/mailmate, MIT). `mmgrep` shares no code with the gem and every format fact was re-verified against copies of real data.

**Undocumented format.** MailMate's index is private and undocumented. Its author has said that reading it directly "would never be safe to do so while MailMate is running" and that it "could also change at any time" (https://lists.freron.com/mailmate/2015-May/004377.html). `mmgrep` was verified only against MailMate 2.0 build 6272 with index version 1.1. It warns on stderr when `Main.plist` reports another version.

**Reading a live database.** MailMate rewrites index files while it runs, so a file read is not an atomic snapshot. `mmgrep` checks for changes and retries, validates every record and confirms hits against the `.eml` files. Results can still miss mail that arrived moments ago or that MailMate has not indexed yet. An index-only (`--no-confirm`) result can contain stale values. Exit code 3 flags a read that never stabilized.

**Safety promise.** `mmgrep` is strictly read-only. It never writes, renames, deletes or creates anything under the MailMate folder. It takes no locks of any kind (no `flock`, no `fcntl`, no lock files) and does not use `mmap`. Every file is opened with `open(path, "rb")`, read and closed before the next one. Worker processes are capped at 4. Read-only access cannot rule out some disk or memory contention while a search runs. It cannot corrupt MailMate's data or keep an arriving message from being written.

How this is tested: `tests/test_safety.py` builds a synthetic store, records the content hash, size, mode and mtime of every path in it, runs the package's read operations and every CLI mode (index, `--body`, `--full`, `--no-confirm`) and asserts that nothing changed and no path appeared. A static scan of the package source fails on any forbidden call (file locks, `mmap`, writes, renames, deletes, `chmod`, `utime`, extended attributes, temp files, subprocesses) and on any `open()` other than `open(path, "rb")`. The one exception is `mmgrep/mailmate_ui.py`, used only by `--open`: It runs `/usr/bin/open` with an `mlmt:quicksearch` URL and nothing else, which a dedicated test pins.

**Not a backup and no warranty.** `mmgrep` is not a replacement for backups of your mail. It comes with no warranty of any kind; see [LICENSE](LICENSE).

## Usage

Clone or download the repository and run the wrapper from the checkout:

```
bin/mmgrep --help
```

`bin/mmgrep` runs the package with `python3 -I` (isolated mode, so `PYTHONPATH` and the current directory are ignored). It works from any directory and through a symlink. To put it on your `PATH`, link it into a per-user bin directory that is already on `PATH` (no sudo; `git pull` updates it in place):

```
ln -s "$PWD/bin/mmgrep" ~/.local/bin/mmgrep
```

From the checkout root `python3 -m mmgrep` works too.

The default data root is `~/Library/Application Support/MailMate`; `--root PATH` points elsewhere.

```
mmgrep [filters] [--body PATTERN] [output options]
```

Filters combine with AND. A repeated option ORs its values. At least one filter is required.

### Examples

Every message from anyone at example.com or its subdomains:

```
bin/mmgrep --from @example.com
```

Mail exchanged with one address in any address field, including Bcc where MailMate indexes it:

```
bin/mmgrep --any-address alice@example.com
```

Mail from either of two senders with a subject starting with "Invoice" during 2024:

```
bin/mmgrep --from @example.com --from bob@example.org --subject '^Invoice' --since 2024-01-01 --until 2024-12-31
```

A body search narrowed by address, as JSON lines:

```
bin/mmgrep --any-address @example.net --body 'order [0-9]{6}' --json
```

The `.eml` paths of matching messages in one mailbox, one per line. The default store lives under `Application Support`, so convert the newlines to NUL bytes before handing the paths to `xargs`:

```
bin/mmgrep --to carol@example.com --mailbox '^Archive/' --paths-only | tr '\n' '\0' | xargs -0 ls -l
```

A header without an index, through the exhaustive scan:

```
bin/mmgrep --full --header 'reply-to=@example.org'
```

### Options

Filters:

| Option | Meaning |
| --- | --- |
| `--from ADDR` | Sender address |
| `--to ADDR` | Recipient address in To |
| `--cc ADDR` | Recipient address in Cc |
| `--any-address ADDR` | Address in From, To, Cc or Bcc (Bcc where indexed, which is mostly sent mail) |
| `--subject PATTERN` | Regex over the decoded subject |
| `--header NAME=PATTERN` | Regex over an indexed header; any header with `--full`. A name MailMate has no index for is exit 2 with a hint to use `--full` |
| `--since DATE` | Date on or after DATE |
| `--until DATE` | Date before DATE; a date only means before the next local midnight |
| `--account PATTERN` | Regex over the account name |
| `--mailbox PATTERN` | Regex over the mailbox path |
| `--body PATTERN` | Regex over the unquoted body text (only once per search) |
| `--include-quoted` | Also search quoted body text |
| `-s`, `--case-sensitive` | Match patterns case-sensitively |

Search:

| Option | Meaning |
| --- | --- |
| `--full` | Ignore the index and scan every `.eml` file |
| `--no-confirm` | Trust the index and skip the `.eml` recheck (not with `--full`) |
| `--jobs N` | Worker processes, clamped to 4 (default: The CPU count, at most 4) |
| `--root PATH` | MailMate data root (default `~/Library/Application Support/MailMate`) |
| `--open` | Also show the results in MailMate: Opens an `mlmt:quicksearch` URL that searches for their Message-IDs (the first 50). MailMate searches its selected mailbox, so select "All Messages" first. `mlmt:quicksearch` is undocumented; it was found in the MailMate 2.0 binary and works there |

Output:

| Option | Meaning |
| --- | --- |
| `--json` | JSON lines |
| `--paths-only` | The first `.eml` path of each message |
| `--all-paths` | With `--paths-only`: Every copy's path |
| `--limit N` | Print at most N messages (after sorting by date) |
| `-v`, `--verbose` | Table: Add to, cc and every path |
| `--who NAME` | List the addresses a person uses, most used first: Every word of NAME must start a word of the display name or address (case and accents ignored). Combines only with `--json`, `--limit` and `--root` |
| `--help-agents` | Print an extended guide for coding agents and scripts (JSON schema, exit codes, semantics, recipes) |
| `-h`, `--help` | Show the help and exit |

`--json` and `--paths-only` exclude each other. `--all-paths` needs `--paths-only` and `--no-confirm` cannot be combined with `--full`. `--body` may be given only once; a second one is exit 2.

Value syntax:

- `ADDR` is `@domain` (that domain and its subdomains), `local@domain` (that exact address, case-insensitive) or `re:PATTERN` (a regex over the whole header value).
- `PATTERN` is a Python regex, case-insensitive unless `-s` is given. Patterns are never rewritten. `re.MULTILINE` is on, so `^` and `$` match at line boundaries.
- `DATE` is `YYYY-MM-DD` or an ISO 8601 datetime such as `2024-03-31T10:00+02:00` (a trailing `Z` means UTC). Values without an offset are in the local time zone. A date-only `--until` covers that whole local day, including days with a daylight saving change.

A message's date is its `Date` header. When that is missing or unparsable MailMate's received date is used instead. The same rule drives the date filters, sorting and the output.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | At least one message found |
| 1 | No message found |
| 2 | Usage or format error (bad option or pattern, no filter, unknown `--header` index, not a MailMate store) |
| 3 | Results printed but the snapshot never stabilized, so they may be incomplete (stderr says why) |
| 130 | Interrupted with Ctrl-C |

When several apply, 130 wins over 2, 2 over 3 and 3 over 0 and 1. Exit 3 is used even when no row was printed.

### Output formats

One row per message. Copies of one message in several mailboxes (same Message-ID) are merged into one row that lists every copy. Rows are sorted by date ascending, then by Message-ID. Rows without a date come last. `--limit` applies after sorting.

- **Table** (default): Local date and time, From, Subject and the number of copies. Long values are cut with `...` and control characters are shown escaped. `-v` adds the To and Cc lines and every path.
- **JSON lines** (`--json`): One object per line with the keys `date` (ISO 8601 with offset; null when there is no date), `from`, `to`, `cc`, `subject`, `message_id`, `paths` (list), `ids` (list of MailMate message ids), `confirmed` and `confirmed_path`. `confirmed` is `true` for a hit confirmed against an `.eml` file, `false` with `--no-confirm` and `"indeterminate"` when no copy could be decided. `confirmed_path` is the copy that confirmed (null when none did). Output is ASCII-safe.
- **Paths** (`--paths-only`): The first path of each message per line; `--all-paths` prints every copy. Paths are written as the bytes the file system holds.

Diagnostics go to stderr, one line each: Index hits not confirmed, indeterminate confirmations, skipped malformed records, body segments without a live message, copies without an `.eml` file, snapshot retries, directories `--full` could not read and warnings. Output into a closed pipe (`| head`) ends quietly.

## How it works

1. Read each index file the query needs whole in one read and check that the snapshot is consistent (retrying when MailMate changed a file mid-read).
2. Evaluate the filters on the index, cheapest first: Account and mailbox, dates, addresses and headers, then the body.
3. Group the hits by Message-ID and expand each group to every copy MailMate holds.
4. Confirm each group on its `.eml` files: The Message-ID must match and every filter is rechecked on the parsed file. The first copy that passes confirms the message. A copy that cannot be decoded makes the row indeterminate unless another copy confirms.

`--full` skips the index and applies the same checks to every `.eml` file under `Messages.noindex`.

The index format and the reader rules are described in [docs/FORMAT.md](docs/FORMAT.md).

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

## Verification against an exhaustive scan

`tools/groundtruth.py` is an independent second implementation: It reads every `.eml` file with the stdlib `email` package (retrying with the lenient `compat32` policy where the modern parser raises) and evaluates a set of queries without using any `mmgrep` code. `tools/compare.py` runs the same queries through `bin/mmgrep --json` and diffs the Message-ID sets. The queries live in a local JSON file; `tools/acceptance.example.json` shows the shape.

```
python3 -I tools/groundtruth.py my-queries.json /tmp/truth.json
python3 -I tools/compare.py my-queries.json /tmp/truth.json /tmp/diff.json
```

On the maintainer's store (179,059 `.eml` files, scanned in about 85 s with 4 workers) four queries were checked: Any address at three domains or From one of seven addresses (two `mmgrep` runs, unioned), one sender within one month, a non-ASCII RFC 2047 encoded subject, and a body word with the address set of the first query. Results:

| Query | Exhaustive scan | `mmgrep` | Difference | `mmgrep` time |
| --- | --- | --- | --- | --- |
| Addresses at domains or listed senders | 301 | 301 | 0 | 2.3 s (two runs) |
| One sender in one month | 24 | 24 | 0 | 1.4 s |
| Encoded non-ASCII subject | 4 | 4 | 0 | 0.8 s |
| Body word narrowed by addresses | 40 | 7 | 33 | 4.0 s |
| The same with `--include-quoted` | 40 | 40 | 0 | 8.1 s |

The body difference is by design: By default `mmgrep` searches only unquoted body text (MailMate's `#unquoted` index), while the scan searches all text. The 33 extra messages are found once MailMate's quoted-text index is included: `--include-quoted` matches the scan exactly. Every row `mmgrep` printed was confirmed against its `.eml` file (`"confirmed": true`), the scan read every file without error and `compare.py` exits 0 (PASS) only when runs succeed, all rows are confirmed and no difference is unexpected.

## Known limits

- Mail MailMate has not indexed is invisible to index searches. Only `--full` sees it.
- Addresses written with a comment or whitespace around the `@` (legal but rare, for example `alice (work) @example.com`) are not found by address filters.
- Malformed MIME structure (a missing or unclosed boundary) makes a body confirmation indeterminate rather than a definite miss.
- `--full` has no index to fall back on for dates: A message without a parsable `Date` header that carries a time zone fails every date filter there.
- Body text is extracted from HTML with Python's `html.parser`, which differs from MailMate's own extraction. Confirmation tries two variants (block elements joined by a newline and joined with nothing). Some HTML can still match differently.
- Quoted text is normalized differently: With `--include-quoted` confirmation strips `>` markers per line and matches line by line. Quoting that MailMate detects in other ways and other whitespace normalization inside quotes can make an index hit fail confirmation or the other way around.
- Quoted local parts in addresses (`"a b"@example.com`) are not supported by address filters.
- `^` and `$` match at line boundaries, not only at the start and end of the whole value or body.
- An encoded word glued to plain text (`x =?UTF-8?Q?a?=b`) decodes with a space before the plain part, so such a subject or name can fail confirmation.
- Real mail with header parse defects can yield indeterminate rows. Index searches print those rows with index values; `--full` prints them with only their paths and Message-ID. Their count goes to stderr.
- Verified only on MailMate 2.0 build 6272 with index version 1.1. Other versions may work or may not; `mmgrep` warns when the version differs.

## License

MIT; see [LICENSE](LICENSE).
