"""Sync view - pulpline ingestion status + syncthing delivery status.

Pulpline writes to a folder; syncthing pushes that folder to the reader.
This view shows both halves so "is everything flowing?" is one tab away.
Syncthing data is best-effort - the view degrades gracefully when syncthing
is not installed or its daemon is not running.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from pulpline.config import load_config
from pulpline.state import connect, get_subscription_state
from pulpline.syncthing import (
    SyncthingDevice,
    SyncthingFolder,
    SyncthingStatus,
    get_status,
)
from pulpline.tui.views.base import View


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

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(id="sync-pulpline")
            yield Static(id="sync-syncthing")

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        self.query_one("#sync-pulpline", Static).update(_pulpline_text())
        self.query_one("#sync-syncthing", Static).update(_syncthing_text(get_status()))


def _pulpline_text() -> Text:
    config = load_config()
    text = Text()
    text.append("PULPLINE\n", style="bold")

    if not config.subscriptions:
        text.append(
            "  no subscriptions configured.  add one with `pulp add <url>`.\n",
            style="dim",
        )
        return text

    rows: list[tuple[str, str | None, str | None, str | None]] = []
    with connect() as conn:
        for sub in config.subscriptions:
            state = get_subscription_state(conn, sub.name)
            rows.append(
                (
                    sub.name,
                    state.last_status if state else None,
                    state.last_synced_at if state else None,
                    state.last_error if state else None,
                )
            )

    ok_count = sum(1 for _, status, _, _ in rows if status == "ok")
    err_count = sum(1 for _, status, _, _ in rows if status == "error")
    pending = len(rows) - ok_count - err_count

    summary_parts = []
    if ok_count:
        summary_parts.append(f"[green]{ok_count} ok[/green]")
    if err_count:
        summary_parts.append(f"[red]{err_count} error[/red]")
    if pending:
        summary_parts.append(f"[dim]{pending} pending[/dim]")
    text.append("  Subscriptions:  ")
    text.append(Text.from_markup("  ·  ".join(summary_parts) or "[dim]none[/dim]"))
    text.append("\n\n")

    name_w = max((len(name) for name, *_ in rows), default=0)
    for name, status, last_synced, error in rows:
        status_marker = _status_marker(status)
        last = last_synced[:16].replace("T", " ") if last_synced else "never"
        text.append("  ")
        text.append_text(status_marker)
        text.append(f"  {name:<{name_w}}  ")
        text.append(last, style="dim")
        if error:
            text.append(f"  {error}", style="red")
        text.append("\n")
    return text


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


def _status_marker(status: str | None) -> Text:
    if status == "ok":
        return Text("✓", style="green")
    if status == "error":
        return Text("✗", style="red")
    return Text("·", style="dim")


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
