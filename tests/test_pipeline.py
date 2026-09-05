"""The two-stage pipeline, driven by a fake checker.

`_screen_stage` and `_validate_stage` carry the decisions that cost the most
when they are wrong - retrying a chunk instead of paying stage-2 prices for
its 200 names, and never losing a name that failed to resolve. Neither was
reachable by a test while both lived inside `_run_checker`.
"""

import asyncio
import io
from collections import deque

import pytest
from rich.console import Console

from checker import (
    MAX_CHUNK_TRIES,
    _Live,
    _screen_stage,
    _StreamPrinter,
    _validate_stage,
)
from config import Stats
from engine import AVAILABLE, CENSORED, INVALID, MALFORMED_CHUNK, TAKEN


def make_run(screen_total=0):
    stats = Stats()
    printer = _StreamPrinter(
        Console(file=io.StringIO(), width=100), stats, 0.0, enabled=True,
    )
    return _Live(
        stats=stats,
        stream=printer,
        feed=deque(maxlen=16),
        unresolved=[],
        screen_total=screen_total,
    )


class FakeChecker:
    """Stands in for RobloxChecker: canned answers, no network.

    `screen_results` is consumed one entry per chunk; each is either a set of
    taken names, None (transient failure - stage 1 should try again), or
    MALFORMED_CHUNK.
    """

    def __init__(self, screen_results=None, verdicts=None):
        self.screen_results = list(screen_results or [])
        self.verdicts = dict(verdicts or {})
        self.screen_calls = 0
        self.validate_calls = []

    async def batch_screen(self, session, names):
        self.screen_calls += 1
        if not self.screen_results:
            return set()
        result = self.screen_results.pop(0)
        return result

    async def validate(self, session, name):
        self.validate_calls.append(name)
        outcome = self.verdicts.get(name, TAKEN)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, None


# ── stage 1 ────────────────────────────────────────────────────────────────

async def test_survivors_are_published_and_taken_counted():
    names = ["free1", "taken1", "free2"]
    run = make_run(screen_total=len(names))
    checker = FakeChecker(screen_results=[{"taken1"}])
    published = []

    await _screen_stage(
        checker, None, names, run, workers=1,
        on_candidates=published.extend,
    )

    assert published == ["free1", "free2"]
    assert run.stats.taken == 1
    assert run.screened == 3


async def test_a_transient_failure_is_rescreened_not_dumped_on_stage_2():
    # The whole point of MAX_CHUNK_TRIES: re-screening costs one request,
    # falling back costs one per name in the chunk.
    names = ["a", "b"]
    run = make_run(screen_total=2)
    checker = FakeChecker(screen_results=[None, None, {"a"}])
    published = []

    await _screen_stage(
        checker, None, names, run, workers=1,
        on_candidates=published.extend,
    )

    assert checker.screen_calls == 3
    assert published == ["b"]
    assert run.stats.fellback_chunks == 0


async def test_a_chunk_that_never_screens_falls_back_after_the_try_limit():
    names = ["a", "b"]
    run = make_run(screen_total=2)
    checker = FakeChecker(screen_results=[None] * (MAX_CHUNK_TRIES + 5))
    published = []

    await _screen_stage(
        checker, None, names, run, workers=1,
        on_candidates=published.extend,
    )

    assert checker.screen_calls == MAX_CHUNK_TRIES
    assert published == names          # every name now costs a stage-2 request
    assert run.stats.fellback_chunks == 1


async def test_a_malformed_chunk_does_not_burn_retries():
    # HTTP 400 means the chunk itself is wrong; trying again cannot fix it.
    names = ["a", "b"]
    run = make_run(screen_total=2)
    checker = FakeChecker(screen_results=[MALFORMED_CHUNK])
    published = []

    await _screen_stage(
        checker, None, names, run, workers=1,
        on_candidates=published.extend,
    )

    assert checker.screen_calls == 1
    assert published == names
    assert run.stats.fellback_chunks == 1


async def test_empty_input_screens_nothing():
    run = make_run()
    checker = FakeChecker()
    await _screen_stage(checker, None, [], run, workers=4, on_candidates=print)
    assert checker.screen_calls == 0


# ── stage 2 ────────────────────────────────────────────────────────────────

async def drain(names, verdicts, workers=2):
    """Run _validate_stage over *names* and return (run, hits, checker)."""
    run = make_run()
    checker = FakeChecker(verdicts=verdicts)
    queue: asyncio.Queue = asyncio.Queue()
    for name in names:
        queue.put_nowait(name)
    for _ in range(workers):
        queue.put_nowait(None)

    hits = []

    async def on_hit(name):
        hits.append(name)

    await _validate_stage(
        checker, None, queue, run, workers=workers, on_hit=on_hit,
    )
    return run, hits, checker


async def test_every_outcome_lands_in_the_right_counter():
    run, hits, _ = await drain(
        ["good", "gone", "rude", "bad"],
        {
            "good": AVAILABLE,
            "gone": TAKEN,
            "rude": CENSORED,
            "bad": INVALID,
        },
    )
    assert hits == ["good"]
    assert run.stats.taken == 1
    assert run.stats.censored == 1
    assert run.stats.invalid == 1
    assert run.unresolved == []
    assert run.validated == 4


async def test_an_unknown_outcome_goes_to_the_retry_list():
    run, hits, _ = await drain(["mystery"], {"mystery": "exhausted"})
    assert hits == []
    assert run.unresolved == ["mystery"]


async def test_a_raising_validate_does_not_lose_the_name():
    run, _, _ = await drain(["boom"], {"boom": RuntimeError("network gone")})
    assert run.unresolved == ["boom"]
    assert run.validated == 1


async def test_workers_stop_on_their_sentinel_and_leave_nothing_behind():
    run, hits, checker = await drain(
        [f"n{i}" for i in range(20)], {"n7": AVAILABLE}, workers=4,
    )
    assert hits == ["n7"]
    assert sorted(checker.validate_calls) == sorted(f"n{i}" for i in range(20))


async def test_cancelling_mid_flight_files_the_name_for_retry():
    run = make_run()
    started = asyncio.Event()

    class Hanging(FakeChecker):
        async def validate(self, session, name):
            started.set()
            await asyncio.sleep(3600)

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait("stuck")

    async def on_hit(name):  # pragma: no cover - never reached
        raise AssertionError

    task = asyncio.create_task(
        _validate_stage(Hanging(), None, queue, run, workers=1, on_hit=on_hit)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert run.unresolved == ["stuck"]
