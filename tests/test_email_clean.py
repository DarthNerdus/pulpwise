"""Tests for util/email_clean.py."""

from __future__ import annotations

import base64

import pytest

from pulpwise.models import ExtractionError
from pulpwise.util.email_clean import clean_email_html, find_web_permalink, parse_data_uri


def test_strips_style_script_and_comments() -> None:
    html = (
        "<html><head><style>p{color:red}</style><title>t</title></head>"
        "<body><script>alert(1)</script><!-- preheader --><p>Real content</p></body></html>"
    )
    cleaned = clean_email_html(html)
    assert "Real content" in cleaned
    assert "alert" not in cleaned
    assert "color:red" not in cleaned
    assert "preheader" not in cleaned


def test_drops_hidden_preview_text() -> None:
    html = (
        "<body><div style='display:none;max-height:0'>Teaser teaser teaser</div><p>Body</p></body>"
    )
    cleaned = clean_email_html(html)
    assert "Teaser" not in cleaned
    assert "Body" in cleaned


def test_drops_tracking_pixels_but_keeps_content_images() -> None:
    html = (
        '<body><p>Hi</p><img src="https://x.test/open.php?id=1" width="1" height="1"/>'
        '<img src="https://substack.com/o/whatever"/>'
        '<img src="https://cdn.test/photo.jpg" alt="photo"/></body>'
    )
    cleaned = clean_email_html(html)
    assert "open.php" not in cleaned
    assert "substack.com/o/" not in cleaned
    assert "photo.jpg" in cleaned


def test_strips_presentation_attributes_keeps_semantics() -> None:
    html = (
        '<body><table width="600" bgcolor="#fff"><tr><td style="padding:20px">'
        '<a href="https://x.test/a" style="color:blue">link</a>'
        "</td></tr></table></body>"
    )
    cleaned = clean_email_html(html)
    assert 'href="https://x.test/a"' in cleaned
    assert "bgcolor" not in cleaned
    assert "style=" not in cleaned
    assert "width=" not in cleaned


def test_unwraps_single_cell_layout_tables() -> None:
    html = (
        "<body><table><tbody><tr><td>"
        "<table><tr><td><p>Nested content</p></td></tr></table>"
        "</td></tr></tbody></table></body>"
    )
    cleaned = clean_email_html(html)
    assert "<table" not in cleaned
    assert "Nested content" in cleaned


def test_keeps_real_data_tables() -> None:
    html = "<body><table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table></body>"
    cleaned = clean_email_html(html)
    assert "<table" in cleaned


def test_inlines_cid_images_as_data_uris() -> None:
    html = '<body><p>pic:</p><img src="cid:img001@mail"/></body>'
    cleaned = clean_email_html(html, inline_images={"img001@mail": ("image/png", b"PNGBYTES")})
    expected = base64.b64encode(b"PNGBYTES").decode()
    assert f"data:image/png;base64,{expected}" in cleaned


def test_drops_unresolvable_cid_images() -> None:
    html = '<body><p>text</p><img src="cid:gone@mail"/></body>'
    cleaned = clean_email_html(html)
    assert "cid:" not in cleaned
    assert "text" in cleaned


def test_strips_img_sizing_attributes() -> None:
    html = '<body><p>x</p><img src="https://cdn.test/i.jpg" width="600" height="400"/></body>'
    cleaned = clean_email_html(html)
    assert "i.jpg" in cleaned
    assert "width" not in cleaned


def test_empty_or_contentless_html_raises() -> None:
    with pytest.raises(ExtractionError):
        clean_email_html("")
    with pytest.raises(ExtractionError):
        clean_email_html("<body><style>p{}</style></body>")


def test_find_web_permalink() -> None:
    html = (
        '<body><a href="https://news.test/unsubscribe">Unsubscribe</a>'
        '<a href="https://news.test/p/issue-42">View this post on the web</a></body>'
    )
    assert find_web_permalink(html) == "https://news.test/p/issue-42"


def test_find_web_permalink_ignores_long_paragraph_links(  # anchor text must stay short
) -> None:
    html = (
        '<body><a href="https://news.test/x">'
        "this is a long sentence that happens to mention view online somewhere in "
        "the middle of a much longer run of copy text</a></body>"
    )
    assert find_web_permalink(html) is None


def test_find_web_permalink_none_when_absent() -> None:
    assert find_web_permalink("<body><p>no links</p></body>") is None


def test_parse_data_uri_roundtrip() -> None:
    encoded = base64.b64encode(b"IMAGEBYTES").decode()
    parsed = parse_data_uri(f"data:image/jpeg;base64,{encoded}")
    assert parsed == ("image/jpeg", b"IMAGEBYTES")


def test_parse_data_uri_rejects_non_image_and_garbage() -> None:
    assert parse_data_uri("data:text/plain;base64,aGk=") is None
    assert parse_data_uri("https://example.com/img.png") is None


def test_drops_iframes_and_forms() -> None:
    html = (
        '<body><iframe src="https://x.test/embed"></iframe>'
        '<form action="https://x.test/subscribe"><input type="email"/>'
        "<button>Subscribe</button></form><p>Article text</p></body>"
    )
    cleaned = clean_email_html(html)
    assert "iframe" not in cleaned
    assert "form" not in cleaned
    assert "Subscribe" not in cleaned
    assert "Article text" in cleaned


def test_strips_non_web_link_schemes() -> None:
    html = (
        '<body><a href="javascript:alert(1)">click</a>'
        '<a href="https://x.test/ok">fine</a><p>t</p></body>'
    )
    cleaned = clean_email_html(html)
    assert "javascript:" not in cleaned
    assert 'href="https://x.test/ok"' in cleaned
    assert "click" in cleaned  # link text survives, href does not


def test_drops_style_sized_tracking_pixels() -> None:
    html = (
        '<body><p>Hi</p><img src="https://x.test/t.gif" '
        'style="width:1px;height:1px;display:inline"/></body>'
    )
    cleaned = clean_email_html(html)
    assert "t.gif" not in cleaned


def test_unwrapped_tables_keep_block_boundaries() -> None:
    """drop_tag would fuse adjacent text runs; converted <div>s keep them apart."""
    html = (
        "<body><table><tr><td>First block</td></tr></table>"
        "<table><tr><td>Second block</td></tr></table></body>"
    )
    cleaned = clean_email_html(html)
    assert "<table" not in cleaned
    assert "<div>First block</div>" in cleaned
    assert "<div>Second block</div>" in cleaned
    assert "First blockSecond" not in cleaned
