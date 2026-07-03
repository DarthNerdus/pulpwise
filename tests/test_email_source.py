"""Tests for the email source (fake IMAP session, real message parsing)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage as StdEmailMessage
from pathlib import Path

import httpx
import pytest
from imap_tools import MailMessage

from pulpline.config import Config, Subscription
from pulpline.models import ExtractionError, FetchError, ItemRef
from pulpline.sources.email import EmailSource
from pulpline.util.dedup import normalize_url
from pulpline.util.imap import MessageSummary, MimePart

MAILBOX_URL = "imaps://imap.example.com/Pulpline"
ClientFactory = Callable[..., httpx.Client]


# --- fixture builders ---------------------------------------------------------


def make_summary(
    uid: str,
    message_id: str | None = None,
    subject: str = "Subject",
    from_name: str | None = "Sender Name",
    from_addr: str | None = "sender@newsletter.example.com",
    seen: bool = False,
    parts: tuple[MimePart, ...] = (),
    structure_known: bool = True,
) -> MessageSummary:
    return MessageSummary(
        uid=uid,
        message_id=message_id if message_id is not None else f"<msg-{uid}@example.com>",
        subject=subject,
        from_name=from_name,
        from_addr=from_addr,
        date=datetime(2026, 7, 2, 10, 0, tzinfo=UTC),
        seen=seen,
        parts=parts,
        structure_known=structure_known,
    )


def epub_part(section: str = "2", filename: str = "Some Book.epub") -> MimePart:
    return MimePart(
        section=section,
        content_type="application/epub+zip",
        filename=filename,
        encoding="base64",
    )


def html_part(section: str = "1") -> MimePart:
    return MimePart(
        section=section, content_type="text/html", filename=None, encoding="quoted-printable"
    )


def newsletter_eml(
    subject: str = "Issue 42",
    html: str = "<html><body><p>Newsletter body text.</p></body></html>",
) -> bytes:
    msg = StdEmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Sender Name <sender@newsletter.example.com>"
    msg["Date"] = "Thu, 02 Jul 2026 10:00:00 +0000"
    msg["Message-ID"] = "<msg-1@example.com>"
    msg.set_content("plain text fallback")
    msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


def attachment_eml(filename: str = "Some Book.epub", payload: bytes = b"EPUB-BYTES") -> bytes:
    msg = StdEmailMessage()
    msg["Subject"] = "Your book"
    msg["From"] = "Calibre <calibre@home.example.com>"
    msg["Date"] = "Thu, 02 Jul 2026 10:00:00 +0000"
    msg["Message-ID"] = "<msg-1@example.com>"
    msg.set_content("see attachment")
    msg.add_attachment(payload, maintype="application", subtype="epub+zip", filename=filename)
    return msg.as_bytes()


def text_only_eml() -> bytes:
    msg = StdEmailMessage()
    msg["Subject"] = "Plain"
    msg["From"] = "someone@example.com"
    msg["Message-ID"] = "<msg-1@example.com>"
    msg.set_content("only plain text here")
    return msg.as_bytes()


class FakeImapSession:
    """In-memory ImapSessionLike over prepared summaries/messages."""

    def __init__(
        self,
        summaries: list[MessageSummary] | None = None,
        messages: dict[str, bytes] | None = None,
        parts: dict[tuple[str, str], bytes] | None = None,
    ) -> None:
        self.summaries = summaries or []
        self.messages = messages or {}
        self.parts = parts or {}
        self.seen_uids: list[str] = []
        self.moved: list[tuple[str, str]] = []
        self.closed = False
        self.last_search: tuple[date | None, bool] | None = None

    def list_summaries(self, since: date | None, unseen_only: bool) -> list[MessageSummary]:
        self.last_search = (since, unseen_only)
        return list(self.summaries)

    def list_all_uids_newest_first(self) -> list[str]:
        return sorted((s.uid for s in self.summaries), key=int, reverse=True)

    def summaries_for_uids(self, uids: list[str]) -> list[MessageSummary]:
        return [s for s in self.summaries if s.uid in uids]

    def fetch_part(self, uid: str, part: MimePart) -> bytes:
        return self.parts[(uid, part.section)]

    def fetch_message(self, uid: str) -> MailMessage:
        message: MailMessage = MailMessage.from_bytes(self.messages[uid])
        return message

    def find_uid_by_message_id(self, message_id: str) -> str | None:
        for s in self.summaries:
            if s.message_id == message_id:
                return s.uid
        return None

    def mark_seen(self, uid: str) -> None:
        self.seen_uids.append(uid)

    def move(self, uid: str, folder: str) -> None:
        self.moved.append((uid, folder))

    def close(self) -> None:
        self.closed = True


def make_source(
    session: FakeImapSession,
    options: dict[str, str | int] | None = None,
    client: httpx.Client | None = None,
) -> EmailSource:
    return EmailSource(
        client=client,
        username="user@example.com",
        password="app-password",
        subscription_url=MAILBOX_URL,
        options=options,
        session_factory=lambda loc, user, pwd: session,
    )


# --- URL claiming / registry ---------------------------------------------------


def test_registry_and_url_claiming() -> None:
    from pulpline.sources import REGISTRY, pick_source_for_url

    assert REGISTRY["email"] is EmailSource
    assert pick_source_for_url("imaps://imap.gmail.com/Pulpline") is EmailSource
    assert EmailSource.is_subscribable("imaps://imap.gmail.com/Pulpline")
    assert not EmailSource.matches_url("https://example.com/feed")


def test_default_subscription_name() -> None:
    assert EmailSource.default_subscription_name("imaps://imap.gmail.com/Pulpline") == "pulpline"
    assert EmailSource.default_subscription_name("imaps://imap.gmail.com") == "mailbox"
    assert EmailSource.default_subscription_name("not a url") == "mailbox"


# --- discovery -------------------------------------------------------------------


def test_discover_yields_one_ref_per_ebook_attachment() -> None:
    summary = make_summary(
        "10",
        parts=(html_part("1"), epub_part("2", "Book One.epub"), epub_part("3", "Book Two.epub")),
    )
    source = make_source(FakeImapSession([summary]))

    refs = list(source.discover(MAILBOX_URL))

    assert [r.url for r in refs] == [
        "mid:msg-10@example.com/att/2",
        "mid:msg-10@example.com/att/3",
    ]
    assert refs[0].title == "Book One"
    # last_known_total means all-time population; a since-window count would
    # render nonsense ingested/total ratios in the TUI, so it stays unset.
    assert source.last_known_total is None


def test_discover_yields_whole_message_ref_for_newsletter() -> None:
    summary = make_summary("11", subject="Issue 42", parts=(html_part("1"),))
    source = make_source(FakeImapSession([summary]))

    refs = list(source.discover(MAILBOX_URL))

    assert [r.url for r in refs] == ["mid:msg-11@example.com"]
    assert refs[0].title == "Issue 42"


def test_discover_degrades_to_whole_message_when_structure_unknown() -> None:
    summary = make_summary("12", parts=(), structure_known=False)
    source = make_source(FakeImapSession([summary]))

    refs = list(source.discover(MAILBOX_URL))

    assert [r.url for r in refs] == ["mid:msg-12@example.com"]


def test_discover_passes_window_and_unseen_options_to_session() -> None:
    session = FakeImapSession([])
    source = make_source(session, options={"since_days": 7, "unseen_only": 1})
    list(source.discover(MAILBOX_URL))

    assert session.last_search is not None
    since, unseen = session.last_search
    assert unseen is True
    assert since is not None
    expected = (datetime.now(tz=UTC) - timedelta(days=7)).date()
    assert abs((since - expected).days) <= 1  # catches sign/unit errors

    session2 = FakeImapSession([])
    source2 = make_source(session2, options={"since_days": 0})
    list(source2.discover(MAILBOX_URL))
    assert session2.last_search == (None, False)


def test_mid_urls_survive_normalize_url() -> None:
    summary = make_summary("13", message_id="<CAJx+9yZ/AbC=dEf@Mail.GMail.com>")
    source = make_source(FakeImapSession([summary]))
    (ref,) = list(source.discover(MAILBOX_URL))

    assert normalize_url(ref.url) == ref.url  # case + symbols preserved


def test_missing_message_id_gets_stable_synthetic_key() -> None:
    a = make_summary("14", message_id="")
    b = make_summary("99", message_id="")  # same message re-listed under a new uid
    source_a = make_source(FakeImapSession([a]))
    source_b = make_source(FakeImapSession([b]))

    (ref_a,) = list(source_a.discover(MAILBOX_URL))
    (ref_b,) = list(source_b.discover(MAILBOX_URL))

    assert ref_a.url == ref_b.url  # key is uid-independent
    assert ref_a.url.startswith("mid:pulpline-")


def test_discover_backwards_pages_newest_first() -> None:
    summaries = [make_summary(str(uid), subject=f"s{uid}") for uid in (1, 2, 3)]
    source = make_source(FakeImapSession(summaries))

    refs = list(source.discover_backwards(MAILBOX_URL))

    assert [r.url for r in refs] == [
        "mid:msg-3@example.com",
        "mid:msg-2@example.com",
        "mid:msg-1@example.com",
    ]


# --- fetch: attachments ----------------------------------------------------------


def test_fetch_attachment_returns_raw_bytes_via_render() -> None:
    summary = make_summary("20", parts=(html_part("1"), epub_part("2")))
    session = FakeImapSession([summary], parts={("20", "2"): b"EPUB-BYTES"})
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    article = source.fetch(ref)

    assert article.title == "Some Book"
    assert article.body_html == ""
    assert article.publisher == "newsletter.example.com"
    assert source.extension == "epub"
    assert source.render(article) == b"EPUB-BYTES"


def test_fetch_attachment_extension_is_case_insensitive() -> None:
    summary = make_summary("21", parts=(epub_part("2", "SHOUTY.EPUB"),))
    session = FakeImapSession([summary], parts={("21", "2"): b"X"})
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    source.fetch(ref)

    assert source.extension == "epub"


def test_fetch_pdf_attachment_sets_pdf_extension() -> None:
    part = MimePart(
        section="2", content_type="application/pdf", filename="paper.pdf", encoding="base64"
    )
    summary = make_summary("22", parts=(part,))
    session = FakeImapSession([summary], parts={("22", "2"): b"%PDF"})
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    source.fetch(ref)

    assert source.extension == "pdf"


def test_structure_unknown_message_with_epub_saves_attachment() -> None:
    summary = make_summary("23", structure_known=False)
    session = FakeImapSession(
        [summary], messages={"23": attachment_eml("Recovered.epub", b"RECOVERED")}
    )
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    article = source.fetch(ref)

    assert article.title == "Recovered"
    assert source.extension == "epub"
    assert source.render(article) == b"RECOVERED"


# --- fetch: newsletters -----------------------------------------------------------


def test_fetch_newsletter_renders_cleaned_html() -> None:
    summary = make_summary("30", subject="Issue 42", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"30": newsletter_eml()})
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    article = source.fetch(ref)

    assert article.title == "Issue 42 - Sender Name"
    assert "Newsletter body text." in article.body_html
    assert article.canonical_url == ref.url
    assert article.author == "Sender Name"
    assert article.publisher == "newsletter.example.com"
    assert source.extension == "epub"

    epub_bytes = source.render(article)
    assert epub_bytes[:2] == b"PK"  # rendered through the real EPUB renderer


def test_extension_resets_to_epub_after_attachment_item() -> None:
    att = make_summary("31", message_id="<a@x>", parts=(epub_part("2", "b.pdf"),))
    news = make_summary("32", message_id="<b@x>", subject="Issue", parts=(html_part("1"),))
    session = FakeImapSession(
        [att, news], messages={"32": newsletter_eml()}, parts={("31", "2"): b"%PDF"}
    )
    source = make_source(session)

    ref_att, ref_news = list(source.discover(MAILBOX_URL))
    source.fetch(ref_att)
    assert source.extension == "pdf"
    source.fetch(ref_news)
    assert source.extension == "epub"


def test_fetch_text_only_email_raises_extraction_error() -> None:
    summary = make_summary("33", parts=(MimePart("1", "text/plain", None, "7bit"),))
    session = FakeImapSession([summary], messages={"33": text_only_eml()})
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    with pytest.raises(ExtractionError, match="no HTML body"):
        source.fetch(ref)


def test_prefer_web_follows_permalink(mock_client_factory: ClientFactory, sample_html: str) -> None:
    permalink = "https://news.example.com/p/issue-42"
    html = (
        f'<html><body><p>Short body.</p><a href="{permalink}">View this post on the web</a>'
        "</body></html>"
    )
    summary = make_summary("34", subject="Issue 42", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"34": newsletter_eml(html=html)})
    source = make_source(
        session, options={"prefer_web": 1}, client=mock_client_factory({permalink: sample_html})
    )

    (ref,) = list(source.discover(MAILBOX_URL))
    article = source.fetch(ref)

    assert article.canonical_url != ref.url  # came from the web fetch
    assert "example" in article.canonical_url


def test_prefer_web_falls_back_to_email_body_on_fetch_failure(
    mock_client_factory: ClientFactory,
) -> None:
    permalink = "https://news.example.com/p/issue-43"
    html = f'<html><body><p>Email body wins.</p><a href="{permalink}">View online</a></body></html>'
    summary = make_summary("35", subject="Issue 43", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"35": newsletter_eml(html=html)})
    source = make_source(session, options={"prefer_web": 1}, client=mock_client_factory({}))

    (ref,) = list(source.discover(MAILBOX_URL))
    article = source.fetch(ref)

    assert "Email body wins." in article.body_html


def test_newsletter_canonical_url_uses_permalink_without_prefer_web() -> None:
    permalink = "https://news.example.com/p/issue-44"
    html = f'<html><body><p>Body text here.</p><a href="{permalink}">View online</a></body></html>'
    summary = make_summary("36", subject="Issue 44", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"36": newsletter_eml(html=html)})
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    article = source.fetch(ref)

    assert article.canonical_url == permalink
    assert "Body text here." in article.body_html


# --- fetch without discover (rebuild path) ----------------------------------------


def test_fetch_resolves_ledger_url_without_prior_discover() -> None:
    summary = make_summary("40", parts=(html_part("1"), epub_part("2")))
    session = FakeImapSession([summary], parts={("40", "2"): b"REBUILT"})
    source = make_source(session)

    article = source.fetch(ItemRef(url="mid:msg-40@example.com/att/2"))

    assert source.render(article) == b"REBUILT"


def test_fetch_unknown_message_raises_fetch_error() -> None:
    source = make_source(FakeImapSession([]))
    with pytest.raises(FetchError, match="not found"):
        source.fetch(ItemRef(url="mid:gone@example.com"))


def test_fetch_http_url_rebuilds_from_web(
    mock_client_factory: ClientFactory, sample_html: str
) -> None:
    """Rebuild passes the ledger's canonical_url; for newsletters that had a
    web permalink that's an http URL, which must re-render from the web."""
    permalink = "https://news.example.com/p/issue-9"
    source = make_source(FakeImapSession([]), client=mock_client_factory({permalink: sample_html}))

    article = source.fetch(ItemRef(url=permalink))

    assert article.body_html
    assert source.extension == "epub"


# --- ack / mark_read policies ------------------------------------------------------


def test_ack_marks_seen_only_after_processing() -> None:
    summary = make_summary("50", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"50": newsletter_eml()})
    source = make_source(session)  # default mark_read="seen"

    (ref,) = list(source.discover(MAILBOX_URL))
    assert session.seen_uids == []  # discover/fetch never touch flags
    source.fetch(ref)
    assert session.seen_uids == []
    source.ack(ref)
    assert session.seen_uids == ["50"]
    source.ack(ref)  # idempotent
    assert session.seen_uids == ["50"]


def test_ack_skips_already_seen_messages() -> None:
    summary = make_summary("51", seen=True, parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"51": newsletter_eml()})
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    source.ack(ref)
    assert session.seen_uids == []


def test_ack_none_policy_touches_nothing() -> None:
    summary = make_summary("52", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"52": newsletter_eml()})
    source = make_source(session, options={"mark_read": "none"})

    (ref,) = list(source.discover(MAILBOX_URL))
    source.ack(ref)
    assert session.seen_uids == []
    assert session.moved == []


def test_ack_seen_waits_for_all_attachments_of_a_message() -> None:
    """With unseen_only, marking seen after the FIRST attachment would drop
    the message from discovery with its siblings never ingested - so "seen"
    must gate on all-items-acked exactly like "move"."""
    summary = make_summary("56", parts=(epub_part("2", "a.epub"), epub_part("3", "b.epub")))
    session = FakeImapSession([summary], parts={("56", "2"): b"A", ("56", "3"): b"B"})
    source = make_source(session)  # default mark_read="seen"

    ref_a, ref_b = list(source.discover(MAILBOX_URL))
    source.ack(ref_a)
    assert session.seen_uids == []  # sibling attachment still outstanding
    source.ack(ref_b)
    assert session.seen_uids == ["56"]


def test_ack_move_waits_for_all_attachments_of_a_message() -> None:
    summary = make_summary("53", parts=(epub_part("2", "a.epub"), epub_part("3", "b.epub")))
    session = FakeImapSession([summary], parts={("53", "2"): b"A", ("53", "3"): b"B"})
    source = make_source(session, options={"mark_read": "move", "move_to": "Pulpline/done"})

    ref_a, ref_b = list(source.discover(MAILBOX_URL))
    source.ack(ref_a)
    assert session.moved == []  # second attachment still pending
    source.ack(ref_b)
    assert session.moved == [("53", "Pulpline/done")]
    source.ack(ref_b)  # idempotent
    assert session.moved == [("53", "Pulpline/done")]


def test_ack_move_heals_on_next_sync_via_skip_acks() -> None:
    """Crash after record-but-before-move: the next sync's skip-acks move it."""
    summary = make_summary("54", parts=(epub_part("2", "a.epub"), epub_part("3", "b.epub")))
    session = FakeImapSession([summary], parts={("54", "2"): b"A", ("54", "3"): b"B"})
    options: dict[str, str | int] = {"mark_read": "move", "move_to": "done"}

    # Sync 1: only the first attachment gets processed (simulated crash after).
    source1 = make_source(session, options=options)
    ref_a, _ = list(source1.discover(MAILBOX_URL))
    source1.ack(ref_a)
    assert session.moved == []

    # Sync 2: first item is a ledger-skip (still acked), second processes.
    source2 = make_source(session, options=options)
    ref_a2, ref_b2 = list(source2.discover(MAILBOX_URL))
    source2.ack(ref_a2)
    source2.ack(ref_b2)
    assert session.moved == [("54", "done")]


def test_invalid_mark_read_or_missing_move_to_fails_discover() -> None:
    source = make_source(FakeImapSession([]), options={"mark_read": "delete"})
    with pytest.raises(FetchError, match="invalid mark_read"):
        list(source.discover(MAILBOX_URL))

    source2 = make_source(FakeImapSession([]), options={"mark_read": "move"})
    with pytest.raises(FetchError, match="move_to"):
        list(source2.discover(MAILBOX_URL))


# --- config / credentials -----------------------------------------------------------


def _config(auth: dict[str, dict[str, str | list[str]]]) -> Config:
    return Config(
        auth=auth,
        subscriptions=(Subscription(name="mailbox", source="email", url=MAILBOX_URL),),
    )


def test_from_config_reads_auth_and_password_file(tmp_path: Path) -> None:
    secret = tmp_path / "email_password"
    secret.write_text("s3cret\n")
    cfg = _config({"email": {"username": "user@example.com", "password_path": str(secret)}})
    captured: list[tuple[str, str]] = []

    source = EmailSource.from_config(cfg, subscription=cfg.subscriptions[0])

    def factory(loc: object, user: str, pwd: str) -> FakeImapSession:
        captured.append((user, pwd))
        return FakeImapSession([])

    source._session_factory = factory

    list(source.discover(MAILBOX_URL))
    assert captured == [("user@example.com", "s3cret")]


def test_env_password_wins_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "email_password"
    secret.write_text("file-secret")
    monkeypatch.setenv("PULPLINE_EMAIL_PASSWORD", "env-secret")
    cfg = _config({"email": {"username": "user@example.com", "password_path": str(secret)}})
    captured: list[str] = []

    source = EmailSource.from_config(cfg, subscription=cfg.subscriptions[0])

    def factory(loc: object, user: str, pwd: str) -> FakeImapSession:
        captured.append(pwd)
        return FakeImapSession([])

    source._session_factory = factory

    list(source.discover(MAILBOX_URL))
    assert captured == ["env-secret"]


def test_missing_credentials_raise_fetch_error_at_discover() -> None:
    cfg = _config({})
    source = EmailSource.from_config(cfg, subscription=cfg.subscriptions[0])
    with pytest.raises(FetchError, match="not configured"):
        list(source.discover(MAILBOX_URL))


def test_one_shot_construction_rejected_at_discover() -> None:
    """`pulp add --once` builds sources without a subscription; mailboxes
    have no one-shot form and must fail with a friendly FetchError."""
    source = EmailSource.from_config(_config({}))  # no subscription passed
    with pytest.raises(FetchError, match="not one-shots"):
        list(source.discover(MAILBOX_URL))


def test_unreadable_password_file_error_names_path_not_secret(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    cfg = _config({"email": {"username": "user@example.com", "password_path": str(missing)}})
    source = EmailSource.from_config(cfg, subscription=cfg.subscriptions[0])
    with pytest.raises(FetchError, match="nope"):
        list(source.discover(MAILBOX_URL))


def test_close_closes_session() -> None:
    session = FakeImapSession([])
    source = make_source(session)
    list(source.discover(MAILBOX_URL))
    source.close()
    assert session.closed


# --- full pipeline integration --------------------------------------------------


def test_pipeline_sync_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real pipeline drives EmailSource through from_config: discover ->
    fetch -> render -> sink -> record -> ack -> close."""
    from pulpline import pipeline
    from pulpline.sources import email as email_module

    summary = make_summary("60", subject="Issue 1", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"60": newsletter_eml(subject="Issue 1")})

    class StubImapSession:
        @staticmethod
        def open(location: object, username: str, password: str) -> FakeImapSession:
            assert (username, password) == ("u@example.com", "pw")
            return session

    monkeypatch.setattr(email_module, "ImapSession", StubImapSession)

    out = tmp_path / "out"
    cfg = Config(
        auth={"email": {"username": "u@example.com", "password": "pw"}},
        subscriptions=(
            Subscription(name="mailbox", source="email", url=MAILBOX_URL, output_dir=str(out)),
        ),
    )
    total = pipeline.sync(config=cfg)

    assert total.total_new == 1
    assert total.total_errors == 0
    files = list(out.iterdir())
    assert len(files) == 1
    assert files[0].name == "Issue 1 - Sender Name.epub"
    assert files[0].read_bytes()[:2] == b"PK"
    assert session.seen_uids == ["60"]  # acked after record
    assert session.closed  # context manager closed the session

    # Second sync: ledger-skip, no duplicate file, still zero errors.
    second = pipeline.sync(config=cfg)
    assert second.total_new == 0
    assert second.total_skipped == 1
    assert len(list(out.iterdir())) == 1
