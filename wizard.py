#!/usr/bin/env python3
"""RoValid v1.0 - Setup wizard and proxy scraper."""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import re
import string
import sys
import time
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
from proxy import prescreen
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


def _proxies_from_json(text: str) -> list[str] | None:
    """host:port pairs out of a JSON body, or None if this is not JSON.

    Most lists are plain lines, but the checked ones tend to publish records
    instead - geonode's carry uptime, latency and a last-checked timestamp,
    which is exactly the material worth having. Rather than special-casing one
    source, anything that parses as JSON and holds objects with an ip and a
    port is read this way.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        host, port = row.get("ip"), row.get("port")
        if host and port:
            proxy = _normalise_proxy(f"{host}:{port}")
            if proxy:
                out.append(proxy)
    return out


def _normalise_proxy(line: str) -> str | None:
    """A scraped line as `host:port`, or None if it is not a proxy at all.

    Most lists are already bare `host:port`. Some append fields - hideip.me
    publishes `host:port:Country` - and dropping those lines loses the source
    entirely, so the extra fields are trimmed rather than rejected. Anything
    that still fails the pattern (headers, prose, socks URLs) is discarded.
    """
    line = line.strip()
    if not line:
        return None
    if _PROXY_RE.match(line):
        return line
    parts = line.split(":")
    if len(parts) > 2:
        candidate = ":".join(parts[:2])
        if _PROXY_RE.match(candidate):
            return candidate
    return None


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

    repeat = bool(gen_params and gen_params[3])

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

# Nobody keeps thousands of paid proxies in a text file. When the config
# does not say what kind of pool was saved - it did not record it before -
# infer it from the size, so an existing free pool is treated as free on the
# very next run rather than only after a fresh scrape.
FREE_POOL_HINT = 50


def _pool_is_free(config: Config, pool: list[str]) -> bool:
    known = config.get("proxies_are_free")
    if known is not None:
        return bool(known)
    return len(pool) > FREE_POOL_HINT


async def _step_proxies(config: Config) -> tuple[list[str], bool, bool]:
    """Returns (proxies, remove_bad, scraped)."""
    proxies: list[str] = []
    remove_bad = False
    scraped = False
    screened_here = False

    existing = load_lines(DEFAULT_PROXY_FILE)
    reuse_cfg = config.get("reuse_proxies")

    if existing and reuse_cfg is None:
        console.print()
        info_card(f"Found {len(existing)} proxies from last session.")
        if Confirm.ask("Reuse them?", default=False):
            proxies = existing
            scraped = _pool_is_free(config, proxies)
            ok(f"Reusing {len(proxies)} proxies")
            if Confirm.ask("Always reuse without asking?", default=False):
                config.set("reuse_proxies", True)
        else:
            if Confirm.ask("Always skip and ask for new ones?", default=False):
                config.set("reuse_proxies", False)
    elif existing and reuse_cfg:
        proxies = existing
        # A reused pool is the same kind of pool it was when it was saved.
        # Forgetting that turned every scoring and screening protection off:
        # `scraped` gates all of it, so an auto-reused free pool ran with no
        # scoring, no benching, no burial and no pre-flight screen - which is
        # every proxy fix in this file doing nothing at all.
        scraped = _pool_is_free(config, proxies)
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
                scraped_proxies = await _prescreen_proxies(scraped_proxies)
                screened_here = True
            if scraped_proxies:
                proxies = scraped_proxies
                scraped = True
                config.set("proxies_are_free", True)
                ensure_file(DEFAULT_PROXY_FILE)
                Path(DEFAULT_PROXY_FILE).write_text("\n".join(proxies), encoding="utf-8")

    # Screen here rather than only on the scrape branch. Proxies go stale
    # between sessions, so a reused list is exactly the one most in need of
    # it - and that was the path where it never ran.
    if len(proxies) > 1 and not screened_here:
        proxies = await _prescreen_proxies(proxies)

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

def _underscored(stem: str) -> str:
    """*stem* with an underscore at a random interior position.

    The underscore takes one of the name's characters rather than being added
    to them, so a stem of n characters becomes a name of n+1 and the caller
    passes a stem one shorter than the length it wants. Roblox counts the
    underscore against its own 3-20 limit, and asking for five characters and
    receiving six is not what anyone means by it.
    """
    pos = random.randint(1, len(stem) - 1)
    return stem[:pos] + "_" + stem[pos:]


def _generate(length: int, count: int, allow_underscore: bool) -> list[str]:
    """Generate valid Roblox usernames of exactly *length* characters."""
    total_space = len(GEN_CHARS) ** length

    # Small enough to enumerate exhaustively, then sample.
    if total_space <= 2_000_000:
        combos = ["".join(c) for c in itertools.product(GEN_CHARS, repeat=length)]
        if allow_underscore and length >= 3:
            # An underscore name of this length is a stem one character
            # shorter with a separator dropped into it, so its own space is
            # enumerated rather than carved out of the plain one.
            #
            # The stems are drawn at random, which the old code did not do:
            # it sliced the first 20,000 entries off a lexicographic product,
            # and since 36^3 = 46,656 four-character names begin with each
            # letter, every single underscore name it produced at length 4
            # started with an "a". Length 3 was biased the same way, just
            # less visibly - it never got past "p".
            stems = ["".join(c) for c in
                     itertools.product(GEN_CHARS, repeat=length - 1)]
            combos.extend(_underscored(stem) for stem in stems)
        combos = [c for c in combos if is_valid_username(c)]
        return random.sample(combos, min(count, len(combos)))

    # Space too large to enumerate - sample randomly.
    seen: set[str] = set()
    out: list[str] = []
    attempts = 0
    while len(out) < count and attempts < count * 50:
        attempts += 1
        if allow_underscore and length >= 3 and random.random() < 0.15:
            cand = _underscored("".join(random.choices(GEN_CHARS, k=length - 1)))
        else:
            cand = "".join(random.choices(GEN_CHARS, k=length))
        if cand not in seen and is_valid_username(cand):
            seen.add(cand)
            out.append(cand)
    return out


def generate_usernames(length: int, count: int, allow_underscore: bool = False) -> list[str]:
    """Draw a fresh batch of generated names (used between repeat rounds)."""
    return _generate(length, count, allow_underscore)


def _step_usernames() -> tuple[list[str], int, tuple[int, int, bool, bool] | None]:
    """Returns (valid_usernames, invalid_count, generation_params).

    generation_params is (length, count, allow_underscore, repeat) when the
    names were generated, or None when they came from a file - repeat rounds
    need it to redraw, and a file cannot be redrawn.
    """
    raw = Prompt.ask(
        f"[{C.PRIMARY}](f)ile[/] or [{C.PRIMARY}](g)enerate[/] usernames?",
        choices=["f", "g"],
        default="g",
    )
    mode = "generate" if raw == "g" else "file"
    usernames: list[str] = []
    invalid_count = 0
    gen_params: tuple[int, int, bool, bool] | None = None

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
        # Asked before the count, because it changes what the count means.
        repeat = Confirm.ask(
            "Keep drawing new names until one is free? "
            "[dim](stops on the first hit, Ctrl+C to give up)[/]",
            default=False,
        )
        length = IntPrompt.ask(
            "Username length", default=5, choices=["3", "4", "5", "6"],
        )
        if repeat:
            info("Rounds run until a hit; below is the batch size for each one.")
            count = IntPrompt.ask("How many per round", default=1000)
        else:
            count = IntPrompt.ask("How many to generate", default=5000)
        allow_us = Confirm.ask("Include underscore names?", default=False)

        gen_params = (length, count, allow_us, repeat)
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
        "Use fast 2-stage mode? [dim](200 names/request screen, then confirm)[/]",
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
        # One worker per proxy. The old default was three, which measured
        # squarely in the degrading zone: each proxy carries its own bucket,
        # so once there is a worker per proxy the pool's refill rate is the
        # bound and extra workers only queue up behind it. On a 25-proxy
        # pool, 75 workers took 485s and found 1270 names where 25 workers
        # took 350s and found all 1294 - 39% slower for 24 fewer results,
        # because the surplus workers spend their requests on 429s and push
        # names past the point where they get abandoned.
        default_conc = min(MAX_CONCURRENCY, max(10, len(proxies)))
        info("About one worker per proxy - more than that just queues up.")
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


# Proxy pre-flight
# ---------------------------------------------------------------------------

async def _prescreen_proxies(pool: list[str]) -> list[str]:
    """Drop the corpses before the run rather than during it.

    A scraped pool is roughly 95% dead. Left alone, the run discovers that one
    proxy at a time, each costing a full request timeout while a worker sits
    on it: a measured 10,000-name run spent 411 batch requests to clear 26
    chunks - 6.3% success, 15.8 attempts per chunk - and only 4 of those
    failures were real rate limits. The rest was the pool.

    Testing here costs one short timeout per proxy and they all run at once,
    so the whole sweep is bounded by the timeout rather than by the number of
    dead proxies.
    """
    console.print(f"[{C.MUTED}]Testing {len(pool):,} proxies against Roblox...[/]")

    last = [0.0]

    def _progress(done: int, total: int, live: int) -> None:
        now = time.monotonic()
        if now - last[0] < 0.5 and done < total:
            return
        last[0] = now
        console.print(
            f"  [{C.MUTED}]{done:,}/{total:,} tested · "
            f"[{C.SUCCESS}]{live:,} alive[/][/]",
        )

    started = time.monotonic()
    live = await prescreen(pool, on_progress=_progress)
    elapsed = time.monotonic() - started

    if not live:
        warn_card(
            f"None of the {len(pool):,} scraped proxies could reach Roblox.",
            "",
            "Free lists go stale fast. Proxyless is usually the better call -",
            "your own IP is one clean route instead of thousands of dead ones.",
        )
        if not Confirm.ask("Keep the unscreened list anyway?", default=False):
            return []
        return pool

    ok(f"{len(live):,} of {len(pool):,} proxies alive "
       f"({len(live) / len(pool) * 100:.1f}%) in {elapsed:.0f}s")
    return live


# ---------------------------------------------------------------------------
# Proxy scraper
# ---------------------------------------------------------------------------

# The four unchecked dumps. Between them they are ~93% of everything the
# scrape returns, so a uniform sample down to the cap below would be ~93%
# unchecked and would crowd out the curated lists - the ones that publish
# only proxies they have already validated, and that therefore survive the
# pre-flight screen at a far better rate. Every other source is kept whole
# and these four fill only whatever cap budget is left over.
BULK_SOURCES = {
    "mishakorzik", "casals-ar", "SevenworksDev", "MuRongPIG",
    "ErcinDedeoglu", "zevtyardt", "yuceltoluyag",
}

# How many of the unchecked dumps to screen alongside the curated lists.
#
# Everything curated goes through; the dumps add this many on top rather than
# competing with them for one fixed total. That distinction matters: with a
# single cap, adding a curated source silently squeezed the hedge, and it hit
# zero twice while these sources were being assembled - 23,045 curated against
# a 25,000 cap, then 32,593 against 30,000, each time leaving the dumps
# fetching 1.9 million lines for no slots at all.
BULK_SAMPLE = 7_000

# Hard ceiling on the whole pool, and the only thing here that is really about
# time. The pre-flight screen is the expensive step - the scrape itself takes
# about four seconds - and it costs up to PRESCREEN_TIMEOUT per proxy at
# PRESCREEN_CONCURRENCY at a time, so a pool of N takes roughly N/120 seconds.
# At the current ~32,600 curated plus the hedge that is around five and a half
# minutes; the ceiling only bites if the curated lists grow far beyond that.
SCRAPE_POOL_CAP = 45_000


def _select_pool(
    batches: dict[str, list[str]],
    cap: int = SCRAPE_POOL_CAP,
    bulk_sample: int = BULK_SAMPLE,
) -> tuple[list[str], int, int]:
    """Dedupe across sources and pick the pool. Returns (pool, unique, curated).

    `batches` maps source name to what that source returned, in SOURCES order.
    Curated sources are consumed first, so a proxy that appears in both a
    curated list and a bulk dump is credited to the curated one; every curated
    proxy is then kept. The dumps contribute *bulk_sample* on top of that, not
    whatever a shared total happens to leave them, so adding a curated source
    grows the pool rather than quietly starving the hedge. `cap` is only a
    ceiling for the pathological case.
    """
    seen: set[str] = set()
    curated: list[str] = []
    bulk: list[str] = []

    def _take(name: str, into: list[str]) -> None:
        for proxy in batches.get(name, ()):
            key = proxy.split("@")[-1] if "@" in proxy else proxy
            if key not in seen:
                seen.add(key)
                into.append(proxy)

    for name in batches:
        if name not in BULK_SOURCES:
            _take(name, curated)
    for name in batches:
        if name in BULK_SOURCES:
            _take(name, bulk)

    total = len(curated) + len(bulk)
    if len(curated) >= cap:
        random.shuffle(curated)
        pool = curated[:cap]
    elif bulk:
        random.shuffle(bulk)
        pool = curated + bulk[:min(bulk_sample, cap - len(curated))]
    else:
        pool = curated
    return pool, total, min(len(curated), len(pool))


async def _scrape_proxies() -> list[str]:
    """Fetch free HTTP proxies from multiple sources, deduplicate."""

    # Every URL here was probed before it was added: it answers 200 and its
    # body parses as host:port. Two dead ones (mmpx12 http/https, both 404)
    # were dropped at the same time. Protocol matters - aiohttp's proxy=
    # speaks HTTP CONNECT, so socks lists are deliberately not here even
    # though they parse.
    SOURCES = [
        # The big four. Unchecked dumps, so the hit rate is poor, but between
        # them they are ~95% of everything the scrape returns.
        ("SevenworksDev",   "https://raw.githubusercontent.com/SevenworksDev/proxy-list/main/proxies/http.txt"),
        ("MuRongPIG",       "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt"),
        ("ErcinDedeoglu",   "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt"),
        ("zevtyardt",       "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt"),
        ("mishakorzik",     "https://raw.githubusercontent.com/mishakorzik/Free-Proxy/master/proxy.txt"),
        ("casals-ar",       "https://raw.githubusercontent.com/casals-ar/proxy-list/main/http"),
        ("yuceltoluyag",    "https://raw.githubusercontent.com/yuceltoluyag/GoodProxy/main/raw.txt"),
        # Mid-sized, and several of these are checked lists, so proportionally
        # more of them survive the pre-flight screen.
        ("openproxylist",   "https://api.openproxylist.xyz/http.txt"),
        ("aslisk",          "https://raw.githubusercontent.com/aslisk/proxyhttps/main/https.txt"),
        ("proxyspace",      "https://proxyspace.pro/http.txt"),
        ("B4RC0DE",         "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/HTTP.txt"),
        ("TheSpeedX",       "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
        ("sunny9577",       "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt"),
        ("jetkai",          "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt"),
        ("proxyscrape",     "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all"),
        ("proxyscrape-v4",  "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=ipport&format=text"),
        ("iplocate-http",   "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt"),
        ("proxifly",        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt"),
        ("rdavydov",        "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt"),
        ("vakhov",          "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt"),
        ("almroot",         "https://raw.githubusercontent.com/almroot/proxylist/master/list.txt"),
        ("elliottophellia", "https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/http/global/http_checked.txt"),
        ("monosans",        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
        ("clarketm",        "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"),
        ("Zaeem20",         "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt"),
        ("TuanMinPay",      "https://raw.githubusercontent.com/TuanMinPay/live-proxy/master/http.txt"),
        ("dpangestuw",      "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/http_proxies.txt"),
        ("Anonym0us",       "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt"),
        ("sunny9577-all",   "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt"),
        ("Vann-Dev",        "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/http.txt"),
        ("zloi-https",      "https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt"),
        ("proxylist-to",    "https://raw.githubusercontent.com/proxylist-to/proxy-list/main/http.txt"),
        ("hendrikbgr",      "https://raw.githubusercontent.com/hendrikbgr/Free-Proxy-Repo/master/proxy_list.txt"),
        ("andigwandi",      "https://raw.githubusercontent.com/andigwandi/free-proxy/main/proxy_list.txt"),
        ("themiralay",      "https://raw.githubusercontent.com/themiralay/Proxy-List-World/master/data.txt"),
        ("im-razvan",       "https://raw.githubusercontent.com/im-razvan/proxy_list/main/http.txt"),
        ("rdavydov-anon",   "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies_anonymous/http.txt"),
        ("zloi-http",       "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt"),
        ("vmheaven",        "https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/main/http.txt"),
        ("databay-labs",    "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt"),
        ("openproxy-https", "https://api.openproxylist.xyz/https.txt"),
        ("proxyscrape-v4s", "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=https&proxy_format=ipport&format=text"),
        ("ShiftyTR-all",    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/proxy.txt"),
        ("zloi-connect",    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/connect.txt"),
        ("ALIILAPRO",       "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt"),
        ("proxifly-US",     "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/US/data.txt"),
        ("MrMarble",        "https://raw.githubusercontent.com/MrMarble/proxy-list/main/all.txt"),
        ("proxyscrape-elite", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=elite"),
        ("berkay-digital",  "https://raw.githubusercontent.com/berkay-digital/Proxy-Scraper/main/proxies.txt"),
        ("proxyspace-https", "https://proxyspace.pro/https.txt"),
        ("MuRongPIG-check", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http_checked.txt"),
        ("Firdoxx",         "https://raw.githubusercontent.com/Firdoxx/proxy-list/main/http"),
        ("prxchk-all",      "https://raw.githubusercontent.com/prxchk/proxy-list/main/all.txt"),
        ("proxyscrape-anon", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=anonymous"),
        # Publishes records rather than lines, read by _proxies_from_json.
        # Small, but every entry carries a last-checked time, a latency and an
        # uptime percentage - the only source here that has been verified by
        # anyone before it arrives.
        ("geonode",         "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http"),
        ("geonode-p2",      "https://proxylist.geonode.com/api/proxy-list?limit=500&page=2&sort_by=lastChecked&sort_type=desc&protocols=http"),
        # Small and mostly stale, but they cost one request each and the
        # occasional live proxy in them is one the big dumps missed.
        ("roosterkid",      "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
        ("prxchk",          "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt"),
        ("ShiftyTR-http",   "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt"),
        ("iplocate-https",  "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/https.txt"),
        ("ShiftyTR-https",  "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt"),
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
                    # Decode leniently. At least one list carries country
                    # names in a non-UTF-8 encoding, and resp.text() raising
                    # on those threw away the whole source silently.
                    text = (await resp.read()).decode("utf-8", errors="ignore")
                    found = _proxies_from_json(text)
                    if found is None:
                        found = [q for q in map(_normalise_proxy, text.splitlines())
                                 if q]
                    console.print(f"  [{C.SUCCESS}]OK[/] {name} {len(found)} proxies")
                    return found
        except Exception as e:
            console.print(f"  [{C.DANGER}]X[/] {name} {type(e).__name__}")
            return []

    results = await asyncio.gather(*[_fetch_one(n, u) for n, u in SOURCES])
    by_source = dict(zip((n for n, _ in SOURCES), results, strict=True))

    pool, total, kept_curated = _select_pool(by_source)
    if not pool:
        fail("All sources failed - no proxies.")
        return []

    if len(pool) < total:
        ok(f"{len(pool):,} of {total:,} unique proxies "
           f"[{C.MUTED}]({kept_curated:,} curated + "
           f"{len(pool) - kept_curated:,} sampled; a few % usually work)[/]")
    else:
        ok(f"{total:,} unique proxies [{C.MUTED}](a few % usually work)[/]")
    return pool
