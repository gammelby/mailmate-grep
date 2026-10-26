---
name: mmgrep
description: Search the user's local MailMate mail with the mmgrep CLI. Use whenever asked to find, search, list, count or look up emails, mail, messages or correspondence (by sender, recipient, domain, subject, date, mailbox, flag or body text), to find mail that still needs a reply, or to read a specific mail found that way. Do not scan .eml files by hand or with ad hoc scripts.
---

# Searching mail with mmgrep

`mmgrep` searches the local MailMate store through MailMate's own index and confirms each hit in the message's `.eml` file. It is strictly read-only and safe to run while MailMate is open. A header query takes 1 to 2 s over about 180,000 messages; a body query about 4 s.

If `mmgrep` is not on PATH, tell the user and stop; do not install anything. Never scan `~/Library/Application Support/MailMate/Messages.noindex` yourself and never write anything under `~/Library/Application Support/MailMate/`.

## Rules that matter most

- **Use `--json` whenever you read the results.** One JSON object per message, sorted by date.
- **Given a person's name, resolve it first; never guess addresses:**

  ```
  mmgrep --who 'carol jensen' --json --limit 5
  ```

  Take the top address, or several when the person clearly uses more than one, and search with repeated `--from` (or `--any-address`) options.
- **Dates are local; a date-only `--until` includes that whole day.** "Last week" is `--since <Monday> --until <Sunday>`.
- **Addresses are `@domain`, `local@domain` or `re:PATTERN`.** A bare `example.com` is an error.
- **Different options AND; repeating one option ORs.** For OR across options, run twice and union by `message_id`.
- **Exit code 1 means nothing found**, not an error. Exit 2 is a usage error (message on stderr); exit 3 means the index changed during the read: Retry once.
- **Stderr notes are informational.** "N indeterminate confirmations" means N rows could not be double-checked in their `.eml` file (usually odd encodings); they are still printed with `"confirmed": "indeterminate"`. Treat them as probable hits and say so when you report them.

## Typical searches

```
mmgrep --any-address @example.com --since 2026-10-01 --until 2026-10-31 --json
mmgrep --from alice@example.com --subject 'contract' --json
mmgrep --to bob@example.org --body 'renewal' --json
mmgrep --list-mailboxes --json                    # mailbox names per account
```

Read a message with the Read tool on one of its `paths`.

## Mail that still needs a reply

First find the user's own addresses (`mmgrep --who '<their name>' --json --limit 5`; skip robots such as notification senders). Then one query does the filtering:

```
mmgrep --mailbox '^INBOX$' --since 2026-10-19 --until 2026-10-25 \
  --no-flag answered --no-flag forwarded \
  --not-header list-id --not-header list-unsubscribe \
  --not-header 'from=no-?reply|do-?not-?reply|notifications?@' \
  --no-reply-from me@example.com --no-reply-from me@example.net \
  --json --show-body --body-chars 300 --fields date,from,subject,message_id,confirmed,body
```

This drops mail the user answered, forwarded, sent or replied to anywhere further down the thread, mailing lists, newsletters and robot senders. Do not write scripts to match sent mail; `--no-reply-from` already did that.

`--show-body --body-chars 300` adds the start of each message's text (quoted replies removed) and `--fields` keeps only the keys you need, so a week stays around 25 KB. Read it directly; do not save it to a file or reformat it with scripts or jq. Then judge each mail: Robots, receipts and FYIs need no reply; a direct question or request does. Some newsletters put CSS in their plain-text part; skip those. A preview can hide a request further down, so before you call a mail from a person "no reply needed", read it in full unless the preview settles it.

To read one message in full, rerun with `--header 'message-id=<its id>' --json --show-body` (escape regex characters such as `+` in the id with a backslash). That still drops quoted text and stops at 20,000 characters; add `--include-quoted` when the request may sit in a quoted part, or use the Read tool on the message's `confirmed_path`.

## Showing mails in MailMate

When the user wants to see or act on found mails, rerun the search with `--open`: MailMate shows exactly those messages (first 50). If fewer appear than mmgrep printed, tell the user to select "All Messages" in MailMate's sidebar.

## Everything else

Run `mmgrep --help-agents` for the full guide: JSON schema, every option and its semantics, flags and tags, `--full` for mail MailMate has not indexed yet, performance and limits.

## Privacy

Results are private mail. Report what the user asked for; do not paste mail content into commits, issues, shared documents or any network service unless the user explicitly asks.
