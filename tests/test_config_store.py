"""Persistent config and the run's counter block."""

from config import Config, Stats


def test_set_and_get_round_trip(tmp_path):
    path = tmp_path / "config.json"
    Config(path).set("concurrency", 50)
    assert Config(path).get("concurrency") == 50


def test_missing_key_returns_the_default(tmp_path):
    assert Config(tmp_path / "config.json").get("nope", "fallback") == "fallback"


def test_corrupt_file_does_not_crash_startup(tmp_path):
    # A killed run can leave a half-written file. Losing the saved settings is
    # survivable; refusing to start is not.
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    assert Config(path).get_all() == {}


def test_get_all_is_a_copy(tmp_path):
    cfg = Config(tmp_path / "config.json")
    cfg.set("timeout", 10)
    cfg.get_all()["timeout"] = 999
    assert cfg.get("timeout") == 10


def test_streak_tracks_the_longest_run_of_hits():
    s = Stats()
    s.inc_works()
    s.inc_works()
    s.inc_taken()
    s.inc_works()
    assert s.best_streak == 2
    assert s.works == 3
    assert s.taken == 1


def test_peak_rps_never_falls():
    s = Stats()
    s.set_rps(120.0)
    s.set_rps(5.0)
    assert s.rps == 5.0
    assert s.peak_rps == 120.0


def test_snapshot_reports_every_counter_the_summary_prints():
    snap = Stats().snapshot()
    for key in ("requests", "batch_requests", "works", "taken", "censored",
                "invalid", "ratelimited", "fellback_chunks", "peak_rps"):
        assert key in snap
