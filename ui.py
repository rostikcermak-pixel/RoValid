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
# Display primitives
# ---------------------------------------------------------------------------

# A 3-row block font for the one number people actually watch. A terminal
# cannot make text bigger, so the hit counter is drawn instead of printed -
# it survives being shrunk into a phone-sized video frame, which a normal
# digit does not.
_BLOCK_DIGITS = {
    "0": ("█▀█", "█ █", "█▄█"),
    "1": (" ▄█", "  █", "  █"),
    "2": ("▀▀█", "▄▀▀", "█▄▄"),
    "3": ("▀▀█", " ▀█", "▀▀█"),
    "4": ("█ █", "▀▀█", "  █"),
    "5": ("█▀▀", "▀▀█", "▀▀█"),
    "6": ("█▀▀", "█▀█", "█▄█"),
    "7": ("▀▀█", "  █", "  █"),
    "8": ("█▀█", "█▀█", "█▄█"),
    "9": ("█▀█", "▀▀█", "▀▀█"),
    ",": ("   ", "   ", "▗  "),
    ".": ("   ", "   ", "▄  "),
    "K": ("█ █", "██ ", "█ █"),
    "M": ("█▄█", "█ █", "█ █"),
}

_BLANK_GLYPH = ("   ", "   ", "   ")

# Each glyph costs four columns, so the hero is abbreviated past a thousand.
# Spelling out 1,247 is 24 columns of block art and squeezes everything else
# off a narrow terminal; "1.2K" is 16 and still reads at video size.
def abbreviate(value: int) -> str:
    if value < 1_000:
        return str(value)
    # 999,999 rounds up to "1000K" at one decimal place, so the promotion to
    # the next unit is keyed to what the rounding actually produces.
    if value < 999_950:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return f"{value / 1_000_000:.1f}M".replace(".0M", "M")


def big_number(value: int) -> list[str]:
    """Render *value* as three rows of block glyphs.

    The rows are padded rather than stripped. Trailing spaces are load-bearing
    here: a glyph like "2" ends its rows with different amounts of whitespace,
    so stripping leaves the three rows at different lengths and centring them
    shears the number apart by a column.
    """
    rows = ["", "", ""]
    for ch in abbreviate(value):
        glyph = _BLOCK_DIGITS.get(ch, _BLANK_GLYPH)
        for i in range(3):
            rows[i] += glyph[i] + " "
    width = max(len(r) for r in rows)
    return [r.ljust(width) for r in rows]


# A left edge and nothing else. Drawn as the stats table's own border rather
# than as a parallel column of glyphs, because a column has to guess how many
# lines the table next to it will occupy - and guesses wrong the moment a
# value wraps, leaving the rule stopping short of the rows it should span.
# The top and bottom lines carry the rule too. Left blank they still occupy a
# line each, which pushed the stats one row down relative to the hero beside
# them and made the two columns look misaligned.
LEFT_RULE = box.Box(
    "┃   \n"
    "┃   \n"
    "    \n"
    "┃   \n"
    "┃   \n"
    "    \n"
    "┃   \n"
    "┃   \n"
)

# The hero block and its rule cost about 22 columns. Below this there is not
# enough left for a stat value to fit on one line, so the panels stack into a
# single column instead of standing the hero beside the stats.
NARROW = 74


def progress_bar(pct: float, width: int = 26) -> Text:
    filled = int(width * max(0.0, min(pct, 100.0)) / 100.0)
    return (
        Text("█" * filled, style=C.SUCCESS)
        + Text("░" * (width - filled), style=C.BORDER)
    )


_SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values, style: str = C.PRIMARY, width: int = 16) -> Text:
    """A tiny history plot. Motion is what makes a still frame look live."""
    vals = list(values)[-width:]
    if not vals:
        return Text(" " * width)
    peak = max(vals) or 1
    return Text(
        "".join(_SPARK[min(7, int(v / peak * 7))] for v in vals), style=style,
    )


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
    round_no: int = 1,
    rps_history: list[float] | None = None,
) -> Panel:
    """Build the live display panel.

    Laid out around one hero number - the hit count - because that is what a
    viewer looks for and everything else is supporting detail. The old panel
    gave all eight stats equal weight, which reads as a wall of numbers at
    video size.
    """
    pct = stage_done / max(stage_total, 1) * 100
    stage_name = "Screening" if stage == 1 else "Validating"

    # Left gutter: the hit count, drawn large, with its label underneath.
    digits = big_number(works)
    gutter_width = max(len(digits[0]), len("FOUND"))
    gutter = Table(box=None, show_header=False, padding=(0, 0))
    gutter.add_column(width=gutter_width, no_wrap=True)
    hero_style = f"bold {C.SUCCESS}" if works else f"bold {C.BORDER}"
    for row in digits:
        gutter.add_row(Text(row.center(gutter_width), style=hero_style))
    gutter.add_row(Text(
        "FOUND".center(gutter_width),
        style=f"bold {C.SUCCESS}" if works else C.MUTED,
    ))

    # Right column: progress, position, and rate.
    right = Table(
        box=LEFT_RULE, show_header=False, show_edge=True,
        padding=(0, 2), expand=True, border_style=C.BORDER,
    )
    right.add_row(
        progress_bar(pct)
        + Text(f"  {pct:.0f}%", style="bold white")
    )
    done_word = "screened" if stage == 1 else "validated"
    right.add_row(Text(
        f"{stage_done:,} / {stage_total:,} {done_word}", style=C.MUTED,
    ))

    rate_line = sparkline(rps_history or [rps], C.PRIMARY)
    rate_line += Text(f"  {rps:.0f}/s", style=f"bold {C.PRIMARY}")
    if ratelimited:
        rate_line += Text(f"   {ratelimited:,} limited", style=C.WARNING)
    elif proxy_alive > 1:
        # alive_count reports 1 for a rotating gateway and for proxyless, so
        # only a real pool is worth naming here.
        rate_line += Text(f"   {proxy_alive:,} proxies", style=C.MUTED)
    right.add_row(rate_line)
    right.add_row(Text(
        f"{taken:,} taken   ·   {requests:,} requests"
        + (f"   ·   {censored:,} censored" if censored else ""),
        style=C.MUTED,
    ))

    content = Table(box=None, show_header=False, padding=(0, 0), expand=True)

    if console.width < NARROW:
        # Too narrow to stand the hero beside the stats without every value
        # wrapping, so stack them instead of shipping a broken card.
        content.add_row(gutter)
        content.add_row(right)
    else:
        hero = Table(box=None, show_header=False, padding=(0, 0), expand=True)
        hero.add_column(width=gutter_width + 3, no_wrap=True)
        hero.add_column(ratio=1)
        hero.add_row(gutter, right)
        content.add_row(hero)

    if paused:
        content.add_row(Text(
            "Circuit breaker active - workers paused to protect the proxy.",
            style=C.WARNING,
        ))

    title = f"[bold {C.PRIMARY}]RoValid[/]"
    if round_no > 1:
        title += f"  [dim]Round {round_no}[/]"
    title += f"  [dim]Stage {stage}/2 · {stage_name}[/]"

    return Panel(
        content,
        title=title,
        title_align="left",
        subtitle=f"[dim]{elapsed:.0f}s · Ctrl+C saves and stops[/]",
        subtitle_align="right",
        box=box.ROUNDED,
        border_style=C.BORDER if not paused else C.WARNING,
        padding=(1, 3),
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
    """The last frame of a run, and the one people screenshot.

    Same visual language as the live panel - the hit count drawn large on the
    left, everything else supporting it - so the run ends on the number it
    spent the whole time counting up to. The old version nested a bordered
    table inside a bordered panel, which drew two boxes around the result and
    pushed it into the left third of the screen.
    """
    console.print()

    digits = big_number(works)
    gutter_width = max(len(digits[0]), len("AVAILABLE"))
    hero_style = f"bold {C.SUCCESS}" if works else f"bold {C.MUTED}"

    gutter = Table(box=None, show_header=False, padding=(0, 0))
    gutter.add_column(width=gutter_width, no_wrap=True)
    for row in digits:
        gutter.add_row(Text(row.center(gutter_width), style=hero_style))
    gutter.add_row(Text(
        "AVAILABLE".center(gutter_width),
        style=f"bold {C.SUCCESS}" if works else C.MUTED,
    ))

    stats = Table(
        box=LEFT_RULE, show_header=False, show_edge=True,
        padding=(0, 2), expand=True, border_style=C.BORDER,
    )
    # ratio on the value column keeps the spare width there; without it an
    # expanding table splits it evenly and strands the labels miles from
    # their values on a wide terminal.
    stats.add_column(style=C.MUTED, no_wrap=True)
    stats.add_column(style="white", ratio=1)

    # Values are kept short deliberately: the gutter and divider eat ~14
    # columns, so anything much longer than 30 characters wraps onto a second
    # line and the card stops looking like a card.
    stats.add_row("Checked", f"{taken + works + censored:,} names")
    detail = f"[{C.DANGER}]{taken:,}[/] taken"
    if censored:
        detail += f"  ·  [{C.WARNING}]{censored:,}[/] censored"
    if invalid:
        detail += f"  ·  [{C.MUTED}]{invalid:,} invalid[/]"
    stats.add_row("Of those", detail)
    stats.add_row(
        "Requests",
        f"{requests:,} in {elapsed:.0f}s"
        + (f"  [{C.MUTED}]({batch_requests:,} batched)[/]"
           if batch_requests else ""),
    )
    stats.add_row(
        "Speed",
        f"[{C.PRIMARY}]{requests / max(elapsed, 0.1):.0f}/s[/] avg"
        + (f"  ·  [{C.PRIMARY}]{peak_rps:.0f}/s[/] peak" if peak_rps else ""),
    )
    if requests > 0 and total_names > 0:
        stats.add_row(
            "Efficiency",
            f"[{C.PRIMARY}]{total_names / requests:.1f}[/] names/request",
        )
    if unresolved:
        stats.add_row(
            f"[{C.WARNING}]Unresolved[/]",
            f"[{C.WARNING}]{unresolved:,}[/] — re-run to retry",
        )
    if ratelimited:
        stats.add_row("Rate limited", f"[{C.WARNING}]{ratelimited:,}[/] times")
    if best_streak > 1:
        stats.add_row("Best streak", f"[{C.SUCCESS}]{best_streak:,}[/] in a row")
    if works:
        stats.add_row("Saved to", f"[{C.SUCCESS}]results/hits.txt[/]")

    layout = Table(box=None, show_header=False, padding=(0, 0), expand=True)
    if console.width < NARROW:
        layout.add_row(gutter)
        layout.add_row(stats)
    else:
        layout.add_column(width=gutter_width + 3, no_wrap=True)
        layout.add_column(ratio=1)
        layout.add_row(gutter, stats)

    console.print(Panel(
        layout,
        title=f"[bold {C.SUCCESS}]Done[/]" if works
              else f"[bold {C.MUTED}]Done[/]",
        title_align="left",
        subtitle=f"[dim]{elapsed:.0f}s[/]",
        subtitle_align="right",
        box=box.ROUNDED,
        border_style=C.SUCCESS if works else C.BORDER,
        padding=(1, 3),
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
