"""Subscriptions view - per-subscription health and management."""

from __future__ import annotations

from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.widgets import DataTable, Static

from pulpline import pipeline
from pulpline.config import (
    ConfigError,
    Subscription,
    load_config,
    remove_subscription,
    save_config,
)
from pulpline.state import (
    SubscriptionState,
    connect,
    count_by_subscription,
    get_subscription_state,
)
from pulpline.tui.views.base import View

_BACKFILL_DEFAULT_POSTS = 50


class SubscriptionsView(View):
    DISPLAY_NAME: ClassVar[str] = "Subscriptions"
    ID: ClassVar[str] = "subscriptions"

    DEFAULT_CSS = """
    SubscriptionsView {
        layout: vertical;
    }
    SubscriptionsView DataTable {
        height: 1fr;
    }
    SubscriptionsView #subs-detail {
        height: 3;
        padding: 0 1;
        color: $text-muted;
    }
    SubscriptionsView #subs-empty {
        height: 1fr;
        padding: 2;
        color: $text-muted;
        content-align: center middle;
    }
    """

    BINDINGS = [  # noqa: RUF012
        ("d", "delete_selected", "Delete"),
        ("b", "backfill_selected", "Backfill"),
    ]

    def compose(self) -> ComposeResult:
        yield DataTable(id="subs-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="subs-detail")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Name", "Source", "Items", "Last Sync", "Status", "URL")
        self._subs: list[Subscription] = []
        self._states: dict[str, SubscriptionState | None] = {}
        self.refresh_data()

    def refresh_data(self) -> None:
        config = load_config()
        with connect() as conn:
            counts = count_by_subscription(conn)
            self._states = {
                sub.name: get_subscription_state(conn, sub.name) for sub in config.subscriptions
            }

        self._subs = list(config.subscriptions)

        table = self.query_one(DataTable)
        detail = self.query_one("#subs-detail", Static)
        table.clear()

        if not self._subs:
            detail.update("no subscriptions yet. `pulp add <url>` to get started.")
            return

        for sub in self._subs:
            state = self._states.get(sub.name)
            count = counts.get(sub.name, 0)
            last_sync = (
                _short_iso(state.last_synced_at) if state and state.last_synced_at else "never"
            )
            status = (state.last_status if state else None) or "-"
            table.add_row(
                sub.name,
                sub.source,
                str(count),
                last_sync,
                status,
                sub.url,
            )
        detail.update("")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        del event  # we read the cursor directly below
        self._update_detail()

    def _update_detail(self) -> None:
        table = self.query_one(DataTable)
        detail = self.query_one("#subs-detail", Static)
        row = table.cursor_row
        if row is None or row >= len(self._subs):
            detail.update("")
            return
        sub = self._subs[row]
        state = self._states.get(sub.name)
        if state and state.last_error:
            detail.update(f"[red]last error:[/red] {state.last_error}")
        else:
            detail.update("")

    def action_delete_selected(self) -> None:
        table = self.query_one(DataTable)
        row = table.cursor_row
        if row is None or row >= len(self._subs):
            return
        sub = self._subs[row]
        config = load_config()
        try:
            new_config = remove_subscription(config, sub.name)
        except ConfigError:
            return
        save_config(new_config)
        self.refresh_data()
        self.notify(f"removed {sub.name!r}", severity="information")

    def action_backfill_selected(self) -> None:
        """Pull older posts of the highlighted subscription.

        Fixed default of 50 posts so a press doesn't accidentally pull
        a decade-long archive. For more or less, use `pulp backfill` from
        the shell with `--posts N` (or 0 for unlimited).
        """
        table = self.query_one(DataTable)
        row = table.cursor_row
        if row is None or row >= len(self._subs):
            return
        sub = self._subs[row]
        self.notify(
            f"backfilling up to {_BACKFILL_DEFAULT_POSTS} older posts of {sub.name!r}...",
            timeout=4,
        )
        self._do_backfill(sub)

    @work(thread=True, exclusive=True, group="backfill")
    def _do_backfill(self, sub: Subscription) -> None:
        """Runs in a worker thread - backfill is network-bound and can take a while."""
        try:
            report = pipeline.backfill(sub, max_new=_BACKFILL_DEFAULT_POSTS)
        except pipeline.BackfillUnsupported as exc:
            self.app.call_from_thread(
                self.app.notify,
                str(exc),
                severity="warning",
                timeout=8,
            )
            return
        except Exception as exc:
            self.app.call_from_thread(
                self.app.notify,
                f"backfill failed: {type(exc).__name__}: {exc}",
                severity="error",
                timeout=8,
            )
            return

        bits = [f"+{report.new_items} from {sub.name}"]
        if report.skipped_already_ingested:
            bits.append(f"{report.skipped_already_ingested} dedup'd")
        if report.errors:
            bits.append(f"{report.errors} err")
        bits.append(f"(stopped: {report.stopped_reason})")
        self.app.call_from_thread(
            self.app.notify,
            ", ".join(bits),
            severity="information",
            timeout=6,
        )
        # Refresh DataTable in the UI thread so item-count column reflects the new arrivals.
        self.app.call_from_thread(self.refresh_data)


def _short_iso(iso: str) -> str:
    """Trim to YYYY-MM-DD HH:MM if present, else first 16 chars."""
    if len(iso) >= 16:
        return f"{iso[:10]} {iso[11:16]}"
    return iso
