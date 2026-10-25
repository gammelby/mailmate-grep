# MailMate Index Format

Credit: The first description of this format came from the format notes in Brian Murphy-Dye's `mailmate` Ruby gem (https://github.com/brianmd/mailmate, MIT). `mmgrep` shares no code with the gem and every fact below was re-verified against copies of real data.

Not affiliated: `mmgrep` is an independent project with no affiliation with or endorsement by MailMate or Freron Software. MailMate is their product and name.

Undocumented format: MailMate's index is private and undocumented. Its author has said that reading it directly "would never be safe to do so while MailMate is running" and that it "could also change at any time" (https://lists.freron.com/mailmate/2015-May/004377.html). Treat everything here as observation, not specification.

Reading a live database: MailMate rewrites index files while it runs, so a file read is not an atomic snapshot. `mmgrep` checks for changes and retries, validates every record and confirms hits against the `.eml` files. Results can still miss mail that arrived moments ago or that MailMate has not indexed yet. An index-only (`--no-confirm`) result can contain stale values. Exit code 3 flags a read that never stabilized.

Verified against MailMate 2.0 build 6272, index version 1.1 (`Main.plist` `version = "1.1"`). The `revision` seen was `"5509"` in every copy taken (two snapshots 10 min 39 s apart and 16 event copies over 20 minutes); no other value was observed.

## Evidence tags

Every claim ends with one tag:

- `[documented]`: Stated by the gem notes or by MailMate itself and not contradicted.
- `[verified: <counts>]`: Checked on copies of a real store of about 179,000 live messages (2,656 header indexes, 30,413,326 records).
- `[inferred: <basis>]`: Follows from the stated evidence but was not checked directly.

All examples are synthetic. Byte strings use Python notation (`\t` is 0x09, `\n` is 0x0A).

## 1. Layout

The data root is `~/Library/Application Support/MailMate` `[documented]`. Below it:

- `Database.noindex/Main.plist`: Store identity and counters `[verified: 1 file]`.
- `Database.noindex/Headers/<name>.plist`, `<name>.cache`, `<name>.offsets`: One triple per index. `<name>` is a lowercased header name (`from`, `cc`, `subject`) or a `#` pseudo-header (`#date`, `#source`, `#unquoted`) `[verified: 2,656 triples, 0 incomplete, 0 other files]`. Some indexes have a lowercased twin `<name>#lc`: 9 `#lc` triples exist, 7 of them for header indexes (among them `from`, `to`, `cc`, `bcc` and `subject`) and 2 for `#unquoted` and `#quoted` `[verified: Snapshot 1]`. Readers match the original-case indexes and do not need the twins (design section 7).
- `Database.noindex/Sets/`: Exactly `Msgs.ids`, `NewMsgs.ids`, `Parts.ids` and `NewParts.ids` `[verified: 2 snapshots]`.
- `Messages.noindex/IMAP/<account>/<mailbox path>/Messages/<id>.eml`: One file per message (section 6) `[verified: 179,058 files named <digits>.eml, all directly under a Messages directory]`. The only other files seen were `State.plist` files and one `.DS_Store` `[verified: 6 and 1]`. The `.eml` files use LF line endings, not CRLF `[verified: 0 CR bytes in 45 of 45 sampled copies]`.

## 2. Property lists

`Main.plist` and every header `.plist` are OpenStep ("old-style") ASCII property lists. They are neither XML nor binary plists and `plistlib` cannot read them `[verified: The file tool prints "ASCII text" for both kinds; plistlib.loads raises InvalidFileException on 2,656 of 2,656 header plists and on Main.plist]`.

Exact bytes: `{`, then for each key a tab, the key, ` = `, the value, `;` and `\n`; then `}` with no trailing newline. There is no newline after the opening `{`. Keys appear in ASCII-sorted order, so uppercase `UUID` comes first `[verified: 2,656 of 2,656 header plists and every Main.plist copy]`. A value is either a bare integer (`36`) or a double-quoted string (`"1.1"`) `[verified: Same]`.

`Main.plist` has exactly the keys `UUID`, `largestUsedBodyPartID`, `revision` and `version`. `UUID`, `revision` and `version` are quoted strings; `largestUsedBodyPartID` is a bare integer `[verified: Every copy]`. With a 36-character UUID, a 7-digit `largestUsedBodyPartID` and a 4-digit `revision` the file is 122 bytes `[verified: Every copy]`. The synthetic example below has a 4-digit id and a 1-digit revision, so it is 116 bytes (checked with Python `len()`):

```
b'{\tUUID = "00000000-0000-4000-8000-000000000001";\n\tlargestUsedBodyPartID = 1206;\n\trevision = "1";\n\tversion = "1.1";\n}'
```

A header `.plist` has exactly the keys `offsetsFileSize` and `stringsFileSize` `[verified: 2,656 of 2,656, no other key]`. `offsetsFileSize` is always bare. `stringsFileSize` is bare in 2,513 files and quoted in 143; no size pattern explains which `[verified: 2,656 files]`. Readers must accept both; fixtures should write both. Synthetic examples (48 and 50 bytes):

```
b'{\toffsetsFileSize = 48;\n\tstringsFileSize = 34;\n}'
b'{\toffsetsFileSize = 48;\n\tstringsFileSize = "34";\n}'
```

In a quiescent store `offsetsFileSize == len(.offsets)` and `stringsFileSize == len(.cache)` `[verified: 2,656 of 2,656 for both]`.

## 3. Records

`.offsets` is a concatenation of 12-byte records `struct.pack("<III", id, start, end)` with no header `[verified: Length % 12 == 0 for 2,656 of 2,656; big-endian fails 192,777 of 192,777 bounds checks on from; 64-bit layouts leave remainders]`. The value of a record is `cache[start:end]` `[verified]`.

Neither ids nor starts are monotonic in file order `[verified: Ids nondecreasing in 1,677 of 2,656 headers; starts in 1,617]`.

`.cache` packs values back to back, each followed by one `\n` byte that `end` excludes. So `cache[end] == b"\n"` and the byte before `start` is `\n` unless `start == 0`. Every `.cache` ends in `\n` `[verified: 29,408,960 of 29,408,960 in-range records, the 33,896 empty ones included; 2,656 of 2,656 files]`. Values may themselves contain `\n` in body indexes but not in header indexes `[verified: 958,328 #unquoted records contain one; 0 header-index values do]`.

Offsets are byte offsets. Slice bytes first, then decode `[verified: By construction of the bounds check over byte lengths]`.

Values are partially deduplicated. Several records, including records of different ids, often share one byte range `[verified: For example 183,137 of 192,777 from records share a range]`. The same value can also be stored at several ranges `[verified: #source 494 ranges for 58 distinct values (18 repeated); from 13,605 for 10,268 (791 repeated); subject 80,589 for 77,668 (1,464 repeated); message-id 169,365 for 168,713 (646 repeated)]`. Readers must not assume one range per distinct value. A writer that always reuses the first occurrence produces a valid subset of the observed layouts `[inferred: Both layouts satisfy every record check above]`.

The marker record `(id, 1, 0)` clears the current value of `id` (start 1, end 0). It is the only record that fails `start <= end <= len(cache)` `[verified: 1,004,366 of 1,004,366 bounds failures are exactly (1, 0)]`. The binary has a `clear_header` symbol, which suggests the name `[inferred: Symbol name from nm]`. Markers are interleaved with later appends, not collected at the end `[verified: 760 marker runs in from]`. True empty records (`start == end`) are rare `[verified: 33,896 over all headers; 2 in to, 0 in the other focus indexes]`. They are not deletions: 15,228 ids over 8 indexes (mostly `#flags`, the `#lc` twins and `#send-later-string`) have an empty record among their current records and 10,554 of those ids are live `[verified: Review recount on snapshot 1]`. A current record with `start == end` is the empty value `b""`, not a deletion (controller ruling, design 6a; R1).

Every rewrite of a header value appends `(id, 1, 0)` and then the new value records `[verified: Record patterns in section 7]`. A header that occurs on several lines of one message gets one record per line after the marker `[verified: 2 of 2 inspected messages with two Message-ID lines]`. Ids with several value records after the last marker: `message-id` 3, `subject` 3, `from` 3, `to` 8, `date` 6 `[verified: Snapshot 1]`.

Synthetic `from` index: Ids 1200 and 1205 share one value, id 1207 had a value and was then cleared.

```
cache   = b"alice@example.com\nbob@example.org\n"       (34 bytes)
offsets = pack("<III", 1200, 0, 17) + pack("<III", 1205, 0, 17)
        + pack("<III", 1207, 18, 33) + pack("<III", 1207, 1, 0)   (48 bytes)
        = b0 04 00 00 00 00 00 00 11 00 00 00  b5 04 00 00 00 00 00 00 11 00 00 00
          b7 04 00 00 12 00 00 00 21 00 00 00  b7 04 00 00 01 00 00 00 00 00 00 00
plist   = b'{\toffsetsFileSize = 48;\n\tstringsFileSize = 34;\n}'
```

Current values: 1200 and 1205 are `alice@example.com`; 1207 has none.

## 4. Value forms

Header index values are RFC 2047 decoded, unfolded and stripped of outer whitespace. Case and address order are kept `[verified: 1,000 seeded live ids plus a 45-id sample]`. All header index values are valid UTF-8 `[verified: 100% of in-range records in from, to, cc, #cc, bcc, subject, message-id, date, in-reply-to, references, #date, #source]`.

| Index | Holds | Form | Evidence |
|---|---|---|---|
| `from`, `to`, `cc`, `bcc` | That header | Decoded, not reformatted; quotes and RFC 822 comments kept | `[verified: Same specs in the same order from getaddresses in order for from 1,000/1,000, to 997/997, cc 278/279, bcc 93/93]` |
| `subject` | Subject | Decoded; a fold loses the line break and keeps the following whitespace | `[verified: Equal to str(policy.default value).strip() for 1,000/1,000; 107/107 encoded words decoded]` |
| `message-id` | Message-ID | As written, angle brackets kept | `[verified: 999/1,000; the exception keeps trailing text after ">" verbatim]` |
| `date` | Date | Raw header text | `[verified: 44/44]` |
| `#date`, `#date-sent`, `#date-received` | Dates | Section 5 | Section 5 |
| `#source` | Mailbox URL | Section 6 | Section 6 |
| `#cc` | Not the Cc header | Unknown | `[verified: 4,870 ids; only 1,203 have a cc record and 1,129 of those equal it]` |
| `#unquoted`, `#quoted`, `#lc` twins | Body text | Section 8 | Section 8 |

The Cc header index is `cc` (57,162 records), not `#cc` `[verified: 8 sampled messages with a Cc header have no #cc value]`. A `#derive-bcc` index exists and was not analyzed `[verified: Present]`.

Display names can differ from `policy.default`: `alice@example.com (Alice)` and `"Alice  Smith" <alice@example.com>` are stored as written. MailMate also sometimes adds quotes around a decoded name `[verified: From 15, to 2, cc 8 of the 1,000-id run]`.

One known misparse: A decoded encoded word that contains a comma is stored without quotes, for example `Doe, Jane <jane@example.com>`. `getaddresses` on that index value splits it into a wrong list `[verified: 1 of 279 sampled cc values]`.

Synthetic raw header and stored value:

```
Subject: =?UTF-8?Q?Gr=C3=BC=C3=9Fe?= aus Berlin
stored:  b"Gr\xc3\xbc\xc3\x9fe aus Berlin"     (18 bytes, so end == start + 18)
```

## 5. Dates

All three date indexes are ASCII text, not integers `[verified: 997 of 997 and 997 of 999 matches below]`.

- `#date` is `%Y-%m-%d %H:%M:%S %z`: The Date header's instant with its own offset. Parse with `datetime.strptime(v, "%Y-%m-%d %H:%M:%S %z")` `[verified: 997 of 999 ids with both; the other 2 have a Date header without a zone]`.
- `#date-sent` is `%Y-%m-%d %H:%M:%S` in UTC with no offset: The Date header converted to UTC `[verified: 997 of 997]`.
- `#date-received` has the `#date-sent` form (UTC, no offset). It equals the first Received header for 30 of the 33 sampled ids that have one, so it is the arrival time `[inferred: 45-id sample]`. Its source for mail without a Received header is unknown `[verified: 11 such ids, 12 sampled ids equal the Date header]`.

Synthetic:

```
Date: Tue, 05 Mar 2024 14:07:09 +0100
date           b"Tue, 05 Mar 2024 14:07:09 +0100"
#date          b"2024-03-05 14:07:09 +0100"
#date-sent     b"2024-03-05 13:07:09"
#date-received b"2024-03-05 13:07:12"     (from a Received header)
```

## 6. `#source` and `.eml` paths

Grammar: `imap://<account>/<mailbox>[/<submailbox>...]`. `<account>` is the whole URL authority, either `<user>%40<domain>@<host>` or `<user>@<host>` `[verified: 494 of 494 ranges (58 distinct URLs); imap is the only scheme]`. Messages in one mailbox share its `#source` value, usually through one range; 18 URLs are stored at more than one range `[verified: 494 ranges, 58 distinct URLs; live mail in 54 URLs over 6 accounts]`.

Path rule from `#source` and id:

1. Map the scheme to an uppercase directory under `Messages.noindex` (`imap` becomes `IMAP`) `[inferred: Only imap and IMAP were observed]`.
2. Keep `<account>` byte for byte; `%40` stays `%40` and nothing is percent-decoded.
3. Split the mailbox path on `/` and append `.mailbox` to every segment. Spaces and brackets stay raw.
4. Append `Messages/<id>.eml`.

`[verified: 179,057 of 179,057 live ids map to an existing file; nested 9,079/9,079; %40 169,220/169,220; space 44,082/44,082; "[" 7,556/7,556]`. Percent escapes other than `%40` and non-ASCII mailbox names were not observed `[verified: 0 in #source.cache and 0 in the Messages.noindex listing]`.

Synthetic examples for id 1200:

```
imap://user%40example.com@imap.example.com/Archive/2024
  -> Messages.noindex/IMAP/user%40example.com@imap.example.com/Archive.mailbox/2024.mailbox/Messages/1200.eml
imap://bob@mail.example.org/Sent Messages
  -> Messages.noindex/IMAP/bob@mail.example.org/Sent Messages.mailbox/Messages/1200.eml
imap://alice%40example.com@imap.example.com/[Gmail]/All Mail
  -> Messages.noindex/IMAP/alice%40example.com@imap.example.com/[Gmail].mailbox/All Mail.mailbox/Messages/1200.eml
```

## 7. Live set, tombstones and id lifetime

The id is the `.eml` file's numeric name `[verified: Path rule above]`. The live message set is `Msgs.ids | NewMsgs.ids`; the two are disjoint and their union equals the set of ids whose last `#source` record is a value `[verified: Both snapshots, exact equality]`. The binary has `database_t::is_part_of_msgs` and `is_part_of_new_msgs` `[verified: Symbols from nm]`. A new arrival went directly into `Msgs.ids`, not `NewMsgs.ids`, so the meaning of `NewMsgs.ids` is unknown `[verified: 1 test mail]`.

Disk against index: Live id without `.eml` 0 in both snapshots. `.eml` without live id 1 in snapshot 1 and 0 in snapshot 2, a mail that arrived between the snapshot and the listing `[verified: 0 persistent mismatches, 1 transient; the two listings were 7 min 0 s apart]`.

Deleted message: Its id leaves the live set, its `.eml` is gone and its `#source` ends in `(id, 1, 0)` `[verified: 3,086 of 3,086 non-live #source ids end in the marker; 0 end in a start == end record]`. Only `#source` reliably ends in a marker. Other headers usually do too but not always `[verified: Of those 3,086 ids, message-id ends in a marker for 3,032 and is absent for 54; subject 2,988 and 98]`.

Orphans: Some ids are not live yet still carry current header values. None of them ever appears in `#source` `[verified: Snapshot 1]`:

- Non-live ids whose last record is a value: `from` 7,383, `subject` 7,216, `to` 7,164. Of these, 5,855, 5,686 and 5,632 are in neither `Parts.ids` nor `#root-body-part` `[verified: Snapshot 1]`.
- `message-id`: 24,468 ids not in `#source`. 17,318 end in a marker and are linked nowhere; 5,617 end in a value and are linked nowhere (orphans); 1,533 end in a value and have a `#root-body-part` pointing to a live message (embedded messages) `[verified: An independent recount differs by 4 in the marker row only]`.
- `#date`: 24,527 ids not in `#source`. 22,722 end in a marker and are linked nowhere; 1,805 end in a value and are parts of live messages `[verified: Same recount, 4 apart in the marker row]`.

The 5,617 `message-id` orphans are old ids (5,607 lie below the median live id). None is in `Parts.ids` and none has a current `#date-received`, `#flags`, `#uid`, `#imap-size`, `#old-source`, `#parent-body-part` or `#unquoted` value; 2,071 have a current `#import-date` and 2,692 share their Message-ID with a live id `[verified: Snapshot 1]`. They are header remnants of messages removed long ago, possibly from an early import, whose header records were never cleared `[inferred: Age and missing mailbox, flags and body; probe capped at 15 minutes]`.

A current header value never makes an id live `[verified: Every orphan above lies outside Msgs.ids | NewMsgs.ids]`.

Moved message: The id is kept. `#source` gets `(id, 1, 0)` and then the new mailbox URL `[verified: 7,051 ids moved within one account with a stable Message-ID; caught live once with no cache growth]`. Patterns of ids with several `#source` records (V value, M marker): VM 3,043, VMV 9,152, VMVMV 151, others below 50 `[verified: Snapshot 1]`.

Id reuse for an unrelated message was not observed. Ids with several distinct `message-id` values: 2,322 draft resaves in a Drafts mailbox (77 of them also changed account), 2 messages with two Message-ID lines and 2 dead ids `[verified: 2,326 classified]`. The last value matched the current `.eml` and no earlier value did `[verified: 13 of 13 checks on 12 seeded ids]`.

`message-id` and `#date` cover more ids than `#source`. Only part of that excess is embedded `message/rfc822` parts (1,533 and 1,805 above); most of the rest are orphans `[verified: Counts above]`.

## 8. Body indexes and the part tree

Body indexes are `#unquoted` (unquoted text segments) and `#quoted` (quoted text), each with a lowercased `#lc` twin. Each holds one record per segment `[verified: 2,973,729 #unquoted records over 188,410 ids]`. The twins have the same ids and record counts as the originals `[verified: Both]`. The twin of `#unquoted` is the triple `Headers/#unquoted#lc.{plist,cache,offsets}` `[verified: Present in snapshot 1]`. So is `Headers/#quoted#lc.{plist,cache,offsets}` `[verified: Present in snapshot 1]`.

Body and part-tree indexes also carry `(id, 1, 0)` markers, so "current records" means the records after the last marker there too `[verified: #unquoted 5,572, #quoted 2,637, #root-body-part 10,810 markers]`.

Body records key on body part ids `[verified: Of 184,974 live #unquoted ids, 49,202 are message ids and 135,767 are part ids of live messages; 5 have a non-live root; 0 unmapped]`.

The part tree (all values decimal ASCII text, keyed by part id) `[verified: 354,950 live ids in #root-body-part, none of them message ids]`:

- The message id is the root body part and has no `#root-body-part` record.
- `#root-body-part`: Part id to its message id.
- `#parent-body-part`: Part id to its parent part id. A top-level part's parent is the message id `[verified: 275,910 parts with a live root]`. The other parts are nested: Their parent is a part with the same root (78,827) or a part whose own root record was cleared (199) `[verified: Review recount on snapshot 1]`. Nested parts carry the message id as their root, so one `#root-body-part` lookup maps any part `[verified: 78,827 nested parts]`.
- `#subpart-number`: 0-based position under the parent (`b"0"`, `b"1"`).

A single-part message keeps its segments on the message id `[verified: 12 of 12]`. A multipart message keeps them only on leaf part ids `[verified: 33 of 33; 0 on the message id]`. Part ids usually run consecutively from message id + 1 in depth-first order but not always: One resaved draft had parts well above that `[verified: 44 of 45]`. `Parts.ids` holds every `Msgs.ids` id plus part ids `[verified: 179,047 of 179,047 Msgs.ids ids plus 356,384 others, 354,950 of them with a #root-body-part]`. The 10 `NewMsgs.ids` ids are only in `NewParts.ids` `[verified: Both snapshots]`. Content backing: 636 of 664 segments of 33 multipart samples were found in the MIME part the tree maps them to; the 28 others are the HTML join cases below `[verified]`.

Synthetic multipart/alternative message 1200 with parts 1201 (text/plain) and 1202 (text/html):

```
#root-body-part     1201 -> b"1200"   1202 -> b"1200"
#parent-body-part   1201 -> b"1200"   1202 -> b"1200"
#subpart-number     1201 -> b"0"      1202 -> b"1"
#unquoted           cache b"Hi there\nSee you\n" (17 bytes)
                    records (1201, 0, 8), (1201, 9, 16)
#unquoted#lc        cache b"hi there\nsee you\n", records (1201, 0, 8), (1201, 9, 16)
```

Segment forms:

- `#unquoted` segments are paragraphs: 510 of 550 text/plain segments equal a blank-line paragraph after stripping and 39 more after removing quote markers and collapsing whitespace. Line breaks inside a paragraph are kept `[verified: 118 contain "\n"; 36 have outer whitespace]`.
- HTML parts: Tags are dropped and entities decoded. Some block elements join with no separator: `<div>Title</div><div>Body</div>` gives `TitleBody` `[verified: 28 segments disagree with an html.parser reduction]`.
- `#quoted` holds quoted text with `>` markers removed and lines rejoined `[verified: 214 of 231 verbatim; 17 after prefix removal and whitespace collapse]`.
- Signature lines after a `-- ` delimiter stay in `#unquoted` `[inferred: 9 of 9 nonblank signature lines in 3 text/plain parts occur inside #unquoted segments of the same message and 0 in #signature]`. A separate `#signature` index exists on message ids; its content was not analyzed `[verified: 686 live ids]`.
- Unquoted attribution lines ("On ... wrote:") go into `#unquoted`, not `#quoted` `[inferred: 13 of 13 such lines]`.
- In `multipart/alternative` usually one alternative carries segments; one sample had both `[verified: 45-id sample]`.
- `#lc` equals `original.lower()` except where the original starts with U+FEFF (the twin drops it) or is not valid UTF-8 (the twin differs, often empty) `[verified: 2,959,821 of 2,968,157 #unquoted pairs; 5,976,951 of 5,985,944 #quoted pairs]`. Python `str.lower()` fits the rest `[inferred: Python casefold explains nothing more]`.
- Body records are not always UTF-8 `[verified: 6,291 of 2,968,157 #unquoted and 3,120 of 5,985,944 #quoted invalid]`. Many are cp1252 or a truncated multibyte character `[inferred: The cp1252 codec decodes 6,221 and 2,931; 1,765 and 605 fail only in the last 3 bytes]`.

## 9. `Sets/*.ids`

Each file is a bare array of little-endian uint32 ids, strictly increasing, with no header, count, bitmap or ranges, so its length is `4 * count` `[verified: 4 files in 2 snapshots, xxd]`. Synthetic ids 1, 2 and 1200:

```
01 00 00 00 02 00 00 00 b0 04 00 00
```

`NewParts.ids` equaled `NewMsgs.ids` in both snapshots `[verified: 10 ids each]`.

## 10. Writer protocol

- `.cache` and `.offsets` are appended in place: Same inode on every change and each later copy keeps the earlier bytes as an exact prefix `[verified: 8 change events in 20 minutes, 15 event copies]`.
- No data file shrank `[verified: Same]`. The binary has a `truncate` symbol and a "Failed to truncate file" string, so shrinking is possible on some path `[inferred: Binary strings and symbols]`.
- Every `.plist` and `Main.plist` change replaces the file atomically (new inode each time) `[verified: Main.plist 9 inodes over 8 changes; consistent with writeToFile:atomically:]`. Six events rewrote `Main.plist` alone with unchanged content: All 16 `Main.plist` event copies are byte-identical `[verified: 16 copies, events 1 to 8, start and end]`.
- Order within one write: An index's `.cache` before its `.offsets`; all data files before all `.plist` files; `Main.plist` last. The order is verified from final nanosecond mtimes; the intermediate order is inferred `[verified: Final nanosecond mtimes in every event; widest span 27.2 ms]`. The poll cannot see writes earlier in the same burst `[inferred: An mtime keeps only the last write]`.
- `revision` and `UUID` did not change across a new mail, a move and a body reindex; only `largestUsedBodyPartID` moved `[verified: "5509" in both snapshots and 16 event copies]`. What bumps `revision` is unknown.
- No copy ever caught a write in progress: In every copy the file lengths equal the `.plist` sizes `[verified: 15 index copies]`.

## Rules for readers

**R1 Live set and tombstones.** An id is a live message when it is in `Msgs.ids | NewMsgs.ids` (R8). A current header value never makes an id live: Every candidate from any header or body index (body part ids first mapped through `#root-body-part`, R3) is intersected with the live set before use, which drops the orphans of section 7 `[verified: Section 7 counts]`. For a header, body or part-tree index the current records of an id are its records after the last `(id, 1, 0)` marker (section 8 shows markers in `#unquoted`, `#quoted` and `#root-body-part`); if no record follows the marker or the id has no records, the id has no value. Validation is all or nothing per id and index: If any record after the last marker is malformed (any bounds failure other than the marker), the id has no value in that index and is counted. An earlier value is never exposed (controller ruling, design 6a). A current record with `start == end` is the empty value `b""`, not a deletion; 15,228 ids over 8 indexes have one and 10,554 of them are live (section 3) (controller ruling, design 6a). For a multi-valued header all records after the last marker count for matching: A filter matches when any one of them does. Scalar uses (`message-id` for dedup, `#date`, `#source`, `#root-body-part`) take the last record after the marker (controller ruling, design 6a).

**R2 Id lifetime and reuse.** Milestone 2 treats an id as stable for one message across moves (marker then new `#source`) and draft resaves (marker then new values). Reuse for an unrelated message was not observed, so milestone 2 does not guard against it beyond confirmation's Message-ID check. Unresolved: Tried classifying all 2,326 multi-value ids and found no reuse but did not check whether a move can allocate a new id; milestone 2 relies on Message-ID dedup (design section 8 step 4) to fold any such copy.

**R3 Header versus body keys.** Header indexes key on message ids, plus embedded-message part ids and orphans in `message-id`, `#date` and other headers. Neither is live, so the intersection with the live set (R1) drops them. Body indexes key on body part ids; a segment's message is the part's `#root-body-part` value; when the id is itself a live message id, the segment belongs to that message. Never derive the message from id arithmetic. Part to message (controller ruling, design 6a): Design section 8 step 2 maps every matching segment id to its message through `#root-body-part` (an id that is itself a live message maps to itself, as for single-part messages: 49,202 live `#unquoted` ids), then intersects the resulting message ids with the live set `Msgs.ids | NewMsgs.ids` (R1).

**R4 Value encoding and match form.** Match `subject` and other header patterns against the decoded UTF-8 index value (`cache[start:end].decode("utf-8")`); no RFC 2047 decoding is needed. Use the `cc` index for `--cc` and `bcc` for Bcc; never `#cc`. Address tokens (controller ruling, design 6a): Address filters extract `<local>@<domain>` tokens from index values with a regex to generate candidates, not `getaddresses`, which misparses some values (section 4). Confirmation rechecks the parsed header in the `.eml`, so a token found only in a display name or comment is dropped there.

**R5 Body normalization.** Match each body record on its own in the original-case indexes `#unquoted` (and `#quoted` with `--include-quoted`), decoded with `errors="replace"`; the `#lc` twins differ from `lower()` for some records (section 8) and are not the match target (design section 7). Confirmation (milestone 3) extracts decoded text parts (all alternatives, HTML reduced with `html.parser`) and searches the whole text, not per segment, because MailMate joins some HTML blocks without a separator. Unresolved: Tried reproducing segments exactly (636 of 664 matched); milestone 3 never requires segment equality. Whitespace and anchors (controller ruling, design 6a): Confirmation never collapses whitespace. It searches the extracted text with `re.MULTILINE`, so `^` and `$` anchor at line boundaries. HTML is extracted in two variants, block elements joined by `\n` and block elements joined with nothing (MailMate sometimes joins blocks without a separator, section 8); a copy confirms if either variant matches.

**R6 Date fields.** Parse `#date` with `"%Y-%m-%d %H:%M:%S %z"` into an aware datetime. Parse `#date-received` with `"%Y-%m-%d %H:%M:%S"` and attach UTC. Fall back to `#date-received` when `#date` is absent or unparsable (design section 7). Unresolved: Tried tracing `#date-received` for mail without a Received header and found no source; milestone 2 uses it as stored.

**R7 `#source` to path.** Apply the path rule in section 6: Uppercase scheme directory, account kept byte for byte, every mailbox segment plus `.mailbox`, then `Messages/<id>.eml`. The mailbox is the `/` separated path. Unresolved: Escapes other than `%40`, non-ASCII mailbox names and schemes other than `imap` were not observed; milestone 2 keeps every byte as stored (no decoding), treats a URL with any scheme other than `imap` as unmapped and counts every unmapped URL as an index id without `.eml` (R10). Account matching (controller ruling, design 6a): `--account` matches the URL authority with `%40` decoded to `@` (`<user>%40<domain>@<host>` reads as `<user>@<domain>@<host>`), so a user's own address matches. Paths keep the stored bytes, `%40` included.

**R8 `Sets/*.ids` use.** Read `Msgs.ids` and `NewMsgs.ids` as `<I` arrays and take their union as the live set; a file whose length is not a multiple of 4 triggers a retry (R11). If the sets still fail after the last retry, the fallback live set is the ids whose last `#source` record is a value, which equaled the union exactly in both snapshots (section 7). `Parts.ids` and `NewParts.ids` are not needed. Unresolved: Tried a live arrival and found `NewMsgs.ids` does not hold the newest mail; milestone 2 uses only the union and does not interpret `NewMsgs.ids` alone.

**R9 `.plist` sizes as a check, not a fence.** No copy ever caught a write in progress, so nothing shows that the bytes up to the `.plist` sizes are consistent while a write runs. Tails are never sliced and nothing claims a prefix is consistent (controller ruling, design 6a). Readers validate the whole files: The `.offsets` length is a multiple of 12, every record is the marker or satisfies `start <= end <= len(cache)` and each file's length equals its `.plist` size. Instability triggers the R11 retry: A file shorter or longer than its `.plist` size or any other validation failure (controller ruling, design 6a). After the last retry the whole files are used with per-id validation (R1) and the run exits 3 (R11).

**R10 Index ids without `.eml` and the reverse.** Index ids outside the live set (tombstones and orphans with current values, section 7) are never candidates; R1 removes them before any `.eml` lookup. A live id whose derived `.eml` is missing is skipped for confirmation and counted (not observed persistently). An `.eml` with no live id is not found by index mode; only `--full` sees it (seen once, transient).

**R11 Snapshot consistency and retry.** Read `Main.plist`; read `Sets/Msgs.ids` and `Sets/NewMsgs.ids`; per needed index read the `.plist`, then `.offsets`, then `.cache`, then the `.plist` again; read `Main.plist` again. Each file is read whole in one `read()`. Parse plists with a strict OpenStep parser for the grammar in section 2 (not `plistlib`). Retry the whole sequence, up to 3 times with a short backoff, when any of these holds: The `revision` or the `UUID` differs between the two `Main.plist` reads; an index's `.plist` differs between its reads before and after its data files; a `Sets` file length is not a multiple of 4; an `.offsets` length is not a multiple of 12; a file is shorter or longer than its `.plist` size; a record fails the R9 checks (marker allowed, else `start <= end <= len(cache)`); a `.plist` fails to parse; a file vanished. After the last retry the whole files are used with per-id validation: An id with a malformed record after its last marker has no value in that index and is counted (R1); a trailing partial record of fewer than 12 bytes is skipped and counted; failed `Sets` files are replaced by the R8 fallback; results are printed and the run exits 3. Snapshot signals (controller ruling, design 6a): The check keeps comparing `revision` as well as `UUID`, although `revision` did not change across a new mail, a move and a reindex (section 10). It also compares each index's `.plist` read before and after its data files. Any difference triggers the retry.

## Unresolved

- The HTML to text rule (28 of 664 segments) and which alternative MailMate indexes.
- The origin of the 5,617 `message-id` orphans (inferred only).
- The source of `#date-received` for mail without a Received header.
- The meaning of `NewMsgs.ids` and of `#cc`.
- What bumps `revision`.
- The path rule for escapes other than `%40`, for non-ASCII mailbox names and for schemes other than `imap` (POP or local mailboxes).
- How and when MailMate writes `Sets/*.ids` (in place or by replacement): The poller watched only `Main.plist` and four indexes.
- Whether a move can allocate a new id.
- Whether `truncate` ever shrinks an index file.
- Whether the `.plist` sizes would hold as a fence during a write (never caught in progress). Readers do not rely on it (R9).
