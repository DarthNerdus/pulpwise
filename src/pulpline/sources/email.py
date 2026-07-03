"""Email source: one IMAP mailbox as a feed, dispatched per message.

A dedicated mailbox receives two kinds of mail, interleaved: emails carrying
ebook attachments (Calibre share, send-to-device flows) and HTML newsletters.
The fork between them is a property of each *message*, not of the
subscription, so this is a single source with per-message dispatch:

- a message with ebook attachments yields one item per attachment, saved
  verbatim (the AnnaSource render()/extension pattern); its HTML body is
  treated as a delivery envelope and never rendered;
- anything else must have an HTML body, which is cleaned
  (`util.email_clean`) and rendered to EPUB.

Idempotency lives entirely in pulpline's ledger, keyed on synthetic
`mid:<Message-ID>` URLs (plus `/att/<section>` per attachment part).
Message-ID survives UIDVALIDITY resets, Gmail's per-folder UIDs, and other
clients toggling flags - so IMAP \\Seen is never load-bearing. Discovery
lists a SINCE window rather than UNSEEN, and the `mark_read` policy is a
cosmetic courtesy applied through `ack()` only after the pipeline has
recorded the item.
"""

from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import httpx

from pulpline.models import ExtractionError, FetchError, ItemRef, RawArticle
from pulpline.sources.base import Source
from pulpline.util.email_clean import clean_email_html, find_web_permalink
from pulpline.util.extract import fetch_article
from pulpline.util.imap import (
    ImapSession,
    ImapSessionLike,
    MailboxLocation,
    MessageSummary,
    MimePart,
    parse_mailbox_url,
)
from pulpline.util.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from imap_tools import MailAttachment, MailMessage

    from pulpline.config import Config, Subscription

    SessionFactory = Callable[..., ImapSessionLike]

_log = get_logger("sources.email")

_ENV_PASSWORD = "PULPLINE_EMAIL_PASSWORD"
_DEFAULT_SINCE_DAYS = 60
_BACKFILL_PAGE_SIZE = 25  # mirrors the page-size assumption in pipeline.backfill

_MARK_READ_POLICIES = frozenset({"none", "seen", "move"})

# What counts as "an ebook arrived by mail". Filename extension is checked
# case-insensitively (the KOReader plugin famously missed `.EPUB`);
# content-type catches attachments with mangled or missing filenames.
_EBOOK_EXTENSIONS = frozenset({"epub", "pdf", "mobi", "azw", "azw3", "fb2", "cbz", "cbr", "djvu"})
_EBOOK_CONTENT_TYPES = {
    "application/epub+zip": "epub",
    "application/pdf": "pdf",
    "application/x-mobipocket-ebook": "mobi",
    "application/vnd.amazon.ebook": "azw",
    "application/x-cbz": "cbz",
    "application/x-cbr": "cbr",
    "application/x-fictionbook+xml": "fb2",
    "image/vnd.djvu": "djvu",
}

# Characters allowed verbatim inside the mid: URL path. Everything else is
# percent-encoded ('%' itself is never safe), keeping distinct Message-IDs
# distinct after `normalize_url` (which lowercases only scheme + netloc, and
# mid: URLs have no netloc).
_MID_SAFE = "@!$&'()*+,;=:._~-"


class EmailSource(Source):
    name: ClassVar[str] = "email"

    def __init__(
        self,
        client: httpx.Client | None = None,
        username: str | None = None,
        password: str | None = None,
        password_path: str | None = None,
        subscription_url: str | None = None,
        options: dict[str, str | int] | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        super().__init__(client=client)
        self._username = username
        self._password = password
        self._password_path = password_path
        self._subscription_url = subscription_url
        self._options = dict(options or {})
        self._session_factory: SessionFactory = (
            session_factory if session_factory is not None else ImapSession.open
        )
        self._session: ImapSessionLike | None = None
        self._session_url: str | None = None

        # Same instance-shadows-ClassVar pattern as AnnaSource: the pipeline
        # reads `source.extension` at sink time, and we only know the real
        # extension per item. Safe because the pipeline is strictly
        # sequential per item.
        self._extension = "epub"

        # discover() -> fetch()/ack() bridges, keyed by ref.url (the
        # RSSSource._entries pattern).
        self._by_url: dict[str, _RefEntry] = {}
        self._payloads: dict[str, bytes] = {}
        # mark_read="move" bookkeeping: a message moves only when every one
        # of its discovered items has been acked (recorded or skipped), so a
        # partially-failed multi-attachment message stays in the folder for
        # the next sync.
        self._pending_acks: dict[str, set[str]] = {}
        self._seen_done: set[str] = set()
        self._moved: set[str] = set()

    @property
    def extension(self) -> str:
        return self._extension

    @extension.setter
    def extension(self, value: str) -> None:
        self._extension = value

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        client: httpx.Client | None = None,
        subscription: Subscription | None = None,
    ) -> EmailSource:
        auth = cfg.auth_for("email")
        username = auth.get("username")
        password = auth.get("password")
        password_path = auth.get("password_path")
        return cls(
            client=client,
            username=username if isinstance(username, str) else None,
            password=password if isinstance(password, str) else None,
            password_path=password_path if isinstance(password_path, str) else None,
            subscription_url=subscription.url if subscription is not None else None,
            options=dict(subscription.options) if subscription is not None else None,
        )

    @classmethod
    def matches_url(cls, url: str) -> bool:
        return urllib.parse.urlsplit(url.strip()).scheme.lower() in {"imap", "imaps"}

    @classmethod
    def is_subscribable(cls, url: str) -> bool:
        # A mailbox is always a recurring feed; there is no one-shot form.
        return cls.matches_url(url)

    @classmethod
    def default_subscription_name(cls, url: str) -> str:
        try:
            folder = parse_mailbox_url(url).folder
        except ValueError:
            return "mailbox"
        if folder.upper() == "INBOX":
            return "mailbox"
        slug = re.sub(r"[^a-z0-9]+", "-", folder.lower()).strip("-")
        return slug or "mailbox"

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        super().close()

    # --- discovery -----------------------------------------------------------

    def discover(self, target_url: str) -> Iterable[ItemRef]:
        if self._subscription_url is None:
            # Only the one-shot path constructs this source without a
            # subscription (`pulp add --once`); a mailbox has no one-shot form.
            raise FetchError(
                "email mailboxes are subscriptions, not one-shots; "
                "use `pulp add imaps://...` without --once"
            )
        self._validate_mark_read()
        session = self._session_for(target_url)

        since: datetime | None = None
        since_days = _int_option(self._options.get("since_days"), _DEFAULT_SINCE_DAYS)
        if since_days > 0:
            since = datetime.now(tz=UTC) - timedelta(days=since_days)

        summaries = session.list_summaries(
            since=since.date() if since is not None else None,
            unseen_only=_truthy(self._options.get("unseen_only")),
        )
        # last_known_total is deliberately NOT set: it means "total population
        # of the subscription" (all-time), and the since-window count would
        # render nonsense ingested/total ratios in the TUI.
        refs: list[ItemRef] = []
        for summary in summaries:
            refs.extend(self._register_summary(summary))
        return refs

    def discover_backwards(self, target_url: str) -> Iterator[ItemRef]:
        """Walk the whole folder newest-first for `pulp backfill`."""
        self._validate_mark_read()
        session = self._session_for(target_url)
        uids = session.list_all_uids_newest_first()
        for page in batched(uids, _BACKFILL_PAGE_SIZE, strict=False):
            by_uid = {s.uid: s for s in session.summaries_for_uids(list(page))}
            for uid in page:
                summary = by_uid.get(uid)
                if summary is None:
                    continue
                yield from self._register_summary(summary)

    def _register_summary(self, summary: MessageSummary) -> list[ItemRef]:
        base_url = _mid_url(summary)
        ebook_parts = [p for p in summary.parts if _ebook_extension(p) is not None]

        refs: list[ItemRef] = []
        if summary.structure_known and ebook_parts:
            for part in ebook_parts:
                url = f"{base_url}/att/{part.section}"
                self._by_url[url] = _RefEntry(summary=summary, part=part)
                refs.append(
                    ItemRef(
                        url=url,
                        title=_filename_stem(part.filename) or summary.subject or None,
                        pub_date=summary.date,
                        guid=summary.uid,
                    )
                )
        else:
            # No ebook attachments - or structure unknown, in which case
            # fetch() downloads the whole message and dispatches there.
            self._by_url[base_url] = _RefEntry(summary=summary, part=None)
            refs.append(
                ItemRef(
                    url=base_url,
                    title=summary.subject or None,
                    pub_date=summary.date,
                    guid=summary.uid,
                )
            )

        self._pending_acks[summary.uid] = {r.url for r in refs}
        return refs

    # --- fetch ---------------------------------------------------------------

    def fetch(self, ref: ItemRef) -> RawArticle:
        if ref.url.startswith(("http://", "https://")):
            # `pulp migrate --rebuild` re-fetches by the ledger's
            # canonical_url; for newsletters that shipped a web permalink
            # that's an http URL, so re-render from the web version.
            self._extension = "epub"
            return fetch_article(
                ref.url, self.client, source_url=self._subscription_url or "direct"
            )
        entry = self._by_url.get(ref.url) or self._resolve_without_discover(ref)
        if entry.part is not None:
            return self._fetch_attachment(ref, entry.summary, entry.part)
        return self._fetch_whole_message(ref, entry.summary)

    def _fetch_attachment(
        self, ref: ItemRef, summary: MessageSummary, part: MimePart
    ) -> RawArticle:
        session = self._require_session()
        payload = session.fetch_part(summary.uid, part)
        self._payloads[ref.url] = payload
        self._extension = _ebook_extension(part) or "epub"
        title = _filename_stem(part.filename) or summary.subject or "attachment"
        return self._attachment_article(ref, summary, title)

    def _fetch_whole_message(self, ref: ItemRef, summary: MessageSummary) -> RawArticle:
        session = self._require_session()
        message = session.fetch_message(summary.uid)

        if not summary.structure_known:
            # BODYSTRUCTURE was unparsable at discover time; now that the
            # whole message is here, apply the same attachment-trumps rule.
            attachment = _first_ebook_attachment(message)
            if attachment is not None:
                extras = [
                    a.filename
                    for a in message.attachments
                    if a is not attachment and _attachment_extension(a) is not None
                ]
                if extras:
                    _log.warning(
                        "email uid=%s has %d extra ebook attachment(s) not ingested "
                        "(structure parse failed; only the first is saved): %s",
                        summary.uid,
                        len(extras),
                        ", ".join(extras),
                    )
                self._payloads[ref.url] = attachment.payload
                self._extension = _attachment_extension(attachment) or "epub"
                title = _filename_stem(attachment.filename) or summary.subject or "attachment"
                return self._attachment_article(ref, summary, title)

        self._extension = "epub"
        return self._newsletter_article(ref, summary, message)

    def _attachment_article(self, ref: ItemRef, summary: MessageSummary, title: str) -> RawArticle:
        return RawArticle(
            title=title,
            body_html="",  # render() returns the cached attachment bytes
            canonical_url=ref.url,
            source_url=self._subscription_url or self._session_url or ref.url,
            author=summary.from_name or summary.from_addr,
            publisher=_sender_domain(summary.from_addr),
            pub_date=summary.date,
        )

    def _newsletter_article(
        self, ref: ItemRef, summary: MessageSummary, message: MailMessage
    ) -> RawArticle:
        html = message.html or ""
        if not html.strip():
            raise ExtractionError(
                f"email {ref.url} has no HTML body and no ebook attachment; "
                "text-only emails are not supported"
            )

        permalink = find_web_permalink(html)
        if permalink and _truthy(self._options.get("prefer_web")):
            try:
                return fetch_article(
                    permalink,
                    self.client,
                    source_url=self._subscription_url or self._session_url or "direct",
                )
            except (FetchError, ExtractionError) as exc:
                _log.warning(
                    "prefer_web fetch failed for %s (%s); falling back to email body",
                    permalink,
                    exc,
                )

        inline_images = {
            a.content_id.strip().strip("<>"): (a.content_type, a.payload)
            for a in message.attachments
            if a.content_id
        }
        body_html = clean_email_html(html, inline_images=inline_images)

        sender = summary.from_name or summary.from_addr
        subject = summary.subject or "Untitled"
        title = f"{subject} - {sender}" if sender else subject
        return RawArticle(
            title=title,
            body_html=body_html,
            canonical_url=permalink or ref.url,
            source_url=self._subscription_url or self._session_url or ref.url,
            author=summary.from_name or summary.from_addr,
            publisher=_sender_domain(summary.from_addr),
            pub_date=summary.date,
        )

    def render(self, article: RawArticle) -> bytes:
        payload = self._payloads.pop(article.canonical_url, None)
        if payload is not None:
            return payload
        return super().render(article)

    def _resolve_without_discover(self, ref: ItemRef) -> _RefEntry:
        """Rebuild a ref entry for a ledger-stored mid: URL (`pulp migrate
        --rebuild` calls fetch() without a prior discover())."""
        message_id, section = _split_mid_url(ref.url)
        session = self._require_session()
        uid = None
        if message_id is not None:
            uid = session.find_uid_by_message_id(message_id)
        if uid is None:
            raise FetchError(f"message for {ref.url} not found in mailbox (moved or deleted?)")

        summaries = session.summaries_for_uids([uid])
        if not summaries:
            raise FetchError(f"message uid={uid} vanished while resolving {ref.url}")
        summary = summaries[0]

        part = None
        if section is not None:
            part = next((p for p in summary.parts if p.section == section), None)
            if part is None:
                raise FetchError(f"attachment part {section} not found for {ref.url}")

        entry = _RefEntry(summary=summary, part=part)
        self._by_url[ref.url] = entry
        self._pending_acks.setdefault(summary.uid, set()).add(ref.url)
        return entry

    # --- post-record acknowledgement ------------------------------------------

    def ack(self, ref: ItemRef) -> None:
        """Apply the mark_read policy for a fully-processed item.

        Called by the pipeline after the ledger records the item (and on
        ledger-skips), never before - so a crash mid-item leaves the message
        untouched and the next sync retries it.
        """
        policy = str(self._options.get("mark_read", "seen"))
        if policy == "none":
            return
        entry = self._by_url.get(ref.url)
        if entry is None or self._session is None:
            return
        uid = entry.summary.uid

        # Both policies wait for every item of the message to be accounted
        # for. This matters even for "seen": with unseen_only=1 a message
        # marked seen after its FIRST attachment would drop out of discovery
        # with its remaining attachments never ingested.
        pending = self._pending_acks.get(uid)
        if pending is not None:
            pending.discard(ref.url)
            if pending:
                return  # sibling items of this message still outstanding

        if policy == "seen":
            if uid not in self._seen_done and not entry.summary.seen:
                self._session.mark_seen(uid)
                self._seen_done.add(uid)
        elif uid not in self._moved:  # policy == "move"
            self._session.move(uid, str(self._options.get("move_to")))
            self._moved.add(uid)

    # --- plumbing --------------------------------------------------------------

    def _session_for(self, target_url: str) -> ImapSessionLike:
        if self._session is not None and self._session_url == target_url:
            return self._session
        if self._session is not None:
            self._session.close()
            self._session = None
        location = self._parse_url(target_url)
        username, password = self._credentials()
        self._session = self._session_factory(location, username, password)
        self._session_url = target_url
        return self._session

    def _require_session(self) -> ImapSessionLike:
        if self._session is not None:
            return self._session
        if self._subscription_url is None:
            raise FetchError("email source has no open session and no subscription URL to open one")
        return self._session_for(self._subscription_url)

    def _parse_url(self, target_url: str) -> MailboxLocation:
        try:
            return parse_mailbox_url(target_url)
        except ValueError as exc:
            raise FetchError(str(exc)) from exc

    def _credentials(self) -> tuple[str, str]:
        if not self._username:
            # No square brackets in the message: the CLI's rich output eats
            # "[auth.email]" as markup.
            raise FetchError(
                "email source is not configured: add an auth.email table to "
                "config.toml with username and password_path (or password)"
            )
        password = os.environ.get(_ENV_PASSWORD)
        if not password and self._password_path:
            try:
                password = (
                    Path(self._password_path).expanduser().read_text(encoding="utf-8").strip()
                )
            except OSError as exc:
                raise FetchError(
                    f"could not read auth.email password_path {self._password_path!r}: "
                    f"{exc.__class__.__name__}"
                ) from exc
        if not password:
            password = self._password
        if not password:
            raise FetchError(
                "email password not found: set password_path or password in the "
                f"auth.email table, or the {_ENV_PASSWORD} env var"
            )
        return self._username, password

    def _validate_mark_read(self) -> None:
        policy = str(self._options.get("mark_read", "seen"))
        if policy not in _MARK_READ_POLICIES:
            raise FetchError(
                f"invalid mark_read {policy!r}: expected one of {sorted(_MARK_READ_POLICIES)}"
            )
        if policy == "move" and not self._options.get("move_to"):
            raise FetchError('mark_read = "move" requires options.move_to = "<folder>"')


class _RefEntry:
    """discover() -> fetch()/ack() bridge for one ItemRef."""

    __slots__ = ("part", "summary")

    def __init__(self, summary: MessageSummary, part: MimePart | None) -> None:
        self.summary = summary
        self.part = part


def _mid_url(summary: MessageSummary) -> str:
    message_id = (summary.message_id or "").strip().strip("<>").strip()
    if not message_id:
        # No Message-ID (rare, RFC-violating). Hash stable headers instead of
        # the UID so the key survives UIDVALIDITY resets.
        digest = hashlib.sha256(
            f"{summary.subject}|{summary.date}|{summary.from_addr}".encode()
        ).hexdigest()
        return f"mid:pulpline-{digest[:32]}@synthetic"
    return f"mid:{urllib.parse.quote(message_id, safe=_MID_SAFE)}"


def _split_mid_url(url: str) -> tuple[str | None, str | None]:
    """`mid:<quoted-id>[/att/<section>]` -> (raw Message-ID with brackets, section)."""
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme.lower() != "mid":
        raise FetchError(f"not an email item URL: {url}")
    path = parts.path
    section: str | None = None
    if "/att/" in path:
        path, _, section = path.rpartition("/att/")
    message_id = urllib.parse.unquote(path)
    if not message_id or message_id.startswith("pulpline-"):
        return None, section  # synthetic ids can't be searched by header
    return f"<{message_id}>", section


def _ebook_extension(part: MimePart) -> str | None:
    ext = _extension_of(part.filename)
    if ext in _EBOOK_EXTENSIONS:
        return ext
    return _EBOOK_CONTENT_TYPES.get(part.content_type)


def _attachment_extension(attachment: MailAttachment) -> str | None:
    ext = _extension_of(attachment.filename)
    if ext in _EBOOK_EXTENSIONS:
        return ext
    return _EBOOK_CONTENT_TYPES.get((attachment.content_type or "").split(";")[0].lower())


def _first_ebook_attachment(message: MailMessage) -> MailAttachment | None:
    for attachment in message.attachments:
        if _attachment_extension(attachment) is not None:
            return attachment
    return None


def _extension_of(filename: str | None) -> str | None:
    if not filename or "." not in filename:
        return None
    return filename.rsplit(".", 1)[1].lower() or None


def _filename_stem(filename: str | None) -> str | None:
    if not filename:
        return None
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return stem.strip() or None


def _sender_domain(from_addr: str | None) -> str | None:
    if not from_addr or "@" not in from_addr:
        return None
    return from_addr.rsplit("@", 1)[1].lower() or None


def _truthy(value: str | int | None) -> bool:
    if value is None:
        return False
    if isinstance(value, int):
        return bool(value)
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_option(value: str | int | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default
