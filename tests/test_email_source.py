"""Tests for the email source (fake IMAP session, real message parsing)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage as StdEmailMessage
from pathlib import Path

import httpx
import pytest
from imap_tools import MailMessage

from pulpwise.config import Config, Subscription
from pulpwise.models import ExtractionError, FetchError, ItemRef, ItemSkipped
from pulpwise.sinks.readwise import ReadwiseSink
from pulpwise.sources.email import EmailSource
from pulpwise.util.dedup import normalize_url
from pulpwise.util.imap import MessageSummary, MimePart

MAILBOX_URL = "imaps://imap.example.com/Pulpwise"


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
    from pulpwise.sources import REGISTRY, pick_source_for_url

    assert REGISTRY["email"] is EmailSource
    assert pick_source_for_url("imaps://imap.gmail.com/Pulpwise") is EmailSource
    assert EmailSource.is_subscribable("imaps://imap.gmail.com/Pulpwise")
    assert not EmailSource.matches_url("https://example.com/feed")


def test_default_subscription_name() -> None:
    assert EmailSource.default_subscription_name("imaps://imap.gmail.com/Pulpwise") == "pulpwise"
    assert EmailSource.default_subscription_name("imaps://imap.gmail.com") == "mailbox"
    assert EmailSource.default_subscription_name("not a url") == "mailbox"


# --- discovery -------------------------------------------------------------------


def test_discover_skips_ebook_delivery_messages(caplog: pytest.LogCaptureFixture) -> None:
    """Messages carrying ebook attachments are book deliveries - the mainline
    pulpline tool's job, not this fork's. They yield nothing at discovery."""
    delivery = make_summary(
        "10",
        parts=(html_part("1"), epub_part("2", "Book One.epub"), epub_part("3", "Book Two.epub")),
    )
    newsletter = make_summary("11", message_id="<n@x>", subject="Issue 42", parts=(html_part("1"),))
    source = make_source(FakeImapSession([delivery, newsletter]))

    with caplog.at_level(logging.INFO, logger="pulpwise.sources.email"):
        refs = list(source.discover(MAILBOX_URL))

    assert [r.url for r in refs] == ["mid:n@x"]
    assert "skipping ebook delivery message" in caplog.text


def test_discover_yields_whole_message_ref_for_newsletter() -> None:
    summary = make_summary("11", subject="Issue 42", parts=(html_part("1"),))
    source = make_source(FakeImapSession([summary]))

    refs = list(source.discover(MAILBOX_URL))

    assert [r.url for r in refs] == ["mid:msg-11@example.com"]
    assert refs[0].title == "Issue 42"
    # last_known_total means all-time population; a since-window count would
    # render nonsense ingested/total ratios in the TUI, so it stays unset.
    assert source.last_known_total is None


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
    assert ref_a.url.startswith("mid:pulpwise-")


def test_discover_backwards_pages_newest_first_and_skips_deliveries() -> None:
    summaries = [make_summary(str(uid), subject=f"s{uid}") for uid in (1, 2, 3)]
    summaries.append(make_summary("4", parts=(epub_part("2"),)))  # ebook delivery
    source = make_source(FakeImapSession(summaries))

    refs = list(source.discover_backwards(MAILBOX_URL))

    assert [r.url for r in refs] == [
        "mid:msg-3@example.com",
        "mid:msg-2@example.com",
        "mid:msg-1@example.com",
    ]


# --- fetch: newsletters -----------------------------------------------------------


def test_fetch_newsletter_returns_gated_cleaned_article() -> None:
    summary = make_summary("30", subject="Issue 42", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"30": newsletter_eml()})
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    article = source.fetch(ref)

    assert article.title == "Issue 42"  # subject only, no " - Sender" suffix
    assert "Newsletter body text." in article.body_html
    assert article.canonical_url == ref.url  # no permalink -> mid: identity
    assert article.author == "Sender Name"
    assert article.publisher == "newsletter.example.com"
    assert article.content_gated is True


def test_newsletter_without_permalink_submits_html_under_synthetic_url() -> None:
    summary = make_summary("31", subject="Issue 42", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"31": newsletter_eml()})
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    submission = source.submission_for_article(source.fetch(ref))

    assert submission.kind == "html"
    assert submission.url.startswith("https://pulpwise.invalid/")
    assert submission.html is not None
    assert "Newsletter body text." in submission.html
    assert submission.title == "Issue 42"
    assert submission.author == "Sender Name"


def test_newsletter_with_permalink_submits_html_under_permalink() -> None:
    """The web permalink becomes the document identity in Readwise while the
    content still comes from the email body."""
    permalink = "https://news.example.com/p/issue-44"
    html = f'<html><body><p>Body text here.</p><a href="{permalink}">View online</a></body></html>'
    summary = make_summary("36", subject="Issue 44", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"36": newsletter_eml(html=html)})
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    article = source.fetch(ref)
    submission = source.submission_for_article(article)

    assert article.canonical_url == permalink
    assert article.content_gated is True
    assert submission.kind == "html"
    assert submission.url == permalink
    assert submission.html is not None
    assert "Body text here." in submission.html


def test_prefer_web_with_permalink_becomes_bare_url_save() -> None:
    """prefer_web means: let Readwise fetch and parse the web version itself.
    No trafilatura fetch happens client-side (no HTTP client is even set)."""
    permalink = "https://news.example.com/p/issue-42"
    html = (
        f'<html><body><p>Short body.</p><a href="{permalink}">View this post on the web</a>'
        "</body></html>"
    )
    summary = make_summary("34", subject="Issue 42", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"34": newsletter_eml(html=html)})
    source = make_source(session, options={"prefer_web": 1})

    (ref,) = list(source.discover(MAILBOX_URL))
    article = source.fetch(ref)
    submission = source.submission_for_article(article)

    assert article.canonical_url == permalink
    assert article.content_gated is False
    assert submission.kind == "url"
    assert submission.url == permalink
    assert submission.html is None


def test_prefer_web_without_permalink_falls_back_to_gated_email_body() -> None:
    summary = make_summary("35", subject="Issue 43", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"35": newsletter_eml()})
    source = make_source(session, options={"prefer_web": 1})

    (ref,) = list(source.discover(MAILBOX_URL))
    article = source.fetch(ref)
    submission = source.submission_for_article(article)

    assert article.content_gated is True
    assert submission.kind == "html"
    assert submission.url.startswith("https://pulpwise.invalid/")


def test_fetch_text_only_email_raises_extraction_error() -> None:
    summary = make_summary("33", parts=(MimePart("1", "text/plain", None, "7bit"),))
    session = FakeImapSession([summary], messages={"33": text_only_eml()})
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    with pytest.raises(ExtractionError, match="no HTML body"):
        source.fetch(ref)


def test_structure_unknown_ebook_delivery_skipped_at_fetch() -> None:
    """When BODYSTRUCTURE was unparsable, the delivery skip can't happen at
    discovery; fetch applies it once the whole message is downloaded."""
    summary = make_summary("23", structure_known=False)
    session = FakeImapSession([summary], messages={"23": attachment_eml()})
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    # ItemSkipped, not an error: the pipeline counts it as a skip and still
    # acks, so the message isn't re-downloaded as a failure every sync.
    with pytest.raises(ItemSkipped, match="ebook delivery"):
        source.fetch(ref)


def test_structure_unknown_newsletter_still_fetches() -> None:
    summary = make_summary("24", subject="Issue 9", structure_known=False)
    session = FakeImapSession([summary], messages={"24": newsletter_eml(subject="Issue 9")})
    source = make_source(session)

    (ref,) = list(source.discover(MAILBOX_URL))
    article = source.fetch(ref)

    assert article.title == "Issue 9"
    assert "Newsletter body text." in article.body_html


# --- fetch without discover (ledger-resolved refs) ---------------------------------


def test_fetch_resolves_ledger_url_without_prior_discover() -> None:
    summary = make_summary("40", subject="Issue 40", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"40": newsletter_eml(subject="Issue 40")})
    source = make_source(session)

    article = source.fetch(ItemRef(url="mid:msg-40@example.com"))

    assert article.title == "Issue 40"
    assert article.content_gated is True


def test_fetch_unknown_message_raises_fetch_error() -> None:
    source = make_source(FakeImapSession([]))
    with pytest.raises(FetchError, match="not found"):
        source.fetch(ItemRef(url="mid:gone@example.com"))


def test_fetch_http_url_is_not_an_email_item() -> None:
    source = make_source(FakeImapSession([]))
    with pytest.raises(FetchError, match="not an email item URL"):
        source.fetch(ItemRef(url="https://news.example.com/p/issue-9"))


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


def test_ack_move_policy_moves_once() -> None:
    summary = make_summary("53", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"53": newsletter_eml()})
    source = make_source(session, options={"mark_read": "move", "move_to": "Pulpwise/done"})

    (ref,) = list(source.discover(MAILBOX_URL))
    assert session.moved == []
    source.ack(ref)
    assert session.moved == [("53", "Pulpwise/done")]
    source.ack(ref)  # idempotent
    assert session.moved == [("53", "Pulpwise/done")]


def test_ack_move_heals_on_next_sync_via_skip_acks() -> None:
    """Crash after record-but-before-move: the next sync's skip-acks move it."""
    summary = make_summary("54", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"54": newsletter_eml()})
    options: dict[str, str | int] = {"mark_read": "move", "move_to": "done"}

    # Sync 1: item recorded, but the process dies before ack runs.
    source1 = make_source(session, options=options)
    list(source1.discover(MAILBOX_URL))
    assert session.moved == []

    # Sync 2: the item is a ledger-skip, but skips are acked too.
    source2 = make_source(session, options=options)
    (ref2,) = list(source2.discover(MAILBOX_URL))
    source2.ack(ref2)
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
    monkeypatch.setenv("PULPWISE_EMAIL_PASSWORD", "env-secret")
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


def _mock_sink() -> tuple[ReadwiseSink, list[dict[str, object]]]:
    """Real ReadwiseSink over a MockTransport that records save payloads."""
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payloads.append(body)
        doc_id = f"doc-{len(payloads)}"
        return httpx.Response(
            201, json={"id": doc_id, "url": f"https://read.readwise.io/read/{doc_id}"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ReadwiseSink("test-token", client=client, sleep=lambda _s: None), payloads


def test_pipeline_sync_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real pipeline drives EmailSource through from_config: discover ->
    fetch -> submission -> sink push -> record -> ack -> close."""
    from pulpwise import pipeline
    from pulpwise.sources import email as email_module

    summary = make_summary("60", subject="Issue 1", parts=(html_part("1"),))
    session = FakeImapSession([summary], messages={"60": newsletter_eml(subject="Issue 1")})

    class StubImapSession:
        @staticmethod
        def open(location: object, username: str, password: str) -> FakeImapSession:
            assert (username, password) == ("u@example.com", "pw")
            return session

    monkeypatch.setattr(email_module, "ImapSession", StubImapSession)

    cfg = Config(
        auth={"email": {"username": "u@example.com", "password": "pw"}},
        subscriptions=(Subscription(name="mailbox", source="email", url=MAILBOX_URL),),
    )
    sink, payloads = _mock_sink()
    total = pipeline.sync(config=cfg, sink=sink)

    assert total.total_new == 1
    assert total.total_errors == 0
    (payload,) = payloads
    assert str(payload["url"]).startswith("https://pulpwise.invalid/")  # no permalink
    assert "Newsletter body text." in str(payload["html"])
    assert payload["title"] == "Issue 1"
    assert payload["author"] == "Sender Name"
    assert session.seen_uids == ["60"]  # acked after record
    assert session.closed  # context manager closed the session

    # Second sync: ledger-skip, nothing new pushed, still zero errors.
    second = pipeline.sync(config=cfg, sink=sink)
    assert second.total_new == 0
    assert second.total_skipped == 1
    assert len(payloads) == 1
