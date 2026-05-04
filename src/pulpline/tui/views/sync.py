"""Sync view - one per-subscription list that doubles as run progress.

Idle state: each row shows the last-sync timestamp from SQLite.
During a run: each row morphs to a progress indicator + current item.
Just-finished: the row shows the result counts ('+3 new', '4 paywalled',
or 'no change') alongside its updated timestamp until the next run.

Hit `s` from any tab to start a sync (binding lives at the app level).
The Syncthing block below is unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from pulpline.config import Subscription, load_config
from pulpline.models import ExtractionError, FetchError
from pulpline.pipeline import (
    ProgressReporter,
    SyncReport,
    SyncTotal,
)
from pulpline.pipeline import (
    sync as pipeline_sync,
)
from pulpline.state import SubscriptionState, connect, get_subscription_state
from pulpline.syncthing import (
    SyncthingDevice,
    SyncthingFolder,
    SyncthingStatus,
    get_status,
)
from pulpline.tui.views.base import View


@dataclass(slots=True)
class _SubRun:
    """Per-subscription state for the current/most-recent sync run."""

    total: int = 0
    done: int = 0
    current: str = ""
    finished: bool = False
    new_items: int = 0
    skipped: int = 0
    errors: int = 0
    paywalled: int = 0


@dataclass(slots=True)
class _RunState:
    subs: dict[str, _SubRun] = field(default_factory=dict)
    final: SyncTotal | None = None
    error: str | None = None
    in_flight: bool = False


class SyncView(View):
    DISPLAY_NAME: ClassVar[str] = "Sync"
    ID: ClassVar[str] = "sync"

    DEFAULT_CSS = """
    SyncView {
        layout: vertical;
    }
    SyncView Static {
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._run = _RunState()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(id="sync-pulpline")
            yield Static(id="sync-hint")
            yield Static(id="sync-syncthing")

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        self._render_pulpline()
        self.query_one("#sync-syncthing", Static).update(_syncthing_text(get_status()))

    def action_run_sync(self) -> None:
        if self._run.in_flight:
            return
        # Reset run state for the new run; previous results stop being shown.
        self._run = _RunState(in_flight=True)
        self._render_pulpline()
        self._do_sync()

    @work(thread=True, exclusive=True, group="sync")
    def _do_sync(self) -> None:
        try:
            cfg = load_config()
            reporter = _TuiProgress(self)
            total = pipeline_sync(config=cfg, progress=reporter)
        except (FetchError, ExtractionError) as exc:
            self.app.call_from_thread(self._on_done, None, str(exc))
            return
        except Exception as exc:
            self.app.call_from_thread(self._on_done, None, f"{type(exc).__name__}: {exc}")
            return
        self.app.call_from_thread(self._on_done, total, None)

    # ---- progress callbacks (UI thread) ----

    def _on_sub_started(self, name: str, item_total: int) -> None:
        self._run.subs.setdefault(name, _SubRun()).total = max(item_total, 0)
        self._render_pulpline()

    def _on_item_started(self, name: str, title: str) -> None:
        sub = self._run.subs.setdefault(name, _SubRun())
        sub.current = title[:60]
        self._render_pulpline()

    def _on_item_finished(self, name: str, skipped: bool) -> None:
        sub = self._run.subs.setdefault(name, _SubRun())
        sub.done += 1
        if skipped:
            sub.skipped += 1
        self._render_pulpline()

    def _on_sub_finished(self, name: str, report: SyncReport) -> None:
        sub = self._run.subs.setdefault(name, _SubRun())
        sub.finished = True
        sub.new_items = report.new_items
        sub.skipped = report.skipped
        sub.errors = report.errors
        sub.paywalled = len(report.paywalled)
        sub.current = ""
        self._render_pulpline()

    def _on_done(self, total: SyncTotal | None, error: str | None) -> None:
        self._run.in_flight = False
        self._run.final = total
        self._run.error = error
        self._render_pulpline()

    # ---- rendering ----

    def _render_pulpline(self) -> None:
        self.query_one("#sync-pulpline", Static).update(self._build_pulpline_text())
        self.query_one("#sync-hint", Static).update(self._build_hint_text())

    def _build_pulpline_text(self) -> Text:
        config = load_config()
        text = Text()
        text.append("PULPLINE\n", style="bold")

        if not config.subscriptions:
            text.append(
                "  no subscriptions configured.  add one with `pulp add <url>`.\n",
                style="dim",
            )
            return text

        with connect() as conn:
            states: dict[str, SubscriptionState | None] = {
                sub.name: get_subscription_state(conn, sub.name) for sub in config.subscriptions
            }

        text.append("  Subscriptions:  ")
        text.append_text(self._summary_chips(config.subscriptions, states))
        text.append("\n\n")

        name_w = max((len(sub.name) for sub in config.subscriptions), default=0)
        for sub in config.subscriptions:
            text.append("  ")
            text.append_text(self._row_marker(sub, states.get(sub.name)))
            text.append(f"  {sub.name:<{name_w}}  ")
            text.append_text(self._row_status(sub, states.get(sub.name)))
            text.append("\n")
        return text

    def _summary_chips(
        self,
        subs: tuple[Subscription, ...],
        states: dict[str, SubscriptionState | None],
    ) -> Text:
        if self._run.in_flight:
            done = sum(1 for s in self._run.subs.values() if s.finished)
            return Text.from_markup(f"[cyan]syncing... {done}/{len(subs)}[/]")

        ok = sum(1 for s in subs if _last_status(states.get(s.name)) == "ok")
        err = sum(1 for s in subs if _last_status(states.get(s.name)) == "error")
        pending = len(subs) - ok - err

        parts: list[str] = []
        if ok:
            parts.append(f"[green]{ok} ok[/]")
        if err:
            parts.append(f"[red]{err} error[/]")
        if pending:
            parts.append(f"[dim]{pending} pending[/]")
        return Text.from_markup("  ·  ".join(parts) or "[dim]none[/]")

    def _row_marker(self, sub: Subscription, state: SubscriptionState | None) -> Text:
        run = self._run.subs.get(sub.name)
        if run is not None:
            if not run.finished and self._run.in_flight:
                return Text("►", style="cyan")
            if run.finished:
                if run.errors:
                    return Text("✗", style="red")
                if run.paywalled:
                    return Text("⚠", style="yellow")
                return Text("✓", style="green")
        # Idle: read from DB.
        last = state.last_status if state else None
        if last == "ok":
            return Text("✓", style="green")
        if last == "error":
            return Text("✗", style="red")
        return Text("·", style="dim")

    def _row_status(self, sub: Subscription, state: SubscriptionState | None) -> Text:
        run = self._run.subs.get(sub.name)
        if run is not None:
            if not run.finished and self._run.in_flight:
                progress = f"{run.done}/{run.total}" if run.total else f"{run.done}"
                if run.current:
                    return Text(f"{progress}  {run.current}", style="dim")
                return Text(progress, style="dim")
            if run.finished:
                bits: list[str] = []
                style = "dim"
                if run.new_items:
                    bits.append(f"+{run.new_items} new")
                    style = "green"
                if run.paywalled:
                    bits.append(f"{run.paywalled} paywalled")
                    style = "yellow"
                if run.errors:
                    bits.append(f"{run.errors} error(s)")
                    style = "red"
                if not bits:
                    bits.append("no change")
                return Text(" · ".join(bits), style=style)
        # Idle: timestamp from DB, or "never".
        if state and state.last_synced_at:
            stamp = state.last_synced_at[:16].replace("T", " ")
            text = Text(stamp, style="dim")
            if state.last_error:
                text.append(f"  {state.last_error}", style="red")
            return text
        return Text("never", style="dim")

    def _build_hint_text(self) -> Text:
        text = Text()
        if self._run.error is not None:
            text.append("sync failed: ", style="red bold")
            text.append(self._run.error)
            return text
        if self._run.in_flight:
            text.append("syncing...  ", style="cyan")
            return text
        if self._run.final is not None:
            t = self._run.final
            if t.total_new:
                text.append(f"+{t.total_new} new", style="green")
                text.append("  ")
            if t.total_paywalled:
                text.append(f"{t.total_paywalled} paywalled", style="yellow")
                text.append("  ")
            if t.total_errors:
                text.append(f"{t.total_errors} error(s)", style="red")
                text.append("  ")
            text.append(f"across {len(t.reports)} sub(s)", style="dim")
            text.append("    [press s to re-run]", style="dim")
            return text
        return Text.from_markup("[dim]press [b]s[/] to run a sync[/]")


class _TuiProgress(ProgressReporter):
    """Bridge `pipeline.sync` progress events into the SyncView."""

    def __init__(self, view: SyncView) -> None:
        self._view = view

    def subscription_discovered(self, name: str, item_total: int) -> None:
        self._view.app.call_from_thread(self._view._on_sub_started, name, item_total)

    def item_started(self, name: str, title: str) -> None:
        self._view.app.call_from_thread(self._view._on_item_started, name, title)

    def item_finished(self, name: str, *, skipped: bool = False) -> None:
        self._view.app.call_from_thread(self._view._on_item_finished, name, skipped)

    def subscription_finished(self, name: str, report: SyncReport) -> None:
        self._view.app.call_from_thread(self._view._on_sub_finished, name, report)


def _last_status(state: SubscriptionState | None) -> str | None:
    return state.last_status if state else None


def _syncthing_text(status: SyncthingStatus) -> Text:
    text = Text()
    text.append("\nSYNCTHING\n", style="bold")

    if not status.installed:
        text.append(
            "  not detected. install Syncthing if you want pulpline's output\n"
            "  pushed to your reader automatically.\n",
            style="dim",
        )
        return text

    if not status.running:
        text.append(
            "  installed, but the daemon does not respond. start it with\n"
            "  `brew services start syncthing` or `syncthing` in another terminal.\n",
            style="yellow",
        )
        return text

    version_str = f" {status.version}" if status.version else ""
    text.append("  Daemon:  ")
    text.append("running", style="green")
    text.append(version_str)
    if status.my_id:
        text.append("   ID: ")
        text.append(f"{status.my_id[:7]}-...", style="dim")
    text.append("\n\n")

    remote_devices = [d for d in status.devices if not d.is_self]
    if remote_devices:
        text.append("  Devices:\n")
        name_w = max((len(d.name) for d in remote_devices), default=0)
        for dev in remote_devices:
            text.append_text(_device_line(dev, name_w))
    else:
        text.append("  Devices:  ")
        text.append("none paired", style="dim")
        text.append("\n")

    if status.folders:
        text.append("\n  Folders:\n")
        for folder in status.folders:
            text.append_text(_folder_line(folder, status.my_id))
    else:
        text.append("\n  Folders:  ")
        text.append("none configured", style="dim")
        text.append("\n")

    return text


def _device_line(dev: SyncthingDevice, name_w: int) -> Text:
    line = Text("    ")
    if dev.online:
        line.append("● ", style="green")
        line.append(f"{dev.name:<{name_w}}  ")
        line.append(f"{dev.conn_type or 'unknown':<10}  ", style="dim")
        line.append(dev.address or "", style="dim")
    else:
        line.append("○ ", style="dim")
        line.append(f"{dev.name:<{name_w}}  ", style="dim")
        line.append("offline", style="dim")
    line.append("\n")
    return line


def _folder_line(folder: SyncthingFolder, my_id: str | None) -> Text:
    line = Text("    ")
    line.append(folder.label, style="bold")
    if folder.folder_type:
        line.append(f"  ({folder.folder_type})", style="dim")
    if folder.path:
        line.append(f"  {folder.path}", style="dim")
    line.append("\n")

    remote_ids = [did for did in folder.device_ids if did != my_id]
    if remote_ids:
        sharing = ", ".join(did[:7] for did in remote_ids)
        line.append(f"      shared with: {sharing}\n", style="dim")
    return line
