"""Subscriptions view - per-subscription health and management."""

from __future__ import annotations

from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Label, Static

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


class BackfillPromptScreen(ModalScreen[int | None]):
    """Asks how many older posts to fetch. Returns the count, or None on cancel.

    Pre-fills with the last sensible default so a quick Enter accepts;
    typing a different number overrides. 0 means 'walk the whole archive'.
    """

    DEFAULT_CSS = """
    BackfillPromptScreen {
        align: center middle;
    }
    BackfillPromptScreen > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 60;
        height: auto;
    }
    BackfillPromptScreen Label {
        padding: 0 0 1 0;
    }
    BackfillPromptScreen #backfill-hint {
        color: $text-muted;
        padding: 1 0 0 0;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, sub_name: str, default_count: int = _BACKFILL_DEFAULT_POSTS) -> None:
        super().__init__()
        self._sub_name = sub_name
        self._default = default_count

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Backfill {self._sub_name!r}: how many older posts?")
            yield Input(value=str(self._default), id="backfill-count")
            yield Label("Enter to confirm  ·  Esc to cancel  ·  0 = unlimited", id="backfill-hint")

    def on_mount(self) -> None:
        # Focus the input and put the cursor at the end so backspace clears
        # the default if the user wants to type a different number.
        count_input = self.query_one("#backfill-count", Input)
        count_input.focus()
        count_input.action_end()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        try:
            count = int(raw)
        except ValueError:
            self.notify(f"not a number: {raw!r}", severity="warning")
            return
        if count < 0:
            self.notify("count must be 0 or higher", severity="warning")
            return
        self.dismiss(count)

    def action_cancel(self) -> None:
        self.dismiss(None)


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
        """Open a modal asking how many older posts to fetch, then run."""
        table = self.query_one(DataTable)
        row = table.cursor_row
        if row is None or row >= len(self._subs):
            return
        sub = self._subs[row]

        def on_count(count: int | None) -> None:
            if count is None:
                return  # user pressed Esc
            max_new = count if count > 0 else None
            label = "unlimited" if max_new is None else f"up to {max_new}"
            self.notify(
                f"backfilling {label} older posts of {sub.name!r}...",
                timeout=4,
            )
            self._do_backfill(sub, max_new)

        self.app.push_screen(BackfillPromptScreen(sub.name), on_count)

    @work(thread=True, exclusive=True, group="backfill")
    def _do_backfill(self, sub: Subscription, max_new: int | None) -> None:
        """Runs in a worker thread - backfill is network-bound and can take a while."""
        try:
            report = pipeline.backfill(sub, max_new=max_new)
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

        message, severity = _backfill_outcome_message(sub.name, report)
        self.app.call_from_thread(
            self.app.notify,
            message,
            severity=severity,
            timeout=8,
        )
        # Refresh DataTable in the UI thread so item-count column reflects the new arrivals.
        self.app.call_from_thread(self.refresh_data)


def _backfill_outcome_message(
    sub_name: str, report: pipeline.BackfillReport
) -> tuple[str, str]:
    """Turn a BackfillReport into a Telegram-grade one-liner + severity.

    The three outcomes that matter to the user, in priority order:

      1. 'nothing to add' (new=0, exhausted): the user asked for posts but
         the publication's archive is fully drained. Be loud about this so
         they don't think it's a bug.
      2. 'partial - more available' (new>0, max_new): the limit fired before
         the archive ran out. There's more if you want it.
      3. 'partial - fully drained' (new>0, exhausted): the user got the
         remaining posts; the archive has nothing left.

    The 'since' case is rare in the TUI (no date prompt in the modal yet)
    so it falls back to a generic message.
    """
    base_count = report.skipped_already_ingested + report.new_items
    if report.new_items == 0 and report.stopped_reason == "exhausted":
        return (
            f"{sub_name}: nothing more to fetch - "
            f"full archive ({base_count} posts) already ingested",
            "information",
        )

    bits = [f"+{report.new_items} from {sub_name}"]
    if report.stopped_reason == "max_new":
        bits.append("archive has more (raise --posts to get them)")
    elif report.stopped_reason == "exhausted":
        bits.append(f"full archive now ingested ({base_count} posts)")
    elif report.stopped_reason == "since":
        bits.append("stopped at --since date")

    if report.errors:
        bits.append(f"{report.errors} error(s)")
    severity = "warning" if report.errors else "information"
    return ", ".join(bits), severity


def _short_iso(iso: str) -> str:
    """Trim to YYYY-MM-DD HH:MM if present, else first 16 chars."""
    if len(iso) >= 16:
        return f"{iso[:10]} {iso[11:16]}"
    return iso
