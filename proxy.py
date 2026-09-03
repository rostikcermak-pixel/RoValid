#!/usr/bin/env python3
"""RoValid v1.0 - Proxy manager with rotation, cooldowns, and scoring."""

from __future__ import annotations

import asyncio
import bisect
import itertools
import random
import re
import time

import aiohttp

from config import BATCH_ENDPOINT

# Quick proxy-URL sanity check (same logic as wizard._PROXY_RE)
_VALID_PROXY = re.compile(
    r"^https?://(?:[^@\s]+@)?[a-zA-Z0-9](?:[a-zA-Z0-9\-.]*[a-zA-Z0-9])?:\d{1,5}$"
)


# ---------------------------------------------------------------------------
# Pre-flight screen
# ---------------------------------------------------------------------------

# A scraped pool is ~95% corpses. Left to the run, each one costs a full
# request timeout to discover, one at a time, while a worker sits on it - a
# measured run spent 411 batch requests to clear 26 chunks (6.3% success,
# 15.8 attempts per chunk) with only 4 of those failures being real rate
# limits. Discovering the same thing here costs one short timeout each and
# they all happen at once, so the run itself starts against live proxies.
PRESCREEN_CONCURRENCY = 600
PRESCREEN_TIMEOUT = 5.0


async def prescreen(
    proxies: list[str],
    concurrency: int = PRESCREEN_CONCURRENCY,
    timeout: float = PRESCREEN_TIMEOUT,
    on_progress=None,
) -> list[str]:
    """Return the subset of *proxies* that can actually reach Roblox.

    Tests the real endpoint rather than a generic connectivity check: a proxy
    that is up but cannot reach users.roblox.com is useless here, and a
    generic check would keep it.
    """
    if not proxies:
        return []

    # The scrapers hand back bare "host:port". aiohttp's proxy= argument
    # needs a scheme and raises InvalidURL without one, which would fail
    # every proxy instantly and report a pool of zero.
    proxies = [ProxyManager._format(p) for p in proxies if p and p.strip()]

    live: list[str] = []
    gate = asyncio.Semaphore(concurrency)
    done = 0
    payload = {"usernames": ["roblox"], "excludeBannedUsers": False}

    async def _test(session: aiohttp.ClientSession, proxy: str) -> None:
        nonlocal done
        try:
            async with gate:
                async with session.post(
                    BATCH_ENDPOINT, json=payload, proxy=proxy,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    # 429 means the proxy reached Roblox and Roblox pushed
                    # back - that is a working route, just a busy one, and
                    # the run's own cooldown handling deals with it.
                    if resp.status in (200, 429):
                        live.append(proxy)
        except Exception:
            pass
        finally:
            done += 1
            if on_progress and done % 250 == 0:
                on_progress(done, len(proxies), len(live))

    conn = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)
    async with aiohttp.ClientSession(
        connector=conn,
        timeout=aiohttp.ClientTimeout(total=timeout, sock_connect=timeout / 2),
        trust_env=False,
    ) as session:
        await asyncio.gather(
            *(_test(session, p) for p in proxies), return_exceptions=True,
        )

    if on_progress:
        on_progress(len(proxies), len(proxies), len(live))
    return live


class ProxyManager:
    """Manages proxy rotation with per-proxy ratelimit cooldowns.

    For a single proxy (rotating gateway) every request gets a fresh
    IP from the pool - the proxy itself is never marked dead.
    """

    def __init__(
        self,
        proxies: list[str],
        remove_on_fail: bool = True,
        scored: bool = False,
    ) -> None:
        cleaned = [p.strip() if p else None for p in proxies]
        self._proxies: list[str | None] = cleaned if cleaned else [None]
        self._proxyless = all(p is None for p in self._proxies)

        # Pre-format every proxy URL once. `_format` strips and builds an
        # f-string, and the old code ran it on every rotation step - up to
        # 2 * len(proxies) times per request on the static path.
        self._formatted: list[str | None] = [self._format(p) for p in self._proxies]
        self._single: str | None = self._formatted[0] if self._formatted else None

        self._cycle = itertools.cycle(self._formatted)
        self._dead: set[str] = set()
        self._cooldowns: dict[str, float] = {}
        self.remove_on_fail = remove_on_fail
        self._lock = asyncio.Lock()

        # ── scoring (scraped free proxies only) ──
        self._scored = scored
        self._scores: dict[str, int] = {}
        self._rate_limited_until: dict[str, float] = {}
        self._bench_count: dict[str, int] = {}
        if scored:
            for raw, key in zip(self._proxies, self._formatted):
                if raw and raw.strip():
                    self._scores[key or raw.strip()] = 1

        # Cached weighted-pick table for the scored path (see `_rebuild_ready`).
        self._ready_keys: list[str] = []
        self._ready_cum: list[int] = []
        self._ready_total: int = 0
        self._ready_at: float = 0.0
        # Earliest moment a currently-unavailable proxy becomes usable again.
        # Reaching it forces a rebuild, so a cooldown expiring is picked up the
        # instant it lapses rather than whenever the TTL happens to run out.
        self._next_expiry: float = float("inf")

    # ── Properties ──

    @property
    def is_proxyless(self) -> bool:
        return self._proxyless

    @property
    def is_single(self) -> bool:
        """True if exactly one proxy (rotating gateway)."""
        return len(self._proxies) == 1 and not self._proxyless

    @property
    def alive_count(self) -> int:
        if self.is_single:
            return 1
        if self._scored:
            # Benched proxies are still in _scores but cannot be handed out,
            # so counting them would report a pool that is not really there.
            now = time.time()
            return sum(
                1 for k in self._scores
                if self._rate_limited_until.get(k, 0.0) <= now
                and k not in self._dead
            )
        return max(0, len(self._proxies) - len(self._dead))

    @property
    def total_count(self) -> int:
        return len(self._proxies)

    # ── Formatting ──

    @staticmethod
    def _format(raw: str | None) -> str | None:
        if raw is None:
            return None
        raw = raw.strip()
        return f"http://{raw}" if not raw.startswith("http") else raw

    # ── Core: next proxy ──

    # How long a built weighted-pick table stays usable. Scores drift slowly
    # (+5 a hit, -1 a miss), so a pick made against a table a fraction of a
    # second old is indistinguishable from an exact one - and every entry is
    # re-verified against the live dicts before it is handed out anyway.
    READY_CACHE_TTL = 0.5

    def _rebuild_ready(self, now: float) -> None:
        """Rebuild the cumulative-weight table of currently usable proxies.

        Caller must hold `self._lock`. This is the O(n) pass the old code ran
        on *every single request*; it now runs at most twice a second, and the
        per-request cost drops to a bisect over the cached table.
        """
        keys: list[str] = []
        cum: list[int] = []
        total = 0
        next_expiry = float("inf")
        dead = self._dead
        cooldowns = self._cooldowns
        limited = self._rate_limited_until
        for k, v in self._scores.items():
            if not k or v <= 0 or k in dead:
                continue
            cd = cooldowns.get(k, 0.0)
            rl = limited.get(k, 0.0)
            until = cd if cd > rl else rl
            if until > now:
                # Held back only by time - note when it comes back.
                if until < next_expiry:
                    next_expiry = until
                continue
            total += v
            keys.append(k)
            cum.append(total)
        self._ready_keys = keys
        self._ready_cum = cum
        self._ready_total = total
        self._ready_at = now
        self._next_expiry = next_expiry

    def _pick_ready(self, now: float) -> str | None:
        """Weighted-random pick from the cached table, or None if it is empty.

        Caller must hold `self._lock`.
        """
        if self._ready_total <= 0:
            return None
        pick = random.uniform(0, self._ready_total)
        i = bisect.bisect_left(self._ready_cum, pick)
        if i >= len(self._ready_keys):
            i = len(self._ready_keys) - 1
        return self._ready_keys[i]

    async def next(self) -> str | None:
        """Return the next ready proxy, or None if exhausted/proxyless."""
        if self._proxyless:
            return None

        if self.is_single:
            return self._single

        # Scored path (scraped free proxies) - weighted random
        if self._scored:
            deadline = time.time() + 30.0
            while True:
                async with self._lock:
                    now = time.time()
                    if (
                        now - self._ready_at >= self.READY_CACHE_TTL
                        or now >= self._next_expiry
                    ):
                        self._rebuild_ready(now)

                    candidate = self._pick_ready(now)
                    if candidate is not None:
                        # The table may name a proxy that went onto cooldown
                        # since it was built, so confirm against the live
                        # dicts. A stale hit forces one rebuild, and the
                        # retry then picks from a table that is exact as of
                        # `now` - so this never loops more than twice.
                        if (
                            candidate not in self._dead
                            and self._cooldowns.get(candidate, 0.0) <= now
                            and self._rate_limited_until.get(candidate, 0.0) <= now
                            and self._scores.get(candidate, 0) > 0
                        ):
                            return candidate
                        self._rebuild_ready(now)
                        if self._ready_total > 0:
                            continue

                    # Nothing live right now. If everything is permanently
                    # dead we are done; if it is only cooldowns, wait it out.
                    if not self._scores:
                        return None
                    soonest = min(
                        [t for t in list(self._rate_limited_until.values())
                         + list(self._cooldowns.values()) if t > now],
                        default=None,
                    )
                if soonest is None or time.time() > deadline:
                    return None
                await asyncio.sleep(min(max(soonest - time.time(), 0.2), 5.0))

        # Static multi-proxy path. `self._cycle` yields pre-formatted URLs, so
        # a rotation step is now just a dict lookup instead of a strip plus an
        # f-string per candidate.
        while True:
            async with self._lock:
                if len(self._dead) >= len(self._proxies):
                    return None

                now = time.time()
                dead = self._dead
                cooldowns = self._cooldowns
                for _ in range(len(self._proxies) * 2):
                    proxy = next(self._cycle)
                    key = proxy or ""
                    if key in dead:
                        continue
                    if cooldowns.get(key, 0.0) > now:
                        continue
                    return proxy

                earliest = min(
                    (v for v in cooldowns.values() if v > now),
                    default=now + 1,
                )
                wait = max(earliest - now, 0.2)

            await asyncio.sleep(wait)

    # How long to bench a proxy that returned 429 with no number attached.
    # Roblox's 429 carries no Retry-After (verified against the live
    # endpoint), so this is a guess - but a measured one: making it adaptive
    # was tried and was 4-5x SLOWER. With a pool, 429s arrive from many
    # proxies at once, so a pool-wide estimate grows once per proxy per
    # episode while decaying once per episode; it ratchets to the ceiling
    # and benches healthy proxies for far longer than the endpoint needs.
    # A flat value beat every adaptive variant tried, so it stays flat.
    COOLDOWN_DEFAULT = 10.0

    # ── Scoring API (scraped proxies) ──

    def score_hit(self, proxy: str | None) -> None:
        """+5 points for a working proxy."""
        if not self._scored or not proxy:
            return
        if proxy in self._scores:
            self._scores[proxy] = min(self._scores[proxy] + 5, 100)
            # It came back and worked, so it is not a repeat offender - clear
            # the bench history rather than escalating it forever on a proxy
            # that is merely busy sometimes.
            self._bench_count.pop(proxy, None)

    def set_rate_limit(self, proxy: str | None, seconds: float) -> None:
        """Mark proxy as rate-limited until *now + seconds*.

        A 429 means the proxy WORKS - it reached Roblox and got a real
        answer - so it is rewarded, not penalised.
        """
        if not self._scored or not proxy:
            return
        if proxy in self._scores:
            until = time.time() + seconds
            self._rate_limited_until[proxy] = until
            self._scores[proxy] = min(self._scores[proxy] + 1, 100)
            if until < self._next_expiry:
                self._next_expiry = until

    # How long a proxy sits out after its score runs down, growing each time
    # it happens. Free proxies are flaky rather than dead - a timeout usually
    # means busy - so a bad moment costs a rest, not a burial.
    BENCH_BACKOFF = (30.0, 60.0, 120.0, 300.0)

    def score_miss(self, proxy: str | None) -> None:
        """-1 point for a failed proxy. Score <= 0 -> benched, not buried.

        This used to delete the proxy permanently. Every proxy starts on 1
        point, so that was one bad moment each, and nothing ever came back:
        `_dead` only grew. Over a long run the pool drained monotonically and
        throughput went with it - a measured 100,000-name run opened at 627
        names/sec and finished at 0, having run itself out of proxies rather
        than out of names.

        Benching reuses the cooldown machinery `next` already waits on, so a
        rested proxy returns on its own and the pool stops shrinking.
        """
        if not self._scored or not proxy:
            return
        if proxy not in self._scores:
            return

        self._scores[proxy] -= 1
        if self._scores[proxy] > 0:
            return

        benched = self._bench_count.get(proxy, 0)
        wait = self.BENCH_BACKOFF[min(benched, len(self.BENCH_BACKOFF) - 1)]
        self._bench_count[proxy] = benched + 1
        self._scores[proxy] = 1
        until = time.time() + wait
        self._rate_limited_until[proxy] = until
        if until < self._next_expiry:
            self._next_expiry = until
        # The cached pick table still lists it, so force a rebuild.
        self._ready_at = 0.0

    async def mark_dead(self, proxy: str | None) -> None:
        if not self.remove_on_fail or proxy is None:
            return
        async with self._lock:
            self._dead.add(proxy)
            # Drop the cached pick table so a dead proxy is never offered.
            self._ready_at = 0.0

    async def set_cooldown(self, proxy: str | None, seconds: float) -> None:
        if proxy is None:
            return
        async with self._lock:
            until = time.time() + seconds
            self._cooldowns[proxy] = until
            if until < self._next_expiry:
                self._next_expiry = until

    # ── Validation ──

    async def validate(
        self,
        timeout: float = 15.0,
        concurrency: int = 200,
        sample: int | None = None,
    ) -> tuple[int, int]:
        """Test proxies concurrently against the real endpoint."""

        async def _test_one(proxy_raw: str | None) -> bool:
            proxy = self._format(proxy_raw) if proxy_raw else None
            if proxy and not _VALID_PROXY.match(proxy):
                return False
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    trust_env=False,
                ) as sess:
                    async with sess.post(
                        BATCH_ENDPOINT,
                        json={"usernames": ["roblox"], "excludeBannedUsers": False},
                        proxy=proxy,
                        headers={"Content-Type": "application/json"},
                    ) as resp:
                        # 429 still proves the proxy reached Roblox.
                        return resp.status in (200, 429)
            except Exception:
                return False

        sem = asyncio.Semaphore(concurrency)

        async def _test_with_limit(p) -> bool:
            async with sem:
                return await _test_one(p)

        pool = self._proxies[:]
        if sample is not None:
            pool = random.sample(pool, min(sample, len(pool)))

        results = await asyncio.gather(*[_test_with_limit(p) for p in pool])
        return sum(results), len(pool)
