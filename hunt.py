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
import watchlist

OUT = Path("docs/hits.json")

# The page shows a column per length, newest first, and nobody scrolls past a
# few dozen. Keeping the file small also keeps every scheduled run's commit
# small, which matters when it commits every few minutes forever.
KEEP_PER_LENGTH = 60
KEEP_RELEASED = 40

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


async def watch_pass(
    checker: RobloxChecker,
    session: aiohttp.ClientSession,
    names: list[str],
    budget: float,
    start: int = 0,
) -> tuple[list[str], int, int]:
    """Re-check the watchlist and return any name that has come free.

    This is the pass worth having. The random hunt finds licence plates
    because that is all that is left unclaimed; a name anyone would want is
    taken, and only shows up here, on the run after somebody renamed away
    from it.
    """
    freed: list[str] = []
    checked = 0
    started = time.monotonic()
    # Resume where the last run stopped and wrap around. A budget always
    # cuts this pass short, so starting from zero every time would watch the
    # first few hundred names forever and never once look at the rest.
    order = names[start:] + names[:start]
    cursor = start

    for i in range(0, len(order), BATCH_MAX):
        # A run on a schedule has a hard job timeout, and this pass has no
        # natural end - it will screen every name it is given and validate
        # every survivor. Without a deadline it eats the whole run, which is
        # exactly what it did the first time.
        if time.monotonic() - started >= budget:
            break
        chunk = order[i:i + BATCH_MAX]
        taken = await checker.batch_screen(session, chunk)
        if taken is None or taken is MALFORMED_CHUNK:
            continue
        checked += len(chunk)
        for name in chunk:
            if name.lower() in taken:
                continue
            if time.monotonic() - started >= budget:
                break
            # Stage 1 only says no account holds it. The validator is what
            # separates a genuine release from a name Roblox censors or
            # reserves, and on a watchlist that distinction is the point.
            outcome, _code = await checker.validate(session, name)
            if outcome == AVAILABLE:
                freed.append(name)
        cursor = (start + i + len(chunk)) % len(names)
    return freed, checked, cursor


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=8.0,
                    help="total wall-clock budget (default 8)")
    ap.add_argument("--lengths", default="3,4,5")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-watch", action="store_true",
                    help="skip the watchlist pass and only draw random names")
    args = ap.parse_args()

    lengths = [int(x) for x in args.lengths.split(",") if x.strip()]
    data = load_existing()
    data.setdefault("lengths", {})
    data.setdefault("totals", {})
    data.setdefault("released", [])

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
    # The watchlist gets the first slice because it is the pass that can
    # actually surface a name worth having; the random draw fills whatever
    # time is left.
    watch_budget = 0.0 if args.no_watch else args.minutes * 60 * 0.4
    seconds = args.minutes * 60 - watch_budget
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fresh_total = 0

    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0 (compatible; RoValid/1.0)"},
        timeout=aiohttp.ClientTimeout(total=20, sock_connect=8),
    ) as session:

        if not args.no_watch:
            names = watchlist.build(lengths)
            freed, watched, cursor = await watch_pass(
                checker, session, names, watch_budget,
                start=int(data.get("watch_cursor", 0)) % max(len(names), 1),
            )
            data["watch_cursor"] = cursor
            already = {e["name"] for e in data["released"]}
            for name in freed:
                if name in already:
                    continue          # still free from an earlier run
                tier, weight = rate(name)
                data["released"].insert(0, {
                    "name": name, "found": now, "tier": tier, "weight": weight,
                })
            data["released"] = data["released"][:KEEP_RELEASED]
            data["watching"] = len(names)
            print(f"  watchlist: {watched:,} of {len(names):,} re-checked "
                  f"(resuming at {cursor:,}), {len(freed)} free", flush=True)

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
