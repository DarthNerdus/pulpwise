"""IMAP session wrapper for the email source.

The only module that imports imap-tools - EmailSource talks to the
`ImapSessionLike` protocol so tests inject a fake, and a future auth change
(XOAUTH2) swaps implementations here without touching the source.

Two deliberate design points:

- Message listing fetches headers + BODYSTRUCTURE only, never bodies. A
  mailbox receiving ebook attachments holds tens of MB per message; the
  two-phase Source contract (cheap discover, expensive fetch) only holds if
  listing stays metadata-only.
- BODYSTRUCTURE is parsed by hand (imap-tools has no API for it) with a
  small s-expression parser. Any parse failure degrades gracefully: the
  summary reports `structure_known=False` and the caller falls back to
  downloading the whole message.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import imaplib
import quopri
import re
import ssl
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from email.header import decode_header
from itertools import batched
from typing import Protocol, Self, cast

from imap_tools import AND, Header, MailBox, MailBoxStartTls, MailMessage, MailMessageFlags
from imap_tools.errors import ImapToolsError

from pulpwise.models import FetchError

_SExpr = list["str | None | _SExpr"]

# Everything a live IMAP exchange can throw. imaplib.IMAP4.error subclasses
# Exception directly (NOT OSError) and imaplib converts mid-command socket
# errors into IMAP4.abort - so OSError alone never catches a dropped
# connection, and imap-tools doesn't wrap imaplib's exceptions either.
_IMAP_ERRORS: tuple[type[Exception], ...] = (ImapToolsError, imaplib.IMAP4.error, OSError)

# open() additionally sees ValueError: imap-tools rejects bad scheme/port
# combos at construction, and imaplib raises UnicodeEncodeError (a ValueError)
# for non-ASCII credentials.
_OPEN_ERRORS: tuple[type[Exception], ...] = (*_IMAP_ERRORS, ValueError)

# Cap per-command UID set size: servers cap command-line length (Dovecot
# defaults to 64KB), and `since_days = 0` on a big folder is a documented
# configuration, not an edge case.
_SUMMARY_CHUNK_SIZE = 200


@dataclass(frozen=True, slots=True)
class MailboxLocation:
    """Parsed `imaps://host[:port]/folder` subscription URL."""

    host: str
    port: int
    folder: str
    starttls: bool  # imap:// (STARTTLS on 143) vs imaps:// (implicit TLS on 993)


def parse_mailbox_url(url: str) -> MailboxLocation:
    """Parse an imap(s):// URL into host/port/folder.

    Credentials never live in the URL - they come from `[auth.email]`.
    The path is the IMAP folder (URL-escaped, so `Pulpwise%20Inbox` works);
    empty path means INBOX.
    """
    parts = urllib.parse.urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in {"imap", "imaps"}:
        raise ValueError(f"not an imap(s):// URL: {url}")
    if not parts.hostname:
        raise ValueError(f"imap URL missing host: {url}")
    if parts.username or parts.password:
        raise ValueError(
            "credentials in imap URLs are not supported; set [auth.email] in config instead"
        )
    starttls = scheme == "imap"
    port = parts.port or (143 if starttls else 993)
    if starttls and port == 993:
        raise ValueError(f"port 993 is implicit TLS; use imaps:// instead of imap://: {url}")
    folder = urllib.parse.unquote(parts.path.strip("/")) or "INBOX"
    return MailboxLocation(host=parts.hostname, port=port, folder=folder, starttls=starttls)


@dataclass(frozen=True, slots=True)
class MimePart:
    """One leaf part from a message's BODYSTRUCTURE."""

    section: str  # IMAP fetch section, e.g. "2" or "1.2"
    content_type: str  # lowercased "type/subtype"
    filename: str | None  # RFC2047/2231-decoded, best effort
    encoding: str  # lowercased content-transfer-encoding ("base64", ...)


@dataclass(frozen=True, slots=True)
class MessageSummary:
    """Cheap listing of one message: headers + structure, no bodies."""

    uid: str
    message_id: str | None
    subject: str
    from_name: str | None
    from_addr: str | None
    date: datetime | None
    seen: bool
    parts: tuple[MimePart, ...]
    structure_known: bool  # False = BODYSTRUCTURE parse failed; parts is empty


class ImapSessionLike(Protocol):
    """What EmailSource needs from a live IMAP connection.

    `ImapSession` implements it over imap-tools; tests implement it over
    fixtures. All methods may raise `FetchError` on connection trouble.
    """

    def list_summaries(self, since: date | None, unseen_only: bool) -> list[MessageSummary]: ...

    def list_all_uids_newest_first(self) -> list[str]: ...

    def summaries_for_uids(self, uids: list[str]) -> list[MessageSummary]: ...

    def fetch_part(self, uid: str, part: MimePart) -> bytes: ...

    def fetch_message(self, uid: str) -> MailMessage: ...

    def find_uid_by_message_id(self, message_id: str) -> str | None: ...

    def mark_seen(self, uid: str) -> None: ...

    def move(self, uid: str, folder: str) -> None: ...

    def close(self) -> None: ...


class ImapSession:
    """Live imap-tools connection to one account+folder."""

    def __init__(self, mailbox: MailBox | MailBoxStartTls) -> None:
        self._mb = mailbox

    @classmethod
    def open(cls, location: MailboxLocation, username: str, password: str) -> Self:
        """Connect, authenticate, and select the folder. TLS verification is ON.

        The ssl_context must be passed explicitly: with ssl_context=None,
        imaplib falls back to ssl._create_unverified_context (no certificate
        or hostname checks), which would hand LOGIN credentials to any
        active MITM.
        """
        context = ssl.create_default_context()
        try:
            mailbox: MailBox | MailBoxStartTls
            if location.starttls:
                mailbox = MailBoxStartTls(location.host, location.port, ssl_context=context)
            else:
                mailbox = MailBox(location.host, location.port, ssl_context=context)
            mailbox.login(username, password, initial_folder=location.folder)
        except _OPEN_ERRORS as exc:
            raise FetchError(
                f"IMAP connect/login failed for {username} at "
                f"{location.host}:{location.port} folder {location.folder!r}: {exc}"
            ) from exc
        return cls(mailbox)

    def close(self) -> None:
        # Closing a dead connection is fine.
        with contextlib.suppress(*_IMAP_ERRORS):
            self._mb.logout()

    def list_summaries(self, since: date | None, unseen_only: bool) -> list[MessageSummary]:
        query: str | AND
        if since is not None and unseen_only:
            query = AND(date_gte=since, seen=False)
        elif since is not None:
            query = AND(date_gte=since)
        elif unseen_only:
            query = AND(seen=False)
        else:
            query = "ALL"
        try:
            uids = self._mb.uids(query)
        except _IMAP_ERRORS as exc:
            raise FetchError(f"IMAP search failed: {exc}") from exc
        return self.summaries_for_uids(_sorted_numerically(uids))

    def list_all_uids_newest_first(self) -> list[str]:
        try:
            uids = self._mb.uids("ALL")
        except _IMAP_ERRORS as exc:
            raise FetchError(f"IMAP search failed: {exc}") from exc
        return list(reversed(_sorted_numerically(uids)))

    def summaries_for_uids(self, uids: list[str]) -> list[MessageSummary]:
        # Chunked so no single FETCH/SEARCH command line grows with folder size.
        summaries: list[MessageSummary] = []
        for chunk in batched(uids, _SUMMARY_CHUNK_SIZE, strict=False):
            summaries.extend(self._summaries_chunk(list(chunk)))
        return summaries

    def _summaries_chunk(self, uids: list[str]) -> list[MessageSummary]:
        if not uids:
            return []
        structures = self._fetch_structures(uids)
        try:
            messages = list(
                self._mb.fetch(AND(uid=uids), mark_seen=False, headers_only=True, bulk=True)
            )
        except _IMAP_ERRORS as exc:
            raise FetchError(f"IMAP header fetch failed: {exc}") from exc

        summaries: list[MessageSummary] = []
        for msg in messages:
            if msg.uid is None:
                continue
            parts, known = structures.get(msg.uid, ((), False))
            from_values = msg.from_values
            summaries.append(
                MessageSummary(
                    uid=msg.uid,
                    message_id=_first_header(msg, "message-id"),
                    subject=msg.subject,
                    from_name=(from_values.name or None) if from_values else None,
                    from_addr=(from_values.email or None) if from_values else None,
                    date=msg.date,
                    seen=MailMessageFlags.SEEN in msg.flags,
                    parts=parts,
                    structure_known=known,
                )
            )
        return summaries

    def fetch_part(self, uid: str, part: MimePart) -> bytes:
        """Download and decode a single MIME part (BODY.PEEK - flags untouched)."""
        try:
            typ, data = self._mb.client.uid("FETCH", uid, f"(BODY.PEEK[{part.section}])")
        except _IMAP_ERRORS as exc:
            raise FetchError(
                f"IMAP part fetch failed uid={uid} part={part.section}: {exc}"
            ) from exc
        if typ != "OK":
            raise FetchError(f"IMAP part fetch returned {typ} for uid={uid} part={part.section}")
        for item in data or []:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
                return decode_transfer_encoding(item[1], part.encoding)
        raise FetchError(f"IMAP returned no data for uid={uid} part={part.section}")

    def fetch_message(self, uid: str) -> MailMessage:
        try:
            messages = list(self._mb.fetch(AND(uid=uid), mark_seen=False, limit=1))
        except _IMAP_ERRORS as exc:
            raise FetchError(f"IMAP message fetch failed uid={uid}: {exc}") from exc
        if not messages:
            raise FetchError(f"message uid={uid} no longer in mailbox")
        return messages[0]

    def find_uid_by_message_id(self, message_id: str) -> str | None:
        """Locate a message by Message-ID header. Used by the email source's
        `_resolve_without_discover` when fetch() runs on a ledger-stored
        mid: URL without a prior discover() in this session."""
        try:
            uids = self._mb.uids(AND(header=Header("Message-ID", message_id)))
        except _IMAP_ERRORS as exc:
            raise FetchError(f"IMAP Message-ID search failed: {exc}") from exc
        return uids[-1] if uids else None

    def mark_seen(self, uid: str) -> None:
        # Raw UID STORE instead of imap-tools' flag(): flag() unconditionally
        # EXPUNGEs the folder afterwards, which would permanently destroy any
        # \Deleted messages other clients are holding in this folder.
        try:
            typ, _ = self._mb.client.uid("STORE", uid, "+FLAGS", r"(\Seen)")
        except _IMAP_ERRORS as exc:
            raise FetchError(f"IMAP mark-seen failed uid={uid}: {exc}") from exc
        if typ != "OK":
            raise FetchError(f"IMAP mark-seen returned {typ} for uid={uid}")

    def move(self, uid: str, folder: str) -> None:
        # Server-side MOVE only. imap-tools falls back to COPY+delete on
        # servers without the capability, and its delete() EXPUNGEs the whole
        # folder - same collateral damage as flag() above. MOVE is universal
        # on the servers that matter (Gmail, Dovecot, Fastmail, Outlook).
        try:
            if "MOVE" not in self._mb.client.capabilities:
                raise FetchError(
                    'IMAP server lacks the MOVE capability; use mark_read = "seen" instead'
                )
            self._mb.move(uid, folder)
        except _IMAP_ERRORS as exc:
            raise FetchError(f"IMAP move failed uid={uid} -> {folder!r}: {exc}") from exc

    def _fetch_structures(self, uids: list[str]) -> dict[str, tuple[tuple[MimePart, ...], bool]]:
        """Batched BODYSTRUCTURE fetch. Parse failures map to ((), False)."""
        try:
            typ, data = self._mb.client.uid("FETCH", ",".join(uids), "(BODYSTRUCTURE)")
        except _IMAP_ERRORS as exc:
            raise FetchError(f"IMAP BODYSTRUCTURE fetch failed: {exc}") from exc
        if typ != "OK":
            raise FetchError(f"IMAP BODYSTRUCTURE fetch returned {typ}")

        out: dict[str, tuple[tuple[MimePart, ...], bool]] = {}
        for record in _flatten_fetch_records(data or []):
            try:
                uid, parts = parse_bodystructure_record(record)
            except ValueError:
                fallback_uid = _uid_from_record(record)
                if fallback_uid is not None:
                    out[fallback_uid] = ((), False)
                continue
            out[uid] = (tuple(parts), True)
        return out


def _first_header(msg: MailMessage, name: str) -> str | None:
    # imap-tools' LazyHeaders is an unannotated Mapping[str, tuple[str, ...]].
    headers = cast("Mapping[str, tuple[str, ...]]", msg.headers)
    values = headers.get(name)
    if not values:
        return None
    first = str(values[0]).strip()
    return first or None


def _sorted_numerically(uids: Iterable[str]) -> list[str]:
    def key(uid: str) -> int:
        try:
            return int(uid)
        except ValueError:
            return 0

    return sorted(uids, key=key)


def decode_transfer_encoding(payload: bytes, encoding: str) -> bytes:
    """Decode a raw MIME part body per its content-transfer-encoding."""
    enc = encoding.lower()
    if enc == "base64":
        try:
            return base64.decodebytes(payload)
        except (binascii.Error, ValueError) as exc:
            raise FetchError(f"attachment base64 decode failed: {exc}") from exc
    if enc == "quoted-printable":
        return quopri.decodestring(payload)
    return payload  # 7bit / 8bit / binary / unknown: already raw


# --- BODYSTRUCTURE parsing ---------------------------------------------------
#
# imaplib hands back FETCH responses as a list where plain fragments are bytes
# and string literals ({n}-prefixed) split a record into (head, literal) tuples
# followed by a continuation fragment. `_flatten_fetch_records` reassembles one
# bytes blob per message, re-quoting literals so the s-expression parser below
# sees a uniform grammar.

_LITERAL_MARKER = re.compile(rb"\{\d+\}\s*$")
_UID_IN_RECORD = re.compile(rb"UID (\d+)", re.IGNORECASE)
_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|(\()|(\))|([^\s()"]+)')


def _flatten_fetch_records(data: list[object]) -> list[bytes]:
    records: list[bytes] = []
    buf = b""
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            head, literal = item
            if isinstance(head, bytes) and isinstance(literal, bytes):
                head = _LITERAL_MARKER.sub(b"", head)
                quoted = literal.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
                buf += head + b'"' + quoted + b'"'
        elif isinstance(item, bytes):
            # A plain fragment terminates the current record.
            records.append(buf + item)
            buf = b""
    if buf:
        records.append(buf)
    return [r for r in records if r.strip()]


def _uid_from_record(record: bytes) -> str | None:
    match = _UID_IN_RECORD.search(record)
    return match.group(1).decode("ascii") if match else None


def parse_bodystructure_record(record: bytes) -> tuple[str, list[MimePart]]:
    """Parse one `<seq> (UID <n> BODYSTRUCTURE (...))` record into leaf parts.

    Raises ValueError on anything unexpected - callers treat that as
    "structure unknown" and fall back to whole-message handling.
    """
    tokens = _parse_sexp(record.decode("latin-1"))
    envelope = next((t for t in tokens if isinstance(t, list)), None)
    if envelope is None:
        raise ValueError("no response list in FETCH record")

    uid: str | None = None
    structure: _SExpr | None = None
    for i, token in enumerate(envelope[:-1]):
        if not isinstance(token, str):
            continue
        upper = token.upper()
        nxt = envelope[i + 1]
        if upper == "UID" and isinstance(nxt, str):
            uid = nxt
        elif upper == "BODYSTRUCTURE" and isinstance(nxt, list):
            structure = nxt
    if uid is None or structure is None:
        raise ValueError("record missing UID or BODYSTRUCTURE")
    return uid, _walk_structure(structure, "")


def _parse_sexp(text: str) -> _SExpr:
    """Tokenize an IMAP parenthesized response into nested lists.

    Quoted strings unescape to str, NIL becomes None, everything else
    (atoms, numbers) stays str.
    """
    root: _SExpr = []
    stack: list[_SExpr] = [root]
    for match in _TOKEN.finditer(text):
        quoted, open_paren, close_paren, atom = match.groups()
        if open_paren:
            nested: _SExpr = []
            stack.append(nested)
        elif close_paren:
            if len(stack) == 1:
                raise ValueError("unbalanced ')' in FETCH record")
            done = stack.pop()
            stack[-1].append(done)
        elif quoted is not None:
            stack[-1].append(re.sub(r"\\(.)", r"\1", quoted))
        else:
            stack[-1].append(None if atom.upper() == "NIL" else atom)
    if len(stack) != 1:
        raise ValueError("unbalanced '(' in FETCH record")
    return root


def _walk_structure(node: _SExpr, section_prefix: str) -> list[MimePart]:
    """Flatten a BODYSTRUCTURE tree into leaf parts with IMAP section numbers.

    RFC 3501: a multipart is a list starting with child part lists; leaf
    parts start with the type string. Children of the multipart at section
    `p` are `p.1`, `p.2`, ... (top level: `1`, `2`, ...). A non-multipart
    message's single body is section `1`.
    """
    if not node:
        raise ValueError("empty BODYSTRUCTURE node")

    if isinstance(node[0], list):  # multipart: leading lists are the children
        parts: list[MimePart] = []
        for i, child in enumerate(node, 1):
            if not isinstance(child, list):
                break  # subtype string reached - children exhausted
            section = f"{section_prefix}.{i}" if section_prefix else str(i)
            parts.extend(_walk_structure(child, section))
        return parts

    if len(node) < 7:
        raise ValueError(f"leaf part too short: {node!r}")
    maintype, subtype = node[0], node[1]
    if not isinstance(maintype, str) or not isinstance(subtype, str):
        raise ValueError("leaf part type/subtype not strings")
    params = _params_to_dict(node[2])
    encoding = node[5] if isinstance(node[5], str) else ""

    # Extension fields (everything past size/lines) hold the disposition:
    # a list like ("attachment" ("filename" "book.epub")). Position varies
    # between text and non-text parts, so scan rather than index.
    disposition_filename: str | None = None
    for ext in node[7:]:
        if isinstance(ext, list) and ext and isinstance(ext[0], str):
            disp_params = ext[1] if len(ext) > 1 and isinstance(ext[1], list) else []
            disposition_filename = _lookup_param(_params_to_dict(disp_params), "filename")
            if disposition_filename:
                break

    filename = disposition_filename or _lookup_param(params, "name")
    return [
        MimePart(
            section=section_prefix or "1",
            content_type=f"{maintype}/{subtype}".lower(),
            filename=filename,
            encoding=encoding.lower(),
        )
    ]


def _params_to_dict(node: object) -> dict[str, str]:
    """BODYSTRUCTURE parameter lists are flat ("key" "value" ...) pairs."""
    if not isinstance(node, list):
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(node) - 1, 2):
        key, value = node[i], node[i + 1]
        if isinstance(key, str) and isinstance(value, str):
            out[key.lower()] = value
    return out


def _lookup_param(params: dict[str, str], base: str) -> str | None:
    """Read a MIME parameter, handling RFC2047 words and RFC2231 encoding.

    Best effort: `filename`, `filename*` (extended value), and
    `filename*0*`/`filename*1` continuations. Anything undecodable falls
    back to the raw value rather than None - a mangled filename still
    carries the extension we dispatch on.
    """
    plain = params.get(base)
    if plain is not None:
        return _decode_rfc2047(plain)

    extended = params.get(f"{base}*")
    if extended is not None:
        return _decode_rfc2231_value(extended)

    chunks: list[tuple[int, str, bool]] = []
    pattern = re.compile(re.escape(base) + r"\*(\d+)(\*)?$")
    for key, value in params.items():
        match = pattern.match(key)
        if match:
            chunks.append((int(match.group(1)), value, match.group(2) is not None))
    if not chunks:
        return None
    chunks.sort()
    joined = "".join(
        _decode_rfc2231_value(value, strip_charset=index == 0) if is_ext else value
        for index, value, is_ext in chunks
    )
    return joined or None


def _decode_rfc2047(value: str) -> str:
    if "=?" not in value:
        return value
    try:
        decoded = decode_header(value)
    except ValueError:
        return value
    out: list[str] = []
    for data, charset in decoded:
        if isinstance(data, bytes):
            try:
                out.append(data.decode(charset or "ascii", errors="replace"))
            except LookupError:
                # Unknown codec name: errors="replace" doesn't guard the
                # charset lookup itself. latin-1 never fails and preserves
                # the ASCII extension the dispatcher needs.
                out.append(data.decode("latin-1"))
        else:
            out.append(data)
    return "".join(out)


_RFC2231_PREFIX = re.compile(r"^([^']*)'[^']*'")  # charset'language' - language may be empty


def _decode_rfc2231_value(value: str, strip_charset: bool = True) -> str:
    """Decode `utf-8''caf%C3%A9.epub` (and `utf-8'en'...`) extended values."""
    raw = value
    charset = "utf-8"
    if strip_charset:
        match = _RFC2231_PREFIX.match(value)
        if match:
            raw = value[match.end() :]
            charset = match.group(1) or "utf-8"
    try:
        return urllib.parse.unquote(raw, encoding=charset, errors="replace")
    except LookupError:  # unknown charset name
        return urllib.parse.unquote(raw, errors="replace")
