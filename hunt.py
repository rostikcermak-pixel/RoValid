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
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

import watchlist
from config import BATCH_MAX, Stats, is_valid_username
from engine import AVAILABLE, MALFORMED_CHUNK, RobloxChecker, SharedCooldown
from proxy import ProxyManager
from rarity import rate

# The live file the page reads. It belongs to the scheduled run, which is
# the only thing that should ever write it: a local test run holds a stale
# snapshot, and committing that resets the totals the schedule has been
# accumulating. It happened once - a hand-committed copy rolled the board
# back from 4,000 names screened to 800 - so a local run now has to say
# --out docs/hits.json to touch it at all.
OUT = Path("docs/hits.json")
LOCAL_OUT = Path("hits.local.json")

# The page shows a column per length, newest first, and nobody scrolls past a
# few dozen. Keeping the file small also keeps every scheduled run's commit
# small, which matters when it commits every few minutes forever.
# How often the board may be rewritten mid-run. The scheduled job commits
# whatever it sees, so this is really "how fresh the site is" - against a run
# that now lasts most of an hour, twenty seconds is plenty.
WRITE_EVERY = 20.0
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


def load_existing(out: Path) -> dict:
    if not out.exists():
        return {"updated": None, "lengths": {}, "totals": {}}
    try:
        return json.loads(out.read_text(encoding="utf-8"))
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
    record: Callable[[str], None],
) -> tuple[int, int]:
    """Screen names for *budget*s, calling *record* on each free one.

    Finds are reported as they happen rather than returned in a batch: the
    board is meant to be sat on, and a name that surfaces in the first
    minute of an hour-long run should not wait for the run to end.
    """
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
                record(name)
    return checked, survivors_seen


async def watch_pass(
    checker: RobloxChecker,
    session: aiohttp.ClientSession,
    names: list[str],
    budget: float,
    record: Callable[[str], None],
    start: int = 0,
) -> tuple[int, int]:
    """Re-check the watchlist and return any name that has come free.

    This is the pass worth having. The random hunt finds licence plates
    because that is all that is left unclaimed; a name anyone would want is
    taken, and only shows up here, on the run after somebody renamed away
    from it.
    """
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
                record(name)
        cursor = (start + i + len(chunk)) % len(names)
    return checked, cursor


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=8.0,
                    help="total wall-clock budget (default 8)")
    ap.add_argument("--lengths", default="3,4,5")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-watch", action="store_true",
                    help="skip the watchlist pass and only draw random names")
    ap.add_argument("--out", type=Path, default=LOCAL_OUT,
                    help=f"where to write results (default {LOCAL_OUT}; the "
                         f"scheduled run passes {OUT})")
    args = ap.parse_args()

    lengths = [int(x) for x in args.lengths.split(",") if x.strip()]
    out = args.out
    data = load_existing(out)
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
    fresh_total = 0
    last_write = 0.0

    def stamp() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def publish(force: bool = False) -> None:
        """Write the board out mid-run.

        The scheduled job commits this file while the hunt is still going, so
        a reader can catch it at any moment. The write goes to a temp file and
        is renamed over the real one, which is atomic on POSIX - a reader sees
        either the old file or the new one, never half a JSON document.

        Throttled, because a good minute can turn up finds faster than there
        is any point rewriting the file.
        """
        nonlocal last_write
        if not force and time.monotonic() - last_write < WRITE_EVERY:
            return
        last_write = time.monotonic()
        data["updated"] = stamp()
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        tmp.replace(out)


    async def work(session) -> None:
        nonlocal fresh_total

        if not args.no_watch:
            names = watchlist.build(lengths)
            data["watching"] = len(names)
            already = {e["name"] for e in data["released"]}
            freed = 0

            def release(name: str) -> None:
                nonlocal freed
                if name in already:
                    return            # still free from an earlier run
                already.add(name)
                freed += 1
                tier, weight = rate(name)
                data["released"].insert(0, {
                    "name": name, "found": stamp(), "tier": tier,
                    "weight": weight,
                })
                del data["released"][KEEP_RELEASED:]
                publish()

            watched, cursor = await watch_pass(
                checker, session, names, watch_budget, release,
                start=int(data.get("watch_cursor", 0)) % max(len(names), 1),
            )
            data["watch_cursor"] = cursor
            print(f"  watchlist: {watched:,} of {len(names):,} re-checked "
                  f"(resuming at {cursor:,}), {freed} free", flush=True)
            publish(force=True)

        for length in lengths:
            budget = seconds * weights.get(length, 4.0) / total_w
            key = str(length)
            totals = data["totals"].setdefault(key, {"checked": 0, "found": 0})
            hits = 0

            def keep(name: str, key=key, totals=totals) -> None:
                nonlocal fresh_total, hits
                tier, weight = rate(name)
                entries = data["lengths"].setdefault(key, [])
                entries.append({
                    "name": name, "found": stamp(),
                    "tier": tier, "weight": weight,
                })
                entries.sort(key=_order)
                del entries[KEEP_PER_LENGTH:]
                totals["found"] += 1
                fresh_total += 1
                hits += 1
                publish()

            checked, survivors = await hunt_length(
                checker, session, length, budget, seen, keep,
            )
            totals["checked"] += checked
            print(
                f"  len {length}: {budget:.0f}s, screened {checked:,}, "
                f"survivors {survivors:,}, free {hits}", flush=True,
            )
            publish(force=True)

    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0 (compatible; RoValid/1.0)"},
        timeout=aiohttp.ClientTimeout(total=20, sock_connect=8),
    ) as session:
        # The per-pass budgets are checked between requests, and a single
        # request can outlast one by minutes when the limiter is cooling
        # things down - a 90-second run was still going at 300. Left alone
        # that overruns the scheduled job's timeout, the job is killed, and
        # the run commits nothing at all. This is the hard stop: whatever has
        # been found by then still gets written.
        hard_cap = args.minutes * 60 * 1.6
        try:
            await asyncio.wait_for(work(session), timeout=hard_cap)
        except TimeoutError:
            print(f"  (hit the {hard_cap:.0f}s hard stop - saving what we have)",
                  flush=True)

    publish(force=True)
    print(f"\n{fresh_total} new free name(s) -> {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
