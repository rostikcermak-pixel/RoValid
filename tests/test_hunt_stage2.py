"""The hunt's stage 2.

Both of hunt.py's passes awaited one `validate` at a time, which made
--workers a knob that did nothing. These pin the fix: the names all get
checked, they get checked concurrently, and the pass still stops dead on its
budget so a scheduled run cannot overrun its job timeout.
"""

import asyncio
import time

from engine import AVAILABLE, TAKEN
from hunt import validate_all


class SlowChecker:
    def __init__(self, delay=0.0, free=()):
        self.delay = delay
        self.free = set(free)
        self.seen = []

    async def validate(self, session, name):
        self.seen.append(name)
        if self.delay:
            await asyncio.sleep(self.delay)
        return (AVAILABLE if name in self.free else TAKEN), None


def far_future():
    return time.monotonic() + 30.0


async def test_every_name_is_checked_and_free_ones_recorded():
    checker = SlowChecker(free={"gem"})
    found = []
    await validate_all(
        checker, None, ["gem", "cat", "dog"], far_future(), found.append, 4,
    )
    assert sorted(checker.seen) == ["cat", "dog", "gem"]
    assert found == ["gem"]


async def test_each_name_goes_to_exactly_one_worker():
    names = [f"n{i}" for i in range(50)]
    checker = SlowChecker()
    await validate_all(checker, None, names, far_future(), lambda n: None, 8)
    assert sorted(checker.seen) == sorted(names)


async def test_the_workers_actually_run_concurrently():
    # 12 names at 50ms each is 600ms serial. With 6 workers it is ~100ms;
    # this used to be the serial number, which is the bug being fixed.
    names = [f"n{i}" for i in range(12)]
    checker = SlowChecker(delay=0.05)
    started = time.monotonic()
    await validate_all(checker, None, names, far_future(), lambda n: None, 6)
    elapsed = time.monotonic() - started
    assert len(checker.seen) == 12
    assert elapsed < 0.3, f"took {elapsed:.2f}s - looks serial"


async def test_a_passed_deadline_checks_nothing():
    checker = SlowChecker()
    await validate_all(
        checker, None, ["a", "b"], time.monotonic() - 1, lambda n: None, 4,
    )
    assert checker.seen == []


async def test_the_deadline_cuts_a_long_queue_short():
    names = [f"n{i}" for i in range(200)]
    checker = SlowChecker(delay=0.01)
    await validate_all(
        checker, None, names, time.monotonic() + 0.1, lambda n: None, 2,
    )
    assert 0 < len(checker.seen) < len(names)


async def test_no_names_is_a_no_op():
    checker = SlowChecker()
    await validate_all(checker, None, [], far_future(), lambda n: None, 8)
    assert checker.seen == []


async def test_more_workers_than_names_is_harmless():
    checker = SlowChecker(free={"a"})
    found = []
    await validate_all(checker, None, ["a"], far_future(), found.append, 64)
    assert checker.seen == ["a"]
    assert found == ["a"]
