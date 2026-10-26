"""Tests for mmgrep.query (milestone 3, Tasks 1 and 2).

Filters, address specs, dates and candidates on synthetic stores built
with tests.fixtures.build_store, then Message-ID grouping and the index
header values. Expected ids and paths are literal. RFC 2606 domains only;
the negative domain case is built at run time.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from mmgrep.query import (AddrSpec, Copy, Filters, Group, GroupResult,
                          addr_matches, address_tokens, candidates,
                          effective_index_date, flags_ok, group_copies,
                          has_filter, index_headers, mailbox_counts, message_ids,
                          needed_indexes, parse_addr,
                          parse_flag, parse_header_arg, parse_index_date,
                          parse_not_header_arg, reply_ids,
                          parse_since, parse_until)
from mmgrep.store import Store
from tests import fixtures
from tests.fixtures import FixtureMessage, FixturePart, build_store

ACCOUNT = "user%40example.com@imap.example.com"
OTHER = "bob@mail.example.org"
UTC = timezone.utc


def msg(id: int, account: str = ACCOUNT, mailbox: str = "INBOX",
        **kw) -> FixtureMessage:
    return FixtureMessage(id=id, account=account, mailbox=mailbox, **kw)


def hdr(id: int, **extra: object) -> dict:
    headers = {"From": f"sender{id}@example.org", "Subject": f"Message {id}",
               "Message-ID": f"<{id}@example.com>",
               "Date": "Tue, 05 Mar 2024 14:07:09 +0100"}
    for key, value in extra.items():
        name = key.replace("_", "-")
        if value is None:
            headers.pop(name, None)
        else:
            headers[name] = value
    return headers


class _StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, messages: list[FixtureMessage]) -> None:
        build_store(self.root, messages)
        self.store = Store(self.root)

    def snap(self, f: Filters, extra: tuple = ()):
        snap = self.store.snapshot(needed_indexes(f) | set(extra), backoff=0)
        self.assertFalse(snap.unstable, snap.warnings)
        return snap

    def ids(self, f: Filters) -> set[int]:
        return candidates(self.snap(f), f).ids

    def groups(self, f: Filters, ids=None) -> GroupResult:
        snap = self.snap(f)
        if ids is None:
            ids = candidates(snap, f).ids
        return group_copies(snap, ids, f, self.store.messages_dir)


class AddrSpecTest(unittest.TestCase):
    def test_parse_addr(self) -> None:
        self.assertEqual(parse_addr("@example.com"), AddrSpec("domain", "example.com"))
        self.assertEqual(parse_addr("Bob@Example.ORG"), AddrSpec("exact", "bob@example.org"))
        self.assertEqual(parse_addr("re:^a"), AddrSpec("regex", "^a"))
        for bad in ("example.com", "@", "a@", "re:("):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_addr(bad)

    def test_domain_spec(self) -> None:
        spec = parse_addr("@example.com")
        self.assertTrue(addr_matches(spec, "a@example.com", 0))
        self.assertTrue(addr_matches(spec, "a@sub.example.com", 0))
        self.assertFalse(addr_matches(spec, "a@example.org", 0))
        lookalike = "@".join(("a", "not" + "example.com"))
        self.assertFalse(addr_matches(spec, lookalike, 0))

    def test_address_tokens_atext(self) -> None:
        self.assertEqual(
            address_tokens('"X" <!bang@example.com>, o\'brien@example.org'),
            {"!bang@example.com", "o'brien@example.org"})

    def test_parse_header_arg(self) -> None:
        self.assertEqual(parse_header_arg("X-Mailer=^Tool"), ("x-mailer", "^Tool"))
        self.assertEqual(parse_header_arg("x-a=b=c"), ("x-a", "b=c"))
        for bad in ("x:y=a", "#date=a", "x/y=a", "..=a", "=a", "x y=a", "noequals"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_header_arg(bad)

    def test_has_filter(self) -> None:
        self.assertFalse(has_filter(Filters()))
        self.assertFalse(has_filter(Filters(include_quoted=True, case_sensitive=True)))
        self.assertTrue(has_filter(Filters(subject=["x"])))
        self.assertTrue(has_filter(Filters(body="x")))
        self.assertTrue(has_filter(Filters(no_flag=[frozenset({"\\seen"})])))
        self.assertTrue(has_filter(Filters(since=datetime(2024, 1, 1, tzinfo=UTC))))

    def test_needed_indexes(self) -> None:
        base = {"#source", "message-id", "#date", "#date-received", "from", "to",
                "cc", "subject"}
        self.assertEqual(needed_indexes(Filters()), base)
        self.assertEqual(needed_indexes(Filters(any_address=[parse_addr("@example.com")])),
                         base | {"bcc"})
        self.assertEqual(needed_indexes(Filters(body="x")),
                         base | {"#unquoted", "#root-body-part"})
        self.assertEqual(needed_indexes(Filters(body="x", include_quoted=True)),
                         base | {"#unquoted", "#root-body-part", "#quoted"})
        self.assertEqual(needed_indexes(Filters(headers={"x-mailer": ["a"]})),
                         base | {"x-mailer"})
        self.assertEqual(needed_indexes(Filters(no_flag=[parse_flag("seen")])),
                         base | {"#flags"})


class DateParseTest(unittest.TestCase):
    def setUp(self) -> None:
        old = os.environ.get("TZ")

        def restore() -> None:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = "Europe/Copenhagen"
        time.tzset()

    def test_since_until_dst_bounds(self) -> None:
        self.assertEqual(parse_since("2024-03-31"), datetime(2024, 3, 30, 23, tzinfo=UTC))
        self.assertEqual(parse_until("2024-03-31"), datetime(2024, 3, 31, 22, tzinfo=UTC))
        self.assertEqual(parse_until("2024-03-31T10:00+02:00"),
                         datetime(2024, 3, 31, 8, tzinfo=UTC))
        self.assertEqual(parse_since("2024-03-31T10:00:00Z"),
                         datetime(2024, 3, 31, 10, tzinfo=UTC))
        self.assertEqual(parse_since("2024-03-31 10:00"),
                         datetime(2024, 3, 31, 8, tzinfo=UTC))
        for bad in ("2024-3-31", "yesterday", "2024-02-30", "2024-03-31T10", ""):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "--until"):
                    parse_until(bad)
                with self.assertRaisesRegex(ValueError, "--since"):
                    parse_since(bad)

    def test_parse_index_date_equals_strptime(self) -> None:
        def reference(value: bytes, received: bool):
            try:
                text = value.decode("ascii")
                if received:
                    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")
            except ValueError:
                return None

        dated = [b"2024-03-05 14:07:09 +0100", b"2024-03-05 14:07:09 -0000",
                 b"2024-03-05 14:07:09 +0530", b"2024-03-05 14:07:09 +1400",
                 b"2024-03-05 14:07:09 -1200", b"2024-02-30 10:00:00 +0000",
                 b"2024-02-29 23:59:59 +0000", b"garbage", b"",
                 b"2024-03-05 14:07:09", b"2024-03-05 24:00:00 +0000",
                 b"2024-03-05 14:07:60 +0000", b"2024-03-05 14:07:09 +2400",
                 b"2024-3-5 1:2:3 +0100", b"2024-03-05  14:07:09 +0100",
                 b"2024-03-05 14:07:09 +0160"]
        received = [b"2024-03-05 13:07:12", b"2024-02-30 13:07:12",
                    b"2024-03-05 13:07:12 +0000", b"garbage"]
        self.assertEqual(len(dated) + len(received), 20)
        for value in dated:
            with self.subTest(value=value):
                got = parse_index_date(value, False)
                self.assertEqual(got, reference(value, False))
                if got is not None:
                    self.assertEqual(got.utcoffset(), reference(value, False).utcoffset())
        for value in received:
            with self.subTest(value=value):
                self.assertEqual(parse_index_date(value, True), reference(value, True))
        self.assertEqual(parse_index_date(b"2024-03-05 14:07:09 +0530", False).utcoffset(),
                         timedelta(hours=5, minutes=30))
        self.assertIsNone(parse_index_date("2024-03-05 14:07:09 +0100".encode("utf-16"), False))


class CandidateTest(_StoreTest):
    def test_exact_spec_case_insensitive(self) -> None:
        self.build([msg(1200, headers=hdr(1200, From="Alice <Alice@Example.com>"))])
        self.assertEqual(self.ids(Filters(from_=[parse_addr("alice@example.COM")])), {1200})

    def _three(self) -> None:
        self.build([
            msg(1200, headers=hdr(1200, From="a@example.com", Subject="Plan A",
                                  To="t1@example.net")),
            msg(1300, headers=hdr(1300, From="b@example.org", Subject="Plan B",
                                  Cc="c1@example.net", X_Mailer="Tool 1")),
            msg(1400, mailbox="Archive", headers=hdr(1400, From="a@example.com",
                                                     Subject="Other")),
        ])

    def test_each_filter_alone(self) -> None:
        self._three()
        cases = {
            "from": (Filters(from_=[parse_addr("a@example.com")]), {1200, 1400}),
            "to": (Filters(to=[parse_addr("@example.net")]), {1200}),
            "cc": (Filters(cc=[parse_addr("re:^c1@")]), {1300}),
            "any": (Filters(any_address=[parse_addr("c1@example.net")]), {1300}),
            "subject": (Filters(subject=["^Plan"]), {1200, 1300}),
            "header": (Filters(headers={"x-mailer": ["tool"]}), {1300}),
            "mailbox": (Filters(mailbox=["^Archive$"]), {1400}),
            "account": (Filters(account=["^user@"]), {1200, 1300, 1400}),
            "since": (Filters(since=datetime(2024, 3, 5, 13, 7, 9, tzinfo=UTC)),
                      {1200, 1300, 1400}),
            "until": (Filters(until=datetime(2024, 3, 5, 13, 7, 9, tzinfo=UTC)), set()),
        }
        for label, (f, expected) in cases.items():
            with self.subTest(option=label):
                self.assertEqual(self.ids(f), expected)

    def test_and_across_or_within(self) -> None:
        self._three()
        self.assertEqual(self.ids(Filters(from_=[parse_addr("a@example.com")],
                                          subject=["^Plan"])), {1200})
        self.assertEqual(self.ids(Filters(from_=[parse_addr("a@example.com"),
                                                 parse_addr("b@example.org")])),
                         {1200, 1300, 1400})
        self.assertEqual(self.ids(Filters(subject=["Plan A", "Other"])), {1200, 1400})
        # Narrowed first by the mailbox, so the per-candidate loop runs.
        self.assertEqual(self.ids(Filters(mailbox=["^INBOX$"], subject=["Plan A", "Other"],
                                          from_=[parse_addr("@example.com")])), {1200})

    def test_narrowed_address_filters(self) -> None:
        self.build([
            msg(1200, headers=hdr(1200, From="a@example.com", To="t1@example.net")),
            msg(1300, headers=hdr(1300, From="b@example.org", To="t2@example.net")),
            msg(1400, mailbox="Archive", headers=hdr(1400, From="a@example.com")),
        ])
        a = [parse_addr("a@example.com")]
        self.assertEqual(self.ids(Filters(mailbox=["^INBOX$"], from_=a)), {1200})
        self.assertEqual(self.ids(Filters(mailbox=["^INBOX$"],
                                          from_=[parse_addr("@example.com")])), {1200})
        self.assertEqual(self.ids(Filters(from_=a, to=[parse_addr("x@example.net")])),
                         set())
        self.assertEqual(self.ids(Filters(from_=a, to=[parse_addr("t1@example.net")])),
                         {1200})

    def test_any_address_bcc_only(self) -> None:
        self.build([msg(1500, headers=hdr(1500, Bcc="x@example.net"))])
        spec = [parse_addr("x@example.net")]
        self.assertEqual(self.ids(Filters(any_address=spec)), {1500})
        self.assertEqual(self.ids(Filters(to=spec)), set())

    def test_since_until_dst(self) -> None:
        old = os.environ.get("TZ")

        def restore() -> None:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = "Europe/Copenhagen"
        time.tzset()
        self.build([
            msg(1200, headers=hdr(1200, Date="Sun, 31 Mar 2024 23:30:00 +0200")),
            msg(1300, headers=hdr(1300, Date="Mon, 01 Apr 2024 00:30:00 +0200")),
            msg(1400, headers=hdr(1400, Date="Sat, 30 Mar 2024 23:59:59 +0100")),
            msg(1500, headers=hdr(1500, Date="Sun, 31 Mar 2024 00:00:00 +0100")),
        ])
        f = Filters(since=parse_since("2024-03-31"), until=parse_until("2024-03-31"))
        self.assertEqual(self.ids(f), {1200, 1500})

    def test_date_fallback_received(self) -> None:
        self.build([msg(1600, headers=hdr(1600, Date=None),
                        index={"#date-received": b"2024-03-05 13:07:12"})])
        self.assertEqual(self.ids(Filters(since=parse_since("2024-03-05T13:07:12+00:00"))),
                         {1600})
        self.assertEqual(self.ids(Filters(since=parse_since("2024-03-05T13:07:13+00:00"))),
                         set())

    def test_effective_index_date(self) -> None:
        self.build([
            msg(1200, headers=hdr(1200)),
            msg(1600, headers=hdr(1600, Date=None),
                index={"#date-received": b"2024-03-05 13:07:12"}),
            msg(1700, headers=hdr(1700, Date=None)),
        ])
        snap = self.snap(Filters())
        dt, source = effective_index_date(snap, 1200)
        self.assertEqual((dt, source),
                         (datetime(2024, 3, 5, 13, 7, 9, tzinfo=UTC), "date"))
        self.assertEqual(dt.utcoffset(), timedelta(hours=1))
        self.assertEqual(effective_index_date(snap, 1600),
                         (datetime(2024, 3, 5, 13, 7, 12, tzinfo=UTC), "received"))
        self.assertEqual(effective_index_date(snap, 1700), (None, None))

    def test_subject_rfc2047(self) -> None:
        self.build([msg(1200, headers=hdr(
            1200, Subject="=?UTF-8?Q?Gr=C3=BC=C3=9Fe?= aus Berlin"))])
        self.assertEqual(self.ids(Filters(subject=["grüße"])), {1200})
        self.assertEqual(self.ids(Filters(subject=["grüße"], case_sensitive=True)), set())

    def test_body_part_maps_to_message(self) -> None:
        self.build([msg(1200, headers=hdr(1200), parts=[
            FixturePart(body=["alpha one"]),
            FixturePart(content_type="text/html", body=["beta two"])])])
        f = Filters(body="alpha")
        result = candidates(self.snap(f), f)
        self.assertEqual(result.ids, {1200})
        self.assertEqual(result.stats["unmapped_parts"], 0)
        self.assertEqual(set(result.stats), {"live", "after_location", "after_dates",
                                             "after_flags", "after_replies", "after_headers", "after_body",
                                             "unmapped_parts"})

    def test_body_multiline(self) -> None:
        self.build([msg(1200, headers=hdr(1200), body=["First line\nSecond paragraph"])])
        self.assertEqual(self.ids(Filters(body="^Second paragraph$")), {1200})
        self.assertEqual(self.ids(Filters(body="^line")), set())

    def test_body_quoted_needs_flag(self) -> None:
        self.build([msg(1200, headers=hdr(1200), body=["hello"],
                        quoted=["only quoted gamma"])])
        self.assertEqual(self.ids(Filters(body="gamma")), set())
        self.assertEqual(self.ids(Filters(body="gamma", include_quoted=True)), {1200})

    def test_account_mailbox(self) -> None:
        self.build([
            msg(1200, mailbox="Archive/2024", headers=hdr(1200)),
            msg(1300, mailbox="Archive/2024/Old", headers=hdr(1300)),
            msg(1400, account=OTHER, mailbox="Archive/2024", headers=hdr(1400)),
        ])
        self.assertEqual(self.ids(Filters(account=["^user@example.com"])), {1200, 1300})
        self.assertEqual(self.ids(Filters(mailbox=["^Archive/2024$"])), {1200, 1400})

    def test_display_name_token_is_candidate(self) -> None:
        self.build([msg(1200, headers=hdr(
            1200, From='"alice@example.com" <mallory@example.org>'))])
        self.assertEqual(self.ids(Filters(from_=[parse_addr("alice@example.com")])), {1200})

    def test_address_tokens_atext_candidate(self) -> None:
        self.build([msg(1700, headers=hdr(1700, From="!bang@example.com"))])
        self.assertEqual(self.ids(Filters(from_=[parse_addr("!bang@example.com")])), {1700})

    def test_exact_not_inside_longer_token(self) -> None:
        self.build([msg(1800, headers=hdr(1800, From="xbob@example.com"))])
        self.assertEqual(self.ids(Filters(from_=[parse_addr("bob@example.com")])), set())
        self.assertEqual(self.ids(Filters(from_=[parse_addr("@example.com")])), {1800})


class GroupTest(_StoreTest):
    def _dup(self) -> None:
        self.build([
            msg(1300, headers=hdr(1300, Subject="Dup")),
            msg(1500, mailbox="Sent Messages", duplicate_of=1300),
        ])

    def test_dedup_two_mailboxes(self) -> None:
        self._dup()
        result = self.groups(Filters(subject=["^Dup$"]))
        self.assertEqual(result.missing_eml, 0)
        self.assertEqual(len(result.groups), 1)
        group = result.groups[0]
        self.assertIsInstance(group, Group)
        self.assertEqual(group.message_id, "<1300@example.com>")
        self.assertEqual([c.id for c in group.copies], [1300, 1500])
        self.assertEqual([c.path for c in group.copies], [
            str(fixtures.eml_path(self.root, ACCOUNT, "INBOX", 1300)),
            str(fixtures.eml_path(self.root, ACCOUNT, "Sent Messages", 1500))])
        self.assertEqual(group.copies[0].account, "user@example.com@imap.example.com")
        self.assertEqual(group.copies[1].mailbox, "Sent Messages")
        self.assertIsInstance(group.copies[0], Copy)

    def test_mailbox_restricts_paths(self) -> None:
        self._dup()
        result = self.groups(Filters(subject=["^Dup$"], mailbox=["^INBOX$"]))
        self.assertEqual([[c.id for c in g.copies] for g in result.groups], [[1300]])

    def test_expansion_adds_non_candidate(self) -> None:
        self._dup()
        result = self.groups(Filters(subject=["^Dup$"]), ids={1300})
        self.assertEqual([[c.id for c in g.copies] for g in result.groups], [[1300, 1500]])

    def test_missing_eml_not_qualifying(self) -> None:
        self.build([
            msg(1300, headers=hdr(1300, Subject="Dup")),
            msg(1500, mailbox="Sent Messages", duplicate_of=1300, write_eml=False),
            msg(1700, headers=hdr(1700, Subject="Dup"), write_eml=False),
        ])
        result = self.groups(Filters(subject=["^Dup$"]))
        self.assertEqual([[c.id for c in g.copies] for g in result.groups], [[1300]])
        self.assertEqual(result.missing_eml, 2)

    def test_no_message_id_stands_alone(self) -> None:
        self.build([
            msg(1200, headers=hdr(1200, Subject="Lone", Message_ID=None)),
            msg(1300, headers=hdr(1300, Subject="Lone", Message_ID=None)),
            msg(1400, headers=hdr(1400, Subject="Lone"), index={"message-id": ""}),
        ])
        result = self.groups(Filters(subject=["^Lone$"]))
        self.assertEqual([(g.message_id, [c.id for c in g.copies]) for g in result.groups],
                         [(None, [1200]), (None, [1300]), (None, [1400])])

    def test_index_headers(self) -> None:
        self.build([msg(1200, headers=hdr(
            1200, From="Alice <alice@example.com>",
            To=["bob@example.org", "Carol <carol@example.net>"],
            Subject="=?UTF-8?Q?Gr=C3=BC=C3=9Fe?= aus Berlin"))])
        expected = {"date": "", "from": "Alice <alice@example.com>",
                    "to": "bob@example.org, Carol <carol@example.net>", "cc": "",
                    "subject": "Grüße aus Berlin", "message-id": "<1200@example.com>",
                    "in-reply-to": "", "references": ""}
        self.assertEqual(index_headers(self.snap(Filters()), 1200), expected)
        expected["date"] = "Tue, 05 Mar 2024 14:07:09 +0100"
        self.assertEqual(index_headers(self.snap(Filters(), extra=("date",)), 1200),
                         expected)

    def test_message_ids(self) -> None:
        self.assertEqual(message_ids("<a@example.com> <b@example.org>"),
                         ["<a@example.com>", "<b@example.org>"])
        self.assertEqual(message_ids("Re: note <c@example.net> (sent)"), ["<c@example.net>"])
        self.assertEqual(message_ids(""), [])
        self.assertEqual(message_ids("<>"), [])
        self.assertEqual(message_ids("<a@example.com>\n\t<b@example.org>, <c@example.net>"),
                         ["<a@example.com>", "<b@example.org>", "<c@example.net>"])

    def test_unmapped_source_counts(self) -> None:
        self.build([msg(1200, headers=hdr(1200, Subject="Pop"),
                        index={"#source": "pop://x/INBOX"})])
        f = Filters(subject=["^Pop$"])
        snap = self.snap(f)
        self.assertEqual(candidates(snap, f).ids, {1200})
        result = group_copies(snap, {1200}, f, self.store.messages_dir)
        self.assertEqual((result.groups, result.missing_eml), ([], 1))

    def test_copy_dates(self) -> None:
        self.build([
            msg(1300, mailbox="A", headers=hdr(1300, Subject="Dup", Date=None),
                index={"#date-received": b"2024-03-01 10:00:00"}),
            msg(1500, mailbox="B", headers=hdr(1300, Subject="Dup", Date=None),
                index={"#date-received": b"2024-03-10 10:00:00"}),
            msg(1600, mailbox="C", headers=hdr(1300, Subject="Dup")),
        ])
        f = Filters(subject=["^Dup$"])
        snap = self.snap(f)
        result = group_copies(snap, candidates(snap, f).ids, f, self.store.messages_dir)
        self.assertEqual(len(result.groups), 1)
        copies = result.groups[0].copies
        self.assertEqual([c.id for c in copies], [1300, 1500, 1600])
        for c in copies:
            self.assertEqual((c.index_date, c.date_source), effective_index_date(snap, c.id))
        self.assertEqual([c.date_source for c in copies], ["received", "received", "date"])
        self.assertEqual(copies[1].index_date, datetime(2024, 3, 10, 10, tzinfo=UTC))


class MailboxCountTest(_StoreTest):
    def test_mailbox_counts(self) -> None:
        self.build([
            msg(1200, headers=hdr(1200)),
            msg(1300, headers=hdr(1300)),
            msg(1400, mailbox="Sent Messages", headers=hdr(1400)),
            msg(1500, account=OTHER, mailbox="Archive/2024", headers=hdr(1500)),
            msg(1600, headers=hdr(1600), deleted=True),
            msg(1700, headers=hdr(1700), index={"#source": "pop://x/INBOX"}),
        ])
        snap = self.snap(Filters())
        user = "user@example.com@imap.example.com"
        self.assertEqual(mailbox_counts(snap, Filters()), ([
            ("bob@mail.example.org", "Archive/2024", 1),
            (user, "INBOX", 2),
            (user, "Sent Messages", 1),
        ], 1))
        self.assertEqual(mailbox_counts(snap, Filters(account=["^bob@"])),
                         ([("bob@mail.example.org", "Archive/2024", 1)], 1))


class ReplyTest(_StoreTest):
    def _thread(self) -> None:
        me = "Me <me@example.com>"
        self.build([
            msg(1200, headers=hdr(1200)),                       # never answered
            msg(1300, headers=hdr(1300)),                       # answered directly
            msg(1400, headers=hdr(1400)),                       # answered further down
            msg(1500, headers=hdr(1500)),                       # only a draft reply
            msg(1600, headers=hdr(1600)),                       # answered by someone else
            msg(2300, mailbox="Sent", headers=hdr(2300, From=me,
                In_Reply_To="<1300@example.com>")),
            msg(2400, mailbox="Sent", headers=hdr(2400, From=me,
                In_Reply_To="<1450@example.com>",
                References="<1400@example.com> <1450@example.com>")),
            msg(2500, mailbox="Drafts", headers=hdr(2500, From=me,
                In_Reply_To="<1500@example.com>"),
                index={"#flags": "\\Draft \\Seen"}),
            msg(2600, headers=hdr(2600, From="other@example.net",
                In_Reply_To="<1600@example.com>")),
        ])

    def test_reply_ids(self) -> None:
        self._thread()
        f = Filters(no_reply_from=[parse_addr("me@example.com")])
        snap = self.snap(f)
        self.assertEqual(reply_ids(snap, f.no_reply_from, 0), {
            "<1300@example.com>", "<1400@example.com>", "<1450@example.com>"})

    def test_no_reply_from(self) -> None:
        self._thread()
        cases = {
            "me": (["me@example.com"], {1200, 1500, 1600}),
            "own mail dropped": (["me@example.com"], {1200, 1500, 1600, 2600}),
            "domain": (["@example.net"], {1200, 1300, 1400, 1500}),
            "either": (["me@example.com", "other@example.net"], {1200, 1500}),
        }
        for label, (specs, expected) in cases.items():
            with self.subTest(case=label):
                narrow = label != "own mail dropped"
                f = Filters(mailbox=["^INBOX$"] if narrow else [],
                            subject=["^Message 1"] if narrow else [],
                            no_reply_from=[parse_addr(s) for s in specs])
                self.assertEqual(self.ids(f), expected)


class NotHeaderTest(_StoreTest):
    def test_parse_not_header_arg(self) -> None:
        self.assertEqual(parse_not_header_arg("List-Id"), ("list-id", ""))
        self.assertEqual(parse_not_header_arg("Precedence=bulk|list"),
                         ("precedence", "bulk|list"))
        for bad in ("", "#flags", "x y"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_not_header_arg(bad)

    def test_not_header_excludes(self) -> None:
        self.build([
            msg(1200, headers=hdr(1200, List_Id="<news.example.com>")),
            msg(1300, headers=hdr(1300, Precedence="bulk")),
            msg(1400, headers=hdr(1400, Precedence="first-class")),
            msg(1500, headers=hdr(1500)),
        ])
        cases = {
            "any value": (Filters(not_headers={"list-id": [""]}), {1300, 1400, 1500}),
            "pattern": (Filters(not_headers={"precedence": ["^(bulk|list)$"]}),
                        {1200, 1400, 1500}),
            "none of": (Filters(not_headers={"list-id": [""], "precedence": [""]}),
                        {1500}),
            "with a positive filter": (Filters(subject=["Message 1[45]00"],
                                               not_headers={"precedence": ["bulk"]}),
                                       {1400, 1500}),
        }
        for label, (f, expected) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(self.ids(f), expected)


class FlagTest(_StoreTest):
    def _flagged(self) -> None:
        # 1200 answered; 1300 unanswered in INBOX, answered in its Archive
        # copy 1500; 1400 forwarded and tagged; 1600 has an empty value.
        self.build([
            msg(1200, headers=hdr(1200), index={"#flags": "\\Answered \\Seen"}),
            msg(1300, headers=hdr(1300), index={"#flags": "\\Seen"}),
            msg(1500, mailbox="Archive", duplicate_of=1300,
                index={"#flags": "\\answered \\SEEN"}),
            msg(1400, headers=hdr(1400), index={"#flags": "$Forwarded Done"}),
            msg(1600, headers=hdr(1600), index={"#flags": b""}),
        ])

    def test_parse_flag(self) -> None:
        self.assertEqual(parse_flag("Answered"), frozenset({"\\answered"}))
        self.assertEqual(parse_flag("forwarded"),
                         frozenset({"$forwarded", "forwarded"}))
        self.assertEqual(parse_flag("\\Seen"), frozenset({"\\seen"}))
        self.assertEqual(parse_flag("$MailFlagBit0"), frozenset({"$mailflagbit0"}))
        self.assertEqual(parse_flag("Done"), frozenset({"done"}))
        self.assertEqual(parse_flag("tag:Seen"), frozenset({"seen"}))
        self.assertEqual(parse_flag("tag:junk"), frozenset({"junk"}))
        for bad in ("", "a b", "x\ty", "tag:"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_flag(bad)

    def test_flags_ok(self) -> None:
        answered, seen = parse_flag("answered"), parse_flag("seen")
        self.assertTrue(flags_ok({"\\answered"}, Filters(flag=[answered])))
        self.assertTrue(flags_ok({"\\seen"}, Filters(flag=[answered, seen])))
        self.assertFalse(flags_ok({"\\seen"}, Filters(no_flag=[answered, seen])))
        self.assertTrue(flags_ok(set(), Filters(no_flag=[answered])))
        self.assertFalse(flags_ok({"\\seen"}, Filters(flag=[seen], no_flag=[seen])))

    def test_flag_filters(self) -> None:
        self._flagged()
        a, fw, done = parse_flag("answered"), parse_flag("forwarded"), parse_flag("done")
        cases = {
            "answered": (Filters(flag=[a]), {1200, 1300, 1500}),
            "not answered": (Filters(no_flag=[a]), {1400, 1600}),
            "none of two": (Filters(no_flag=[a, fw]), {1600}),
            "tag": (Filters(flag=[done]), {1400}),
            "union beats mailbox": (Filters(no_flag=[a], mailbox=["^INBOX$"]),
                                    {1400, 1600}),
        }
        for label, (f, expected) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(self.ids(f), expected)

    def test_group_flags_union(self) -> None:
        self._flagged()
        f = Filters(subject=["^Message 1[34]00$"], mailbox=["^INBOX$"])
        snap = self.snap(f, extra=("#flags",))
        result = group_copies(snap, candidates(snap, f).ids, f, self.store.messages_dir)
        self.assertEqual([([c.id for c in g.copies], g.flags) for g in result.groups], [
            ([1300], ["\\answered", "\\Seen"]),
            ([1400], ["$Forwarded", "Done"]),
        ])

    def test_flag_edge_cases(self) -> None:
        # 1200 has no Message-ID; 1300 has no #flags record; 1400 is seen
        # in March, its copy 1500 answered in May.
        self.build([
            msg(1200, headers=hdr(1200, Message_ID=None), index={"#flags": "\\Seen"}),
            msg(1250, headers=hdr(1250, Message_ID=None),
                index={"#flags": "\\Answered \\Seen"}),
            msg(1300, headers=hdr(1300)),
            msg(1400, headers=hdr(1400), index={"#flags": "\\Seen"}),
            msg(1500, mailbox="Archive", index={"#flags": "\\Answered"},
                headers=hdr(1400, Date="Wed, 01 May 2024 10:00:00 +0000")),
        ])
        a, seen = parse_flag("answered"), parse_flag("seen")
        march = datetime(2024, 4, 1, tzinfo=UTC)
        cases = {
            "own flags only": (Filters(flag=[seen], no_flag=[a]), {1200}),
            "no record": (Filters(no_flag=[a, seen]), {1300}),
            "date-excluded copy counts": (Filters(no_flag=[a], until=march), {1200, 1300}),
            "flag and no-flag": (Filters(flag=[a], no_flag=[parse_flag("flagged")]),
                                 {1250, 1400, 1500}),
            "no-flag sees the union": (Filters(flag=[a], no_flag=[seen]), set()),
        }
        for label, (f, expected) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(self.ids(f), expected)

    def test_group_flags_none_without_index(self) -> None:
        self.build([msg(1200, headers=hdr(1200))])
        f = Filters(subject=["."])
        self.assertIsNone(self.groups(f).groups[0].flags)


if __name__ == "__main__":
    unittest.main()
