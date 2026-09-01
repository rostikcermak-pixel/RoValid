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
    STATIC_TOTAL_TIMEOUT = 120.0

    def __init__(
        self,
        proxy_manager: ProxyManager,
        timeout: int = 10,
        scraped: bool = False,
        circuit_breaker: CircuitBreaker | None = None,
        stats=None,
        max_inflight: int = 0,
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
        self._rotating = proxy_manager.is_single
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
        try:
            data = await resp.json()
            cooldown = float(data.get("retry_after", 0) or 0)
        except Exception:
            cooldown = 0.0
        if not cooldown:
            try:
                cooldown = float(resp.headers.get("Retry-After", 0) or 0)
            except (TypeError, ValueError):
                cooldown = 0.0

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

        # Proxyless: one shared bucket, so escalate and jitter. Jitter is what
        # stops concurrent workers waking in lockstep and re-throttling
        # each other on the same tick.
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
            if not self._rotating and time.time() - started > self.STATIC_TOTAL_TIMEOUT:
                return None

            if self._stats:
                self._stats.inc("requests")
                self._stats.inc("batch_requests")

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
            if not self._rotating and time.time() - started > self.STATIC_TOTAL_TIMEOUT:
                return (ERROR, None)

            if self._stats:
                self._stats.inc("requests")

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
