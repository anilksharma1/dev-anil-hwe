r"""worker_status.py -- live job status TUI with running Azure OpenAI token counts.

Same data source as `scaling-lib status` (the Azure Table Storage status table
scaling-lib's workers already write to), but adds a Tokens column per file and
a running total + tokens/sec rate across the whole job. Read-only -- it only
displays rows, never deletes or re-enqueues anything.

Reuses scaling-lib's own data-fetching/formatting helpers rather than
duplicating them:
    scaling_lib.status._fetch_entities  -- same Table Storage query the real TUI uses
    scaling_lib.status._eta_string      -- progress + ETA text
    scaling_lib.status._STATUS_DISPLAY  -- icon/style per status
    scaling_lib.tui._progress_bar       -- progress bar renderable

Those are underscore-prefixed (not documented/public API) -- they work today
but scaling-lib could change their signature without a changelog note since
they aren't part of the library's documented surface.

tokens_in/tokens_out are only written to a task's row once that file finishes
(completed or dead_lettered) -- see scaling_lib.worker._process_message. So a
file's tokens appear all at once when it completes, not incrementally while
it's mid-flight. With many files in a job this reads as live; with a few very
large/slow files, tokens lag until each one finishes.

Usage:
    python worker_status.py
    python worker_status.py --filter dead_lettered
    python worker_status.py --since 2024-06-01T00:00:00
    python worker_status.py --interval 10
    python worker_status.py --once          # print once and exit, no TUI
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv


def _tokens(task: dict) -> int | None:
    """Total tokens for a task, or None if it hasn't reached a terminal state yet."""
    if "tokens_in" not in task and "tokens_out" not in task:
        return None
    return int(task.get("tokens_in") or 0) + int(task.get("tokens_out") or 0)


# ── --once: plain print, no TUI ────────────────────────────────────────────────

def _print_once(status_filter: str | None, since: datetime | None) -> None:
    from rich.console import Console, Group
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text
    from scaling_lib.status import _fetch_entities, _eta_string, _STATUS_DISPLAY

    entities = _fetch_entities(status_filter, since)
    if not entities:
        Console().print(Text("No tasks found.", style="dim"))
        return

    jobs: dict[str, list] = {}
    for e in entities:
        jobs.setdefault(e.get("PartitionKey", ""), []).append(e)

    renderables = []
    grand_total = 0

    for job_id, tasks in jobs.items():
        total = len(tasks)
        n_done = sum(1 for t in tasks if t.get("status") == "completed")
        n_proc = sum(1 for t in tasks if t.get("status") == "processing")
        n_fail = sum(1 for t in tasks if t.get("status") in ("failed", "dead_lettered"))
        job_tokens = sum(_tokens(t) or 0 for t in tasks)
        grand_total += job_tokens

        pct = n_done / total if total else 0
        bar_width = 24
        filled = int(bar_width * pct)
        bar = "█" * filled + "░" * (bar_width - filled)

        header = Text()
        header.append(f" {job_id}  ", style="bold")
        header.append(f"[{bar}]  ", style="cyan")
        header.append(f"{n_done}/{total} ({int(pct * 100)}%)  ")
        if n_proc:
            header.append(f"⟳ {n_proc}  ", style="blue")
        if n_fail:
            header.append(f"✗ {n_fail}  ", style="red")
        header.append(f"◆ {job_tokens:,} tokens  ", style="magenta")
        eta = _eta_string(tasks)
        if "— avg" in eta:
            header.append(eta[eta.index("— avg"):], style="dim")
        elif n_done == total and total > 0:
            header.append("✓", style="green bold")
        renderables.append(header)

        table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
        table.add_column("File", max_width=36, no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Tries", justify="right")
        table.add_column("Tokens", justify="right")
        table.add_column("Enqueued", no_wrap=True)

        for task in tasks:
            status = task.get("status", "")
            icon, style = _STATUS_DISPLAY.get(status, ("?", ""))
            enqueued = task.get("enqueued_at", "")
            if hasattr(enqueued, "strftime"):
                enqueued = enqueued.strftime("%m-%d %H:%M")
            tok = _tokens(task)

            table.add_row(
                task.get("file_name", ""),
                Text(f"{icon} {status}", style=style),
                str(task.get("attempt_count", "")),
                f"{tok:,}" if tok is not None else "",
                str(enqueued),
            )
            if status in ("failed", "dead_lettered") and task.get("error_message"):
                table.add_row(
                    Text(f"  ↳ {task['error_message'][:70]}", style="red dim"),
                    "", "", "", "",
                )

        renderables.append(table)
        renderables.append(Text(""))

    if len(jobs) > 1:
        renderables.append(Rule(style="dim"))
        renderables.append(Text(f" All: {_eta_string(entities)}  ·  ◆ {grand_total:,} tokens total", style="bold"))

    Console().print(Group(*renderables))


# ── live TUI ────────────────────────────────────────────────────────────────

def _run_tui(status_filter: str | None, since: datetime | None, interval: int) -> None:
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import DataTable, Footer, Header, Static
    from rich.text import Text as RichText
    from scaling_lib.status import _fetch_entities, _eta_string, _STATUS_DISPLAY
    from scaling_lib.tui import _progress_bar

    class TokenStatusApp(App):
        TITLE = "worker_status — job progress + live token usage"

        BINDINGS = [
            Binding("r", "refresh", "Refresh"),
            Binding("q", "quit", "Quit"),
        ]

        DEFAULT_CSS = """
        #overall { height: 1; padding: 0 1; background: $surface-darken-1; }
        #tokens  { height: 1; padding: 0 1; background: $surface-darken-2; }
        DataTable { height: 1fr; }
        """

        def __init__(self) -> None:
            super().__init__()
            self._next_refresh: float = 0.0
            self._last_total_tokens: int | None = None
            self._last_poll_time: float | None = None

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("", id="overall")
            yield Static("", id="tokens")
            yield DataTable(cursor_type="row", zebra_stripes=True)
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one(DataTable)
            table.add_column("Job / File", key="name", width=42)
            table.add_column("Status", key="status", width=18)
            table.add_column("Tries", key="tries", width=6)
            table.add_column("Tokens", key="tokens", width=12)
            table.add_column("Enqueued", key="enqueued", width=14)
            table.add_column("Info", key="info")
            self._do_refresh()
            self._next_refresh = time.monotonic() + interval
            self.set_interval(1, self._tick)

        def _tick(self) -> None:
            remaining = max(0, int(self._next_refresh - time.monotonic()))
            self.sub_title = f"↻ in {remaining}s"
            if remaining == 0:
                self._next_refresh = time.monotonic() + interval
                self._do_refresh()

        @work(thread=True)
        def _do_refresh(self) -> None:
            try:
                entities = _fetch_entities(status_filter, since)
                self.call_from_thread(self._render, entities)
            except Exception as exc:
                self.call_from_thread(
                    self.notify, f"Refresh error: {exc}", severity="error", timeout=3
                )

        def _render(self, entities: list) -> None:
            table = self.query_one(DataTable)
            saved_cursor = table.cursor_row
            table.clear()

            jobs: dict[str, list] = {}
            for e in entities:
                jobs.setdefault(e.get("PartitionKey", ""), []).append(e)

            grand_total = 0

            for job_id, tasks in jobs.items():
                total = len(tasks)
                n_done = sum(1 for t in tasks if t.get("status") == "completed")
                n_proc = sum(1 for t in tasks if t.get("status") == "processing")
                n_fail = sum(1 for t in tasks if t.get("status") in ("failed", "dead_lettered"))
                job_tokens = sum(_tokens(t) or 0 for t in tasks)
                grand_total += job_tokens

                eta_full = _eta_string(tasks)
                eta_short = (
                    eta_full[eta_full.index("— avg"):] if "— avg" in eta_full
                    else ("✓" if n_done == total and total > 0 else "")
                )

                table.add_row(
                    RichText.assemble(("▼ ", "bold cyan"), (job_id, "bold")),
                    _progress_bar(n_done, total, n_proc, n_fail),
                    "",
                    RichText(f"{job_tokens:,}", style="bold magenta"),
                    "",
                    RichText(eta_short, style="dim"),
                    key=f"job:{job_id}",
                )

                for task in tasks:
                    status = task.get("status", "")
                    icon, style = _STATUS_DISPLAY.get(status, ("?", ""))
                    enqueued = task.get("enqueued_at", "")
                    if hasattr(enqueued, "strftime"):
                        enqueued = enqueued.strftime("%m-%d %H:%M")
                    error = task.get("error_message", "") if status in ("failed", "dead_lettered") else ""
                    tok = _tokens(task)

                    table.add_row(
                        RichText.assemble(("   ", ""), (task.get("file_name", ""), "")),
                        RichText(f"{icon} {status}", style=style),
                        str(task.get("attempt_count", "")),
                        f"{tok:,}" if tok is not None else "",
                        str(enqueued),
                        RichText(error[:60], style="red dim") if error else "",
                        key=f"task:{task.get('RowKey', '')}",
                    )

            if table.row_count > 0:
                table.move_cursor(row=min(saved_cursor, table.row_count - 1))

            # overall progress
            if entities:
                total_all = len(entities)
                n_done_all = sum(1 for e in entities if e.get("status") == "completed")
                n_proc_all = sum(1 for e in entities if e.get("status") == "processing")
                n_fail_all = sum(1 for e in entities if e.get("status") in ("failed", "dead_lettered"))
                overall = RichText.assemble(
                    (" All jobs  ", "bold"),
                    _progress_bar(n_done_all, total_all, n_proc_all, n_fail_all),
                )
            else:
                overall = RichText(" No tasks found.", style="dim")
            self.query_one("#overall", Static).update(overall)

            # token line, with a live rate since the last poll
            now = time.monotonic()
            rate_str = ""
            if self._last_total_tokens is not None and self._last_poll_time is not None:
                elapsed = now - self._last_poll_time
                delta = grand_total - self._last_total_tokens
                if elapsed > 0:
                    rate_str = f"   (+{delta:,} in {elapsed:.0f}s, {delta / elapsed:.1f} tok/s)"
            self._last_total_tokens = grand_total
            self._last_poll_time = now

            tokens_line = RichText.assemble(
                (" ◆ Tokens  ", "bold magenta"),
                (f"{grand_total:,}", "bold"),
                (rate_str, "dim"),
            )
            self.query_one("#tokens", Static).update(tokens_line)

        def action_refresh(self) -> None:
            self._next_refresh = time.monotonic() + interval
            self._do_refresh()

    TokenStatusApp().run()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Live job status + Azure OpenAI token usage, reading the same "
                    "scaling-lib status table as `scaling-lib status`."
    )
    parser.add_argument("--filter", dest="status_filter", metavar="STATUS",
                         choices=["pending", "processing", "completed", "failed", "dead_lettered"],
                         help="Filter by status")
    parser.add_argument("--since", metavar="DATETIME",
                         help="Show tasks enqueued after this ISO datetime (e.g. 2024-01-15T00:00:00)")
    parser.add_argument("--interval", type=int, default=3, metavar="SECONDS",
                         help="TUI refresh interval in seconds (default: 3)")
    parser.add_argument("--once", action="store_true",
                         help="Print table once and exit instead of launching the TUI")
    args = parser.parse_args()

    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else None

    if args.once:
        _print_once(args.status_filter, since)
    else:
        _run_tui(args.status_filter, since, args.interval)


if __name__ == "__main__":
    main()
