#!/usr/bin/env python3
"""RoValid bench - measure the rate limiter instead of inferring it from 429s.

The checker's pacing rests on assumptions about Roblox's limiter that were
worked out by watching 429s land. This measures it directly, and the numbers
turn out to be nothing like the assumptions.

Three things it establishes:

1. Both endpoints publish their own limiter state on every response,
   including on a 429:

       x-ratelimit-limit:     500, 500;w=60
       x-ratelimit-remaining: 499
       x-ratelimit-reset:     39

2. They are two independent buckets. Load on one does not touch the other,
   so the fastest configuration runs both flat out at the same time.

3. The two buckets are throttled *very* differently, and which one is the
   bottleneck depends entirely on your IP. The checker treats the batch
   endpoint as the fast path and the validator as a rare follow-up, which is
   the right call on a clean IP and badly wrong on a throttled one.

Run it from the machine you actually check from. The answer is per-IP, so a
number measured anywhere else is not your number.

    python bench.py              # everything, ~4 minutes
    python bench.py --headers    # limiter headers, one request each
    python bench.py --cap        # is 200 really the batch ceiling?
    python bench.py --batch      # sustained rate, batch endpoint
    python bench.py --validate   # sustained rate, validator
    python bench.py --dual       # are the two buckets independent?
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import random
import string
import sys
import time

import aiohttp

BATCH_ENDPOINT = "https://users.roblox.com/v1/usernames/users"
VALIDATE_ENDPOINT = "https://auth.roblox.com/v1/usernames/validate"
BATCH_MAX = 200

# Roblox's edge 429s some User-Agents regardless of remaining quota - a bare
# Python-urllib UA is refused outright. Send what the checker sends, or the
# bench measures a limiter the checker never meets.
UA = {"User-Agent": "Mozilla/5.0 (compatible; RoValid/1.0)"}

SETTLE = 10.0  # seconds between measurements, so each starts from a clean bucket


def _names(n: int = 1) -> list[str]:
    """Names that cannot plausibly exist.

    Keeps the response a constant, near-empty size, so the bench measures the
    limiter rather than how long it takes to serialise a big result.
    """
    return [
        "zq" + "".join(random.choices(string.ascii_lowercase, k=8))
        for _ in range(n)
    ]


def _validate_params() -> dict:
    return {
        "request.username": _names(1)[0],
        "request.birthday": "2000-01-01",
        "request.context": "Signup",
    }


def read_limiter(headers) -> str:
    """Format the limiter headers a response carries, if any."""
    raw = headers.get("x-ratelimit-limit")
    if not raw:
        return "no limiter headers"
    quota, _, window = raw.partition(";")
    quota = quota.split(",")[0].strip()
    window = window[2:].strip() if window.startswith("w=") else "?"
    return (
        f"limit={quota}/{window}s  "
        f"remaining={headers.get('x-ratelimit-remaining')}  "
        f"resets in {headers.get('x-ratelimit-reset')}s"
    )


# ---------------------------------------------------------------------------
# Load generators
# ---------------------------------------------------------------------------

async def _paced(session, secs: float, rate: float, fire) -> tuple[collections.Counter, float]:
    """Fire *rate* requests/sec for *secs*, without waiting on each one.

    Requests are launched on a schedule and awaited at the end. Awaiting each
    in turn would let a slow response slide the schedule, and the bench would
    quietly measure a slower pace than the one it reports.
    """
    counts: collections.Counter = collections.Counter()
    started = time.monotonic()
    next_send = started
    tasks: list[asyncio.Task] = []
    while time.monotonic() - started < secs:
        now = time.monotonic()
        if now < next_send:
            await asyncio.sleep(next_send - now)
        tasks.append(asyncio.create_task(fire(session, counts)))
        next_send += 1.0 / rate
    if tasks:
        await asyncio.gather(*tasks)
    return counts, time.monotonic() - started


async def _fire_batch(session, counts) -> None:
    payload = {"usernames": _names(BATCH_MAX), "excludeBannedUsers": False}
    try:
        async with session.post(BATCH_ENDPOINT, json=payload) as resp:
            await resp.read()
            counts[resp.status] += 1
            counts["limiter"] = read_limiter(resp.headers)
    except Exception as exc:
        counts[type(exc).__name__] += 1


async def _fire_validate(session, counts) -> None:
    try:
        async with session.get(VALIDATE_ENDPOINT, params=_validate_params()) as resp:
            await resp.read()
            counts[resp.status] += 1
            counts["limiter"] = read_limiter(resp.headers)
    except Exception as exc:
        counts[type(exc).__name__] += 1


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_headers(session) -> None:
    print("What each endpoint says about its own limiter\n")
    async with session.post(
        BATCH_ENDPOINT,
        json={"usernames": _names(1), "excludeBannedUsers": False},
    ) as resp:
        print(f"  batch     HTTP {resp.status}   {read_limiter(resp.headers)}")
    async with session.get(VALIDATE_ENDPOINT, params=_validate_params()) as resp:
        print(f"  validate  HTTP {resp.status}   {read_limiter(resp.headers)}")
    print(
        "\n  These arrive on 429s too, so a checker never has to guess at its\n"
        "  remaining budget. Note the published quota is not always the\n"
        "  binding constraint - a short-window burst limiter sits in front of\n"
        "  it, and that one is unpublished. --batch measures the real one."
    )


async def cmd_cap(session) -> None:
    print("Batch size ceiling\n")
    for n in (100, 200, 201, 250, 500):
        payload = {"usernames": _names(n), "excludeBannedUsers": False}
        try:
            async with session.post(BATCH_ENDPOINT, json=payload) as resp:
                detail = "accepted" if resp.status == 200 else (await resp.text())[:70]
                print(f"  {n:>4} names   HTTP {resp.status}   {detail}")
        except Exception as exc:
            print(f"  {n:>4} names   ERROR {type(exc).__name__}")
        await asyncio.sleep(2.0)


async def _sweep(session, label: str, fire, rates, per_request: int, secs: float) -> None:
    print(f"Sustained rate - {label} ({per_request} name(s) per request)\n")
    print(f"  {'target':>9}  {'sent':>5}  {'ok':>5}  {'429':>4}  {'err':>4}  {'names/sec':>10}")
    print("  " + "-" * 48)
    best = (0.0, None)
    for rate in rates:
        counts, elapsed = await _paced(session, secs, rate, fire)
        ok = counts.get(200, 0)
        limited = counts.get(429, 0)
        errs = sum(
            v for k, v in counts.items()
            if isinstance(k, str) and k != "limiter"
        ) + sum(v for k, v in counts.items() if isinstance(k, int) and k not in (200, 429))
        throughput = ok * per_request / elapsed
        flag = "" if limited == 0 else "   <- throttled"
        print(
            f"  {rate:>7.1f}/s  {ok + limited + errs:>5}  {ok:>5}  "
            f"{limited:>4}  {errs:>4}  {throughput:>10,.1f}{flag}"
        )
        if limited == 0 and throughput > best[0]:
            best = (throughput, counts.get("limiter"))
        await asyncio.sleep(SETTLE)

    print()
    if best[1] is None:
        print("  Every pace drew 429s - this IP is already throttled or shared.")
    else:
        print(f"  Best clean throughput: {best[0]:,.0f} names/sec")
        print(f"  Endpoint reported:     {best[1]}")
        print(f"  One million names:     {1_000_000 / best[0] / 60:.1f} minutes")


async def cmd_batch(session, secs: float) -> None:
    await _sweep(
        session, "batch endpoint", _fire_batch,
        (0.25, 0.5, 1.0, 2.0, 4.0, 8.0), BATCH_MAX, secs,
    )


async def cmd_validate(session, secs: float) -> None:
    await _sweep(
        session, "validate endpoint", _fire_validate,
        (10, 25, 60, 100, 250, 400), 1, secs,
    )


async def cmd_dual(session, secs: float) -> None:
    """Does load on one bucket cost the other anything?"""
    print("Bucket independence - each alone, then both at once\n")

    async def measure(use_batch: bool, use_validate: bool):
        counts: collections.Counter = collections.Counter()
        started = time.monotonic()
        jobs = []
        if use_validate:
            jobs.append(_paced(session, secs, 150, _fire_validate))
        if use_batch:
            jobs.append(_paced(session, secs, 2.0, _fire_batch))
        results = await asyncio.gather(*jobs)
        for sub, _ in results:
            for k, v in sub.items():
                if k != "limiter":
                    counts[k] += v
        return counts, time.monotonic() - started

    rows = []
    for label, ub, uv in (
        ("validate alone", False, True),
        ("batch alone", True, False),
        ("both at once", True, True),
    ):
        counts, elapsed = await measure(ub, uv)
        ok = counts.get(200, 0)
        # Only one generator runs in the "alone" rows, so ok is unambiguous;
        # in the combined row the two are summed and split by request size.
        rows.append((label, ok, counts.get(429, 0), elapsed))
        print(f"  {label:<16} ok={ok:>5}  429={counts.get(429, 0):>4}  over {elapsed:.0f}s")
        await asyncio.sleep(SETTLE)

    print(
        "\n  If 'both at once' lands near the sum of the two alone, the buckets\n"
        "  are independent and the checker should be saturating both rather\n"
        "  than parking stage-2 capacity while stage 1 waits out a cooldown."
    )


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--headers", action="store_true", help="limiter headers only")
    ap.add_argument("--cap", action="store_true", help="probe the batch ceiling")
    ap.add_argument("--batch", action="store_true", help="sustained rate, batch endpoint")
    ap.add_argument("--validate", action="store_true", help="sustained rate, validator")
    ap.add_argument("--dual", action="store_true", help="bucket independence test")
    ap.add_argument("--duration", type=float, default=12.0,
                    help="seconds per measurement (default 12)")
    args = ap.parse_args()

    connector = aiohttp.TCPConnector(limit=512, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=20, sock_connect=8)
    async with aiohttp.ClientSession(
        headers=UA, connector=connector, timeout=timeout,
    ) as session:
        picked = args.headers or args.cap or args.batch or args.validate or args.dual
        if args.headers or not picked:
            await cmd_headers(session)
            print()
        if args.cap or not picked:
            await cmd_cap(session)
            print()
        if args.batch or not picked:
            await cmd_batch(session, args.duration)
            print()
        if args.validate or not picked:
            await cmd_validate(session, args.duration)
            print()
        if args.dual or not picked:
            await cmd_dual(session, args.duration)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
