"""CLI integration tests for Phase 2 commands (subscribe, list, remove, sync)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from pulpline import cli
from pulpline.cli import app
from pulpline.config import Config, Subscription, load_config

ClientFactory = Callable[[dict[str, str]], httpx.Client]
runner = CliRunner()


def _feed() -> str:
    return (Path(__file__).parent / "fixtures" / "sample_feed.xml").read_text()


def _patch_build_client(
    monkeypatch: pytest.MonkeyPatch, factory: ClientFactory, routes: dict[str, str]
) -> None:
    """Make `pulpline.cli.build_client` return a mock-transport client."""

    def _factory() -> httpx.Client:
        return factory(routes)

    monkeypatch.setattr(cli, "build_client", _factory)


def test_add_subscribes_with_derived_name(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    feed_url = "https://example.com/feed"
    _patch_build_client(monkeypatch, mock_client_factory, {feed_url: _feed()})

    result = runner.invoke(app, ["add", feed_url])

    assert result.exit_code == 0, result.output
    assert "subscribed" in result.output.lower()
    config = load_config()
    assert len(config.subscriptions) == 1
    sub = config.subscriptions[0]
    assert sub.url == feed_url
    assert sub.source == "rss"
    # Name derived from feed title "Stratechery by Ben Thompson" -> sluggy
    assert sub.name == "stratechery-by-ben-thompson"


def test_add_with_explicit_name(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    feed_url = "https://example.com/feed"
    _patch_build_client(monkeypatch, mock_client_factory, {feed_url: _feed()})

    result = runner.invoke(app, ["add", feed_url, "--name", "stratechery"])

    assert result.exit_code == 0, result.output
    config = load_config()
    assert config.subscriptions[0].name == "stratechery"


def test_is_front_page_helper() -> None:
    from pulpline.cli import _is_front_page

    assert _is_front_page("https://etymology.substack.com")
    assert _is_front_page("https://etymology.substack.com/")
    assert not _is_front_page("https://etymology.substack.com/p/some-post")
    assert not _is_front_page("https://etymology.substack.com/?p=42")


def test_alternate_feed_url_helper() -> None:
    from pulpline.cli import _alternate_feed_url

    html = """<html><head>
      <link rel="alternate" type="application/rss+xml" href="/feed">
    </head><body>x</body></html>"""
    assert (
        _alternate_feed_url(html, "https://etymology.substack.com/")
        == "https://etymology.substack.com/feed"
    )

    # Atom is acceptable too
    html_atom = """<html><head>
      <link rel="alternate" type="application/atom+xml" href="https://example.com/atom.xml">
    </head></html>"""
    assert _alternate_feed_url(html_atom, "https://example.com/") == "https://example.com/atom.xml"

    # No alternate link
    assert _alternate_feed_url("<html><head></head></html>", "https://x.com/") is None


def test_add_autodiscovers_feed_from_front_page(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    """`pulp add https://etymology.substack.com/` follows the alternate link to /feed."""
    front = "https://etymology.substack.com/"
    feed_url = "https://etymology.substack.com/feed"
    front_html = (
        "<html><head>"
        f'<link rel="alternate" type="application/rss+xml" href="{feed_url}">'
        "</head><body></body></html>"
    )
    _patch_build_client(monkeypatch, mock_client_factory, {front: front_html, feed_url: _feed()})

    result = runner.invoke(app, ["add", front])
    assert result.exit_code == 0, result.output
    assert "discovered feed" in result.output
    config = load_config()
    assert len(config.subscriptions) == 1
    assert config.subscriptions[0].url == feed_url


def test_add_does_not_autodiscover_on_article_url(
    monkeypatch: pytest.MonkeyPatch,
    mock_client_factory: ClientFactory,
    tmp_path: Path,
) -> None:
    """A specific article URL with an alternate link must NOT subscribe to the feed."""
    from pulpline import pipeline

    article = "https://etymology.substack.com/p/some-post"
    feed_url = "https://etymology.substack.com/feed"
    article_html = (
        "<html><head>"
        f'<link rel="alternate" type="application/rss+xml" href="{feed_url}">'
        "</head><body>article body</body></html>"
    )
    _patch_build_client(
        monkeypatch, mock_client_factory, {article: article_html, feed_url: _feed()}
    )

    captured: list[str] = []

    def fake_add_once(target: str, *args: object, **kwargs: object) -> Path:
        captured.append(target)
        out = tmp_path / "x.epub"
        out.write_bytes(b"x")
        return out

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)

    result = runner.invoke(app, ["add", article])
    assert result.exit_code == 0, result.output
    assert captured == [article]  # one-shot, not subscribe
    assert load_config().subscriptions == ()


def test_add_dispatches_to_oneshot_when_not_a_feed(
    monkeypatch: pytest.MonkeyPatch,
    mock_client_factory: ClientFactory,
    tmp_path: Path,
) -> None:
    """Auto-detection: article URLs go to one-shot, not subscribe."""
    from pulpline import pipeline

    url = "https://example.com/article"
    _patch_build_client(
        monkeypatch, mock_client_factory, {url: "<html><body>Not a feed</body></html>"}
    )

    captured: dict[str, object] = {}

    def fake_add_once(target: str, *args: object, **kwargs: object) -> Path:
        captured["url"] = target
        out = tmp_path / "x.epub"
        out.write_bytes(b"x")
        return out

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)

    result = runner.invoke(app, ["add", url])
    assert result.exit_code == 0, result.output
    assert captured["url"] == url
    # No subscription should have been written.
    assert load_config().subscriptions == ()


def test_add_with_feed_flag_forces_subscribe(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    """`--feed` skips auto-detection and goes straight to the subscribe path."""
    feed_url = "https://example.com/feed"
    _patch_build_client(monkeypatch, mock_client_factory, {feed_url: _feed()})

    result = runner.invoke(app, ["add", feed_url, "--feed", "--name", "forced"])
    assert result.exit_code == 0, result.output
    assert load_config().subscriptions[0].name == "forced"


def test_add_with_once_flag_forces_oneshot_even_for_feeds(
    monkeypatch: pytest.MonkeyPatch,
    mock_client_factory: ClientFactory,
    tmp_path: Path,
) -> None:
    """`--once` overrides auto-detection - even a feed URL goes to one-shot."""
    from pulpline import pipeline

    feed_url = "https://example.com/feed"
    _patch_build_client(monkeypatch, mock_client_factory, {feed_url: _feed()})

    captured: dict[str, object] = {}

    def fake_add_once(target: str, *args: object, **kwargs: object) -> Path:
        captured["url"] = target
        out = tmp_path / "x.epub"
        out.write_bytes(b"x")
        return out

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)

    result = runner.invoke(app, ["add", feed_url, "--once"])
    assert result.exit_code == 0
    assert captured["url"] == feed_url
    assert load_config().subscriptions == ()


def test_add_rejects_once_and_feed_combined() -> None:
    result = runner.invoke(app, ["add", "https://example.com/x", "--once", "--feed"])
    assert result.exit_code != 0
    assert "mutually exclusive" in (result.output + (result.stderr or "")).lower()


def test_add_rejects_name_with_multiple_urls() -> None:
    result = runner.invoke(
        app,
        ["add", "https://a.example/feed", "https://b.example/feed", "--name", "x"],
    )
    assert result.exit_code != 0
    assert "single url" in (result.output + (result.stderr or "")).lower()


def test_add_processes_multiple_urls_independently(
    monkeypatch: pytest.MonkeyPatch,
    mock_client_factory: ClientFactory,
    tmp_path: Path,
) -> None:
    """Mixing a feed URL and an article URL in one call: each routes correctly."""
    from pulpline import pipeline

    feed_url = "https://a.example/feed"
    article_url = "https://b.example/article"
    _patch_build_client(
        monkeypatch,
        mock_client_factory,
        {feed_url: _feed(), article_url: "<html><body>Not a feed</body></html>"},
    )

    captured: list[str] = []

    def fake_add_once(target: str, *args: object, **kwargs: object) -> Path:
        captured.append(target)
        out = tmp_path / "x.epub"
        out.write_bytes(b"x")
        return out

    monkeypatch.setattr(pipeline, "add_once", fake_add_once)

    result = runner.invoke(app, ["add", feed_url, article_url])
    assert result.exit_code == 0, result.output
    # Feed got subscribed
    assert len(load_config().subscriptions) == 1
    # Article URL went through one-shot
    assert captured == [article_url]
    # Batch summary printed
    assert "batch:" in result.output and "2 ok" in result.output


def test_source_name_for_url_picks_arxiv() -> None:
    from pulpline.cli import _source_name_for_url

    assert _source_name_for_url("http://export.arxiv.org/api/query?x=y") == "arxiv"
    assert _source_name_for_url("https://arxiv.org/abs/2401.12345") == "arxiv"
    # Generic feed URLs default to rss
    assert _source_name_for_url("https://example.com/feed") == "rss"


def test_add_arxiv_query_url_subscribes_with_arxiv_source(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    """arXiv API query URL → subscription with source=arxiv, not rss."""
    api = "http://export.arxiv.org/api/query?search_query=cat:cs.AI"
    atom = (Path(__file__).parent / "fixtures" / "sample_arxiv.xml").read_text()
    _patch_build_client(monkeypatch, mock_client_factory, {api: atom})

    result = runner.invoke(app, ["add", api, "--name", "arxiv-cs-ai"])
    assert result.exit_code == 0, result.output

    config = load_config()
    assert len(config.subscriptions) == 1
    sub = config.subscriptions[0]
    assert sub.source == "arxiv"
    assert sub.url == api


def test_import_opml_writes_subscriptions(tmp_path: Path) -> None:
    """Full OPML import flow end-to-end through the CLI."""
    opml = tmp_path / "feeds.opml"
    opml.write_text(
        '<?xml version="1.0"?><opml version="2.0"><head/><body>'
        '<outline text="One" xmlUrl="https://one.example/feed"/>'
        '<outline text="Two" xmlUrl="https://two.example/feed"/>'
        "</body></opml>",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["import", "opml", str(opml)], input="all\n")
    assert result.exit_code == 0, result.output

    subs = load_config().subscriptions
    assert {s.url for s in subs} == {
        "https://one.example/feed",
        "https://two.example/feed",
    }
    assert all(s.source == "rss" for s in subs)


def test_import_opml_partial_selection(tmp_path: Path) -> None:
    opml = tmp_path / "feeds.opml"
    opml.write_text(
        '<?xml version="1.0"?><opml version="2.0"><head/><body>'
        '<outline text="A" xmlUrl="https://a.example/feed"/>'
        '<outline text="B" xmlUrl="https://b.example/feed"/>'
        '<outline text="C" xmlUrl="https://c.example/feed"/>'
        "</body></opml>",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["import", "opml", str(opml)], input="1,3\n")
    assert result.exit_code == 0, result.output
    urls = {s.url for s in load_config().subscriptions}
    assert urls == {"https://a.example/feed", "https://c.example/feed"}


def test_import_opml_missing_file_errors(tmp_path: Path) -> None:
    result = runner.invoke(app, ["import", "opml", str(tmp_path / "nope.opml")])
    assert result.exit_code != 0
    assert "not found" in (result.output + (result.stderr or "")).lower()


def test_import_substack_writes_subs_and_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Full CLI flow: cookies + mocked API populate config.toml's auth + subscriptions."""
    from pulpline import cli
    from pulpline.importers import substack as substack_importer

    cookies_path = tmp_path / "cookies.json"
    cookies_path.write_text('[{"name":"substack.sid","value":"abc"}]', encoding="utf-8")

    fake_pubs = [
        substack_importer.SubstackPublication(
            name="Sam Kriss", url="https://samkriss.substack.com", paid=True
        ),
        substack_importer.SubstackPublication(
            name="Stratechery", url="https://stratechery.com", paid=True
        ),
    ]

    def fake_list(
        username: str, cookies: dict[str, str], client: httpx.Client
    ) -> list[substack_importer.SubstackPublication]:
        assert username == "egor"
        assert cookies == {"substack.sid": "abc"}
        return fake_pubs

    monkeypatch.setattr(cli, "list_user_subscriptions", fake_list)

    result = runner.invoke(
        app,
        ["import", "substack", "egor", "--cookies", str(cookies_path)],
        input="all\n",
    )
    assert result.exit_code == 0, result.output

    config = load_config()
    assert config.auth_for("substack")["cookies_path"] == str(cookies_path)
    sub_names = {s.name for s in config.subscriptions}
    assert sub_names == {"sam-kriss", "stratechery"}
    sub_sources = {s.source for s in config.subscriptions}
    assert sub_sources == {"substack"}


def test_import_substack_partial_selection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pulpline import cli
    from pulpline.importers import substack as substack_importer

    cookies_path = tmp_path / "cookies.json"
    cookies_path.write_text('[{"name":"substack.sid","value":"abc"}]', encoding="utf-8")

    pubs = [
        substack_importer.SubstackPublication(name="A", url="https://a.example", paid=False),
        substack_importer.SubstackPublication(name="B", url="https://b.example", paid=True),
        substack_importer.SubstackPublication(name="C", url="https://c.example", paid=False),
    ]
    monkeypatch.setattr(cli, "list_user_subscriptions", lambda *a, **kw: pubs)

    result = runner.invoke(
        app,
        ["import", "substack", "egor", "--cookies", str(cookies_path)],
        input="1,3\n",
    )
    assert result.exit_code == 0, result.output
    sub_names = {s.name for s in load_config().subscriptions}
    assert sub_names == {"a", "c"}


def test_import_substack_missing_cookies_file_errors(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["import", "substack", "egor", "--cookies", str(tmp_path / "nope.json")],
    )
    assert result.exit_code != 0
    assert "not found" in (result.output + (result.stderr or "")).lower()


def test_import_substack_no_subs_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pulpline import cli

    cookies_path = tmp_path / "cookies.json"
    cookies_path.write_text('[{"name":"x","value":"y"}]', encoding="utf-8")
    monkeypatch.setattr(cli, "list_user_subscriptions", lambda *a, **kw: [])

    result = runner.invoke(
        app,
        ["import", "substack", "egor", "--cookies", str(cookies_path)],
    )
    assert result.exit_code != 0
    assert "no subscriptions" in (result.output + (result.stderr or "")).lower()


def test_add_batch_continues_on_per_url_failure(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    """One bad URL in a batch must not abort the others."""
    good = "https://good.example/feed"
    bad = "https://bad.example/feed"
    _patch_build_client(
        monkeypatch,
        mock_client_factory,
        {good: _feed(), bad: "<html>not a feed</html>"},
    )

    result = runner.invoke(app, ["add", good, bad, "--feed"])
    # The good one subscribed, the bad one failed but did not abort the batch.
    assert load_config().subscriptions[0].url == good
    assert "1 ok, 1 failed" in result.output
    assert result.exit_code == 1  # nonzero because at least one failed


def test_add_rejects_duplicate_name(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    feed_url = "https://example.com/feed"
    _patch_build_client(monkeypatch, mock_client_factory, {feed_url: _feed()})

    runner.invoke(app, ["add", feed_url, "--name", "dup"])
    result = runner.invoke(app, ["add", feed_url, "--name", "dup"])

    assert result.exit_code != 0
    assert "already exists" in (result.output + (result.stderr or "")).lower()


def test_remove_deletes_subscription_from_config(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    feed_url = "https://example.com/feed"
    _patch_build_client(monkeypatch, mock_client_factory, {feed_url: _feed()})
    runner.invoke(app, ["add", feed_url, "--name", "foo"])

    result = runner.invoke(app, ["remove", "foo"])
    assert result.exit_code == 0
    assert load_config().subscriptions == ()


def test_remove_unknown_name_errors() -> None:
    result = runner.invoke(app, ["remove", "ghost"])
    assert result.exit_code != 0


def test_list_empty_says_so() -> None:
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "nothing yet" in result.output.lower()


def test_list_shows_oneshots(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory, sample_html: str
) -> None:
    """One-shot ingestions appear in `pulp list` under the ONE-SHOTS section."""
    from pulpline import pipeline

    url = "https://example.com/article"
    client = mock_client_factory({url: sample_html})
    pipeline.add_once(url, client=client)

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "ONE-SHOTS" in result.output
    assert "The End of the Beginning" in result.output  # title from sample_html
    assert "SUBSCRIPTIONS" not in result.output  # no subs configured


def test_list_shows_both_sections(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory, sample_html: str
) -> None:
    """When both a sub and a one-shot exist, both sections render."""
    from pulpline import pipeline

    feed_url = "https://example.com/feed"
    article_url = "https://example.com/article"
    _patch_build_client(monkeypatch, mock_client_factory, {feed_url: _feed()})
    runner.invoke(app, ["add", feed_url, "--feed", "--name", "feedname"])

    client = mock_client_factory({article_url: sample_html})
    pipeline.add_once(article_url, client=client)

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "SUBSCRIPTIONS" in result.output
    assert "feedname" in result.output
    assert "ONE-SHOTS" in result.output
    assert "The End of the Beginning" in result.output


def test_list_shows_subscription(
    monkeypatch: pytest.MonkeyPatch, mock_client_factory: ClientFactory
) -> None:
    feed_url = "https://example.com/feed"
    _patch_build_client(monkeypatch, mock_client_factory, {feed_url: _feed()})
    runner.invoke(app, ["add", feed_url, "--name", "foo"])

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "foo" in result.output
    assert feed_url in result.output
    assert "never" in result.output  # not yet synced


def test_sync_with_no_subscriptions_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_load() -> Config:
        return Config()

    monkeypatch.setattr(cli, "load_config", fake_load)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "no subscriptions" in result.output.lower()


def test_sync_invokes_pipeline_and_summarizes(monkeypatch: pytest.MonkeyPatch) -> None:
    from pulpline import pipeline

    config = Config(subscriptions=(Subscription(name="alpha", source="rss", url="https://a/feed"),))

    def fake_load() -> Config:
        return config

    def fake_sync(config: Config = config, **kwargs: object) -> pipeline.SyncTotal:
        return pipeline.SyncTotal(
            reports=(pipeline.SyncReport(name="alpha", new_items=2, skipped=1, errors=0),)
        )

    monkeypatch.setattr(cli, "load_config", fake_load)
    monkeypatch.setattr(pipeline, "sync", fake_sync)

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "2 new" in result.output
    assert "1 skipped" in result.output
    assert "total: 2 new" in result.output
