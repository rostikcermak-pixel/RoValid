#!/usr/bin/env python3
"""RoValid v1.0 - Setup wizard and proxy scraper."""

from __future__ import annotations

import asyncio
import itertools
import random
import re
import string
import sys
from pathlib import Path

import aiohttp
from rich.prompt import Confirm, IntPrompt, Prompt

from config import (
    DATA_DIR,
    MAX_CONCURRENCY,
    PROJECT_ROOT,
    AppSettings,
    Config,
    RunConfig,
    ensure_file,
    is_valid_username,
    load_lines,
)
from ui import (
    C,
    banner,
    config_summary,
    console,
    fail,
    info,
    info_card,
    ok,
    progress_steps,
    warn_card,
)

DEFAULT_PROXY_FILE = str(DATA_DIR / "proxies.txt")
DEFAULT_NAMES_FILE = str(DATA_DIR / "names_to_check.txt")

_PROXY_FILE_DISPLAY = "data/proxies.txt"
_NAMES_FILE_DISPLAY = "data/names_to_check.txt"

# Generation alphabet: lowercase + digits. Underscore is added separately so
# we can honour Roblox's "at most one, never on an edge" rule.
GEN_CHARS = string.ascii_lowercase + string.digits


def _resolve_input_path(raw: str) -> str:
    """Resolve user input to an absolute path against PROJECT_ROOT."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return str(p)


_PROXY_RE = re.compile(
    r"^(?:https?://)?(?:[^@\s]+@)?[a-zA-Z0-9](?:[a-zA-Z0-9\-.]*[a-zA-Z0-9])?:\d{1,5}$"
)


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

async def setup_wizard(config: Config, settings: AppSettings) -> RunConfig:
    """Walk the user through a 4-step setup."""

    console.clear()
    console.print(banner())
    console.print()
    console.print(progress_steps(0))

    # ── Step 1: Proxies ──
    info_card(
        f"[{C.PRIMARY}]Proxies are optional here.[/]",
        f"[{C.MUTED}]Stage 1 checks 200 names per request, so even proxyless[/]",
        f"[{C.MUTED}]runs clear a few hundred names per burst.[/]",
        f"[{C.MUTED}]Format: login:pass@host:port[/]",
    )
    proxies, remove_bad, scraped = await _step_proxies(config)

    # ── Step 2: Usernames ──
    console.print()
    console.print(progress_steps(1))
    usernames, invalid_count, gen_params = _step_usernames()

    # ── Step 3: Speed ──
    console.print()
    console.print(progress_steps(2))
    concurrency, timeout, two_stage = _step_speed(proxies, scraped)

    repeat = False
    if gen_params:
        repeat = Confirm.ask(
            f"Keep drawing new names until one is free? "
            f"[dim](stops on the first hit, Ctrl+C to give up)[/]",
            default=False,
        )
        if repeat:
            info("Each round draws a fresh batch; names already checked are skipped.")

    # ── Step 4: Webhook ──
    console.print()
    console.print(progress_steps(3))
    webhook_url, webhook_msg = _step_webhook(config)

    config.set("timeout", timeout)
    config.set("concurrency", concurrency)
    config.set("remove_proxies", remove_bad)
    config.set("two_stage", two_stage)

    config_summary(
        proxy_count=len(proxies),
        scraped=scraped,
        remove_bad=remove_bad,
        username_count=len(usernames),
        invalid_count=invalid_count,
        concurrency=concurrency,
        timeout=timeout,
        two_stage=two_stage,
        webhook=bool(webhook_url),
    )

    if not usernames:
        fail("No valid usernames to check.")
        sys.exit(1)

    if not Confirm.ask(f"\n[{C.PRIMARY}]Start checking?[/]", default=True):
        console.print(f"[{C.MUTED}]Aborted.[/]")
        sys.exit(0)

    return RunConfig(
        proxies=proxies,
        remove_bad_proxies=remove_bad,
        usernames=usernames,
        concurrency=concurrency,
        timeout=timeout,
        scraped=scraped,
        two_stage=two_stage,
        webhook_url=webhook_url,
        webhook_message=webhook_msg,
        repeat_until_found=repeat,
        gen_length=gen_params[0] if gen_params else 0,
        gen_count=gen_params[1] if gen_params else 0,
        gen_underscore=gen_params[2] if gen_params else False,
    )


# ---------------------------------------------------------------------------
# Step 1: Proxies
# ---------------------------------------------------------------------------

async def _step_proxies(config: Config) -> tuple[list[str], bool, bool]:
    """Returns (proxies, remove_bad, scraped)."""
    proxies: list[str] = []
    remove_bad = False
    scraped = False

    existing = load_lines(DEFAULT_PROXY_FILE)
    reuse_cfg = config.get("reuse_proxies")

    if existing and reuse_cfg is None:
        console.print()
        info_card(f"Found {len(existing)} proxies from last session.")
        if Confirm.ask("Reuse them?", default=False):
            proxies = existing
            ok(f"Reusing {len(proxies)} proxies")
            if Confirm.ask("Always reuse without asking?", default=False):
                config.set("reuse_proxies", True)
        else:
            if Confirm.ask("Always skip and ask for new ones?", default=False):
                config.set("reuse_proxies", False)
    elif existing and reuse_cfg:
        proxies = existing
        ok(f"Auto-reusing {len(proxies)} proxies")

    if not proxies:
        console.print()
        mode = Prompt.ask(
            f"[{C.PRIMARY}]Proxy source:[/] (f)ile  (p)aste  (s)crape  (n)one",
            choices=["f", "p", "s", "n"],
            default="n",
        )

        if mode == "f":
            proxy_path = Prompt.ask("Path to proxy file", default=_PROXY_FILE_DISPLAY)
            proxies = load_lines(_resolve_input_path(proxy_path))
            if not proxies:
                fail("No proxies found in that file.")
        elif mode == "p":
            console.print(f"\n[{C.PRIMARY}]Paste your proxies[/] [dim](one per line, empty line to finish)[/]")
            lines_pasted = []
            while True:
                line = Prompt.ask("", default="")
                if not line.strip():
                    break
                lines_pasted.append(line.strip())
            if lines_pasted:
                ensure_file(DEFAULT_PROXY_FILE)
                Path(DEFAULT_PROXY_FILE).write_text("\n".join(lines_pasted), encoding="utf-8")
                proxies = lines_pasted
            else:
                fail("No proxies provided.")
        elif mode == "s":
            console.print()
            scraped_proxies = await _scrape_proxies()
            if scraped_proxies:
                proxies = scraped_proxies
                scraped = True
                ensure_file(DEFAULT_PROXY_FILE)
                Path(DEFAULT_PROXY_FILE).write_text("\n".join(proxies), encoding="utf-8")

    if proxies:
        tag = f" [{C.MUTED}](free)[/]" if scraped else ""
        ok(f"{len(proxies)} proxies loaded{tag}")
        if len(proxies) > 1:
            remove_bad = Confirm.ask("Auto-remove dead proxies?", default=True)
    else:
        info("Proxyless mode - batching keeps this usable, just slower.")

    return proxies, remove_bad, scraped


# ---------------------------------------------------------------------------
# Step 2: Usernames
# ---------------------------------------------------------------------------

def _generate(length: int, count: int, allow_underscore: bool) -> list[str]:
    """Generate valid Roblox usernames of *length* characters."""
    total_space = len(GEN_CHARS) ** length

    # Small enough to enumerate exhaustively, then sample.
    if total_space <= 2_000_000:
        combos = ["".join(c) for c in itertools.product(GEN_CHARS, repeat=length)]
        if allow_underscore and length >= 3:
            # Insert a single underscore at every interior position.
            base = combos[: min(len(combos), 200_000)]
            for pos in range(1, length):
                combos.extend(n[:pos] + "_" + n[pos:] for n in base[:20_000])
        combos = [c for c in combos if is_valid_username(c)]
        return random.sample(combos, min(count, len(combos)))

    # Space too large to enumerate - sample randomly.
    seen: set[str] = set()
    out: list[str] = []
    attempts = 0
    while len(out) < count and attempts < count * 50:
        attempts += 1
        cand = "".join(random.choices(GEN_CHARS, k=length))
        if allow_underscore and random.random() < 0.15 and length >= 3:
            pos = random.randint(1, length - 1)
            cand = cand[:pos] + "_" + cand[pos:]
            cand = cand[:20]
        if cand not in seen and is_valid_username(cand):
            seen.add(cand)
            out.append(cand)
    return out


def generate_usernames(length: int, count: int, allow_underscore: bool = False) -> list[str]:
    """Draw a fresh batch of generated names (used between repeat rounds)."""
    return _generate(length, count, allow_underscore)


def _step_usernames() -> tuple[list[str], int, tuple[int, int, bool] | None]:
    """Returns (valid_usernames, invalid_count, generation_params).

    generation_params is (length, count, allow_underscore) when the names
    were generated, or None when they came from a file - repeat rounds need
    it to redraw, and a file cannot be redrawn.
    """
    raw = Prompt.ask(
        f"[{C.PRIMARY}](f)ile[/] or [{C.PRIMARY}](g)enerate[/] usernames?",
        choices=["f", "g"],
        default="g",
    )
    mode = "generate" if raw == "g" else "file"
    usernames: list[str] = []
    invalid_count = 0
    gen_params: tuple[int, int, bool] | None = None

    if mode == "file":
        names_path = Prompt.ask("Path to username file", default=_NAMES_FILE_DISPLAY)
        loaded = load_lines(_resolve_input_path(names_path))
        if not loaded:
            fail("File is empty - switching to generate mode.")
            mode = "generate"
        else:
            # Filter locally: every rejected name here is a request we never
            # have to spend, and Roblox would only tell us the same thing.
            usernames = [n for n in loaded if is_valid_username(n)]
            invalid_count = len(loaded) - len(usernames)
            ok(f"Loaded {len(usernames)} valid usernames")
            if invalid_count:
                info(f"Skipped {invalid_count} that break Roblox's rules")

    if mode == "generate":
        length = IntPrompt.ask(
            "Username length", default=5, choices=["3", "4", "5", "6"],
        )
        count = IntPrompt.ask("How many to generate", default=5000)
        allow_us = Confirm.ask("Include underscore names?", default=False)

        gen_params = (length, count, allow_us)
        ok(f"Generating {count} random {length}-char usernames...")
        usernames = _generate(length, count, allow_us)

        ensure_file(DEFAULT_NAMES_FILE)
        Path(DEFAULT_NAMES_FILE).write_text("\n".join(usernames), encoding="utf-8")
        ok(f"Generated {len(usernames)} usernames -> {_NAMES_FILE_DISPLAY}")

    # De-duplicate case-insensitively (Roblox names are case-insensitive
    # for uniqueness, so "Cool" and "cool" are the same registration).
    seen: set[str] = set()
    deduped: list[str] = []
    for n in usernames:
        if n.lower() not in seen:
            seen.add(n.lower())
            deduped.append(n)
    if len(deduped) < len(usernames):
        info(f"Removed {len(usernames) - len(deduped)} case-duplicate names")

    return deduped, invalid_count, gen_params


# ---------------------------------------------------------------------------
# Step 3: Speed
# ---------------------------------------------------------------------------

def _step_speed(proxies: list[str], scraped: bool = False) -> tuple[int, int, bool]:
    """Returns (concurrency, timeout, two_stage)."""

    two_stage = Confirm.ask(
        f"Use fast 2-stage mode? [dim](200 names/request screen, then confirm)[/]",
        default=True,
    )
    if not two_stage:
        warn_card(
            f"[{C.WARNING}]Validator-only mode is ~200x more requests.[/]",
            f"[{C.MUTED}]Only worth it if you distrust the bulk endpoint.[/]",
        )

    if not proxies:
        info("Proxyless - a couple of workers is all Roblox will accept.")
        conc = IntPrompt.ask("Concurrent workers", default=2)
        timeout = IntPrompt.ask("Request timeout (seconds)", default=15)
        return max(1, conc), timeout, two_stage

    if scraped:
        info("Free proxy mode - high concurrency, short timeout.")
        conc = IntPrompt.ask("Concurrent workers", default=100)
        timeout = IntPrompt.ask("Request timeout (seconds)", default=8)
    else:
        default_conc = min(MAX_CONCURRENCY, max(10, len(proxies) * 3))
        conc = IntPrompt.ask("Concurrent workers", default=default_conc)
        timeout = IntPrompt.ask("Request timeout (seconds)", default=10)

    if conc > MAX_CONCURRENCY:
        warn_card(f"Capped at {MAX_CONCURRENCY}.")
        conc = MAX_CONCURRENCY
    return conc, timeout, two_stage


# ---------------------------------------------------------------------------
# Step 4: Webhook
# ---------------------------------------------------------------------------

_DEFAULT_WEBHOOK_MSG = "**<name>** available | <t:time:R>"


def _step_webhook(config: Config) -> tuple[str | None, str | None]:
    saved_url = config.get("webhook")
    saved_msg = config.get("webhook_message", _DEFAULT_WEBHOOK_MSG)
    always = config.get("webhook_always", False)

    if always and saved_url:
        ok("Using saved webhook")
        return saved_url, saved_msg

    if saved_url and not always:
        if not Confirm.ask("Use webhook? [dim](saved from last session)[/]", default=True):
            if Confirm.ask("Forget saved webhook?", default=False):
                config.set("webhook", "")
                config.set("webhook_message", "")
            return None, None
        webhook_url = saved_url
        webhook_msg = saved_msg
    else:
        if not Confirm.ask("Send hits to a Discord webhook?", default=False):
            return None, None
        webhook_url = Prompt.ask("Webhook URL")
        if not webhook_url.strip():
            info("Empty URL - webhook disabled.")
            return None, None
        console.print(f"[{C.MUTED}]Hits are sent in batches to avoid rate-limits.[/]")
        webhook_msg = Prompt.ask(
            "Message template [dim](<name> <link> <time> <elapsed>)[/]",
            default=_DEFAULT_WEBHOOK_MSG,
        )

    config.set("webhook", webhook_url)
    config.set("webhook_message", webhook_msg)
    if not always and Confirm.ask("Always use this webhook?", default=True):
        config.set("webhook_always", True)

    return webhook_url, webhook_msg


# ---------------------------------------------------------------------------
# Proxy scraper
# ---------------------------------------------------------------------------

async def _scrape_proxies() -> list[str]:
    """Fetch free HTTP proxies from multiple sources, deduplicate."""

    SOURCES = [
        ("TheSpeedX",       "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
        ("monosans",        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
        ("proxifly",        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt"),
        ("ShiftyTR-http",   "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt"),
        ("ShiftyTR-https",  "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt"),
        ("roosterkid",      "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
        ("sunny9577",       "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt"),
        ("rdavydov",        "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt"),
        ("mmpx12-http",     "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"),
        ("mmpx12-https",    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt"),
        ("iplocate-http",   "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt"),
        ("iplocate-https",  "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/https.txt"),
        ("openproxylist",   "https://api.openproxylist.xyz/http.txt"),
        ("proxyscrape",     "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all"),
    ]

    console.print(f"[{C.MUTED}]Scraping {len(SOURCES)} sources...[/]")

    async def _fetch_one(name: str, url: str) -> list[str]:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10), trust_env=False,
            ) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        console.print(f"  [{C.DANGER}]X[/] {name} HTTP {resp.status}")
                        return []
                    text = await resp.text()
                    found = [p.strip() for p in text.splitlines() if _PROXY_RE.match(p.strip())]
                    console.print(f"  [{C.SUCCESS}]OK[/] {name} {len(found)} proxies")
                    return found
        except Exception as e:
            console.print(f"  [{C.DANGER}]X[/] {name} {type(e).__name__}")
            return []

    results = await asyncio.gather(*[_fetch_one(n, u) for n, u in SOURCES])

    seen: set[str] = set()
    all_proxies: list[str] = []
    for batch in results:
        for p in batch:
            key = p.split("@")[-1] if "@" in p else p
            if key not in seen:
                seen.add(key)
                all_proxies.append(p)

    if not all_proxies:
        fail("All sources failed - no proxies.")
        return []

    ok(f"{len(all_proxies)} unique proxies [{C.MUTED}](~2-5% usually work)[/]")
    return all_proxies
