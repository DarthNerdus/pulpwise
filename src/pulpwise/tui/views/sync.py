"""Sync view - one per-subscription list that doubles as run progress.

Idle state: each row shows the last-sync timestamp from SQLite.
During a run: each row morphs to a progress indicator + current item.
Just-finished: the row shows the result counts ('+3 new', '4 paywalled',
or 'no change') alongside its updated timestamp until the next run.

Hit `s` from any tab to start a sync (binding lives at the app level).
The Readwise block below reports token setup + validity, refreshed off
the UI thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from pulpwise.config import ConfigError, Subscription, load_config
from pulpwise.importers.substack import (
    SubstackAutoOutcome,
    auto_reconcile,
    auto_reconcile_enabled,
)
from pulpwise.models import ExtractionError, FetchError
from pulpwise.pipeline import (
    ProgressReporter,
    SyncReport,
    SyncTotal,
)
from pulpwise.pipeline import (
    sync as pipeline_sync,
)
from pulpwise.sinks.readwise import ReadwiseAuthError, ReadwiseSink, resolve_token
from pulpwise.state import SubscriptionState, connect, get_subscription_state
from pulpwise.tui.views.base import View


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
    error_messages: tuple[str, ...] = ()


@dataclass(slots=True)
class _RunState:
    subs: dict[str, _SubRun] = field(default_factory=dict)
    final: SyncTotal | None = None
    error: str | None = None
    in_flight: bool = False


@dataclass(frozen=True, slots=True)
class _ReadwiseStatus:
    """Snapshot of the Readwise token situation for the status block.

    `configured is None` means the background probe hasn't reported yet.
    `token_valid is None` with `configured=True` means the auth check
    itself failed (network trouble) - `check_error` carries the reason.
    `config_error` set means config.toml itself didn't load; nothing else
    is known.
    """

    configured: bool | None = None
    hint: str = ""
    token_valid: bool | None = None
    check_error: str | None = None
    config_error: str | None = None


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
        self._readwise = _ReadwiseStatus()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(id="sync-pulpwise")
            yield Static(id="sync-hint")
            yield Static(id="sync-readwise")

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        self._render_pulpwise()
        # Render whatever we already know immediately; the network probe
        # (token validity) runs in a worker thread and re-renders on arrival.
        self._render_readwise()
        self._refresh_readwise_status()

    @work(thread=True, exclusive=True, group="readwise-status")
    def _refresh_readwise_status(self) -> None:
        """Probe token configuration + validity off the UI thread.

        `check_token` is a network call; blocking the UI thread on it would
        freeze every refresh keystroke behind readwise.io latency.
        """
        status = _probe_readwise()
        self.app.call_from_thread(self._on_readwise_status, status)

    def _on_readwise_status(self, status: _ReadwiseStatus) -> None:
        self._readwise = status
        self._render_readwise()

    def _render_readwise(self) -> None:
        self.query_one("#sync-readwise", Static).update(_readwise_text(self._readwise))

    def action_run_sync(self) -> None:
        if self._run.in_flight:
            return
        # Reset run state for the new run; previous results stop being shown.
        self._run = _RunState(in_flight=True)
        self._render_pulpwise()
        self._do_sync()

    @work(thread=True, exclusive=True, group="sync")
    def _do_sync(self) -> None:
        try:
            cfg = load_config()
            # Substack follow-list reconciliation is OPT-IN
            # ([auth.substack].auto_reconcile = true): it silently adds
            # every followed publication that isn't already a subscription,
            # which on a fresh config means the entire follow list - a mass
            # side effect nobody should get from just pressing sync. The
            # explicit path is `pulpwise import substack` (--auto for cron).
            # When enabled, failures (expired cookies, network blip) are
            # surfaced as notifications but do not block the actual sync.
            if auto_reconcile_enabled(cfg):
                cfg, auto = auto_reconcile(cfg)
                self._notify_auto_outcome(auto)
            reporter = _TuiProgress(self)
            total = pipeline_sync(config=cfg, progress=reporter)
        except (FetchError, ExtractionError) as exc:
            self.app.call_from_thread(self._on_done, None, str(exc))
            return
        except Exception as exc:
            self.app.call_from_thread(self._on_done, None, f"{type(exc).__name__}: {exc}")
            return
        self.app.call_from_thread(self._on_done, total, None)

    def _notify_auto_outcome(self, outcome: SubstackAutoOutcome) -> None:
        """Surface substack auto-reconcile result to the UI from the worker thread."""
        if outcome.skipped:
            return
        if outcome.error:
            self.app.call_from_thread(
                self.app.notify,
                outcome.error,
                severity="warning",
                markup=False,
            )
            return
        if outcome.added:
            preview = ", ".join(outcome.added[:3])
            if len(outcome.added) > 3:
                preview += f", +{len(outcome.added) - 3} more"
            self.app.call_from_thread(
                self.app.notify,
                f"imported {len(outcome.added)} new substack(s): {preview}",
                # Publication names are user-derived; don't let a bracket in
                # one parse as (broken) markup and crash the toast.
                markup=False,
            )

    # ---- progress callbacks (UI thread) ----

    def _on_sub_started(self, name: str, item_total: int) -> None:
        self._run.subs.setdefault(name, _SubRun()).total = max(item_total, 0)
        self._render_pulpwise()

    def _on_item_started(self, name: str, title: str) -> None:
        sub = self._run.subs.setdefault(name, _SubRun())
        sub.current = title[:60]
        self._render_pulpwise()

    def _on_item_finished(self, name: str, skipped: bool) -> None:
        sub = self._run.subs.setdefault(name, _SubRun())
        sub.done += 1
        if skipped:
            sub.skipped += 1
        self._render_pulpwise()

    def _on_sub_finished(self, name: str, report: SyncReport) -> None:
        sub = self._run.subs.setdefault(name, _SubRun())
        sub.finished = True
        sub.new_items = report.new_items
        sub.skipped = report.skipped
        sub.errors = report.errors
        sub.paywalled = len(report.paywalled)
        sub.error_messages = report.error_messages
        sub.current = ""
        self._render_pulpwise()

    def _on_done(self, total: SyncTotal | None, error: str | None) -> None:
        self._run.in_flight = False
        self._run.final = total
        self._run.error = error
        self._render_pulpwise()

    # ---- rendering ----

    def _render_pulpwise(self) -> None:
        self.query_one("#sync-pulpwise", Static).update(self._build_pulpwise_text())
        self.query_one("#sync-hint", Static).update(self._build_hint_text())

    def _build_pulpwise_text(self) -> Text:
        config = load_config()
        text = Text()
        text.append("PULPWISE\n", style="bold")

        if not config.subscriptions:
            text.append(
                "  no subscriptions configured.  add one with `pulpwise add <url>`.\n",
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
        # Width of the row-marker (a single styled char) plus its leading/
        # trailing spaces and the name column - used to indent error lines so
        # they hang under the sub name rather than under the marker.
        indent_w = 2 + 1 + 2 + name_w + 2
        for sub in config.subscriptions:
            text.append("  ")
            text.append_text(self._row_marker(sub, states.get(sub.name)))
            text.append(f"  {sub.name:<{name_w}}  ")
            text.append_text(self._row_status(sub, states.get(sub.name)))
            text.append("\n")
            for line in self._error_lines(sub, states.get(sub.name)):
                text.append(" " * indent_w)
                text.append_text(line)
                text.append("\n")
        return text

    def _error_lines(self, sub: Subscription, state: SubscriptionState | None) -> list[Text]:
        """Indented error detail lines for a sub, or [] when nothing to show.

        During a run, prefer the in-flight `error_messages` from the current
        report (most accurate). When idle, fall back to the persisted
        `last_error` on subscription_state so yesterday's failure is still
        visible after pulpwise restarts.
        """
        run = self._run.subs.get(sub.name)
        messages: tuple[str, ...] = ()
        if run is not None and run.finished and run.error_messages:
            messages = run.error_messages
        elif state and state.last_error and not (run and run.finished):
            messages = (state.last_error,)
        if not messages:
            return []
        # Cap at 3 per row so a sub with 20 broken posts doesn't push everything
        # else off-screen; full list lives in the log file.
        out: list[Text] = []
        for msg in messages[:3]:
            out.append(Text(f"└─ {_truncate(msg, 100)}", style="red"))
        if len(messages) > 3:
            out.append(Text(f"   ... and {len(messages) - 3} more (see log)", style="dim red"))
        return out

    def _summary_chips(
        self,
        subs: tuple[Subscription, ...],
        states: dict[str, SubscriptionState | None],
    ) -> Text:
        # Disabled subs never sync, so they get their own chip and stay out
        # of the ok/error/pending math (and the in-flight denominator).
        enabled = [s for s in subs if not s.disabled]
        disabled = len(subs) - len(enabled)

        if self._run.in_flight:
            done = sum(1 for s in self._run.subs.values() if s.finished)
            return Text.from_markup(f"[cyan]syncing... {done}/{len(enabled)}[/]")

        ok = sum(1 for s in enabled if _last_status(states.get(s.name)) == "ok")
        err = sum(1 for s in enabled if _last_status(states.get(s.name)) == "error")
        pending = len(enabled) - ok - err

        parts: list[str] = []
        if ok:
            parts.append(f"[green]{ok} ok[/]")
        if err:
            parts.append(f"[red]{err} error[/]")
        if pending:
            parts.append(f"[dim]{pending} pending[/]")
        if disabled:
            parts.append(f"[dim]{disabled} disabled[/]")
        return Text.from_markup("  ·  ".join(parts) or "[dim]none[/]")

    def _row_marker(self, sub: Subscription, state: SubscriptionState | None) -> Text:
        # Checked before run state: a stale run entry from before the user
        # toggled the sub off must not override the paused marker.
        if sub.disabled:
            return Text("‖", style="dim")
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
        if sub.disabled:
            return Text("disabled", style="dim")
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


def _truncate(text: str, max_len: int) -> str:
    """Single-line, length-capped rendering for inline error display."""
    flat = text.replace("\n", " ").strip()
    if len(flat) <= max_len:
        return flat
    return flat[: max_len - 1] + "…"


def _last_status(state: SubscriptionState | None) -> str | None:
    return state.last_status if state else None


def _probe_readwise() -> _ReadwiseStatus:
    """Resolve the token and, when one exists, ask Readwise whether it works.

    Runs in the SyncView's worker thread only - `check_token` hits the
    network. Never raises: every failure mode maps to a `_ReadwiseStatus`
    the render side knows how to display.
    """
    try:
        cfg = load_config()
    except ConfigError as exc:
        # A malformed config.toml must not escape the worker - an
        # unhandled exception there takes down the whole TUI.
        return _ReadwiseStatus(config_error=str(exc))
    try:
        resolve_token(cfg)
    except ReadwiseAuthError as exc:
        return _ReadwiseStatus(configured=False, hint=str(exc))

    try:
        with ReadwiseSink.from_config(cfg) as sink:
            valid = sink.check_token()
    except ReadwiseAuthError as exc:
        # Token resolved a moment ago but the sink rejected it (e.g. the
        # token_path file vanished between the two reads). Treat as
        # unconfigured with the sink's own hint.
        return _ReadwiseStatus(configured=False, hint=str(exc))
    except FetchError as exc:
        return _ReadwiseStatus(configured=True, check_error=str(exc))
    return _ReadwiseStatus(configured=True, token_valid=valid)


def _readwise_text(status: _ReadwiseStatus) -> Text:
    text = Text()
    text.append("\nREADWISE\n", style="bold")

    if status.config_error is not None:
        text.append("  Token:  ")
        text.append("config error", style="red")
        text.append(f": {status.config_error}", style="dim")
        text.append("\n")
        return text

    if status.configured is None:
        text.append("  Token:  ")
        text.append("checking...", style="dim")
        text.append("\n")
        return text

    if not status.configured:
        text.append("  Token:  ")
        text.append("not configured", style="red")
        text.append(f": {status.hint}", style="dim")
        text.append("\n")
        return text

    text.append("  Token:  ")
    text.append("configured", style="green")
    text.append("\n  Auth:   ")
    if status.check_error is not None:
        text.append("could not verify", style="yellow")
        text.append(f": {status.check_error}", style="dim")
    elif status.token_valid:
        text.append("token accepted by Readwise", style="green")
    else:
        text.append("token rejected by Readwise", style="red")
        text.append(
            " - get a fresh one at https://readwise.io/access_token",
            style="dim",
        )
    text.append("\n")
    return text
