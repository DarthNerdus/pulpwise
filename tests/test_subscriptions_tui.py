"""Interaction tests for destination management in the Subscriptions TUI."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.notifications import Notify
from textual.pilot import Pilot
from textual.widgets import DataTable, OptionList, TabbedContent

from pulpwise import pipeline
from pulpwise.config import Config, Subscription, load_config, save_config
from pulpwise.tui.app import PulpwiseApp
from pulpwise.tui.views import SubscriptionsView
from pulpwise.tui.views import subscriptions as subscriptions_view
from pulpwise.tui.views.subscriptions import DestinationPromptScreen


def _subscription(
    *, location: str | int | None = None, tags: str = "tech", disabled: bool = False
) -> Subscription:
    options: dict[str, str | int] = {"tags": tags}
    if location is not None:
        options["location"] = location
    return Subscription(
        name="feed-one",
        source="rss",
        url="https://example.com/feed",
        options=options,
        disabled=disabled,
    )


def _message_hook(messages: list[str]) -> Callable[[object], None]:
    def capture(message: object) -> None:
        if isinstance(message, Notify):
            notification = message.notification
            messages.append(f"{notification.severity}:{notification.message}")

    return capture


async def _open_destination(app: PulpwiseApp, pilot: Pilot[None]) -> DestinationPromptScreen:
    app.query_one(TabbedContent).active = SubscriptionsView.ID
    await pilot.pause()
    table = app.query_one("#subs-table", DataTable)
    table.focus()
    await pilot.press("l")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, DestinationPromptScreen)
    return screen


def test_destination_modal_persists_inbox_and_preserves_options() -> None:
    save_config(Config(subscriptions=(_subscription(),)))
    messages: list[str] = []

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(
            notifications=True,
            size=(120, 40),
            message_hook=_message_hook(messages),
        ) as pilot:
            screen = await _open_destination(app, pilot)
            options = screen.query_one(OptionList)
            assert [(option.id, str(option.prompt)) for option in options.options] == [
                ("feed", "Feed"),
                ("new", "Inbox"),
                ("later", "Later"),
            ]
            await pilot.press("down", "enter")
            await pilot.pause()
            table = app.query_one("#subs-table", DataTable)
            assert [str(column.label) for column in table.columns.values()] == [
                "Name",
                "Source",
                "Destination",
                "Items",
                "Last Sync",
                "Status",
                "URL",
            ]
            assert table.get_row_at(0)[2] == "Inbox"
            assert table.cursor_row == 0

    asyncio.run(scenario())

    updated = load_config().find("feed-one")
    assert updated is not None
    assert updated.options == {"tags": "tech", "location": "new"}
    assert any("Inbox" in message and "future jobs" in message for message in messages)


@pytest.mark.parametrize("location", ["archive", "", 42])
def test_unsupported_destination_has_no_default_and_enter_does_not_write(
    location: str | int,
) -> None:
    save_config(Config(subscriptions=(_subscription(location=location),)))
    target = Path(os.environ["PULPWISE_CONFIG_PATH"])
    before = target.read_bytes()

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_destination(app, pilot)
            options = app.screen.query_one(OptionList)
            assert options.highlighted is None
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, DestinationPromptScreen)
            await pilot.press("escape")

    asyncio.run(scenario())

    assert target.read_bytes() == before


def test_legacy_archive_can_be_explicitly_repaired() -> None:
    save_config(Config(subscriptions=(_subscription(location="archive"),)))

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_destination(app, pilot)
            await pilot.press("down", "enter")
            await pilot.pause()

    asyncio.run(scenario())

    assert load_config().find("feed-one").option("location") == "feed"  # type: ignore[union-attr]


def test_destination_save_uses_fresh_config_and_preserves_unrelated_edits() -> None:
    neighbor = Subscription(name="neighbor", source="rss", url="https://neighbor.example/feed")
    save_config(Config(subscriptions=(_subscription(), neighbor)))

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_destination(app, pilot)
            fresh = Config(
                subscriptions=(
                    _subscription(tags="fresh", disabled=True),
                    Subscription(
                        name="neighbor",
                        source="rss",
                        url="https://neighbor.example/feed",
                        options={"tags": "also-fresh"},
                    ),
                )
            )
            save_config(fresh)
            await pilot.press("down", "enter")
            await pilot.pause()

    asyncio.run(scenario())

    updated = load_config()
    selected = updated.find("feed-one")
    assert selected is not None
    assert selected.options == {"tags": "fresh", "location": "new"}
    assert selected.disabled is True
    assert updated.find("neighbor").options == {"tags": "also-fresh"}  # type: ignore[union-attr]


def test_destination_conflict_preserves_external_location_change() -> None:
    save_config(Config(subscriptions=(_subscription(),)))
    messages: list[str] = []

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(
            notifications=True, size=(120, 40), message_hook=_message_hook(messages)
        ) as pilot:
            await _open_destination(app, pilot)
            save_config(Config(subscriptions=(_subscription(location="later"),)))
            await pilot.press("down", "enter")
            await pilot.pause()

    asyncio.run(scenario())

    assert load_config().find("feed-one").option("location") == "later"  # type: ignore[union-attr]
    assert any(message.startswith("warning:") and "changed" in message for message in messages)
    assert not any("future jobs" in message for message in messages)


def test_fresh_equivalent_destination_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    save_config(Config(subscriptions=(_subscription(),)))

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_destination(app, pilot)
            save_config(Config(subscriptions=(_subscription(location="new"),)))

            def unexpected_save(config: Config, path: Path | None = None) -> None:
                del config, path
                raise AssertionError("equivalent destination must not be rewritten")

            monkeypatch.setattr(subscriptions_view, "save_config", unexpected_save)
            await pilot.press("down", "enter")
            await pilot.pause()

    asyncio.run(scenario())

    assert load_config().find("feed-one").option("location") == "new"  # type: ignore[union-attr]


@pytest.mark.parametrize("initial", [None, "feed", "inbox", "new"])
def test_current_effective_destination_does_not_save(
    initial: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_config(Config(subscriptions=(_subscription(location=initial),)))

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_destination(app, pilot)

            def unexpected_save(config: Config, path: Path | None = None) -> None:
                del config, path
                raise AssertionError("current destination must not be rewritten")

            monkeypatch.setattr(subscriptions_view, "save_config", unexpected_save)
            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(scenario())


def test_deleted_config_is_not_recreated_on_confirmation() -> None:
    save_config(Config(subscriptions=(_subscription(),)))
    target = Path(os.environ["PULPWISE_CONFIG_PATH"])
    messages: list[str] = []

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(
            notifications=True, size=(120, 40), message_hook=_message_hook(messages)
        ) as pilot:
            await _open_destination(app, pilot)
            target.unlink()
            await pilot.press("down", "enter")
            await pilot.pause()

    asyncio.run(scenario())

    assert not target.exists()
    assert any(message.startswith("error:could not load config") for message in messages)
    assert not any("future jobs" in message for message in messages)


def test_save_failure_keeps_existing_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    save_config(Config(subscriptions=(_subscription(),)))
    messages: list[str] = []

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(
            notifications=True, size=(120, 40), message_hook=_message_hook(messages)
        ) as pilot:
            await _open_destination(app, pilot)

            def fail_save(config: Config, path: Path | None = None) -> None:
                del config, path
                raise OSError("disk full")

            monkeypatch.setattr(subscriptions_view, "save_config", fail_save)
            await pilot.press("down", "enter")
            await pilot.pause()

    asyncio.run(scenario())

    assert load_config().find("feed-one").option("location") is None  # type: ignore[union-attr]
    assert any(message.startswith("error:could not save destination") for message in messages)
    assert not any("future jobs" in message for message in messages)


@pytest.mark.parametrize(
    ("initial", "keys", "expected"),
    [
        ("new", ("up", "enter"), "feed"),
        (None, ("down", "down", "enter"), "later"),
    ],
)
def test_destination_modal_persists_other_supported_choices(
    initial: str | None, keys: tuple[str, ...], expected: str
) -> None:
    save_config(Config(subscriptions=(_subscription(location=initial),)))

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_destination(app, pilot)
            await pilot.press(*keys)
            await pilot.pause()

    asyncio.run(scenario())

    assert load_config().find("feed-one").option("location") == expected  # type: ignore[union-attr]


def test_destination_modal_escape_cancels_supported_choice() -> None:
    save_config(Config(subscriptions=(_subscription(location="new"),)))
    target = Path(os.environ["PULPWISE_CONFIG_PATH"])
    before = target.read_bytes()

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_destination(app, pilot)
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(scenario())

    assert target.read_bytes() == before


@pytest.mark.parametrize(
    "malformed", ["[[subscriptions]\n", 'subscriptions = "bad"\n', b"\xff\xfe"]
)
def test_malformed_config_is_preserved_on_confirmation(malformed: str | bytes) -> None:
    save_config(Config(subscriptions=(_subscription(),)))
    target = Path(os.environ["PULPWISE_CONFIG_PATH"])
    messages: list[str] = []

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(
            notifications=True, size=(120, 40), message_hook=_message_hook(messages)
        ) as pilot:
            await _open_destination(app, pilot)
            if isinstance(malformed, bytes):
                target.write_bytes(malformed)
            else:
                target.write_text(malformed, encoding="utf-8")
            await pilot.press("down", "enter")
            await pilot.pause()

    asyncio.run(scenario())

    assert target.read_bytes() == (
        malformed if isinstance(malformed, bytes) else malformed.encode("utf-8")
    )
    assert any(message.startswith("error:could not load config") for message in messages)
    assert not any("future jobs" in message for message in messages)


def test_changed_subscription_identity_is_not_mutated() -> None:
    save_config(Config(subscriptions=(_subscription(),)))
    messages: list[str] = []

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(
            notifications=True, size=(120, 40), message_hook=_message_hook(messages)
        ) as pilot:
            await _open_destination(app, pilot)
            replacement = Subscription(
                name="feed-one",
                source="rss",
                url="https://replacement.example/feed",
                options={"tags": "replacement"},
            )
            save_config(Config(subscriptions=(replacement,)))
            await pilot.press("down", "enter")
            await pilot.pause()

    asyncio.run(scenario())

    replacement = load_config().find("feed-one")
    assert replacement is not None
    assert replacement.url == "https://replacement.example/feed"
    assert replacement.option("location") is None
    assert any(message.startswith("warning:") and "changed" in message for message in messages)
    assert not any("future jobs" in message for message in messages)


def test_refresh_failure_happens_after_destination_is_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_config(Config(subscriptions=(_subscription(),)))
    messages: list[str] = []

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(
            notifications=True,
            size=(120, 40),
            message_hook=_message_hook(messages),
        ) as pilot:
            await _open_destination(app, pilot)
            view = app.query_one(SubscriptionsView)

            def fail_refresh() -> None:
                raise RuntimeError("state unavailable")

            monkeypatch.setattr(view, "refresh_data", fail_refresh)
            await pilot.press("down", "enter")
            await pilot.pause()

    asyncio.run(scenario())

    assert load_config().find("feed-one").option("location") == "new"  # type: ignore[union-attr]
    assert any("saved, but display refresh failed" in message for message in messages)


def test_backfill_after_refresh_failure_uses_persisted_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_config(Config(subscriptions=(_subscription(),)))
    seen_locations: list[str | int | None] = []

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_destination(app, pilot)
            view = app.query_one(SubscriptionsView)
            original_refresh = view.refresh_data

            def fail_refresh() -> None:
                raise RuntimeError("state unavailable")

            monkeypatch.setattr(view, "refresh_data", fail_refresh)
            await pilot.press("down", "enter")
            await pilot.pause()
            monkeypatch.setattr(view, "refresh_data", original_refresh)

            def fake_backfill(
                sub: Subscription, *args: object, **kwargs: object
            ) -> pipeline.BackfillReport:
                del args
                assert isinstance(kwargs.get("config"), Config)
                seen_locations.append(sub.option("location"))
                return pipeline.BackfillReport(
                    name=sub.name,
                    new_items=0,
                    skipped_already_ingested=0,
                    errors=0,
                    pages_walked=1,
                    stopped_reason="exhausted",
                )

            monkeypatch.setattr(pipeline, "backfill", fake_backfill)
            app.query_one("#subs-table", DataTable).focus()
            await pilot.press("b")
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(scenario())

    assert seen_locations == ["new"]


def test_superseding_destination_edit_suppresses_false_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_config(Config(subscriptions=(_subscription(),)))
    messages: list[str] = []

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(
            notifications=True, size=(120, 40), message_hook=_message_hook(messages)
        ) as pilot:
            await _open_destination(app, pilot)
            view = app.query_one(SubscriptionsView)
            original_refresh = view.refresh_data

            def superseding_refresh() -> None:
                save_config(Config(subscriptions=(_subscription(location="later"),)))
                original_refresh()

            monkeypatch.setattr(view, "refresh_data", superseding_refresh)
            await pilot.press("down", "enter")
            await pilot.pause()
            assert app.query_one("#subs-table", DataTable).get_row_at(0)[2] == "Later"

    asyncio.run(scenario())

    assert load_config().find("feed-one").option("location") == "later"  # type: ignore[union-attr]
    assert any(message.startswith("warning:") and "superseded" in message for message in messages)
    assert not any("→ Inbox for future jobs" in message for message in messages)


def test_removed_subscription_is_not_recreated() -> None:
    save_config(Config(subscriptions=(_subscription(),)))
    messages: list[str] = []

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(
            notifications=True, size=(120, 40), message_hook=_message_hook(messages)
        ) as pilot:
            await _open_destination(app, pilot)
            save_config(Config())
            await pilot.press("down", "enter")
            await pilot.pause()

    asyncio.run(scenario())

    assert load_config().find("feed-one") is None
    assert any(message.startswith("warning:") and "changed" in message for message in messages)
    assert not any("future jobs" in message for message in messages)


def test_destination_binding_with_empty_table_is_a_no_op() -> None:
    save_config(Config())
    target = Path(os.environ["PULPWISE_CONFIG_PATH"])
    before = target.read_bytes()

    async def scenario() -> None:
        app = PulpwiseApp()
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(TabbedContent).active = SubscriptionsView.ID
            await pilot.pause()
            app.query_one("#subs-table", DataTable).focus()
            await pilot.press("l")
            await pilot.pause()
            assert not isinstance(app.screen, DestinationPromptScreen)

    asyncio.run(scenario())

    assert target.read_bytes() == before
