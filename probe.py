#!/usr/bin/env python3
"""Measure what Roblox's bulk-username endpoint actually charges for.

An earlier probe found that twelve single-name requests go through
back-to-back with no 429, while real 200-name runs get throttled after one
or two. That rules out a plain per-request limit and points at a cost that
scales with the number of usernames in the payload - which, if true, means
the batch size is a tuning knob rather than a fixed 200.

This maps the cost: for each batch size, how many back-to-back requests get
through before the endpoint says no. If the limit is per request, the count
is flat across sizes. If it is per username, count x size stays roughly
constant instead, and the product is the real quota.

Around 60 requests over about four minutes. Run it on an idle connection.
"""

from __future__ import annotations

import asyncio
import string
import sys
import time

import aiohttp

from config import BATCH_ENDPOINT

SIZES = [1, 25, 50, 100, 200]
MAX_TRIES = 10        # per size, so one size cannot run away with the budget
REST = 40.0           # silence between sizes, to start each from a full bucket


def names(n: int, salt: int) -> list[str]:
    """Distinct throwaway names, so nothing is served from a cache."""
    al = string.ascii_lowercase
    return [
        f"{al[(salt + i) % 26]}{al[(i // 26) % 26]}{(salt * 7 + i) % 10}"
        f"{al[(i * 3) % 26]}{(i * 13 + salt) % 10}"
        for i in range(n)
    ]


async def send(session: aiohttp.ClientSession, batch: list[str]) -> int:
    try:
        async with session.post(
            BATCH_ENDPOINT,
            json={"usernames": batch, "excludeBannedUsers": False},
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            await r.read()
            return r.status
    except Exception as exc:
        print(f"    (error: {type(exc).__name__})")
        return 0


async def countdown(seconds: float, label: str) -> None:
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        print(f"\r  {label} {left:4.0f}s ", end="", flush=True)
        await asyncio.sleep(min(1.0, left))
    print("\r" + " " * 50 + "\r", end="")


async def main() -> None:
    print("\nRoValid batch-cost probe")
    print("=" * 62)
    print("~60 requests, ~4 minutes. Close any running RoValid first.\n")

    rows = []
    async with aiohttp.ClientSession(trust_env=False) as session:
        for idx, size in enumerate(SIZES):
            await countdown(REST, f"resting before the {size}-name test -")
            print(f"Batch size {size}:")
            ok = 0
            for i in range(MAX_TRIES):
                st = await send(session, names(size, idx * 31 + i))
                if st == 200:
                    ok += 1
                    print(f"  request {i + 1}: 200")
                elif st == 429:
                    print(f"  request {i + 1}: 429  <- shut after {ok} through")
                    break
                else:
                    print(f"  request {i + 1}: HTTP {st}")
                    break
            else:
                print(f"  no 429 in {MAX_TRIES} requests")
            rows.append((size, ok))
            print(f"  => {ok} requests = {ok * size} usernames\n")

    print("=" * 62)
    print(f"{'batch size':>12} {'requests OK':>13} {'usernames through':>19}")
    for size, ok in rows:
        print(f"{size:>12} {ok:>13} {ok * size:>19}")
    print("=" * 62)
    print("\nIf 'requests OK' is flat      -> the limit counts requests.")
    print("If 'usernames through' is flat -> the limit counts usernames,")
    print("   and that number is the real quota per window.\n")
    print("Paste everything above back to Claude.\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\naborted\n")
        sys.exit(0)
