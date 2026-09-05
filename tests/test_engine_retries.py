"""The shared request loop and the published limiter headers.

Both stages ran through their own copy of this loop until it was extracted,
which is why none of it was tested: there was no seam to inject a response
through. `_send_with_retries` takes the request as a callable, so there is
one now.
"""

import aiohttp
import pytest

from config import Stats
from engine import (
    AVAILABLE,
    ERROR,
    EXHAUSTED,
    INVALID,
    MALFORMED_CHUNK,
    RobloxChecker,
    SharedCooldown,
    limiter_state,
)
from proxy import ProxyManager


class FakeResponse:
    def __init__(self, status, json_data=None, text_data="", headers=None):
        self.status = status
        self._json = json_data if json_data is not None else {}
        self._text = text_data
        self.headers = headers or {}

    async def json(self):
        return self._json

    async def text(self):
        return self._text


class FakeRequest:
    """Stands in for aiohttp's request context manager."""

    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class Sequence:
    """Hands out one canned response (or exception) per request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, proxy):
        self.calls += 1
        item = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return FakeRequest(item)


def make_checker(max_retries=3):
    # Proxyless, so the shared cooldown owns the 429 policy. Its fallback is
    # dialled right down: this tests the loop, not the wait.
    cd = SharedCooldown()
    cd._fallback = 0.001
    checker = RobloxChecker(
        ProxyManager([]), timeout=1, stats=Stats(), max_inflight=4, cooldown=cd,
    )
    checker._max_retries = max_retries
    return checker


async def send(checker, make_request, *, batch=False):
    async def _ok(resp):
        return await resp.json()

    async def _bad(resp):
        return "bad-request"

    return await checker._send_with_retries(
        label="test",
        batch=batch,
        make_request=make_request,
        on_ok=_ok,
        on_bad_request=_bad,
        on_give_up=lambda reason: f"gave-up:{reason}",
    )


# ── the loop ───────────────────────────────────────────────────────────────

async def test_a_200_returns_the_callers_answer_in_one_request():
    checker = make_checker()
    seq = Sequence(FakeResponse(200, {"ok": True}))
    assert await send(checker, seq) == {"ok": True}
    assert seq.calls == 1
    assert checker._stats.ok_responses == 1


async def test_a_400_short_circuits_because_retrying_cannot_help():
    checker = make_checker()
    seq = Sequence(FakeResponse(400, text_data="Too many usernames."))
    assert await send(checker, seq) == "bad-request"
    assert seq.calls == 1


async def test_a_429_is_retried_and_counted():
    checker = make_checker()
    seq = Sequence(FakeResponse(429), FakeResponse(200, {"ok": True}))
    assert await send(checker, seq) == {"ok": True}
    assert seq.calls == 2
    assert checker._stats.ratelimited == 1


async def test_running_out_of_retries_reports_why():
    checker = make_checker(max_retries=3)
    seq = Sequence(FakeResponse(429))
    assert await send(checker, seq) == "gave-up:retries"
    assert seq.calls == 3


async def test_a_connection_error_is_retried():
    checker = make_checker()
    seq = Sequence(
        aiohttp.ClientConnectionError("reset"), FakeResponse(200, {"ok": True}),
    )
    assert await send(checker, seq) == {"ok": True}
    assert checker._stats.conn_errors == 1


async def test_an_unexpected_status_is_retried_and_counted_as_an_http_error():
    checker = make_checker()
    seq = Sequence(FakeResponse(503), FakeResponse(200, {"ok": True}))
    assert await send(checker, seq) == {"ok": True}
    assert checker._stats.http_errors == 1
    assert checker._stats.ok_responses == 1


async def test_only_stage_1_counts_a_batch_request():
    checker = make_checker()
    await send(checker, Sequence(FakeResponse(200, {})), batch=True)
    await send(checker, Sequence(FakeResponse(200, {})), batch=False)
    assert checker._stats.requests == 2
    assert checker._stats.batch_requests == 1


async def test_an_exhausted_pool_is_reported_separately_from_spent_retries():
    class DryPool:
        is_proxyless = False
        is_single = False
        COOLDOWN_DEFAULT = 10.0

        async def next(self):
            return None

    checker = RobloxChecker(DryPool(), timeout=1, stats=Stats(), max_inflight=1)
    seq = Sequence(FakeResponse(200, {}))
    assert await send(checker, seq) == "gave-up:no_proxy"
    assert seq.calls == 0
    assert checker._stats.no_proxy == 1


# ── the two stages on top of it ────────────────────────────────────────────

class FakeSession:
    def __init__(self, response):
        self._response = response

    def post(self, *a, **kw):
        return FakeRequest(self._response)

    def get(self, *a, **kw):
        return FakeRequest(self._response)


async def test_batch_screen_lowercases_the_names_that_exist():
    checker = make_checker()
    session = FakeSession(FakeResponse(200, {
        "data": [{"requestedUsername": "Taken"}, {"requestedUsername": "AlSo"}],
    }))
    assert await checker.batch_screen(session, ["Taken", "AlSo", "free"]) == {
        "taken", "also",
    }


async def test_batch_screen_reports_a_rejected_chunk_as_malformed():
    checker = make_checker()
    session = FakeSession(FakeResponse(400, text_data="Too many usernames."))
    assert await checker.batch_screen(session, ["a"]) is MALFORMED_CHUNK


async def test_batch_screen_returns_none_when_it_never_got_through():
    checker = make_checker(max_retries=2)
    assert await checker.batch_screen(FakeSession(FakeResponse(429)), ["a"]) is None


@pytest.mark.parametrize("code, outcome", [
    (0, AVAILABLE), (1, "taken"), (2, "censored"), (3, INVALID), (7, INVALID),
])
async def test_validate_maps_every_response_code(code, outcome):
    checker = make_checker()
    session = FakeSession(FakeResponse(200, {"code": code}))
    assert await checker.validate(session, "name") == (outcome, code)


async def test_validate_reports_an_exhausted_pool_as_exhausted():
    class DryPool:
        is_proxyless = False
        is_single = False
        COOLDOWN_DEFAULT = 10.0

        async def next(self):
            return None

    checker = RobloxChecker(DryPool(), timeout=1, stats=Stats(), max_inflight=1)
    assert await checker.validate(FakeSession(FakeResponse(200, {})), "n") == (
        EXHAUSTED, None,
    )


async def test_validate_reports_spent_retries_as_an_error():
    checker = make_checker(max_retries=2)
    session = FakeSession(FakeResponse(429))
    assert await checker.validate(session, "n") == (ERROR, None)


# ── published limiter headers ──────────────────────────────────────────────

def test_limiter_state_reads_what_roblox_publishes():
    assert limiter_state({
        "x-ratelimit-limit": "500, 500;w=60",
        "x-ratelimit-remaining": "499",
        "x-ratelimit-reset": "39",
    }) == (499, 39.0)


@pytest.mark.parametrize("headers", [
    {},
    {"x-ratelimit-remaining": "", "x-ratelimit-reset": "soon"},
    {"x-ratelimit-remaining": "not-a-number"},
])
def test_limiter_state_survives_headers_a_proxy_mangled(headers):
    assert limiter_state(headers) == (None, None)


async def test_an_exhausted_quota_parks_on_the_servers_own_reset_clock():
    checker = make_checker()
    resp = FakeResponse(429, headers={
        "x-ratelimit-remaining": "0", "x-ratelimit-reset": "12",
    })
    await checker._handle_429(resp, None)
    assert 12.0 <= checker._cooldown.resume_in <= 12.7


async def test_a_burst_limit_429_ignores_the_reset_clock():
    # BENCH.md 1: a throttled IP 429s with 499 of 500 requests still unspent,
    # because an unpublished burst limiter sits in front of the quota. Its
    # reset clock says 39s where the burst window reopens in about 6.
    checker = make_checker()
    checker._cooldown._fallback = 7.0
    resp = FakeResponse(429, headers={
        "x-ratelimit-remaining": "499", "x-ratelimit-reset": "39",
    })
    await checker._handle_429(resp, None)
    assert checker._cooldown.resume_in < 10.0


async def test_retry_after_still_wins_over_everything():
    checker = make_checker()
    resp = FakeResponse(
        429,
        json_data={"retry_after": 3},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "39"},
    )
    await checker._handle_429(resp, None)
    assert 3.0 <= checker._cooldown.resume_in <= 3.3
