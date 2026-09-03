#!/usr/bin/env python3
"""Headless hunter for the live site.

Runs on a schedule with no terminal attached: draws fresh names, screens and
confirms them, and merges anything free into docs/hits.json for the page to
read. Everything the site shows comes out of that one file, so the page needs
no server and no database.

    python hunt.py --minutes 8 --lengths 3,4,5,6
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from config import BATCH_MAX, Stats, is_valid_username
from engine import AVAILABLE, MALFORMED_CHUNK, RobloxChecker, SharedCooldown
from proxy import ProxyManager
from rarity import rate

OUT = Path("docs/hits.json")

# The page shows a column per length, newest first, and nobody scrolls past a
# few dozen. Keeping the file small also keeps every scheduled run's commit
# small, which matters when it commits every few minutes forever.
KEEP_PER_LENGTH = 60

# Ordering on the page. Free names are not scarce - a one-minute sample found
# 158 free five-character names, none of them digit-free - so listing by
# recency alone buries the one name anyone wants under a wall of licence
# plates. Rarity first, then newest, keeps the good ones visible.
def _order(entry: dict) -> tuple:
    # Rarest first; within a tier, newest first. `found` is an ISO timestamp,
    # so reversing the string sorts the dates without parsing them.
    return (-entry.get("weight", 0), _desc(entry.get("found", "")))


def _desc(iso: str) -> tuple:
    """Sort key that orders ISO timestamps newest-first."""
    return tuple(-ord(c) for c in iso)

GEN_CHARS = string.ascii_lowercase + string.digits


def draw(length: int, count: int, exclude: set[str]) -> list[str]:
    """Fresh candidate names of *length*, skipping anything already seen."""
    out: list[str] = []
    seen = set()
    # Bounded rather than "until we have count": at length 3 the whole space
    # is 46,656 names and almost all of them are already in `exclude`, so an
    # unbounded loop would spin forever near exhaustion.
    for _ in range(count * 40):
        if len(out) >= count:
            break
        name = "".join(random.choices(GEN_CHARS, k=length))
        if name in seen or name in exclude or not is_valid_username(name):
            continue
        seen.add(name)
        out.append(name)
    return out


def load_existing() -> dict:
    if not OUT.exists():
        return {"updated": None, "lengths": {}, "totals": {}}
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A half-written file must not stop the next run from producing a
        # good one - the schedule has no operator to notice it failed.
        return {"updated": None, "lengths": {}, "totals": {}}


async def hunt_length(
    checker: RobloxChecker,
    session: aiohttp.ClientSession,
    length: int,
    budget: float,
    seen: set[str],
) -> tuple[list[str], int, int]:
    """Return (free names, names screened, stage-2 survivors) in *budget*s."""
    found: list[str] = []
    checked = 0
    survivors_seen = 0
    started = time.monotonic()

    while time.monotonic() - started < budget:
        names = draw(length, BATCH_MAX, seen)
        if not names:
            break                      # space exhausted for this length
        seen.update(names)

        taken = await checker.batch_screen(session, names)
        if taken is None or taken is MALFORMED_CHUNK:
            continue
        checked += len(names)

        survivors = [n for n in names if n.lower() not in taken]
        survivors_seen += len(survivors)
        for name in survivors:
            if time.monotonic() - started >= budget:
                break
            outcome, _code = await checker.validate(session, name)
            if outcome == AVAILABLE:
                found.append(name)
    return found, checked, survivors_seen


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=8.0,
                    help="total wall-clock budget (default 8)")
    ap.add_argument("--lengths", default="3,4,5")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    lengths = [int(x) for x in args.lengths.split(",") if x.strip()]
    data = load_existing()
    data.setdefault("lengths", {})
    data.setdefault("totals", {})

    # Never re-offer a name the site has already listed, and never re-check
    # one we have already resolved.
    seen: set[str] = set()
    for entries in data["lengths"].values():
        seen.update(e["name"] for e in entries)

    pm = ProxyManager([], remove_on_fail=False, scored=False)
    stats = Stats()
    checker = RobloxChecker(
        pm, timeout=10, scraped=False, stats=stats,
        max_inflight=args.workers, cooldown=SharedCooldown(),
    )

    # An even split wastes most of a run. Every 3- and 4-character name is
    # already taken - an exhaustive sweep of the 1,679,616 four-character
    # names turned up nothing - so those columns are a standing check that
    # the answer has not changed, not a search. Length 5 is where finds
    # actually come from and gets the bulk of the time.
    weights = {3: 1.0, 4: 1.0}
    total_w = sum(weights.get(n, 4.0) for n in lengths)
    seconds = args.minutes * 60
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fresh_total = 0

    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0 (compatible; RoValid/1.0)"},
        timeout=aiohttp.ClientTimeout(total=20, sock_connect=8),
    ) as session:
        for length in lengths:
            budget = seconds * weights.get(length, 4.0) / total_w
            found, checked, survivors = await hunt_length(
                checker, session, length, budget, seen,
            )
            key = str(length)
            entries = data["lengths"].get(key, [])
            for name in found:
                tier, weight = rate(name)
                entries.append({
                    "name": name, "found": now,
                    "tier": tier, "weight": weight,
                })
            entries.sort(key=_order)
            data["lengths"][key] = entries[:KEEP_PER_LENGTH]
            totals = data["totals"].setdefault(key, {"checked": 0, "found": 0})
            totals["checked"] += checked
            totals["found"] += len(found)
            fresh_total += len(found)
            print(
                f"  len {length}: {budget:.0f}s, screened {checked:,}, "
                f"survivors {survivors:,}, free {len(found)}", flush=True,
            )

    data["updated"] = now
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    print(f"\n{fresh_total} new free name(s) -> {OUT}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
