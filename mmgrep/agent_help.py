"""Extended help for coding agents and scripts (mmgrep --help-agents)."""

AGENT_HELP = """\
mmgrep for agents and scripts
=============================

mmgrep searches a local MailMate mail store read only through MailMate's
own index, then confirms every hit against the message's .eml file. It
never writes to, locks or modifies anything under the MailMate folder, so
it is safe to run while MailMate is open and as often as you like.

Use --json for anything you parse. Never parse the table output.

OUTPUT (--json): One JSON object per line, one line per message
(copies of the same message in several mailboxes are merged by
Message-ID). Sorted by date ascending.

  date            ISO 8601 with UTC offset, or null
  from, to, cc    decoded header text (strings, may be empty)
  subject         decoded subject
  message_id      Message-ID header including <>, or null
  paths           every .eml path of this message (list, sorted)
  ids             MailMate message ids of those copies (list of int)
  confirmed       true (rechecked in the .eml), false (--no-confirm) or
                  "indeterminate" (file could not be decoded reliably;
                  treat as a probable hit and read the file yourself)
  confirmed_path  the copy that confirmed, or null
  flags           MailMate flags of all copies (list, e.g. "\\\\Seen",
                  "\\\\Answered", "$Forwarded", tags); null means unknown
                  (--full, or no flag index), not "no flags"
  in_reply_to     every <...> Message-ID in In-Reply-To (list, usually
                  0 or 1 items); null when unknown, as for --full rows
                  that could not be confirmed
  references      every <...> Message-ID in References, oldest first
                  (list); null when unknown
  body            only with --show-body: the main body as text (first
                  text part, plain over HTML in an alternative; quoted
                  text removed unless --include-quoted); null when
                  unreadable or no text
  body_truncated  only with --show-body: true when body was cut at
                  20,000 characters (or --body-chars N)

Other formats: --paths-only prints the first path per message
(--all-paths: every copy), one per line. Paths contain spaces
("Application Support"): use `tr '\\n' '\\0' | xargs -0 ...`.

EXIT CODES
  0    at least one message printed
  1    no match (not an error)
  2    usage or format error (bad option, bad regex, not a MailMate store,
       unreadable store); one line on stderr
  3    results printed but the index kept changing while it was read;
       results may be incomplete, retry once
  130  interrupted
Diagnostics (unconfirmed or indeterminate counts, warnings) go to
stderr, one line each; stdout stays clean JSON.

FINDING A PERSON'S ADDRESSES (--who)
  When you are given a person's name, not an address, resolve it first:
    mmgrep --who 'carol jensen' --json
  One JSON line per address, most used first:
    {"address": "carol@example.com", "names": ["Carol Jensen", ...],
     "messages": 812, "last": "2026-10-01T09:12:00+02:00"}
  Every word must start a word of the display name or of the address;
  case and accents are ignored ("soren" finds "Søren"), word order does
  not matter and initials work ("carol j"). It takes about 2 s. Pick the
  top address, or several when the person clearly uses more than one
  (look at "messages" and "last"), then search with them:
    mmgrep --from carol@example.com --from carol.j@example.net --json
  Role addresses (support@, info@) can appear when someone sent from them
  under their own name; they usually rank lower. --who accepts only
  --json, --limit and --root; exit 1 means no address matched.

SHOWING RESULTS IN MAILMATE (--open)
  Add --open to any search to also show the results inside MailMate as
  a search for their Message-IDs (first 50 messages):
    mmgrep --from carol@example.com --subject 'contract' --open
  Use it when the user wants to see, read or act on the mails in
  MailMate. MailMate searches its selected mailbox: tell the user to
  select "All Messages" if fewer messages appear than mmgrep printed.
  A stderr line says how many were opened. One message alone also opens
  with: open "message://%3C<message-id>%3E" (brackets URL-encoded).

QUERY SEMANTICS
  Different options AND together; repeating one option ORs its values:
    --from @a.example --from @b.example      from a OR b
    --from @a.example --since 2026-01-01      from a AND since
  For "X OR Y" across different options, run mmgrep twice and union the
  results by message_id.

  ADDR (--from, --to, --cc, --any-address):
    @example.com        that domain and its subdomains
    alice@example.com   exact address, case-insensitive
    re:PATTERN          regex over the whole header value
  --any-address covers From, To, Cc and Bcc. Other headers: --header.
  --not-header NAME[=PATTERN] drops messages with that header (any value,
  or one matching PATTERN); repeated, it drops messages with any of them.
  Copies are checked one by one: A message that reached you both directly
  and through a list is kept, through its direct copy.

  PATTERN (--subject, --header NAME=PATTERN, --body, --account,
  --mailbox): Python regex, case-insensitive (use -s for case-sensitive),
  never rewritten. Quote it for the shell. Escape regex metacharacters
  when you mean them literally ("Re: \\[ticket\\]").

  --body searches unquoted body text only, like MailMate's own search.
  Add --include-quoted to also search quoted replies (slower, finds more).
  ^ and $ anchor at line boundaries.

  Dates (--since, --until): YYYY-MM-DD or ISO datetime, local time unless
  an offset is given. --since is inclusive; a date-only --until includes
  that whole day. The date is the Date header, falling back to the
  received date.

  --account and --mailbox are regexes over the account name (with %40
  shown as @) and the mailbox path such as "INBOX" or "Archive/2024".
  --list-mailboxes prints every account and mailbox with its message
  count (copies in MailMate's index, so it can exceed the rows a search
  of that mailbox prints); with --account, -s, --json, --limit and
  --root only.

FLAGS AND TAGS (--flag, --no-flag)
  --flag NAME keeps a message when any copy has the flag; --no-flag NAME
  keeps it when no copy has it. Repeated --flag: any of them; repeated
  --no-flag: none of them. NAME is seen, answered, flagged, draft,
  deleted, forwarded, junk, a raw IMAP flag (quoted for the shell:
  '\\Seen'; $MailFlagBit0 and up are flag colors) or a MailMate tag
  (tag:NAME for a tag named like an alias, such as tag:seen). Case is
  ignored. Flags come from the index only (an .eml has none): A
  change made seconds ago may not show. Not with --full.
  "Answered" means a reply was sent from MailMate or another IMAP client
  that sets the flag; a reply sent elsewhere, by phone or chat, does not
  set it.

COST AND SPEED
  Header queries: about 1 to 2 s on a store of 180,000 messages.
  Body queries: about 4 s, 8 s with --include-quoted; narrow them with an
  address or date filter. --limit N caps output after sorting.
  --no-confirm skips the .eml check (faster, may include stale rows).
  --full ignores the index and scans every .eml (minutes); use it only
  for mail MailMate has not indexed yet.

RECIPES
  Everything with a domain in one month:
    mmgrep --any-address @example.com --since 2026-10-01 --until 2026-10-31 --json
  A subject regardless of encoding (RFC 2047 subjects are decoded):
    mmgrep --subject 'quarterly report' --json
  A body phrase from one sender, quoted replies included:
    mmgrep --from bob@example.org --body 'contract renewal' --include-quoted --json
  Inbox mail that may still need a reply (not answered, forwarded or
  replied to by the user anywhere in the thread; no mailing lists,
  newsletters or bulk mail). Find the user's own addresses with --who
  first, then:
    mmgrep --mailbox '^INBOX$' --since 2026-10-01 --no-flag answered \\
      --no-flag forwarded --not-header list-id \\
      --not-header list-unsubscribe \\
      --not-header 'precedence=^(bulk|list|junk)$' \\
      --not-header 'from=no-?reply|do-?not-?reply|notifications?@' \\
      --no-reply-from me@example.com --json --show-body --body-chars 800
    then judge the bodies: Drop receipts and FYIs. 800 characters show the
    request in most mails; rerun one message without --body-chars for all.
  Mailbox names differ per account; list them first, then target one
  (sent mail, for example):
    mmgrep --list-mailboxes --json
    {"account": "user@example.com@imap.example.com", "mailbox": "Sent Messages",
     "messages": 4210}
    mmgrep --mailbox '^Sent Messages$' --since 2026-10-01 --json
  Replies to one message:
    mmgrep --header 'in-reply-to=<abc@example.com>' --json
  --no-reply-from ADDR drops a message when a message from ADDR (not a
  draft) lists its Message-ID in In-Reply-To or References. It is
  computed from the index and catches replies sent from any client.
  Mailing lists and newsletters carry a List-Id or List-Unsubscribe header:
    mmgrep --header list-unsubscribe=. --since 2026-10-01 --json
  Read what you sent on one day, quoted text removed:
    mmgrep --from me@example.com --since 2026-10-23 --until 2026-10-23 --json --show-body
  Read the matching messages:
    mmgrep --subject 'invoice' --paths-only | tr '\\n' '\\0' | xargs -0 cat

LIMITS
  Mail MailMate has not indexed yet (seconds to minutes after arrival) is
  invisible except to --full. Addresses with a comment or space around
  the @ and quoted local parts are not found. Verified against MailMate
  2.0 build 6272, index version 1.1; another version prints a warning.

PRIVACY
  Results are private mail. Do not send them to network services or
  paste them into shared places unless the owner asks you to.
"""
