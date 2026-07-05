"""Newsletter-email HTML cleanup for Readwise Reader content submissions.

Email HTML is a different beast from web-page HTML: table-based layouts,
inline styles everywhere, tracking pixels, hidden preview text, and inline
images referenced by `cid:`. Trafilatura is tuned for web articles and
routinely guts newsletter markup, so the email source uses this dedicated
cleaner instead: strip the noise, unwrap the layout scaffolding, keep the
content structure intact. The cleaned fragment becomes the HTML payload
of the Reader save request.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping

import lxml.etree
import lxml.html

from pulpwise.models import ExtractionError

# Attributes that only carry email-client presentation. Stripping `style` is
# deliberate: Reader's own typography does better than newsletter-tool
# inline CSS.
_PRESENTATION_ATTRS = frozenset(
    {
        "style",
        "class",
        "id",
        "align",
        "valign",
        "bgcolor",
        "background",
        "border",
        "cellpadding",
        "cellspacing",
        "width",
        "height",
        "role",
    }
)

# Elements that never carry content in an email (or that don't belong in
# a Reader content submission: frames, forms, embedded players).
_DROP_ELEMENTS = (
    "script",
    "style",
    "link",
    "meta",
    "title",
    "base",
    "head",
    "iframe",
    "frame",
    "object",
    "embed",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "video",
    "audio",
)

# Elements whose tag is noise but whose children are content.
_UNWRAP_TAGS = ("font", "center")

_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|max-height\s*:\s*0", re.I)
_TRACKER_SRC = re.compile(r"substack\.com/o/|/open\.php|list-manage\.com/track", re.I)
_DATA_URI = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.+)$", re.I | re.S)

# Link texts that mark a "read this on the web" permalink.
_PERMALINK_PHRASES = (
    "view in browser",
    "view in your browser",
    "view this email in your browser",
    "view this post on the web",
    "view email in browser",
    "view online",
    "read online",
    "read this post online",
    "open in browser",
    "web version",
)


def clean_email_html(
    html_str: str,
    inline_images: Mapping[str, tuple[str, bytes]] | None = None,
) -> str:
    """Return a cleaned HTML fragment for a Reader content submission.

    `inline_images` maps Content-ID (without angle brackets) to
    `(content_type, bytes)`; matching `cid:` image references are rewritten
    to data: URIs, because Reader cannot fetch `cid:` references from a
    submitted document. Raises ExtractionError when the input has no
    usable content.
    """
    body = _parse_body(html_str)

    for tag in _DROP_ELEMENTS:
        for el in body.findall(f".//{tag}"):
            _drop(el)
    for comment in body.xpath(".//comment()"):
        _drop(comment)

    for el in body.xpath(".//*[@style]"):
        style = el.get("style") or ""
        if _HIDDEN_STYLE.search(style):
            _drop(el)

    _rewrite_images(body, inline_images or {})

    for el in body.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            lowered = attr.lower()
            if (
                lowered in _PRESENTATION_ATTRS
                or lowered.startswith(("on", "data-"))
                # Images keep nothing but src/alt; everything else is
                # email-client sizing that fights the reader's layout.
                or (el.tag == "img" and lowered not in {"src", "alt"})
            ):
                del el.attrib[attr]
        # Neutralize non-web link schemes (javascript:, file:, ...).
        href = el.get("href")
        if href is not None and not href.strip().lower().startswith(
            ("http://", "https://", "mailto:", "#")
        ):
            del el.attrib["href"]

    for tag in _UNWRAP_TAGS:
        for el in body.findall(f".//{tag}"):
            el.drop_tag()
    _unwrap_layout_tables(body)

    cleaned = _inner_html(body).strip()
    if not cleaned or not _has_text_or_images(body):
        raise ExtractionError("email HTML contained no usable content after cleaning")
    return cleaned


def find_web_permalink(html_str: str) -> str | None:
    """Find the newsletter's 'view in browser' permalink, if it ships one."""
    try:
        body = _parse_body(html_str)
    except ExtractionError:
        return None
    for anchor in body.xpath(".//a[@href]"):
        href = (anchor.get("href") or "").strip()
        if not href.lower().startswith(("http://", "https://")):
            continue
        text = _WHITESPACE.sub(" ", anchor.text_content()).strip().lower()
        if not text or len(text) > 60:
            continue
        if any(phrase in text for phrase in _PERMALINK_PHRASES):
            return href
    return None


_WHITESPACE = re.compile(r"\s+")


def _parse_body(html_str: str) -> lxml.html.HtmlElement:
    if not html_str or not html_str.strip():
        raise ExtractionError("email has an empty HTML body")
    try:
        doc = lxml.html.document_fromstring(html_str)
    except (lxml.etree.ParserError, ValueError) as exc:
        raise ExtractionError(f"could not parse email HTML: {exc}") from exc
    body = doc.find("body")
    return body if body is not None else doc


def _rewrite_images(body: lxml.html.HtmlElement, images: Mapping[str, tuple[str, bytes]]) -> None:
    """Inline cid: references as data: URIs; drop tracking pixels."""
    for img in body.xpath(".//img"):
        src = (img.get("src") or "").strip()
        if src.lower().startswith("cid:"):
            content_id = src[4:].strip().strip("<>")
            resolved = images.get(content_id)
            if resolved is None:
                _drop(img)  # unresolvable cid renders as a broken box
                continue
            content_type, payload = resolved
            encoded = base64.b64encode(payload).decode("ascii")
            img.set("src", f"data:{content_type};base64,{encoded}")
            continue
        if _is_tracking_pixel(img, src):
            _drop(img)


_STYLE_PIXEL = re.compile(r"(?:^|;)\s*(width|height)\s*:\s*[0-2](?:\.\d+)?px", re.I)


def _is_tracking_pixel(img: lxml.html.HtmlElement, src: str) -> bool:
    if _TRACKER_SRC.search(src):
        return True
    width = _int_attr(img, "width")
    height = _int_attr(img, "height")
    if width is not None and height is not None and width <= 2 and height <= 2:
        return True
    # Style-sized pixels (style="width:1px;height:1px") - the style attr is
    # still present here; it's stripped later in the pipeline, and a kept
    # img would fire the tracker every time Reader renders the document.
    style = img.get("style") or ""
    return len(_STYLE_PIXEL.findall(style)) >= 2


def _int_attr(el: lxml.html.HtmlElement, name: str) -> int | None:
    raw = (el.get(name) or "").strip().removesuffix("px")
    try:
        return int(raw)
    except ValueError:
        return None


def _unwrap_layout_tables(body: lxml.html.HtmlElement) -> None:
    """Unwrap single-cell tables - the classic email centering scaffold.

    Runs to a fixpoint because unwrapping an outer table exposes the next
    one. Tables with real tabular content (multiple rows or cells) are
    left alone.
    """
    for _ in range(20):  # depth bound; newsletter nesting is well under this
        changed = False
        for table in body.findall(".//table"):
            rows = table.findall(".//tr")
            if len(rows) != 1:
                continue
            cells = rows[0].findall("./td") + rows[0].findall("./th")
            if len(cells) != 1:
                continue
            for tag in ("td", "th", "tr", "tbody", "thead", "tfoot"):
                for el in table.findall(f".//{tag}"):
                    el.drop_tag()
            # Become a <div> rather than drop_tag(): dropping would fuse the
            # cell's text straight into adjacent text nodes with no
            # separator; a div keeps the block boundary.
            table.tag = "div"
            changed = True
        if not changed:
            return


def _has_text_or_images(body: lxml.html.HtmlElement) -> bool:
    if body.text_content().strip():
        return True
    return bool(body.xpath(".//img[@src]"))


def _drop(el: object) -> None:
    """Remove an element (or comment) from its tree, tolerating orphans."""
    node = el  # comments come out of xpath as _Comment, still have getparent
    parent = node.getparent()  # type: ignore[attr-defined]
    if parent is not None:
        parent.remove(node)


def _inner_html(el: lxml.html.HtmlElement) -> str:
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(lxml.html.tostring(child, encoding="unicode"))
    return "".join(parts)


def parse_data_uri(src: str) -> tuple[str, bytes] | None:
    """Split a base64 image data: URI into (content_type, bytes), or None."""
    match = _DATA_URI.match(src.strip())
    if not match:
        return None
    try:
        payload = base64.b64decode(match.group(2), validate=False)
    except ValueError:
        return None
    return match.group(1).lower(), payload
