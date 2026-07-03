"""Tests for util/imap.py: URL parsing, BODYSTRUCTURE parsing, decoding, session."""

from __future__ import annotations

import imaplib
from typing import TYPE_CHECKING, cast

import pytest

from pulpline.models import FetchError
from pulpline.util.imap import (
    ImapSession,
    MimePart,
    _flatten_fetch_records,
    decode_transfer_encoding,
    parse_bodystructure_record,
    parse_mailbox_url,
)

if TYPE_CHECKING:
    from imap_tools import MailBox


class TestParseMailboxUrl:
    def test_imaps_defaults(self) -> None:
        loc = parse_mailbox_url("imaps://imap.gmail.com/Pulpline")
        assert loc.host == "imap.gmail.com"
        assert loc.port == 993
        assert loc.folder == "Pulpline"
        assert loc.starttls is False

    def test_imap_starttls_defaults(self) -> None:
        loc = parse_mailbox_url("imap://mail.example.com")
        assert loc.port == 143
        assert loc.folder == "INBOX"
        assert loc.starttls is True

    def test_explicit_port_and_nested_escaped_folder(self) -> None:
        loc = parse_mailbox_url("imaps://mail.example.com:1993/Books%20Inbox/epubs")
        assert loc.port == 1993
        assert loc.folder == "Books Inbox/epubs"

    def test_rejects_non_imap_scheme(self) -> None:
        with pytest.raises(ValueError, match="not an imap"):
            parse_mailbox_url("https://example.com/feed")

    def test_rejects_credentials_in_url(self) -> None:
        with pytest.raises(ValueError, match="credentials"):
            parse_mailbox_url("imaps://user:pass@imap.example.com/INBOX")

    def test_rejects_starttls_scheme_on_implicit_tls_port(self) -> None:
        with pytest.raises(ValueError, match="use imaps://"):
            parse_mailbox_url("imap://imap.example.com:993/INBOX")


class TestBodystructureParsing:
    def test_simple_html_message(self) -> None:
        record = (
            b'1 (UID 5 BODYSTRUCTURE ("text" "html" ("charset" "utf-8") NIL NIL'
            b' "quoted-printable" 1234 20))'
        )
        uid, parts = parse_bodystructure_record(record)
        assert uid == "5"
        assert parts == [
            MimePart(
                section="1",
                content_type="text/html",
                filename=None,
                encoding="quoted-printable",
            )
        ]

    def test_multipart_mixed_with_epub_attachment(self) -> None:
        record = (
            b'2 (UID 77 BODYSTRUCTURE (("text" "html" ("charset" "utf-8") NIL NIL'
            b' "7bit" 100 4 NIL NIL NIL NIL)'
            b'("application" "epub+zip" ("name" "Some Book.epub") NIL NIL "base64" 5000'
            b' NIL ("attachment" ("filename" "Some Book.epub")) NIL NIL)'
            b' "mixed" ("boundary" "b1") NIL NIL NIL))'
        )
        uid, parts = parse_bodystructure_record(record)
        assert uid == "77"
        assert [p.section for p in parts] == ["1", "2"]
        epub = parts[1]
        assert epub.content_type == "application/epub+zip"
        assert epub.filename == "Some Book.epub"
        assert epub.encoding == "base64"

    def test_nested_alternative_inside_mixed_gets_dotted_sections(self) -> None:
        record = (
            b"3 (UID 9 BODYSTRUCTURE ("
            b'(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
            b'("text" "html" ("charset" "utf-8") NIL NIL "7bit" 20 1 NIL NIL NIL NIL)'
            b' "alternative" ("boundary" "b2") NIL NIL NIL)'
            b'("application" "pdf" ("name" "paper.pdf") NIL NIL "base64" 999'
            b' NIL ("attachment" ("filename" "paper.pdf")) NIL NIL)'
            b' "mixed" ("boundary" "b1") NIL NIL NIL))'
        )
        _, parts = parse_bodystructure_record(record)
        assert [(p.section, p.content_type) for p in parts] == [
            ("1.1", "text/plain"),
            ("1.2", "text/html"),
            ("2", "application/pdf"),
        ]

    def test_rfc2047_encoded_filename_is_decoded(self) -> None:
        record = (
            b'4 (UID 11 BODYSTRUCTURE ("application" "epub+zip"'
            b' ("name" "=?utf-8?q?caf=C3=A9=2Eepub?=") NIL NIL "base64" 100))'
        )
        _, parts = parse_bodystructure_record(record)
        assert parts[0].filename == "café.epub"

    def test_rfc2231_extended_filename_is_decoded(self) -> None:
        record = (
            b'5 (UID 12 BODYSTRUCTURE ("application" "pdf"'
            b' ("name*" "utf-8\'\'caf%C3%A9.pdf") NIL NIL "base64" 100))'
        )
        _, parts = parse_bodystructure_record(record)
        assert parts[0].filename == "café.pdf"

    def test_rfc2231_filename_with_language_tag_is_decoded(self) -> None:
        record = (
            b'5 (UID 15 BODYSTRUCTURE ("application" "pdf"'
            b' ("name*" "utf-8\'en\'caf%C3%A9.pdf") NIL NIL "base64" 100))'
        )
        _, parts = parse_bodystructure_record(record)
        assert parts[0].filename == "café.pdf"

    def test_bogus_charset_in_filename_degrades_instead_of_crashing(self) -> None:
        # errors="replace" doesn't guard the codec *lookup*; a bogus charset
        # name must not escape as LookupError and kill the sync.
        record = (
            b'6 (UID 16 BODYSTRUCTURE ("application" "epub+zip"'
            b' ("name" "=?bogus-cs?B?Zm9vLmVwdWI=?=") NIL NIL "base64" 100))'
        )
        _, parts = parse_bodystructure_record(record)
        assert parts[0].filename is not None
        assert parts[0].filename.endswith(".epub")

    def test_literal_filename_is_reassembled(self) -> None:
        # imaplib splits string literals into (head, literal) tuples followed
        # by the record's continuation bytes.
        data: list[object] = [
            (
                b'6 (UID 13 BODYSTRUCTURE ("application" "epub+zip" ("name" {9}',
                b"book.epub",
            ),
            b') NIL NIL "base64" 100))',
        ]
        records = _flatten_fetch_records(data)
        assert len(records) == 1
        uid, parts = parse_bodystructure_record(records[0])
        assert uid == "13"
        assert parts[0].filename == "book.epub"

    def test_garbage_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            parse_bodystructure_record(b"7 (UID 14 BODYSTRUCTURE (nonsense)")
        with pytest.raises(ValueError):
            parse_bodystructure_record(b"total garbage with no parens")

    def test_missing_uid_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            parse_bodystructure_record(b'8 (BODYSTRUCTURE ("text" "html" NIL NIL NIL "7bit" 5 1))')


class TestDecodeTransferEncoding:
    def test_base64(self) -> None:
        assert decode_transfer_encoding(b"aGVsbG8=\r\n", "base64") == b"hello"

    def test_quoted_printable(self) -> None:
        assert decode_transfer_encoding(b"caf=C3=A9", "quoted-printable") == "café".encode()

    def test_7bit_passthrough(self) -> None:
        assert decode_transfer_encoding(b"raw bytes", "7bit") == b"raw bytes"
        assert decode_transfer_encoding(b"raw bytes", "") == b"raw bytes"


# --- ImapSession over a stub mailbox ------------------------------------------


class FakeImapClient:
    """Stands in for the raw imaplib client hanging off imap-tools."""

    def __init__(self, capabilities: tuple[str, ...] = ("IMAP4REV1", "MOVE")) -> None:
        self.capabilities = capabilities
        self.uid_calls: list[tuple[str, ...]] = []
        self.store_response: tuple[str, list[object]] = ("OK", [])
        self.fetch_response: tuple[str, list[object]] = ("OK", [])

    def uid(self, command: str, *args: str) -> tuple[str, list[object]]:
        self.uid_calls.append((command.upper(), *args))
        if command.upper() == "STORE":
            return self.store_response
        return self.fetch_response


class FakeMailBoxForSession:
    """Duck-typed imap-tools MailBox for exercising the real ImapSession."""

    def __init__(self, client: FakeImapClient | None = None) -> None:
        self.client = client or FakeImapClient()
        self.uids_result: list[str] | Exception = []
        self.fetch_calls = 0
        self.moved: list[tuple[str, str]] = []

    def uids(self, criteria: object = "ALL", charset: str = "US-ASCII") -> list[str]:
        if isinstance(self.uids_result, Exception):
            raise self.uids_result
        return list(self.uids_result)

    def fetch(self, criteria: object = "ALL", **kwargs: object) -> list[object]:
        self.fetch_calls += 1
        return []

    def move(self, uid: str, folder: str) -> None:
        self.moved.append((uid, folder))

    def logout(self) -> None:
        pass


def _session(mb: FakeMailBoxForSession) -> ImapSession:
    return ImapSession(cast("MailBox", mb))


def test_imaplib_abort_becomes_fetch_error() -> None:
    """imaplib turns mid-command socket loss into IMAP4.abort, which is NOT
    an OSError - it must still surface as the contractual FetchError."""
    mb = FakeMailBoxForSession()
    mb.uids_result = imaplib.IMAP4.abort("socket error: EOF")
    with pytest.raises(FetchError, match="socket error"):
        _session(mb).list_summaries(since=None, unseen_only=False)


def test_summaries_chunk_uid_sets() -> None:
    """One giant UID list must not become one giant command line."""
    mb = FakeMailBoxForSession()
    session = _session(mb)
    uids = [str(n) for n in range(1, 451)]  # 450 uids -> 3 chunks of <=200

    session.summaries_for_uids(uids)

    structure_calls = [c for c in mb.client.uid_calls if "(BODYSTRUCTURE)" in c]
    assert len(structure_calls) == 3
    assert all(len(call[1].split(",")) <= 200 for call in structure_calls)
    assert mb.fetch_calls == 3


def test_fetch_part_decodes_base64_payload() -> None:
    mb = FakeMailBoxForSession()
    mb.client.fetch_response = ("OK", [(b"1 (UID 5 BODY[2] {10}", b"aGVsbG8=\r\n"), b")"])
    part = MimePart(
        section="2", content_type="application/epub+zip", filename="b.epub", encoding="base64"
    )
    assert _session(mb).fetch_part("5", part) == b"hello"


def test_mark_seen_uses_raw_store_never_expunge() -> None:
    """imap-tools' flag() EXPUNGEs the whole folder after STORE, which would
    destroy other clients' \\Deleted messages - we must go through raw STORE.
    (FakeMailBoxForSession has no flag/expunge methods: calling them would
    AttributeError.)"""
    mb = FakeImapClient()
    box = FakeMailBoxForSession(mb)
    _session(box).mark_seen("42")
    assert mb.uid_calls == [("STORE", "42", "+FLAGS", r"(\Seen)")]


def test_move_requires_server_side_move_capability() -> None:
    """Client-side COPY+delete fallback EXPUNGEs the folder; refuse instead."""
    box = FakeMailBoxForSession(FakeImapClient(capabilities=("IMAP4REV1",)))
    with pytest.raises(FetchError, match="MOVE capability"):
        _session(box).move("42", "done")
    assert box.moved == []


def test_move_with_capability_delegates_to_server_move() -> None:
    box = FakeMailBoxForSession()
    _session(box).move("42", "done")
    assert box.moved == [("42", "done")]
