"""Library view - browse all ingested items, grouped by subscription."""

from __future__ import annotations

import os
import platform
import subprocess
from collections import defaultdict
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Tree
from textual.widgets.tree import TreeNode

from pulpline.state import LibraryItem, connect, delete_item, list_items
from pulpline.tui.views.base import View


class LibraryView(View):
    DISPLAY_NAME: ClassVar[str] = "Library"
    ID: ClassVar[str] = "library"

    BINDINGS = [  # noqa: RUF012
        ("/", "focus_filter", "Filter"),
        ("enter", "open_file", "Open"),
        ("d", "delete_file", "Delete"),
    ]

    DEFAULT_CSS = """
    LibraryView {
        layout: vertical;
    }
    LibraryView Input {
        margin: 0 0 1 0;
    }
    LibraryView Tree {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Input(placeholder="filter by title or url...", id="library-filter")
            yield Tree[LibraryItem]("Library", id="library-tree")

    def on_mount(self) -> None:
        tree: Tree[LibraryItem] = self.query_one(Tree)
        tree.show_root = False
        tree.guide_depth = 3
        self.refresh_data()

    def refresh_data(self) -> None:
        filter_input = self.query_one("#library-filter", Input)
        filter_text = filter_input.value.strip() or None
        with connect() as conn:
            items = list_items(conn, limit=500, filter_text=filter_text)

        # Group items by their on-disk folder name: subscription_name, or
        # "oneshots" for items without a subscription. Mirrors the layout
        # `pulp migrate` produces under `paths.output_dir`.
        groups: dict[str, list[LibraryItem]] = defaultdict(list)
        for item in items:
            bucket = item.subscription_name or "oneshots"
            groups[bucket].append(item)

        tree: Tree[LibraryItem] = self.query_one(Tree)
        tree.clear()
        if not groups:
            tree.root.add_leaf("(empty - add something with `pulp add <url>`)")
            return

        # oneshots first, then subscription names alphabetically.
        ordered = sorted(groups.keys(), key=lambda k: (k != "oneshots", k))
        for bucket in ordered:
            bucket_items = groups[bucket]
            group_node: TreeNode[LibraryItem] = tree.root.add(
                f"{bucket}  ({len(bucket_items)})",
                expand=True,
            )
            for item in bucket_items:
                date = item.ingested_at[:10] if item.ingested_at else ""
                title = item.title or "(untitled)"
                group_node.add_leaf(f"{date}  {title}", data=item)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "library-filter":
            self.refresh_data()

    def action_focus_filter(self) -> None:
        self.query_one("#library-filter", Input).focus()

    def action_open_file(self) -> None:
        item = self._selected_item()
        if item and item.output_path:
            _open_file(item.output_path)

    def action_delete_file(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        title = item.title or "(untitled)"
        with connect() as conn:
            delete_item(conn, item.id)
        self.refresh_data()
        self.notify(f"deleted {title!r}", severity="information")

    def _selected_item(self) -> LibraryItem | None:
        """Return the LibraryItem under the cursor, or None when on a group node."""
        tree: Tree[LibraryItem] = self.query_one(Tree)
        node = tree.cursor_node
        if node is None or not isinstance(node.data, LibraryItem):
            return None
        return node.data


def _open_file(path: str) -> None:
    """Best-effort open with the OS default app. Silent on failure."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["/usr/bin/open", path])
        elif system == "Linux":
            subprocess.Popen(["xdg-open", path])
        elif system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
    except OSError:
        pass
