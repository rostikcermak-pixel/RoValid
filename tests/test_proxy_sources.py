"""Picking a workable pool out of what the scrapers return.

The scrape returns ~1,170,000 unique proxies across 43 sources, and the
pre-flight screen costs up to PRESCREEN_TIMEOUT seconds each. So the pool has
to be trimmed, and *which* proxies get trimmed matters: seven unchecked dumps
are ~98% of the total, so a uniform sample would be almost entirely dumps and
would crowd out the curated lists that publish only validated proxies.
"""

import pytest

from wizard import BULK_SOURCES, SCRAPE_POOL_CAP, _normalise_proxy, _select_pool


def addrs(prefix, n):
    return [f"10.0.{prefix}.{i}:8080" for i in range(n)]


def test_dedupes_across_sources():
    pool, total, _ = _select_pool(
        {"a": ["1.1.1.1:80", "2.2.2.2:80"], "b": ["2.2.2.2:80", "3.3.3.3:80"]},
        cap=100,
    )
    assert sorted(pool) == ["1.1.1.1:80", "2.2.2.2:80", "3.3.3.3:80"]
    assert total == 3


def test_dedupes_on_host_port_ignoring_credentials():
    pool, total, _ = _select_pool(
        {"a": ["user:pass@9.9.9.9:8080"], "b": ["9.9.9.9:8080"]}, cap=100,
    )
    assert total == 1
    assert pool == ["user:pass@9.9.9.9:8080"]


def test_everything_is_kept_when_it_fits_under_the_cap():
    batches = {"curated": addrs(1, 10), "SevenworksDev": addrs(2, 10)}
    pool, total, curated = _select_pool(batches, cap=100)
    assert len(pool) == total == 20
    assert curated == 10


def test_curated_sources_survive_the_cap_and_dumps_are_sampled():
    batches = {"curated": addrs(1, 50), "SevenworksDev": addrs(2, 5_000)}
    pool, total, curated = _select_pool(batches, cap=100)
    assert len(pool) == 100
    assert total == 5_050
    assert curated == 50
    # Every curated proxy is present; the rest of the budget came from the dump.
    assert set(addrs(1, 50)) <= set(pool)


def test_a_proxy_in_both_a_dump_and_a_curated_list_counts_as_curated():
    shared = "7.7.7.7:8080"
    batches = {"curated": [shared], "MuRongPIG": [shared, *addrs(2, 500)]}
    pool, _, curated = _select_pool(batches, cap=10)
    assert curated == 1
    assert shared in pool


def test_curated_alone_overflowing_the_cap_is_sampled_not_truncated_to_zero():
    # This is the case that produced a negative "sampled" count: curated
    # already exceeds the cap, so the dumps contribute nothing at all.
    batches = {"curated": addrs(1, 500), "zevtyardt": addrs(2, 500)}
    pool, total, curated = _select_pool(batches, cap=100)
    assert len(pool) == 100
    assert curated == 100          # never more than the pool itself
    assert total == 1_000
    assert len(pool) - curated == 0


def test_no_sources_yields_nothing():
    assert _select_pool({}, cap=100) == ([], 0, 0)


def test_only_dumps_still_produces_a_pool():
    pool, total, curated = _select_pool({"MuRongPIG": addrs(1, 500)}, cap=50)
    assert len(pool) == 50
    assert total == 500
    assert curated == 0


@pytest.mark.parametrize("name", sorted(BULK_SOURCES))
def test_every_bulk_source_is_actually_a_configured_source(name):
    # A typo here would silently demote a dump to curated and let it crowd
    # out the checked lists.
    import inspect

    import wizard
    src = inspect.getsource(wizard._scrape_proxies)
    assert f'("{name}",' in src, f"{name} is in BULK_SOURCES but not in SOURCES"


def test_the_cap_leaves_room_for_the_dumps():
    # If the cap ever drops below what the curated sources alone return,
    # fetching the dumps is wasted work. Measured: ~23,000 curated.
    assert SCRAPE_POOL_CAP > 23_000


# ── line parsing ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("line, expected", [
    ("1.2.3.4:8080", "1.2.3.4:8080"),
    ("  1.2.3.4:8080  ", "1.2.3.4:8080"),
    ("http://1.2.3.4:8080", "http://1.2.3.4:8080"),
    ("user:pass@1.2.3.4:8080", "user:pass@1.2.3.4:8080"),
    ("proxy.example.com:3128", "proxy.example.com:3128"),
    # hideip.me publishes host:port:Country. Rejecting these lost the whole
    # source; the extra fields are trimmed instead.
    ("222.127.55.155:8082:Philippines", "222.127.55.155:8082"),
    ("84.17.47.150:9002:The Netherlands", "84.17.47.150:9002"),
    ("1.2.3.4:8080:US:elite", "1.2.3.4:8080"),
])
def test_normalise_accepts_real_proxy_lines(line, expected):
    assert _normalise_proxy(line) == expected


@pytest.mark.parametrize("line", [
    "",
    "   ",
    "Proxy list (#400) updated at Sat, 05 Sep 26 16:58:01 +0300",
    "Socks proxy=https://spys.me/socks.txt",
    "not-a-proxy",
    "1.2.3.4",
    "1.2.3.4:notaport",
    "# comment",
])
def test_normalise_rejects_everything_else(line):
    assert _normalise_proxy(line) is None
