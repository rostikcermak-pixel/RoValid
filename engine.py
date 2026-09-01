#!/usr/bin/env python3
"""RoValid v1.0 - Two-stage checking engine, circuit breaker, webhook."""

from __future__ import annotations

import asyncio
import random
import sys
import time

import aiohttp

from config import (
    BATCH_ENDPOINT,
    CODE_AVAILABLE,
    CODE_CENSORED,
    CODE_TAKEN,
    VALIDATE_BIRTHDAY,
    VALIDATE_CONTEXT,
    VALIDATE_ENDPOINT,
)
from proxy import ProxyManager

# Rebuilt on every request in the old code; it never varies.
_JSON_HEADERS = {"Content-Type": "application/json"}

# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

AVAILABLE = "available"
TAKEN = "taken"
CENSORED = "censored"
INVALID = "invalid"
ERROR = "error"
EXHAUSTED = "exhausted"


# ---------------------------------------------------------------------------
# Debug control
# ---------------------------------------------------------------------------

_debug: bool = False


def set_debug(enabled: bool) -> None:
    global _debug
    _debug = enabled


def dbg(*args, **kwargs) -> None:
    """Debug print - only when --debug is active."""
    if _debug:
        print(*args, file=sys.stderr, flush=True, **kwargs)


# ---------------------------------------------------------------------------
# Circuit breaker - prevents thundering herd on a rotating proxy
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Shared across all workers using a single rotating proxy."""

    def __init__(
        self,
        threshold: int = 10,
        window: float = 2.0,
        cooldown: float = 2.0,
        on_open=None,
    ) -> None:
        self.threshold = threshold
        self.window = window
        self.cooldown = cooldown
        self._failures: list[float] = []
        self._lock = asyncio.Lock()
        self._open_until: float = 0.0
        self._on_open = on_open

    async def record_failure(self) -> None:
        async with self._lock:
            now = time.time()
            self._failures.append(now)
            self._failures = [t for t in self._failures if now - t < self.window]
            if len(self._failures) >= self.threshold:
                self._open_until = now + self.cooldown
                if self._on_open:
                    asyncio.ensure_future(self._on_open())

    async def wait_if_open(self) -> None:
        now = time.time()
        if now < self._open_until:
            wait = self._open_until - now
            dbg(f"[cb] circuit open, waiting {wait:.1f}s")
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# SharedCooldown - proxyless rate-limit coordination
# ---------------------------------------------------------------------------

class SharedCooldown:
    """One shared "do not send before" clock for the proxyless bucket.

    Proxyless is the only mode where every worker draws on a single
    rate-limit bucket, and the old policy handled that badly in two ways.
    Each worker backed off on its own escalating schedule (7s doubling
    toward 45s) keyed to a per-name attempt counter, so one worker's
    discovery that the bucket was shut taught the others nothing and the
    counter reset every time a name finally succeeded. Measured against the
    documented limiter, 42% of all requests came back 429 and throughput sat
    at about a quarter of what the bucket would have allowed.

    The fix is not to pace sends proactively. Spacing requests evenly is
    actively worse here: the reopen timer starts when the bucket *empties*
    and any request arriving while it is shut pushes that timer back, so a
    steady trickle can starve indefinitely. What works is to send freely
    while the bucket is giving, and on a 429 park every worker on one shared
    resume time - taken from the server's own Retry-After when it sends one,
    which is ground truth rather than a guess about the bucket's shape.

    That keeps the cost at roughly one wasted request per refill cycle
    instead of one per worker per attempt, and measures 1.3x-3.6x faster
    across every limiter shape tried, landing within ~1.05x of the
    theoretical best in each. It never idles when the bucket still has room,
    which is what makes it safe against a limiter more generous than the one
    documented here.

    Lock-free for the same reason the stats counters are: asyncio runs one
    coroutine at a time and none of these methods awaits between reading and
    writing, so there is nothing for a lock to protect.
    """

    # Measured against the live endpoint: Roblox's 429 carries NO Retry-After,
    # in neither the header nor the body (just {"errors":[{"code":4,...}]}).
    # So on this endpoint the fallback below is not a rare last resort - it is
    # the only policy there is, and it has to be able to come back down.
    FALLBACK = 7.0
    MIN_FALLBACK = 5.0
    MAX_FALLBACK = 45.0
    FALLBACK_GROWTH = 1.5
    # Symmetric with the growth. The old 0.995-per-success decay was a one-way
    # ratchet in practice: a real run grew the wait 7.6x over five 429s while
    # two successes shrank it by 1%, so it climbed to the cap and stayed there,
    # parking 92 of a 93-second run. Stepping back down by the same factor a
    # failure steps up keeps it hunting around the rate that actually works.
    FALLBACK_DECAY = 1 / FALLBACK_GROWTH

    def __init__(self) -> None:
        self._resume_at = 0.0
        self._fallback = self.FALLBACK
        self._parked_since_success = False

    @property
    def resume_in(self) -> float:
        return max(0.0, self._resume_at - time.monotonic())

    async def wait(self) -> None:
        """Block until the shared cooldown has elapsed."""
        while True:
            delay = self._resume_at - time.monotonic()
            if delay <= 0:
                return
            # Loop rather than sleep once: another worker may push the
            # resume time further out while this one is sleeping.
            await asyncio.sleep(delay)

    def trip(self, retry_after: float = 0.0) -> None:
        """A 429 came back - park every worker until the bucket reopens."""
        if retry_after > 0:
            # Trust the server, with a small margin for clock skew.
            wait = retry_after * 1.05
        else:
            # No header to go on, so grow the guess until one sticks.
            wait = self._fallback
            self._fallback = min(
                self._fallback * self.FALLBACK_GROWTH, self.MAX_FALLBACK,
            )
            self._parked_since_success = True
        resume = time.monotonic() + wait
        already = max(0.0, self._resume_at - time.monotonic())
        if resume > self._resume_at:
            self._resume_at = resume
        # The single most useful number when a proxyless run is slower than
        # expected: what the endpoint actually asked for, versus what we
        # guessed when it asked for nothing.
        dbg(
            f"  [429] retry_after={retry_after or 'absent'} "
            f"-> park {wait:.1f}s (fallback={self._fallback:.1f}s"
            + (f", already parked {already:.1f}s" if already > 0 else "")
            + ")"
        )

    def succeed(self) -> None:
        """A request got through - step the fallback back down.

        Only counts once per cooldown: a burst of successes after one park is
        evidence the wait worked, not evidence it should collapse to nothing.
        """
        if self._parked_since_success and self._fallback > self.MIN_FALLBACK:
            self._fallback = max(
                self._fallback * self.FALLBACK_DECAY, self.MIN_FALLBACK,
            )
        self._parked_since_success = False


# ---------------------------------------------------------------------------
# Checker - two-stage Roblox username resolution
# ---------------------------------------------------------------------------

class RobloxChecker:
    """Resolves Roblox username availability in two stages.

    Stage 1 (`batch_screen`) asks users.roblox.com which of up to 200 names
    already belong to an account. That is a 200:1 compression versus checking
    one name per request, which is what makes this workable without paid
    proxies - Roblox's limiter allows only a couple of requests per IP burst,
    but each of those requests clears 200 names.

    Stage 2 (`validate`) runs the signup validator over just the survivors.
    It is the only stage that can see censored and reserved names: those have
    no account behind them, so stage 1 reports them as free when they are not.
    """

    MAX_RETRIES = 20
    MAX_RETRIES_ROTATING = 100

    # Give up on a name after this long and let the caller retry it later.
    # This exists for a static proxy pool, where a name stuck behind one bad
    # proxy is better abandoned than waited on - there are other routes.
    # It deliberately does NOT apply proxyless: there is no other route, the
    # shared cooldown means waiting is exactly the right move, and enforcing
    # it there threw away names that a few more seconds would have resolved
    # (they surfaced as "?" in the feed and landed in unresolved.txt).
    STATIC_TOTAL_TIMEOUT = 120.0

    def __init__(
        self,
        proxy_manager: ProxyManager,
        timeout: int = 10,
        scraped: bool = False,
        circuit_breaker: CircuitBreaker | None = None,
        stats=None,
        max_inflight: int = 0,
        cooldown: "SharedCooldown | None" = None,
    ) -> None:
        self.pm = proxy_manager
        self.timeout = timeout

        # One budget of concurrent in-flight requests, shared by both stages.
        # Stage 1 and stage 2 now run at the same time, so without a shared
        # gate their worker pools would add up and quietly double the request
        # rate the user asked for. Whichever stage has work pending takes the
        # slots, which is also what lets the two overlap without a fixed split
        # starving either one.
        self._gate = asyncio.Semaphore(max_inflight if max_inflight > 0 else 10_000)

        # Proxyless is the only mode where all workers share one rate-limit
        # bucket. A rotating gateway gets a fresh IP per request, and a
        # scraped pool is cooled per proxy by ProxyManager.set_rate_limit.
        # A caller running several rounds passes its own cooldown in, so the
        # rate it learned in round 1 is not thrown away and re-discovered
        # (at the cost of a fresh set of 429s) at the start of round 2.
        if cooldown is not None:
            self._cooldown = cooldown
        else:
            self._cooldown = SharedCooldown() if proxy_manager.is_proxyless else None
        self._rotating = proxy_manager.is_single
        self._proxyless = proxy_manager.is_proxyless
        self._scraped = scraped
        self._cb = circuit_breaker
        self._stats = stats

        if scraped:
            self._max_retries = 3
        elif self._rotating:
            self._max_retries = self.MAX_RETRIES_ROTATING
        else:
            self._max_retries = self.MAX_RETRIES

        # The timeout never changes for the life of a run, so build the
        # (immutable) ClientTimeout once here instead of allocating a fresh
        # one on every single request attempt.
        if scraped:
            self._timeout_obj = aiohttp.ClientTimeout(total=8, sock_connect=5)
        elif self._rotating:
            self._timeout_obj = aiohttp.ClientTimeout(
                total=min(self.timeout, 8), sock_connect=5,
            )
        else:
            self._timeout_obj = aiohttp.ClientTimeout(
                total=self.timeout, sock_connect=8,
            )

    # ── shared plumbing ────────────────────────────────────────────────────

    def _req_timeout(self) -> aiohttp.ClientTimeout:
        return self._timeout_obj

    # Measured against the live endpoint: the bucket allows roughly three
    # requests, then reopens about six seconds after you STOP hammering it.
    # A flat retry shorter than that lands just before every reopen and gets
    # throttled forever, so the floor sits above the observed recovery and
    # escalates while 429s keep coming.
    PROXYLESS_BACKOFF_FLOOR = 7.0
    BACKOFF_CEILING = 45.0

    async def _handle_429(self, resp, proxy, attempt: int = 1) -> float:
        """Shared 429 policy: cool the proxy down, or wait if proxyless.

        Returns the number of seconds the caller should back off for. The
        sleep itself is the caller's job, because it has to happen *outside*
        the in-flight gate - a worker serving out a 45s proxyless backoff
        must not sit on a request slot the whole time.
        """
        body = None
        try:
            body = await resp.json()
            cooldown = float(body.get("retry_after", 0) or 0)
        except Exception:
            cooldown = 0.0
        header = resp.headers.get("Retry-After")
        if not cooldown:
            try:
                cooldown = float(header or 0)
            except (TypeError, ValueError):
                cooldown = 0.0
        dbg(f"  [429] body={str(body)[:160]} Retry-After={header!r}")

        if self._stats:
            self._stats.inc("ratelimited")

        if self._scraped and proxy:
            self.pm.set_rate_limit(proxy, cooldown or 10.0)
            return 0.0
        if self._rotating:
            return 0.0  # fresh IP next request, no point waiting
        if proxy:
            await self.pm.set_cooldown(proxy, cooldown or 10.0)
            return 0.0

        # Proxyless: one shared bucket. The shared cooldown owns the quiet
        # period, so the caller must not sleep on top of it - that would
        # double the delay. Return 0 and let the next wait() do it.
        if self._cooldown is not None:
            self._cooldown.trip(cooldown)
            return 0.0

        # No shared cooldown (shouldn't happen proxyless, kept as a safe
        # fallback): escalate and jitter so workers don't wake in lockstep.
        base = max(cooldown, self.PROXYLESS_BACKOFF_FLOOR)
        wait = min(base * (1.5 ** min(attempt - 1, 4)), self.BACKOFF_CEILING)
        return wait + random.uniform(0, 1.5)

    async def _on_conn_error(self, proxy, attempt: int) -> None:
        """Shared connection-error policy."""
        if self._scraped:
            if proxy:
                self.pm.score_miss(proxy)
            return
        if self._rotating:
            if self._cb:
                await self._cb.record_failure()
                await self._cb.wait_if_open()
            await asyncio.sleep(min(attempt * 0.25, 3.0))
            return
        await self.pm.set_cooldown(proxy, 3)
        await asyncio.sleep(min(attempt * 0.15, 5.0))

    # ── Stage 1: bulk existence screen ─────────────────────────────────────

    async def batch_screen(
        self,
        session: aiohttp.ClientSession,
        names: list[str],
    ) -> set[str] | None:
        """Return the lowercased subset of *names* that already exist.

        Returns None if the chunk could not be resolved (retries exhausted or
        no proxies left), so the caller can fall back rather than silently
        treating every name in the chunk as free.
        """
        attempt = 0
        started = time.time()
        payload = {"usernames": names, "excludeBannedUsers": False}

        while True:
            proxy = await self.pm.next()
            if proxy is None and not self.pm.is_proxyless:
                return None

            attempt += 1
            if attempt > self._max_retries:
                return None
            if (
                not self._rotating
                and not self._proxyless
                and time.time() - started > self.STATIC_TOTAL_TIMEOUT
            ):
                return None

            if self._stats:
                self._stats.inc("requests")
                self._stats.inc("batch_requests")

            if self._cooldown is not None:
                # Clear the cooldown before taking a gate slot, so a parked
                # worker is not sitting on in-flight capacity while it waits.
                await self._cooldown.wait()

            backoff = 0.0
            try:
                # The gate covers only the request itself, so retry sleeps
                # below release the slot for another worker to use.
                async with self._gate:
                    async with session.post(
                        BATCH_ENDPOINT,
                        json=payload,
                        proxy=proxy,
                        headers=_JSON_HEADERS,
                        timeout=self._req_timeout(),
                    ) as resp:
                        dbg(f"  [batch {attempt}] {len(names)} names -> HTTP {resp.status}")

                        if resp.status == 200:
                            data = await resp.json()
                            if self._scraped and proxy:
                                self.pm.score_hit(proxy)
                            if self._cooldown is not None:
                                self._cooldown.succeed()
                            return {
                                entry.get("requestedUsername", "").lower()
                                for entry in data.get("data", [])
                                if entry.get("requestedUsername")
                            }

                        if resp.status == 400:
                            # Malformed chunk - caller falls back to stage 2.
                            dbg(f"  [batch] HTTP 400: {(await resp.text())[:120]}")
                            return None

                        if resp.status == 429:
                            backoff = await self._handle_429(resp, proxy, attempt)
                        else:
                            backoff = 0.5

            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                dbg(f"  [batch {attempt}] {type(exc).__name__}")
                await self._on_conn_error(proxy, attempt)
                continue
            except Exception as exc:
                dbg(f"  [batch {attempt}] {type(exc).__name__}: {exc!s:.100}")
                await asyncio.sleep(0.3)
                continue

            if backoff > 0:
                await asyncio.sleep(backoff)

    # ── Stage 2: signup validator ──────────────────────────────────────────

    async def validate(
        self,
        session: aiohttp.ClientSession,
        username: str,
    ) -> tuple[str, int | None]:
        """Check one username against the signup validator.

        Returns (outcome, code) where outcome is one of
        AVAILABLE / TAKEN / CENSORED / INVALID / ERROR / EXHAUSTED.
        """
        attempt = 0
        started = time.time()
        params = {
            "request.username": username,
            "request.birthday": VALIDATE_BIRTHDAY,
            "request.context": VALIDATE_CONTEXT,
        }

        while True:
            proxy = await self.pm.next()
            if proxy is None and not self.pm.is_proxyless:
                return (EXHAUSTED, None)

            attempt += 1
            if attempt > self._max_retries:
                return (ERROR, None)
            if (
                not self._rotating
                and not self._proxyless
                and time.time() - started > self.STATIC_TOTAL_TIMEOUT
            ):
                return (ERROR, None)

            if self._stats:
                self._stats.inc("requests")

            if self._cooldown is not None:
                await self._cooldown.wait()

            backoff = 0.0
            try:
                # As in batch_screen: hold a request slot for the request
                # only, never across a backoff sleep.
                async with self._gate:
                    async with session.get(
                        VALIDATE_ENDPOINT,
                        params=params,
                        proxy=proxy,
                        timeout=self._req_timeout(),
                    ) as resp:
                        dbg(f"  [{attempt}] {username} -> HTTP {resp.status}")

                        if resp.status == 200:
                            data = await resp.json()
                            code = data.get("code")
                            if self._scraped and proxy:
                                self.pm.score_hit(proxy)
                            if self._cooldown is not None:
                                self._cooldown.succeed()
                            if code == CODE_AVAILABLE:
                                return (AVAILABLE, code)
                            if code == CODE_TAKEN:
                                return (TAKEN, code)
                            if code == CODE_CENSORED:
                                return (CENSORED, code)
                            return (INVALID, code)

                        if resp.status == 400:
                            return (INVALID, None)

                        if resp.status == 429:
                            backoff = await self._handle_429(resp, proxy, attempt)
                        else:
                            backoff = 0.5

            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                dbg(f"  [{attempt}] {username} -> {type(exc).__name__}")
                await self._on_conn_error(proxy, attempt)
                continue
            except Exception as exc:
                dbg(f"  [{attempt}] {username} -> {type(exc).__name__}: {exc!s:.100}")
                await asyncio.sleep(0.3)
                continue

            if backoff > 0:
                await asyncio.sleep(backoff)


# ---------------------------------------------------------------------------
# WebhookSender - async webhook dispatcher
# ---------------------------------------------------------------------------

class WebhookSender:
    """Sends found usernames to a Discord webhook in batches.

    Note: webhook traffic deliberately does NOT go through the proxy pool -
    it is your notification channel, not part of the checking load.
    """

    DISCORD_MAX_CHARS = 1950
    BATCH_SIZE = 15
    FLUSH_INTERVAL = 3.0

    def __init__(
        self,
        webhook_url: str,
        message_template: str,
        session: aiohttp.ClientSession,
        start_time: float = 0.0,
    ) -> None:
        self.url = webhook_url
        self.template = message_template
        self.session = session
        self.start_time = start_time
        self._sent: set[str] = set()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._buffer: list[str] = []
        self._buffer_lock = asyncio.Lock()

    def enqueue(self, username: str) -> None:
        if username not in self._sent:
            self._sent.add(username)
            self._queue.put_nowait(username)

    async def run(self) -> None:
        last_flush = time.time()
        while True:
            try:
                username = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                async with self._buffer_lock:
                    if self._buffer and time.time() - last_flush >= self.FLUSH_INTERVAL:
                        await self._flush_locked()
                        last_flush = time.time()
                continue

            async with self._buffer_lock:
                self._buffer.append(username)
                count = len(self._buffer)
                preview = "\n".join(self.template.replace("<name>", n) for n in self._buffer)
                if (
                    count >= self.BATCH_SIZE
                    or len(preview) >= self.DISCORD_MAX_CHARS
                    or time.time() - last_flush >= self.FLUSH_INTERVAL
                ):
                    await self._flush_locked()
                    last_flush = time.time()

    async def flush(self) -> None:
        async with self._buffer_lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._buffer:
            return

        names = list(self._buffer)
        self._buffer.clear()

        now = time.time()
        elapsed = int(now - self.start_time)
        ts_discord = f"<t:{int(now)}:R>"

        def _fill(n: str) -> str:
            return (
                self.template
                .replace("<name>", n)
                .replace("<link>", f"https://www.roblox.com/users/profile?username={n}")
                .replace("<t:time:R>", ts_discord)
                .replace("<time>", ts_discord)
                .replace("<elapsed>", str(elapsed))
            )

        msg = "\n".join(_fill(n) for n in names)
        if len(msg) > self.DISCORD_MAX_CHARS:
            safe = msg[: self.DISCORD_MAX_CHARS - 3]
            last_nl = safe.rfind("\n")
            msg = (safe[:last_nl] + "\n...") if last_nl > 0 else safe + "..."

        payload = {"content": msg, "username": "RoValid"}

        for _ in range(3):
            try:
                async with self.session.post(self.url, json=payload) as resp:
                    if resp.status == 429:
                        try:
                            wait = float((await resp.json()).get("retry_after", 5))
                        except Exception:
                            wait = 5.0
                        await asyncio.sleep(min(wait, 30))
                        continue
                    return
            except Exception:
                await asyncio.sleep(1)
