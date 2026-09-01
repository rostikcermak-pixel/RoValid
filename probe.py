#!/usr/bin/env python3
"""Measure Roblox's proxyless rate limiter from THIS machine.

RoValid's backoff policy is only as good as its guess at two numbers: how
many requests the bucket gives before it shuts, and how long it must stay
shut before it reopens. The endpoint sends no Retry-After, so neither can
be read off a response - they have to be measured.

Sends about 20 requests over roughly four minutes. Run it on an idle
connection (stop any RoValid run first) or the readings will be garbage.
"""

from __future__ import annotations

import asyncio
import sys
import time

import aiohttp

from config import BATCH_ENDPOINT

REST = 45.0          # silence before a measurement, to start from a full bucket
WAITS = [5, 10, 15, 20, 30, 45]
PROBE_NAMES = ["roblox"]


async def one(session: aiohttp.ClientSession) -> int:
    """Fire a single batch request. Returns the HTTP status (0 = error)."""
    try:
        async with session.post(
            BATCH_ENDPOINT,
            json={"usernames": PROBE_NAMES, "excludeBannedUsers": False},
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            await r.read()
            return r.status
    except Exception as exc:
        print(f"    (request error: {type(exc).__name__})")
        return 0


async def countdown(seconds: float, label: str) -> None:
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        print(f"\r  {label} {left:4.0f}s ", end="", flush=True)
        await asyncio.sleep(min(1.0, left))
    print("\r" + " " * 40 + "\r", end="")


async def main() -> None:
    print("\nRoValid limiter probe")
    print("=" * 58)
    print("Roughly 4 minutes, ~20 requests. Keep the connection otherwise")
    print("idle - close any running RoValid first.\n")

    async with aiohttp.ClientSession(trust_env=False) as session:

        # -- How many requests does a rested bucket give? -------------------
        await countdown(REST, "resting the bucket, starting in")
        print("Test 1: how many back-to-back requests before a 429?")
        burst = 0
        for i in range(12):
            st = await one(session)
            if st == 429:
                print(f"  request {i + 1}: 429  <- shut after {burst} that went through")
                break
            if st == 200:
                burst += 1
                print(f"  request {i + 1}: 200")
            else:
                print(f"  request {i + 1}: HTTP {st}")
        else:
            print(f"  no 429 in 12 requests (bucket is at least that wide)")
        print(f"\n  => burst size: {burst}\n")

        # -- How long must it stay quiet to reopen? -------------------------
        print("Test 2: after a 429, how much silence before one request works?")
        results = {}
        for w in WAITS:
            await countdown(w, f"silent for {w}s -")
            st = await one(session)
            ok = st == 200
            results[w] = ok
            print(f"  waited {w:2d}s -> {'200 OK' if ok else f'HTTP {st}'}")
            if not ok:
                # That 429 restarts the clock, so rest before the next reading.
                await countdown(REST, "  re-resting, starting in")
        print()

        # -- Verdict --------------------------------------------------------
        print("=" * 58)
        working = [w for w, ok in results.items() if ok]
        print(f"burst size            : {burst}")
        if working:
            print(f"shortest silence OK   : {min(working)}s")
            print(f"waits that worked     : {', '.join(str(w) + 's' for w in working)}")
        else:
            print("shortest silence OK   : none of the tested waits worked")
        failed = [w for w, ok in results.items() if not ok]
        if failed:
            print(f"waits that failed     : {', '.join(str(w) + 's' for w in failed)}")
        print("=" * 58)
        print("\nPaste everything above back to Claude.\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\naborted\n")
        sys.exit(0)
