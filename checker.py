#!/usr/bin/env python3
"""RoValid v1.0 - Roblox username availability checker.

Two-stage async engine:
  Stage 1  bulk existence screen  (200 names per request)
  Stage 2  signup validator       (survivors only, confirms availability)
"""

from __future__ import annotations

# ── Python 3.13 SSL bug fix ───────────────────────────────────────────────
import asyncio.sslproto as _sslproto

_orig_eof = _sslproto.SSLProtocol.eof_received


def _safe_eof(self):
    try:
        return _orig_eof(self)
    except RuntimeError:
        return False


_sslproto.SSLProtocol.eof_received = _safe_eof
# ──────────────────────────────────────────────────────────────────────────

import argparse
import asyncio
import sys
import time
from collections import deque

import aiohttp
from rich.live import Live

import config as _config
from config import (
    BATCH_MAX,
    DATA_DIR,
    LOGS_DIR,
    MAX_CONCURRENCY,
    RESULTS_DIR,
    AppSettings,
    Config,
    RunConfig,
    Stats,
    ensure_dir,
    ensure_file,
    is_valid_username,
    load_lines,
)
from engine import (
    AVAILABLE,
    CENSORED,
    EXHAUSTED,
    INVALID,
    TAKEN,
    CircuitBreaker,
    RobloxChecker,
    WebhookSender,
    set_debug,
)
from proxy import ProxyManager
from ui import C, banner, console, final_summary, live_card
from wizard import setup_wizard


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> AppSettings:
    parser = argparse.ArgumentParser(
        description="RoValid - Roblox username availability checker",
    )
    parser.add_argument("-d", "--debug", action="store_true",
                        help="Enable debug output (request/response logs)")
    parser.add_argument("-n", "--no-wizard", action="store_true",
                        help="Skip setup wizard - use saved config and files")
    parser.add_argument("--version", action="version",
                        version=f"RoValid v{_config.VERSION}")
    args = parser.parse_args()
    return AppSettings(debug=args.debug, no_wizard=args.no_wizard)


# ---------------------------------------------------------------------------
# RPS calculator
# ---------------------------------------------------------------------------

async def _rps_calculator(stats: Stats, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        prev_req = stats.requests
        await asyncio.sleep(1)
        stats.set_rps(float(stats.requests - prev_req))


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def _run_checker(cfg: RunConfig, settings: AppSettings) -> None:
    """Two-stage checker with a live display."""

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    hits_path = RESULTS_DIR / "hits.txt"
    unresolved_path = RESULTS_DIR / "unresolved.txt"
    hits_file = hits_path.open("a", encoding="utf-8")
    hits_lock = asyncio.Lock()

    pm = ProxyManager(cfg.proxies, remove_on_fail=cfg.remove_bad_proxies, scored=cfg.scraped)
    stats = Stats()
    start_time = time.time()
    proxyless = not cfg.proxies

    if cfg.concurrency > MAX_CONCURRENCY:
        cfg.concurrency = MAX_CONCURRENCY

    # Bump file-descriptor limit (macOS defaults to 256)
    try:
        import resource as _resource
        _soft, _hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
        _resource.setrlimit(_resource.RLIMIT_NOFILE, (_hard if _hard > 1024 else 4096, _hard))
    except Exception:
        pass

    connector = aiohttp.TCPConnector(
        limit=max(cfg.concurrency * 2, 100),
        limit_per_host=0,
        enable_cleanup_closed=True,
        ttl_dns_cache=300,
        # Hold idle sockets open well past aiohttp's 15s default. Both stages
        # hit only two hosts, so a warm pool means a TLS handshake per
        # connection for the whole run instead of one every 15 idle seconds.
        keepalive_timeout=60,
    )
    session = aiohttp.ClientSession(
        connector=connector,
        trust_env=False,
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=8, sock_read=30),
        headers={"User-Agent": "Mozilla/5.0 (compatible; RoValid/1.0)"},
    )

    # Circuit breaker for a single rotating proxy
    cb: CircuitBreaker | None = None
    paused = False
    if pm.is_single and not pm.is_proxyless:
        async def _on_circuit_open():
            nonlocal paused
            paused = True
            stats.inc("circuit_opens")
            await asyncio.sleep(2.0)
            paused = False
        cb = CircuitBreaker(threshold=10, window=2.0, cooldown=2.0, on_open=_on_circuit_open)

    checker = RobloxChecker(
        pm, timeout=cfg.timeout, scraped=cfg.scraped,
        circuit_breaker=cb, stats=stats,
        # Both stages run at once now, so the concurrency the user picked is
        # enforced as one shared in-flight request budget inside the checker
        # rather than as a worker count per stage.
        max_inflight=cfg.concurrency,
    )

    webhook: WebhookSender | None = None
    if cfg.webhook_url and cfg.webhook_message:
        webhook = WebhookSender(cfg.webhook_url, cfg.webhook_message, session, start_time)

    # Duplicate names would each be screened and validated again for the same
    # answer. dict.fromkeys drops them while preserving input order.
    names = list(dict.fromkeys(cfg.usernames))
    duplicates = len(cfg.usernames) - len(names)
    total_names = len(names)

    # Only the last handful of either list is ever rendered, so bounding them
    # keeps a long run from accumulating a list entry per name checked.
    recent_hits: deque[str] = deque(maxlen=16)
    feed: deque[str] = deque(maxlen=16)
    unresolved: list[str] = []

    # Live-display state. Both stages now run concurrently, so screening and
    # validation progress are tracked separately and `stage` only decides
    # which of the two the panel is currently showing.
    state = {
        "stage": 1,
        "screened": 0,
        "screen_total": total_names,
        "validated": 0,
        "cand_total": 0,
    }

    def _live_render():
        if state["stage"] == 1:
            done, total = state["screened"], state["screen_total"]
        else:
            done, total = state["validated"], state["cand_total"]
        return live_card(
            stage=state["stage"],
            stage_done=done,
            stage_total=total,
            works=stats.works,
            taken=stats.taken,
            censored=stats.censored,
            requests=stats.requests,
            batch_requests=stats.batch_requests,
            candidates=stats.candidates,
            ratelimited=stats.ratelimited,
            circuit_opens=stats.circuit_opens,
            rps=stats.rps,
            elapsed=time.time() - start_time,
            proxy_alive=pm.alive_count,
            paused=paused,
            recent=list(recent_hits),
            feed=list(feed),
        )

    stop_rps = asyncio.Event()
    rps_task = asyncio.create_task(_rps_calculator(stats, stop_rps))
    webhook_task = asyncio.create_task(webhook.run()) if webhook else None

    async def _record_hit(name: str) -> None:
        stats.inc_works()
        async with hits_lock:
            hits_file.write(f"{name}\n")
            hits_file.flush()
        recent_hits.append(name)
        feed.append(f"[{C.SUCCESS}]+[/] {name}")
        if webhook:
            webhook.enqueue(name)

    if duplicates:
        console.print(
            f"[{C.MUTED}]Skipped {duplicates} duplicate name"
            f"{'s' if duplicates != 1 else ''} in the input[/]"
        )

    console.print()
    try:
        with Live(_live_render(), refresh_per_second=4, console=console) as live:

            # ── Stages 1 and 2, pipelined ─────────────────────────────────
            #
            # These used to run strictly one after the other: every chunk was
            # screened, and only then did validation start. Stage 1 compresses
            # 200 names into one request while stage 2 spends one request per
            # survivor, so stage 2 is far and away the longer of the two - and
            # all of stage 1's wall clock was being spent with the validator
            # idle.
            #
            # Now stage 1 publishes survivors to a queue as each chunk comes
            # back and stage 2 drains it concurrently, so screening time hides
            # almost entirely inside validation time. The validators block on
            # an empty queue, which is also what keeps the combined request
            # rate honest early on: they only go wide once there is a backlog
            # to go wide on.
            cand_queue: asyncio.Queue[str | None] = asyncio.Queue()

            def _enqueue(batch: list[str]) -> None:
                for n in batch:
                    cand_queue.put_nowait(n)
                stats.inc("candidates", len(batch))
                state["cand_total"] += len(batch)

            # Both pools are sized to the full concurrency setting. That is
            # not double the load: `checker` holds a shared semaphore of
            # cfg.concurrency in-flight requests, so the two stages compete
            # for one budget and whichever has work pending uses it. A fixed
            # split would starve stage 1 on a low-survival run and stage 2 on
            # a high-survival one.
            n_validators = max(1, cfg.concurrency)
            screen_tasks: list[asyncio.Task] = []

            if cfg.two_stage:
                chunks = [names[i:i + BATCH_MAX] for i in range(0, len(names), BATCH_MAX)]

                chunk_idx = 0
                idx_lock = asyncio.Lock()

                async def _next_chunk():
                    nonlocal chunk_idx
                    async with idx_lock:
                        if chunk_idx >= len(chunks):
                            return None
                        c = chunks[chunk_idx]
                        chunk_idx += 1
                        return c

                async def _screen_worker() -> None:
                    while True:
                        chunk = await _next_chunk()
                        if chunk is None:
                            return
                        taken_set = await checker.batch_screen(session, chunk)
                        if taken_set is None:
                            # Could not resolve - hand the whole chunk to
                            # stage 2 rather than guessing they are free.
                            _enqueue(chunk)
                            feed.append(f"[{C.WARNING}]?[/] chunk unresolved")
                        else:
                            free = [n for n in chunk if n.lower() not in taken_set]
                            stats.inc_taken(len(chunk) - len(free))
                            _enqueue(free)
                        stats.inc("screened", len(chunk))
                        state["screened"] += len(chunk)

                n_screeners = min(len(chunks), max(1, cfg.concurrency))
                screen_tasks = [
                    asyncio.create_task(_screen_worker())
                    for _ in range(n_screeners)
                ]
            else:
                # Validator-only mode: every name is a stage-2 candidate.
                _enqueue(names)
                state["stage"] = 2
                state["screened"] = total_names

            async def _validate_worker() -> None:
                while True:
                    name = await cand_queue.get()
                    if name is None:  # sentinel: screening finished, queue drained
                        return
                    try:
                        outcome, code = await checker.validate(session, name)
                    except Exception:
                        unresolved.append(name)
                        state["validated"] += 1
                        continue

                    if outcome == AVAILABLE:
                        await _record_hit(name)
                    elif outcome == TAKEN:
                        stats.inc_taken()
                        feed.append(f"[{C.DANGER}]-[/] {name}")
                    elif outcome == CENSORED:
                        stats.inc("censored")
                        feed.append(f"[{C.WARNING}]c[/] {name}")
                    elif outcome == INVALID:
                        stats.inc("invalid")
                    else:
                        unresolved.append(name)
                        feed.append(f"[{C.WARNING}]?[/] {name}")

                    state["validated"] += 1
                    if proxyless:
                        await asyncio.sleep(0.5)

            vtasks = [
                asyncio.create_task(_validate_worker())
                for _ in range(n_validators)
            ]

            async def _seal_queue() -> None:
                """Once screening is done, tell the validators when to stop."""
                if screen_tasks:
                    await asyncio.gather(*screen_tasks, return_exceptions=True)
                    # Screening is over, so the panel switches to stage 2 and
                    # the candidate total is final.
                    state["stage"] = 2
                for _ in range(n_validators):
                    cand_queue.put_nowait(None)

            seal_task = asyncio.create_task(_seal_queue())

            running = [*screen_tasks, *vtasks, seal_task]
            pending = set(running)
            while pending:
                # asyncio.wait sleeps on the tasks themselves rather than
                # re-polling `.done()` over every worker four times a second.
                _, pending = await asyncio.wait(pending, timeout=0.25)
                live.update(_live_render())
            await asyncio.gather(*running, return_exceptions=True)

            live.update(_live_render())

    except asyncio.CancelledError:
        pass
    finally:
        stop_rps.set()
        rps_task.cancel()
        if webhook:
            try:
                await webhook.flush()
            except Exception:
                pass
        if webhook_task:
            webhook_task.cancel()

    elapsed = time.time() - start_time
    snap = stats.snapshot()

    hits_file.close()
    await session.close()
    await asyncio.sleep(0.1)  # let the connector close cleanly

    # Names we could not resolve get written out so a re-run can retry
    # exactly those, instead of silently vanishing from the results.
    if unresolved:
        unresolved_path.write_text("\n".join(unresolved), encoding="utf-8")

    final_summary(
        requests=snap["requests"],
        batch_requests=snap["batch_requests"],
        works=snap["works"],
        taken=snap["taken"],
        censored=snap["censored"],
        invalid=snap["invalid"],
        unresolved=len(unresolved),
        ratelimited=snap["ratelimited"],
        elapsed=elapsed,
        peak_rps=snap["peak_rps"],
        best_streak=snap["best_streak"],
        total_names=total_names,
    )

    if unresolved:
        console.print(
            f"[{C.WARNING}]{len(unresolved)}[/] names unresolved -> "
            f"[{C.MUTED}]results/unresolved.txt[/] (re-run with that as your input file)"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    settings = parse_args()
    set_debug(settings.debug)

    ensure_dir(DATA_DIR, LOGS_DIR, RESULTS_DIR)
    ensure_file(DATA_DIR / "config.json")
    ensure_file(DATA_DIR / "proxies.txt")
    ensure_file(DATA_DIR / "names_to_check.txt")

    cfg_store = Config()

    try:
        if settings.no_wizard:
            console.print(banner())
            proxies = load_lines(DATA_DIR / "proxies.txt")
            loaded = load_lines(DATA_DIR / "names_to_check.txt")
            usernames = [n for n in loaded if is_valid_username(n)]
            if not usernames:
                console.print(f"[{C.DANGER}]No valid usernames. Run without --no-wizard first.[/]")
                sys.exit(1)
            skipped = len(loaded) - len(usernames)
            run_config = RunConfig(
                proxies=proxies,
                remove_bad_proxies=cfg_store.get("remove_proxies", False),
                usernames=usernames,
                concurrency=cfg_store.get("concurrency", 2 if not proxies else 50),
                timeout=cfg_store.get("timeout", 10),
                scraped=False,
                two_stage=cfg_store.get("two_stage", True),
                webhook_url=cfg_store.get("webhook") or None,
                webhook_message=cfg_store.get("webhook_message") or None,
            )
            console.print(
                f"[{C.MUTED}]Skipping wizard - {len(proxies)} proxies, "
                f"{len(usernames)} usernames"
                + (f", {skipped} invalid skipped" if skipped else "") + "[/]"
            )
        else:
            run_config = asyncio.run(setup_wizard(cfg_store, settings))

        asyncio.run(_run_checker(run_config, settings))
    except (EOFError, KeyboardInterrupt):
        console.print(f"\n[{C.WARNING}]Aborted.[/]")
        sys.exit(0)


if __name__ == "__main__":
    main()
