"""SharedCooldown - the proxyless rate-limit policy.

This is the piece with the most measured reasoning behind it and no way to
observe it at runtime, so the invariants are pinned here: trust Retry-After
when the server sends one, grow the guess when it does not, and step that
guess back down on success rather than ratcheting to the ceiling.
"""

import asyncio

from engine import SharedCooldown


def test_starts_unparked():
    assert SharedCooldown().resume_in == 0.0


def test_server_retry_after_is_trusted():
    cd = SharedCooldown()
    cd.trip(retry_after=4.0)
    # 5% margin for clock skew, and nothing like the 7s fallback.
    assert 4.0 <= cd.resume_in <= 4.3


def test_fallback_is_used_when_no_retry_after():
    cd = SharedCooldown()
    cd.trip()
    assert cd.resume_in > SharedCooldown.MIN_FALLBACK


def test_fallback_grows_then_caps():
    cd = SharedCooldown()
    for _ in range(20):
        cd.trip()
    assert cd._fallback == SharedCooldown.MAX_FALLBACK


def test_success_steps_the_fallback_back_down():
    cd = SharedCooldown()
    cd.trip()
    grown = cd._fallback
    cd.succeed()
    assert cd._fallback < grown


def test_success_only_counts_once_per_park():
    # A burst of successes after one park is evidence the wait worked, not
    # evidence it should collapse to nothing.
    cd = SharedCooldown()
    cd.trip()
    cd.succeed()
    after_first = cd._fallback
    cd.succeed()
    cd.succeed()
    assert cd._fallback == after_first


def test_a_shorter_trip_never_shortens_an_existing_park():
    cd = SharedCooldown()
    cd.trip(retry_after=30.0)
    cd.trip(retry_after=1.0)
    assert cd.resume_in > 25.0


async def test_wait_returns_immediately_when_not_parked():
    cd = SharedCooldown()
    await asyncio.wait_for(cd.wait(), timeout=0.5)


async def test_wait_blocks_until_the_park_expires():
    cd = SharedCooldown()
    cd.trip(retry_after=0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(cd.wait(), timeout=1.0)
    assert loop.time() - started >= 0.05
