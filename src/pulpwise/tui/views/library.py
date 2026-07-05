"""Library view - browse all ingested items, grouped by subscription."""

from __future__ import annotations

import webbrowser
from collections import defaultdict
from typing import ClassVar

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Tree
from textual.widgets.tree import TreeNode

from pulpwise.state import (
    LibraryItem,
    connect,
    delete_item,
    get_subscription_state,
    list_items,
    list_recently_deleted,
)
from pulpwise.tui.views.base import View

# Tree node payloads: leaf rows carry their LibraryItem; group nodes carry the
# plain bucket key (subscription name / "oneshots") so expand state survives a
# rebuild without parsing rendered labels back apart.
_NodeData = LibraryItem | str


class LibraryView(View):
    DISPLAY_NAME: ClassVar[str] = "Library"
    ID: ClassVar[str] = "library"

    BINDINGS = [  # noqa: RUF012
        ("/", "focus_filter", "Filter"),
        ("enter", "open_item", "Open"),
        ("d", "delete_item", "Delete"),
        ("D", "toggle_deleted", "Deleted"),
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

    def __init__(self) -> None:
        super().__init__()
        # Library has two modes: live items (default) and the tombstone view.
        # The same Tree is reused so collapse state, cursor focus, and filter
        # text continue to feel like one widget.
        self._show_deleted = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Input(placeholder="filter by title or url...", id="library-filter")
            yield Tree[_NodeData]("Library", id="library-tree")

    def on_mount(self) -> None:
        tree: Tree[_NodeData] = self.query_one(Tree)
        tree.show_root = False
        tree.guide_depth = 3
        self.refresh_data()
        # Focus the Tree (not the filter Input) so view-level bindings -
        # `d`, `D`, `enter` - dispatch when you press them. Without this,
        # focus lands on the Input by compose-order default and every
        # keystroke gets eaten by the filter field.
        tree.focus()

    def on_show(self) -> None:
        """When switching back to the Library tab, restore Tree focus.

        Tab switching doesn't re-fire on_mount, so without this the Tree
        only has focus on first load - subsequent visits would land focus
        somewhere unhelpful and view bindings would stop showing in the
        footer.
        """
        self.query_one(Tree).focus()

    def refresh_data(self) -> None:
        filter_input = self.query_one("#library-filter", Input)
        filter_text = filter_input.value.strip() or None
        totals: dict[str, int | None] = {}
        # No limit: the library must show everything. A cap here turns
        # deletes into whack-a-mole - each removal slides the next-oldest
        # item into the window, so the list never visibly shrinks.
        with connect() as conn:
            if self._show_deleted:
                items = list_recently_deleted(conn, limit=None, filter_text=filter_text)
            else:
                items = list_items(conn, limit=None, filter_text=filter_text)
                for bucket in {item.subscription_name for item in items if item.subscription_name}:
                    state = get_subscription_state(conn, bucket)
                    totals[bucket] = state.total_items if state else None

        groups: dict[str, list[LibraryItem]] = defaultdict(list)
        for item in items:
            bucket = item.subscription_name or "oneshots"
            groups[bucket].append(item)

        tree: Tree[_NodeData] = self.query_one(Tree)
        # Tree.clear() drops expand state, so snapshot which buckets the
        # user had expanded (group nodes carry their bucket key as node
        # data) and re-apply after rebuild. New buckets default to
        # collapsed - the user explicitly asked for that; long lists
        # like 'substack-saves' clutter the view otherwise. Manually-
        # expanded buckets stay expanded across refresh/delete actions.
        expanded_buckets = {
            node.data
            for node in tree.root.children
            if node.is_expanded and isinstance(node.data, str)
        }
        tree.clear()
        if not groups:
            empty_msg = (
                "(nothing deleted yet)"
                if self._show_deleted
                else "(empty - add something with `pulpwise add <url>`)"
            )
            tree.root.add_leaf(empty_msg)
            return

        ordered = sorted(groups.keys(), key=lambda k: (k != "oneshots", k))
        for bucket in ordered:
            bucket_items = groups[bucket]
            count_label = _count_label(len(bucket_items), totals.get(bucket))
            group_node: TreeNode[_NodeData] = tree.root.add(
                _group_label(bucket, count_label, deleted=self._show_deleted),
                data=bucket,
                expand=bucket in expanded_buckets,
            )
            for item in bucket_items:
                group_node.add_leaf(_item_label(item, deleted=self._show_deleted), data=item)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "library-filter":
            self.refresh_data()

    def on_tree_node_selected(self, event: Tree.NodeSelected[_NodeData]) -> None:
        """Open the item under the cursor when its row is selected.

        The view-level ("enter", "open_item") binding never fires while the
        Tree has focus: the Tree's own `enter` binding (select_cursor)
        shadows it. So opening hangs off the NodeSelected message the Tree
        posts instead. Group headings and the empty-state leaf carry no
        LibraryItem and are ignored - select just toggles expansion there.
        """
        if isinstance(event.node.data, LibraryItem):
            self._open(event.node.data)

    def action_focus_filter(self) -> None:
        self.query_one("#library-filter", Input).focus()

    def action_open_item(self) -> None:
        item = self._selected_item()
        if item is not None:
            self._open(item)

    def _open(self, item: LibraryItem) -> None:
        if item.readwise_url:
            webbrowser.open(item.readwise_url)
        else:
            # Legacy file-era row that was never pushed to Readwise - there
            # is no Reader document to open, but the URL is still useful.
            self.notify(
                f"not in Readwise (legacy item); source: {item.canonical_url}",
                title="no Reader document",
                timeout=10,
                markup=False,
            )

    def action_delete_item(self) -> None:
        if self._show_deleted:
            # `d` is a no-op in the tombstone view — these are already deleted
            # and there's no "permanent delete" until/unless we add one.
            return
        item = self._selected_item()
        if item is None:
            return
        title = item.title or "(untitled)"
        with connect() as conn:
            delete_item(conn, item.id)
        # Pulp Wise is push-only: the Reader document is deliberately left
        # alone (delete it in Reader if you want it gone there). The local
        # tombstone is what matters - it stops sync from ever re-pushing.
        self.refresh_data()
        self.notify(
            f"deleted {title!r} (document stays in Reader)",
            severity="information",
            markup=False,
        )

    def action_toggle_deleted(self) -> None:
        """Flip between live items and the tombstone view."""
        self._show_deleted = not self._show_deleted
        self.refresh_data()

    def _selected_item(self) -> LibraryItem | None:
        """Return the LibraryItem under the cursor, or None when on a group node."""
        tree: Tree[_NodeData] = self.query_one(Tree)
        node = tree.cursor_node
        if node is None or not isinstance(node.data, LibraryItem):
            return None
        return node.data


def _count_label(downloaded: int, total: int | None) -> str:
    """Render '(10/358)' when total is known, '(10)' otherwise."""
    if total is not None and total > 0:
        return f"({downloaded}/{total})"
    return f"({downloaded})"


def _group_label(bucket: str, count_label: str, *, deleted: bool) -> str:
    """Group-heading label: bucket name + count chip, markup-safe.

    Bucket names are user-derived (subscription names from config), so they
    must be escaped before the Tree parses the label as Rich markup. The
    '[deleted]' prefix rides along inside the escape - unescaped, Rich
    would eat it as a (broken) tag and the marker would never render.
    """
    heading = f"[deleted] {bucket}" if deleted else bucket
    return f"{escape(heading)}  {count_label}"


def _item_label(item: LibraryItem, *, deleted: bool) -> str:
    """Leaf-row label: date stamp + title, markup-safe.

    Titles come from arbitrary feeds - a post titled '[/span] hi' is
    perfectly valid RSS but invalid Rich markup, and an unescaped label
    crashes the whole TUI with MarkupError when the Tree renders it.

    Deleted-mode rows lead with the deletion date (when we have it) so the
    eye lands on "when did I read this." Live rows keep the ingestion date
    to match prior UX.
    """
    if deleted:
        stamp = (item.deleted_at[:10] if item.deleted_at else "(unknown)  ")[:10]
    else:
        stamp = item.ingested_at[:10] if item.ingested_at else ""
    title = item.title or "(untitled)"
    return f"{stamp}  {escape(title)}"
