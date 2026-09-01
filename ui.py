#!/usr/bin/env python3
"""RoValid v1.0 - terminal UI."""

from __future__ import annotations

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import C, VERSION

console = Console()


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def banner() -> Panel:
    inner = Text()
    inner.append("RoValid", style=f"bold {C.PRIMARY}")
    inner.append(f"  v{VERSION}", style=C.MUTED)
    inner.append("\nRoblox username availability checker", style=C.MUTED)
    return Panel(inner, box=box.ROUNDED, border_style=C.BORDER, padding=(1, 4))


# ---------------------------------------------------------------------------
# Progress indicator
# ---------------------------------------------------------------------------

def progress_steps(current: int, total: int = 4) -> str:
    steps = ["Proxies", "Usernames", "Speed", "Webhook"]
    parts: list[str] = []
    for i, label in enumerate(steps):
        if i < current:
            parts.append(f"[{C.SUCCESS}]OK[/] {label}")
        elif i == current:
            parts.append(f"[{C.PRIMARY}]>[/] {label}")
        else:
            parts.append(f"[{C.MUTED}]-[/] {label}")
    return "  ".join(parts)


def section(title: str) -> None:
    console.print()
    console.print(Text(title, style=f"bold {C.PRIMARY}"))
    console.print("-" * 40, style=C.MUTED)


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def card(title: str | None, *lines: str, border: str = C.PRIMARY) -> None:
    body = "\n".join(lines) if lines else ""
    console.print(Panel(
        body, title=title, title_align="left",
        box=box.ROUNDED, border_style=border, padding=(1, 2),
    ))


def info_card(*lines: str) -> None:
    card(None, *lines, border=C.MUTED)


def warn_card(*lines: str) -> None:
    card(None, *lines, border=C.WARNING)


# ---------------------------------------------------------------------------
# Config summary
# ---------------------------------------------------------------------------

def config_summary(
    proxy_count: int,
    scraped: bool,
    remove_bad: bool,
    username_count: int,
    invalid_count: int,
    concurrency: int,
    timeout: int,
    two_stage: bool,
    webhook: bool,
) -> None:
    console.print()
    t = Table(box=box.ROUNDED, border_style=C.BORDER, show_header=False, padding=(0, 2))
    t.add_column(style=C.MUTED, width=16)
    t.add_column(style="white")

    tag = f" [{C.MUTED}](free)[/]" if scraped else ""
    t.add_row("Proxies", f"[{C.PRIMARY}]{proxy_count}[/] loaded{tag}" if proxy_count
              else f"[{C.WARNING}]none (proxyless)[/]")
    if proxy_count > 1 and not scraped:
        t.add_row("Auto-remove", "Yes" if remove_bad else "No")
    t.add_row("Usernames", str(username_count))
    if invalid_count:
        t.add_row("Skipped", f"[{C.MUTED}]{invalid_count} invalid (Roblox rules)[/]")
    t.add_row("Mode", f"[{C.SUCCESS}]2-stage (fast)[/]" if two_stage
              else f"[{C.WARNING}]validator only (slow)[/]")
    t.add_row("Workers", str(concurrency))
    t.add_row("Timeout", f"{timeout}s")
    t.add_row("Webhook", f"[{C.SUCCESS}]on[/]" if webhook else f"[{C.MUTED}]off[/]")
    console.print(Panel(t, title="Summary", title_align="left",
                        box=box.ROUNDED, border_style=C.PRIMARY))


# ---------------------------------------------------------------------------
# Live dashboard
# ---------------------------------------------------------------------------

def live_card(
    stage: int,
    stage_done: int,
    stage_total: int,
    works: int,
    taken: int,
    censored: int,
    requests: int,
    batch_requests: int,
    candidates: int,
    ratelimited: int,
    circuit_opens: int,
    rps: float,
    elapsed: float,
    proxy_alive: int,
    paused: bool = False,
    recent: list[str] | None = None,
    feed: list[str] | None = None,
) -> Panel:
    """Build the live display panel."""

    pct = stage_done / max(stage_total, 1) * 100
    stage_name = "Screening" if stage == 1 else "Validating"

    inner = Table(box=None, show_header=False, padding=(0, 1), expand=True)
    inner.add_column(style=C.MUTED, width=14, no_wrap=True)
    inner.add_column(style="white")
    inner.add_column(style=C.MUTED, width=14, no_wrap=True)
    inner.add_column(style="white")

    inner.add_row(
        "Available", f"[{C.SUCCESS}]{works}[/]",
        "Taken", f"[{C.DANGER}]{taken}[/]",
    )
    inner.add_row(
        "Req/s", f"[{C.PRIMARY}]{rps:.0f}[/]",
        "Requests", str(requests),
    )
    if stage == 1:
        inner.add_row(
            "Screened", f"{stage_done}/{stage_total} ({pct:.0f}%)",
            "Batches", str(batch_requests),
        )
    else:
        inner.add_row(
            "Validated", f"{stage_done}/{stage_total} ({pct:.0f}%)",
            "Censored", f"[{C.WARNING}]{censored}[/]" if censored else "0",
        )

    if ratelimited > 0:
        inner.add_row(
            f"[{C.WARNING}]Rate limited[/]", f"[{C.WARNING}]{ratelimited}[/]",
            "Elapsed", f"{elapsed:.0f}s",
        )
    else:
        inner.add_row(
            "Proxies", f"{proxy_alive} alive",
            "Elapsed", f"{elapsed:.0f}s",
        )

    content = Table(box=None, show_header=False, padding=(0, 0), expand=True)
    content.add_row(inner)

    if stage == 1 and candidates:
        content.add_section()
        content.add_row(Text(
            f"{candidates} candidates survived screening -> stage 2 will confirm them",
            style=C.MUTED,
        ))

    if paused:
        content.add_section()
        content.add_row(Text(
            "Circuit breaker active - workers paused briefly to protect the proxy.",
            style=C.WARNING,
        ))

    if recent:
        content.add_section()
        recent_str = "  ".join(f"[{C.SUCCESS}]{n}[/]" for n in recent[-6:])
        content.add_row(Text("Recent  ", style=C.MUTED) + Text.from_markup(recent_str))

    if feed:
        content.add_section()
        content.add_row(Text.from_markup("  ".join(feed[-8:])))

    return Panel(
        content,
        title=f"[{C.PRIMARY}]RoValid[/] · Stage {stage}/2 {stage_name} · "
              f"{stage_done}/{stage_total} ({pct:.0f}%) · [dim]{elapsed:.0f}s[/]",
        title_align="left",
        box=box.ROUNDED,
        border_style=C.BORDER if not paused else C.WARNING,
        padding=(1, 2),
    )


# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------

def final_summary(
    requests: int,
    batch_requests: int,
    works: int,
    taken: int,
    censored: int,
    invalid: int,
    unresolved: int,
    ratelimited: int,
    elapsed: float,
    peak_rps: float = 0.0,
    best_streak: int = 0,
    total_names: int = 0,
) -> None:
    console.print()

    t = Table(box=box.ROUNDED, border_style=C.BORDER, show_header=False, padding=(0, 2))
    t.add_column(style=C.MUTED, width=18)
    t.add_column(style="white")
    t.add_row("Available", f"[{C.SUCCESS} bold]{works}[/]")
    t.add_row("Taken", f"[{C.DANGER}]{taken}[/]")
    if censored:
        t.add_row("Censored", f"[{C.WARNING}]{censored}[/]")
    if invalid:
        t.add_row("Invalid (local)", f"[{C.MUTED}]{invalid}[/]")
    if unresolved:
        t.add_row("Unresolved", f"[{C.WARNING}]{unresolved}[/] (re-run to retry)")
    t.add_row("Requests", f"{requests}  [dim]({batch_requests} batched)[/]")
    if ratelimited:
        t.add_row("Rate limited", f"[{C.WARNING}]{ratelimited}[/]")
    t.add_row("Elapsed", f"{elapsed:.0f}s")
    t.add_row("Avg req/s", f"{requests / max(elapsed, 0.1):.1f}")
    if peak_rps > 0:
        t.add_row("Peak req/s", f"[{C.PRIMARY}]{peak_rps:.0f}[/]")

    # The headline number: names resolved per request spent.
    if requests > 0 and total_names > 0:
        t.add_row("Efficiency", f"[{C.PRIMARY}]{total_names / requests:.1f}[/] names/request")
    if best_streak > 1:
        t.add_row("Best streak", f"[{C.SUCCESS}]{best_streak}[/] hits")
    if works > 0:
        t.add_row("Saved to", "results/hits.txt")

    console.print(Panel(
        t, title=f"[{C.SUCCESS}]Done[/] · {elapsed:.0f}s", title_align="left",
        box=box.ROUNDED, border_style=C.SUCCESS,
    ))


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def ok(msg: str) -> None:
    console.print(f"  [{C.SUCCESS}]OK[/] {msg}")


def fail(msg: str) -> None:
    console.print(f"  [{C.DANGER}]X[/] {msg}")


def info(msg: str) -> None:
    console.print(f"  [{C.MUTED}]i[/] {msg}")
